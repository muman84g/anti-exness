# MT5 Bridge Stabilization Project: Handover Notes

## 1. Project Overview
- **Goal**: Establish a stable TCP bridge between MT5 (running in Docker/Wine) and a native Python bot.
- **Symbol**: `EURUSDm` (Exness specific suffix).
- **Architecture**:
  - **MT5 (Wine)**: Running in `/portable` mode inside Docker.
  - **EA (BotBridge.mq5)**: Acts as a TCP client connecting to port `5555`.
  - **Python (Linux)**: Acts as a TCP server listening for the EA connection.

## 2. Current Connection Info
- **noVNC (Browser GUI)**: `http://localhost:6080/vnc_lite.html`
- **Password**: `trading`
- **Container IP**: `172.17.0.2` (Verified via `hostname -I`).
- **Bridge Port**: `5555`

## 3. Latest Status (As of 2026-04-08 23:45)
- **MT5 Terminal**: Active and logged in.
- **Chart**: `EURUSDm, H1` is open.
- **EA Status**: `BotBridge` is attached (Blue hat icon), but we just recompiled the source with new settings.
- **EA Settings (Updated)**:
  - `HOST` changed to `172.17.0.2` (to avoid localhost loopback issues in Wine/Docker).
  - Added detailed diagnostic printing for `SocketConnect` errors.
- **Algo Trading**: Enabled globally (Green button) and in EA properties.

## 4. Pending Tasks / Next Steps
1. **Reload EA**: In noVNC, Remove `BotBridge` from the chart and re-attach it to pick up the newly compiled `BotBridge.ex5`.
2. **Check Logs**: Monitor the "Experts" tab at the bottom of the MT5 window for:
   - "Attempting to connect to Python server at 172.17.0.2:5555..."
   - "Successfully connected" OR "SocketConnect failed with error code: XXXX".
3. **Verify Python Side**: Run the test script inside the container to see if it accepts the connection:
   ```bash
   docker exec exness-bot python3 /app/test_local_connectivity.py
   ```
4. **Final Stage**: Once `test_local_connectivity.py` shows "Connected successfully!", run the main live bot:
   ```bash
   docker exec exness-bot python3 /app/live_main.py
   ```

## 5. Key Files
- `entrypoint.sh`: Container startup and Wine/MT5 launch.
- `startup.ini`: MT5 auto-login/config.
- `MetaTrader 5/MQL5/Experts/BotBridge.mq5`: EA Source.
- `ea_bridge.py`: Python-side server logic.
- `test_local_connectivity.py`: Minimal script to test the bridge.
