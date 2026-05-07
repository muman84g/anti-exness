# MT5 Bridge Stabilization Project: Handover Notes

## 1. Project Overview
- **Goal**: Establish a stable TCP bridge between MT5 (running in Docker/Wine) and a native Python bot.
- **Symbol**: `EURUSDm` (Exness specific suffix).
- **Current Status**: **[SUCCESS] TCP Bridge Fully Established.**

## 2. Connection Info
- **noVNC (Browser GUI)**: `http://localhost:6080/vnc.html`
- **VNC Password**: `trading` (Verified)
- **Container IP**: `172.17.0.2`
- **Bridge Port**: `5555`

## 3. Latest Status (Completed on 2026-04-21)
- **EA Compilation**: 
  - `BotBridge.mq5` was manually compiled via GUI inside the container to match the latest MT5 build (5660).
  - The latest `BotBridge.ex5` is located in `MQL5/Experts/`.
- **Security Settings (Error 4014 Fix)**:
  - Enabled "Allow WebRequest for listed URL" in MT5 Options.
  - Added `172.17.0.2` to the allowed URLs list.
- **Connection Verification**:
  - EA Experts logs confirm: `Successfully connected to Python TCP Server`.
  - MT5 "Algo Trading" is Green and the EA has a smiling face on the `EURUSDm` chart.

## 4. Next Steps (Next Session)
1. **Run Live Bot**: 
   Since connectivity is confirmed, start the actual trading logic:
   ```bash
   docker exec exness-bot python3 /app/live_main.py
   ```
2. **Monitor Execution**:
   Watch the terminal and logs to ensure trades are placed correctly when signals occur.
3. **Automate Bridge Test**:
   Consider adding `test_local_connectivity.py` to the health check to ensure the bridge is always up.

## 5. Key Files
- `entrypoint.sh`: Container startup and Wine/MT5 launch.
- `startup.ini`: MT5 auto-login/config.
- `MetaTrader 5/MQL5/Experts/BotBridge.mq5`: EA Source (Updated with correct Host IP).
- `ea_bridge.py`: Python-side server logic.
- `test_local_connectivity.py`: Used to verify the TCP bridge.
- `live_main.py`: Main trading bot logic.
