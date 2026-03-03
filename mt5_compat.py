"""
mt5_compat.py

Cross-platform MetaTrader5 compatibility wrapper.

On Windows:  imports the real MetaTrader5 library.
On Linux:    imports mt5linux, which proxies calls to an MT5 terminal
             running inside Wine via a socket connection.

Usage (everywhere in the project):
    from mt5_compat import mt5
"""

import platform

if platform.system() == "Windows":
    import MetaTrader5 as mt5
else:
    # mt5linux routes calls to Wine-hosted MT5 via localhost socket
    from mt5linux import MetaTrader5
    mt5 = MetaTrader5()
