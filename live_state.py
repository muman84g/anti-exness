import json
import os
import logging
from live_config import LIVE_STATE_DB

class LiveState:
    def __init__(self, db_path=LIVE_STATE_DB):
        self.db_path = db_path
        self.state = {
            "open_pairs": {},      # pair_name -> {leg1_ticket, leg2_ticket, entry_zscore, entry_time}
            "last_calc_time": None # Track when we last did heavy calculations
        }
        self.load()

    def load(self):
        if os.path.exists(self.db_path):
            try:
                with open(self.db_path, "r") as f:
                    data = json.load(f)
                    self.state.update(data)
            except Exception as e:
                logging.error(f"Failed to load state DB: {e}")

    def save(self):
        try:
            with open(self.db_path, "w") as f:
                json.dump(self.state, f, indent=4)
        except Exception as e:
            logging.error(f"Failed to save state DB: {e}")

    def add_open_pair(self, pair_name, leg1_symbol, leg1_ticket, leg1_type, leg2_symbol, leg2_ticket, leg2_type, zscore, spread_val):
        self.state["open_pairs"][pair_name] = {
            "leg1_symbol": leg1_symbol,
            "leg1_ticket": leg1_ticket,
            "leg1_type": leg1_type,
            "leg2_symbol": leg2_symbol,
            "leg2_ticket": leg2_ticket,
            "leg2_type": leg2_type,
            "entry_zscore": zscore,
            "entry_spread": spread_val,
            "entry_time": __import__('time').time()
        }
        self.save()

    def remove_open_pair(self, pair_name):
        if pair_name in self.state["open_pairs"]:
            del self.state["open_pairs"][pair_name]
            self.save()

    def get_open_pairs(self):
        return self.state["open_pairs"]
        
    def get_pair_info(self, pair_name):
        return self.state["open_pairs"].get(pair_name)
