from __future__ import annotations

import time

from .config import load_config
from .cooldown_manager import CooldownManager
from .data_fetcher import fetch_market_data
from .excel_bridge import ExcelBridge
from .exchange import init_exchange
from .exchange_health import check_exchange_health
from .indicators import compute_indicators
from .kill_switch import KillSwitch
from .logger import bootstrap_logger, log
from .order_executor import OrderExecutor
from .position_manager import PositionManager
from .risk_manager import build_risk_plan
from .signal_engine import SignalEngine


def _extract_equity_usdt(balance: dict) -> float:
    """
    CCXT balance ფორმატები სხვადასხვაა. ვცდილობთ უსაფრთხოდ ამოვიღოთ USDT equity.
    """
    try:
        total = balance.get("total", {})
        if isinstance(total, dict) and "USDT" in total:
            return float(total.get("USDT") or 0.0)

        usdt = balance.get("USDT", {})
        if isinstance(usdt, dict) and "total" in usdt:
            return float(usdt.get("total") or 0.0)

        # fallback: ზოგჯერ balance პირდაპირ რიცხვად/სტრინგად მოდის
        if isinstance(usdt, (int, float, str)):
            return float(usdt)

    except Exception:
        pass

    return 0.0


def run() -> None:
    logger = bootstrap_logger("genius_bot")
    cfg = load_config()

    log(
        logger,
        "INFO",
        "BOOT",
        symbols=",".join(cfg.symbols),
        tf=cfg.timeframe,
        dry_run=cfg.dry_run,
        allow_live=cfg.allow_live_signals,
    )

    ex = init_exchange(cfg, logger)

    excel = ExcelBridge(
        path=cfg.excel_path,
        sheet=cfg.excel_sheet,
        in_prefix=cfg.excel_named_inputs_prefix,
        out_prefix=cfg.excel_named_outputs_prefix,
        logger=logger,
    )

    cooldown = CooldownManager(cfg.cooldown_seconds, cfg.post_loss_cooldown_seconds)
    kill = KillSwitch(cfg.max_drawdown, cfg.max_loss_streak, logger)
    posman = PositionManager(ex, logger)
    executor = OrderExecutor(ex, logger, dry_run=cfg.dry_run)
    signal_engine = SignalEngine(excel, logger)

    last_report = 0.0

    while True:
        try:
            health = check_exchange_health(ex, logger)
            if not health.ok:
                log(logger, "WARNING", "EXCHANGE_UNHEALTHY", reason=health.reason)
                time.sleep(cfg.loop_sleep_seconds)
                continue

            bal = ex.fetch_balance_safe()
            equity = _extract_equity_usdt(bal)

            if kill.blocked(equity=equity):
                log(logger, "ERROR", "KILL_SWITCH_ACTIVE", equity=equity)
                time.sleep(max(30.0, cfg.loop_sleep_seconds))
                continue

            # რეალური ღია პოზიციების რაოდენობა ყოველ ციკლზე
            open_count = posman.open_positions_count()

            for symbol in cfg.symbols:
                if open_count >= cfg.max_open_positions:
                    break

                # თუ უკვე გაქვს პოზიცია, არ ვხსნით ახალს
                if posman.has_position(symbol):
                    continue

                # cooldown
                if not cooldown.allowed(symbol):
                    continue

                md = fetch_market_data(ex, symbol, cfg.timeframe, cfg.ohlcv_limit, logger)
                ind = compute_indicators(md.df)
                if ind is None:
                    continue

                sig = signal_engine.generate(symbol, cfg.timeframe, ind)
                if sig.decision == "NO":
                    continue

                if not cfg.allow_live_signals:
                    log(
                        logger,
                        "INFO",
                        "SIGNAL_BLOCKED_ALLOW_LIVE_FALSE",
                        symbol=symbol,
                        decision=sig.decision,
                        conf=sig.confidence,
                    )
                    continue

                plan = build_risk_plan(
                    side=sig.decision,
                    last=ind.last,
                    atr_pct=ind.atr_pct,
                    quote_per_trade=cfg.quote_per_trade,
                    fixed_amount=cfg.position_size,
                )
                if not plan:
                    continue

                res = executor.place_bracket_market(symbol, sig.decision, plan.amount, plan.sl, plan.tp)
                if res.ok:
                    cooldown.mark_signal(symbol)
                    open_count += 1
                else:
                    # სურვილისამებრ: წარუმატებელ attempt-ზე cooldown არ ვნიშნავთ
                    # შეგიძლია აქაც ჩაამატო ლოგი თუ executor აბრუნებს reason-ს
                    pass

            if time.time() - last_report > cfg.report_every_seconds:
                log(logger, "INFO", "HEARTBEAT", equity=equity, open_positions=open_count)
                last_report = time.time()

            time.sleep(cfg.loop_sleep_seconds)

        except Exception as e:
            # ბოტი არ უნდა მოკვდეს უცნობ error-ზე
            log(logger, "ERROR", "MAIN_LOOP_CRASH", error=str(e))
            time.sleep(max(10.0, cfg.loop_sleep_seconds))
