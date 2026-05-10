import json
import os
import logging
from datetime import datetime
from live_config import LIVE_STATE_DB

class LiveState:
    def __init__(self, db_path=LIVE_STATE_DB):
        self.db_path = db_path
        self.state = {
            "open_positions": [],  # List of dicts with p1, p2, action, etc.
            "active_pairs": [],    # Currently selected pairs for trading
            "last_update": None    # Timestamp of last strategy refresh
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
                # Convert datetime to string for JSON
                save_data = self.state.copy()
                if save_data["last_update"] and isinstance(save_data["last_update"], datetime):
                    save_data["last_update"] = save_data["last_update"].isoformat()
                json.dump(save_data, f, indent=4)
        except Exception as e:
            logging.error(f"Failed to save state DB: {e}")

    def get_open_positions(self):
        return self.state["open_positions"]

    def is_pair_open(self, p1, p2):
        for pos in self.state["open_positions"]:
            if (pos["p1"] == p1 and pos["p2"] == p2) or (pos["p1"] == p2 and pos["p2"] == p1):
                return True
        return False

    def open_position(self, p1, p2, trade_res):
        pos = {
            "p1": p1,
            "p2": p2,
            "action": trade_res["action"],
            "ticket1": trade_res["ticket1"],
            "ticket2": trade_res["ticket2"],
            "entry_time": datetime.now().isoformat()
        }
        self.state["open_positions"].append(pos)
        self.save()

    def close_position(self, p1, p2):
        self.state["open_positions"] = [p for p in self.state["open_positions"] if not (p["p1"] == p1 and p["p2"] == p2)]
        self.save()

    def get_last_update_time(self):
        lu = self.state["last_update"]
        if lu is None: return None
        if isinstance(lu, str):
            return datetime.fromisoformat(lu)
        return lu

    def set_last_update_time(self, dt):
        self.state["last_update"] = dt
        self.save()

    def set_active_pairs(self, pairs):
        self.state["active_pairs"] = pairs
        self.save()

    def get_active_pairs(self):
        return self.state["active_pairs"]
