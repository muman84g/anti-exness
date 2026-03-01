from abc import ABC, abstractmethod
import pandas as pd

class BaseDataManager(ABC):
    @abstractmethod
    def connect(self) -> bool:
        pass

    @abstractmethod
    def disconnect(self):
        pass

    @abstractmethod
    def get_historical_data(self, mt5_symbol: str, timeframe: int, num_bars: int) -> pd.DataFrame:
        pass

class BaseExecutor(ABC):
    @abstractmethod
    def open_position(self, symbol: str, order_type: int, lot_size: float, sl: float = 0.0, tp: float = 0.0) -> str:
        """Returns ticket ID or None."""
        pass

    @abstractmethod
    def close_position(self, ticket: str) -> bool:
        pass

    @abstractmethod
    def get_symbol_info(self, symbol: str):
        pass

    @abstractmethod
    def calculate_lot_size(self, symbol: str, risk_usd: float, sl_distance_points: int) -> float:
        pass
