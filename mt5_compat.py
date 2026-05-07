"""
mt5_compat.py

Cross-platform MetaTrader5 compatibility wrapper.
Updated for File-based EA Bridge.
"""

import platform

if platform.system() == "Windows":
    import MetaTrader5 as mt5
else:
    # On Linux/Wine, we provide constants needed by the bot
    # but the functional calls are handled by ea_bridge inside fetcher/executor.
    class MT5Constants:
        # Timeframes
        TIMEFRAME_M1 = 1
        TIMEFRAME_M5 = 5
        TIMEFRAME_M15 = 15
        TIMEFRAME_H1 = 16385
        TIMEFRAME_D1 = 16408
        
        # Order Types
        ORDER_TYPE_BUY = 0
        ORDER_TYPE_SELL = 1
        
        # Order Result Codes
        TRADE_RETCODE_DONE = 10009
        
    mt5 = MT5Constants()
