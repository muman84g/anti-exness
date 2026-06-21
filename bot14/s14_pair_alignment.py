"""bot14のペア状態とエントリー整列を判定する純粋関数。"""


def next_direction_after_outcome(direction, outcome):
    """TPなら反転、SLなら同方向を返す。"""
    if direction not in {"LONG", "SHORT"}:
        raise ValueError(f"Unsupported direction: {direction}")
    if outcome == "WIN":
        return "SHORT" if direction == "LONG" else "LONG"
    if outcome == "LOSE":
        return direction
    raise ValueError(f"Unsupported outcome: {outcome}")


def is_spread_allowed(current_spread_price, max_spread_price):
    """数値誤差を吸収しつつ、上限と同値まで許可する。"""
    tolerance = max(1e-12, abs(float(max_spread_price)) * 1e-9)
    return float(current_spread_price) <= float(max_spread_price) + tolerance


def protection_levels_match(current_sl, current_tp, desired_sl, desired_tp, tolerance=1e-9):
    """server側SL/TPが発注後の目標値と一致するかを返す。"""
    values = (current_sl, current_tp, desired_sl, desired_tp)
    if any(float(value) <= 0.0 for value in values):
        return False
    return (
        abs(float(current_sl) - float(desired_sl)) <= float(tolerance)
        and abs(float(current_tp) - float(desired_tp)) <= float(tolerance)
    )


def evaluate_pair_alignment(
    candidate_price,
    other_position,
    *,
    pip_value,
    w_pips,
    target_ratio=0.5,
    tolerance_pips=2.0,
    current_spread_price=0.0,
    spread_multiplier=2.0,
):
    """新規建値と他方の建値が目標距離内かを返す。

    戻り値は (許可, 実距離pips, 目標pips, 許容差pips)。
    他方が未保有なら配置制約はないため許可する。
    """
    target_pips = float(w_pips) * float(target_ratio)
    spread_pips = max(0.0, float(current_spread_price)) / float(pip_value)
    allowed_tolerance = max(float(tolerance_pips), spread_pips * float(spread_multiplier))

    if not other_position:
        return True, None, target_pips, allowed_tolerance

    other_entry = other_position.get("entry_price")
    if other_entry is None:
        return False, None, target_pips, allowed_tolerance

    distance_pips = abs(float(candidate_price) - float(other_entry)) / float(pip_value)
    is_aligned = abs(distance_pips - target_pips) <= allowed_tolerance
    return is_aligned, distance_pips, target_pips, allowed_tolerance


def classify_pair_mode(pos_a, pos_b, pair_initialized=False):
    """二つの建玉を記事上の状態名へ分類する。"""
    if not pos_a or not pos_b:
        return "REENTRY_PENDING" if pair_initialized else "INITIALIZING"

    direction_a = pos_a.get("direction")
    direction_b = pos_b.get("direction")
    if direction_a == direction_b:
        return "CAPITAL"

    entry_a = float(pos_a.get("entry_price", 0.0))
    entry_b = float(pos_b.get("entry_price", 0.0))
    if abs(entry_a - entry_b) < 1e-12:
        return "MISALIGNED"
    upper = pos_a if entry_a >= entry_b else pos_b
    lower = pos_b if entry_a >= entry_b else pos_a
    if upper.get("direction") == "SHORT" and lower.get("direction") == "LONG":
        return "PROFIT"
    return "MISALIGNED"
