import os
import time
import logging
import threading

logger = logging.getLogger(__name__)

class EABridgeServer:
    def __init__(self, files_dir=None):
        if files_dir is None:
            import platform
            if platform.system() == "Windows":
                # Windows Portable mode or standard path
                files_dir = r"C:\Program Files\MetaTrader 5\MQL5\Files"
            else:
                # Docker container default path for MT5 /portable
                files_dir = "/root/.wine/drive_c/Program Files/MetaTrader 5/MQL5/Files"
        
        self.bridge_dir = files_dir
        self.cmd_file = os.path.join(self.bridge_dir, "cmd.txt")
        self.res_file = os.path.join(self.bridge_dir, "res.txt")
        self.heartbeat_file = os.path.join(self.bridge_dir, "heartbeat.txt")
        self.command_timeout_seconds = float(os.environ.get("EA_BRIDGE_COMMAND_TIMEOUT_SECONDS", "10"))
        self.command_retries = max(0, int(os.environ.get("EA_BRIDGE_COMMAND_RETRIES", "0")))
        self.retry_sleep_seconds = float(os.environ.get("EA_BRIDGE_RETRY_SLEEP_SECONDS", "0.2"))
        self.slow_command_log_seconds = float(os.environ.get("EA_BRIDGE_SLOW_COMMAND_LOG_SECONDS", "1.0"))
        self._command_lock = threading.Lock()
        
        logger.info(f"File IPC Bridge initialized at {self.bridge_dir}")

    def start(self):
        """No background thread needed for file IPC, but maintaining API signature"""
        logger.info("File IPC Bridge is ready.")

    def start_server(self):
        """Alias for start() to match some modules' expectations"""
        self.start()

    def _clear_command_file(self):
        try:
            with open(self.cmd_file, "w") as f:
                f.write("")
                f.flush()
                os.fsync(f.fileno())
        except Exception as exc:
            logger.warning("Could not clear EA bridge command file: %s", exc)

    def send_command(self, cmd_str, timeout=None):
        op = str(cmd_str).split("|", 1)[0].upper()
        read_only_ops = {"CAPS", "ECHO", "INFO", "HIST", "POSITION", "POSITIONS", "ORDERS"}
        effective_timeout = self.command_timeout_seconds if timeout is None else float(timeout)
        attempts = 1 + (self.command_retries if op in read_only_ops else 0)
        last_res = "ERR|TIMEOUT"

        for attempt in range(1, attempts + 1):
            start = time.monotonic()
            res = self._send_command_once(cmd_str, timeout=effective_timeout)
            elapsed = time.monotonic() - start
            if elapsed >= self.slow_command_log_seconds:
                logger.warning(
                    "EA bridge slow command op=%s attempt=%d elapsed=%.2fs res=%s",
                    op,
                    attempt,
                    elapsed,
                    res,
                )
            if res != "ERR|TIMEOUT":
                return res
            last_res = res
            if attempt < attempts:
                logger.warning("EA bridge retrying read-only command op=%s after %s", op, res)
                time.sleep(max(0.0, self.retry_sleep_seconds))
        return last_res

    def _send_command_once(self, cmd_str, timeout):
        # cmd.txt/res.txt are a single shared IPC lane. Keep calls strictly
        # serialized and ignore response files that pre-date this command.
        with self._command_lock:
            if os.path.exists(self.res_file):
                try:
                    os.remove(self.res_file)
                except Exception:
                    pass

            try:
                with open(self.cmd_file, "w") as f:
                    f.write(cmd_str)
                    f.flush()
                    os.fsync(f.fileno())
                command_written_at = time.time()
            except Exception as e:
                logger.error(f"Error writing command file: {e}")
                return "ERR|WRITE_FAILED"

            start_time = time.time()
            while time.time() - start_time < timeout:
                if os.path.exists(self.res_file):
                    try:
                        response_mtime = os.path.getmtime(self.res_file)
                        if response_mtime + 0.001 < command_written_at:
                            os.remove(self.res_file)
                            continue
                        with open(self.res_file, "r") as f:
                            res = f.read().strip()
                        os.remove(self.res_file)
                        if res:
                            return res
                    except Exception:
                        # File might be locked while EA is writing.
                        time.sleep(0.05)
                        continue
                time.sleep(0.1)

            self._clear_command_file()
            return "ERR|TIMEOUT"

    def stop(self):
        """Cleanup if needed"""
        pass

# Singleton instance to be used across all modules
ea_bridge = EABridgeServer()
