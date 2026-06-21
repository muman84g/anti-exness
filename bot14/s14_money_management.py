"""記事記載の分解モンテカルロ法。

各EAは独立した数列を持つ。初期数列は[0, 1]、勝ちは左右を削除、
負けは賭け単位を右端へ追加する。数列解消後は次取引で[0, 1]へ戻る。
"""

import logging


ARTICLE_DMC_STATE_VERSION = "article_dmc_v1"


def calculate_article_lot(bet_units, lot_multiplier, max_bet_units, symbol_info):
    """数列の賭け単位を変えずにbroker lotへ変換する。"""
    if max_bet_units and bet_units > max_bet_units:
        raise ValueError(
            f"Requested {bet_units} units exceeds configured cap {max_bet_units}; "
            "capping would invalidate the Monte Carlo sequence."
        )
    raw_lot = bet_units * lot_multiplier
    min_vol = symbol_info.volume_min
    max_vol = symbol_info.volume_max
    step_vol = symbol_info.volume_step
    if raw_lot < min_vol or raw_lot > max_vol:
        raise ValueError(
            f"Requested lot {raw_lot} is outside broker range {min_vol}..{max_vol}."
        )
    lot = round(raw_lot / step_vol) * step_vol
    if abs(lot - raw_lot) > 1e-9:
        raise ValueError(
            f"Requested lot {raw_lot} is not representable by volume step {step_vol}."
        )
    return round(lot, 8)


class DecomposedMonteCarlo:
    def __init__(self, initial_sequence=None):
        self.initial_sequence = list(initial_sequence or [0, 1])
        self.seq = list(self.initial_sequence)
        self.completed_cycles = 0

    def _prepare_sequence(self):
        if not self.seq:
            self.seq = list(self.initial_sequence)
        if len(self.seq) == 1:
            value = int(self.seq[0])
            left = value // 2
            self.seq = [left, value - left]

    def get_bet_units(self):
        self._prepare_sequence()
        return int(self.seq[0] + self.seq[-1])

    def on_win(self):
        self._prepare_sequence()
        self.seq = self.seq[1:-1]
        completed = not self.seq
        if completed:
            self.completed_cycles += 1
        return completed

    def on_lose(self, bet_units):
        self._prepare_sequence()
        self.seq.append(int(bet_units))

    def to_dict(self):
        return {
            "version": ARTICLE_DMC_STATE_VERSION,
            "initial_sequence": self.initial_sequence,
            "seq": self.seq,
            "completed_cycles": self.completed_cycles,
        }

    def from_dict(self, data):
        if not data:
            return
        if data.get("version") != ARTICLE_DMC_STATE_VERSION:
            logging.warning("Ignoring incompatible pre-article DMC state.")
            return
        self.initial_sequence = list(data.get("initial_sequence", [0, 1]))
        self.seq = list(data.get("seq", self.initial_sequence))
        self.completed_cycles = int(data.get("completed_cycles", 0))


class MonteCarloManager:
    """同じ逆張りEAを2つ独立稼働させるための状態コンテナ。"""

    def __init__(self, initial_sequence=None):
        sequence = list(initial_sequence or [0, 1])
        self.mc_A = DecomposedMonteCarlo(sequence)
        self.mc_B = DecomposedMonteCarlo(sequence)

    def to_dict(self):
        return {
            "version": ARTICLE_DMC_STATE_VERSION,
            "mc_A": self.mc_A.to_dict(),
            "mc_B": self.mc_B.to_dict(),
        }

    def from_dict(self, data):
        if not data:
            return
        if data.get("version") != ARTICLE_DMC_STATE_VERSION:
            logging.warning("Ignoring incompatible coupled Monte Carlo manager state.")
            return
        if isinstance(data.get("mc_A"), dict):
            self.mc_A.from_dict(data["mc_A"])
        if isinstance(data.get("mc_B"), dict):
            self.mc_B.from_dict(data["mc_B"])

    def update_mc(self, outcome_A, outcome_B, bet_A, bet_B):
        if outcome_A in {"WIN", "TP"}:
            self.mc_A.on_win()
        elif outcome_A in {"LOSE", "SL"}:
            self.mc_A.on_lose(bet_A)

        if outcome_B in {"WIN", "TP"}:
            self.mc_B.on_win()
        elif outcome_B in {"LOSE", "SL"}:
            self.mc_B.on_lose(bet_B)
