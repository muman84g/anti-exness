import asyncio
import pandas as pd
import logging
from metaapi_cloud_sdk import MetaApi
from base_interfaces import BaseDataManager, BaseExecutor
from live_config import META_API_TOKEN, META_API_ACCOUNT_ID

logger = logging.getLogger(__name__)

class MetaApiBridge(BaseDataManager, BaseExecutor):
    def __init__(self, token=META_API_TOKEN, account_id=META_API_ACCOUNT_ID):
        self.token = token
        self.account_id = account_id
        self.api = MetaApi(token)
        self.account = None
        self.connection = None

    def connect(self) -> bool:
        return asyncio.run(self._connect_async())

    async def _connect_async(self):
        try:
            self.account = await self.api.metatrader_account_api.get_account(self.account_id)
            await self.account.wait_connected()
            self.connection = self.account.get_rpc_connection()
            await self.connection.connect()
            await self.connection.wait_synchronized()
            logger.info(f"Connected to MetaApi Account: {self.account_id}")
            return True
        except Exception as e:
            logger.error(f"MetaApi connection failed: {e}")
            return False

    def disconnect(self):
        # MetaApi SDK handles cleanup generally, but we can close connection
        pass

    def get_historical_data(self, mt5_symbol: str, timeframe: str, num_bars: int) -> pd.DataFrame:
        return asyncio.run(self._get_historical_data_async(mt5_symbol, timeframe, num_bars))

    async def _get_historical_data_async(self, symbol, timeframe, num_bars):
        # Timeframe mapping (MT5 int to MetaApi string)
        tf_map = {
            15: '15m',
            60: '1h',
            16385: '1h', # mt5.TIMEFRAME_H1
            15: '15m'    # mt5.TIMEFRAME_M15 value is actually 15
        }
        # In MetaTrader5 lib, TIMEFRAME_H1 is 16385, M15 is 15.
        # We need to be careful with the mapping.
        
        # MetaApi uses strings like '15m', '1h'
        ma_tf = '1h' if timeframe >= 60 else '15m'
        
        try:
            candles = await self.connection.get_historical_candles(symbol, ma_tf, None, num_bars)
            df = pd.DataFrame(candles)
            if df.empty:
                return None
            
            df['time'] = pd.to_datetime(df['time'])
            df.set_index('time', inplace=True)
            df.rename(columns={'open': 'Open', 'high': 'High', 'low': 'Low', 'close': 'Close', 'tickVolume': 'Volume'}, inplace=True)
            return df[['Open', 'High', 'Low', 'Close', 'Volume']]
        except Exception as e:
            logger.error(f"Failed to fetch data from MetaApi: {e}")
            return None

    def open_position(self, symbol, order_type, lot_size, sl=0.0, tp=0.0) -> str:
        return asyncio.run(self._open_position_async(symbol, order_type, lot_size, sl, tp))

    async def _open_position_async(self, symbol, order_type, lot_size, sl, tp):
        import MetaTrader5 as mt5
        action = 'BUY' if order_type == mt5.ORDER_TYPE_BUY else 'SELL'
        try:
            result = await self.connection.create_market_order(symbol, action, lot_size, sl, tp)
            logger.info(f"MetaApi Order filled: {result['orderId']}")
            return result['orderId']
        except Exception as e:
            logger.error(f"MetaApi Order failed: {e}")
            return None

    def close_position(self, ticket: str) -> bool:
        return asyncio.run(self._close_position_async(ticket))

    async def _close_position_async(self, ticket):
        try:
            await self.connection.close_position(ticket)
            logger.info(f"MetaApi Position {ticket} closed.")
            return True
        except Exception as e:
            logger.error(f"MetaApi Close failed for {ticket}: {e}")
            return False

    def get_symbol_info(self, symbol: str):
        return asyncio.run(self._get_symbol_info_async(symbol))

    async def _get_symbol_info_async(self, symbol):
        try:
            return await self.connection.get_symbol_specification(symbol)
        except Exception as e:
            logger.error(f"MetaApi get_symbol_info failed: {e}")
            return None

    def calculate_lot_size(self, symbol, risk_usd, sl_distance_points) -> float:
        # For simplicity, returning min lot as in original code
        info = self.get_symbol_info(symbol)
        if info:
            return info.get('minVolume', 0.01)
        return 0.01
