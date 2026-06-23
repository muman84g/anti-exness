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
        self._command_lock = threading.Lock()
        
        logger.info(f"File IPC Bridge initialized at {self.bridge_dir}")

    def start(self):
        """No background thread needed for file IPC, but maintaining API signature"""
        logger.info("File IPC Bridge is ready.")

    def start_server(self):
        """Alias for start() to match some modules' expectations"""
        self.start()

    def send_command(self, cmd_str, timeout=10):
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

            return "ERR|TIMEOUT"

    def stop(self):
        """Cleanup if needed"""
        pass

# Singleton instance to be used across all modules
ea_bridge = EABridgeServer()
