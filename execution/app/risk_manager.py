def calc_position_size(balance, risk_per_trade, stop_distance, min_notional=None, price=None):
    if stop_distance <= 0 or balance <= 0:
        return 0.0

    risk_amount = balance * risk_per_trade
    size = risk_amount / stop_distance

    # min notional guard if provided
    if min_notional and price:
        notional = size * price
        if notional < min_notional:
            size = min_notional / price

    return max(size, 0.0)
