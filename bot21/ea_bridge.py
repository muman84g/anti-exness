import glob
import logging
import os
import platform
import threading
import time
import uuid

logger = logging.getLogger(__name__)


def _configured_files_dir():
    for env_name in ("EA_BRIDGE_FILES_DIR", "MT5_FILES_DIR"):
        value = os.environ.get(env_name)
        if value:
            return os.path.expandvars(os.path.expanduser(value))
    try:
        from live_config import EA_BRIDGE_FILES_DIR
    except Exception:
        return None
    if EA_BRIDGE_FILES_DIR:
        return os.path.expandvars(os.path.expanduser(str(EA_BRIDGE_FILES_DIR)))
    return None


def _windows_terminal_files_dirs():
    appdata = os.environ.get("APPDATA")
    if appdata:
        terminal_root = os.path.join(appdata, "MetaQuotes", "Terminal")
    else:
        user_profile = os.environ.get("USERPROFILE", os.path.expanduser("~"))
        terminal_root = os.path.join(user_profile, "AppData", "Roaming", "MetaQuotes", "Terminal")
    pattern = os.path.join(terminal_root, "*", "MQL5", "Files")
    paths = [path for path in glob.glob(pattern) if os.path.isdir(path)]

    def score(path):
        mql5_dir = os.path.dirname(path)
        experts_dir = os.path.join(mql5_dir, "Experts")
        has_s21_bridge = int(
            os.path.isfile(os.path.join(experts_dir, "BotBridge_s21.ex5"))
            or os.path.isfile(os.path.join(experts_dir, "BotBridge_s21.mq5"))
        )
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            mtime = 0.0
        return (has_s21_bridge, mtime)

    return sorted(paths, key=score, reverse=True)


def resolve_files_dir():
    configured = _configured_files_dir()
    if configured:
        return configured
    if platform.system() == "Windows":
        candidates = _windows_terminal_files_dirs()
        if candidates:
            return candidates[0]
        return r"C:\Program Files\MetaTrader 5\MQL5\Files"
    return "/root/.wine/drive_c/Program Files/MetaTrader 5/MQL5/Files"


class EABridgeServer:
    def __init__(self, files_dir=None):
        if files_dir is None:
            files_dir = resolve_files_dir()

        self.bridge_dir = files_dir
        self.cmd_file = os.path.join(
            self.bridge_dir, os.environ.get("EA_BRIDGE_COMMAND_FILE", "cmd_s21.txt")
        )
        self.res_file = os.path.join(
            self.bridge_dir, os.environ.get("EA_BRIDGE_RESPONSE_FILE", "res_s21.txt")
        )
        self.heartbeat_file = os.path.join(
            self.bridge_dir, os.environ.get("EA_BRIDGE_HEARTBEAT_FILE", "heartbeat_s21.txt")
        )
        self.lock_file = os.path.join(
            self.bridge_dir, os.environ.get("EA_BRIDGE_LOCK_FILE", "ea_bridge_s21.lock")
        )
        self.lock_stale_seconds = float(os.environ.get("EA_BRIDGE_LOCK_STALE_SECONDS", "30"))
        self._command_lock = threading.Lock()

        logger.info("File IPC Bridge initialized at %s", self.bridge_dir)
        logger.info(
            "File IPC Bridge files cmd=%s res=%s lock=%s",
            self.cmd_file,
            self.res_file,
            self.lock_file,
        )

    def _acquire_ipc_lock(self, timeout):
        deadline = time.monotonic() + max(1.0, float(timeout))
        token = f"{os.getpid()}|{time.time():.6f}|{uuid.uuid4().hex}"
        while time.monotonic() < deadline:
            try:
                fd = os.open(self.lock_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, token.encode("ascii", errors="replace"))
                os.fsync(fd)
                return fd
            except FileExistsError:
                try:
                    age = time.time() - os.path.getmtime(self.lock_file)
                except OSError:
                    age = 0.0
                if age > self.lock_stale_seconds:
                    try:
                        os.remove(self.lock_file)
                        logger.warning("Removed stale EA bridge IPC lock: %s", self.lock_file)
                    except OSError:
                        pass
                time.sleep(0.05)
            except FileNotFoundError:
                logger.error("EA bridge directory does not exist: %s", self.bridge_dir)
                return None
            except Exception as exc:
                logger.error("Could not acquire EA bridge IPC lock: %s", exc)
                return None
        return None

    def _release_ipc_lock(self, fd):
        try:
            os.close(fd)
        except Exception:
            pass
        try:
            os.remove(self.lock_file)
        except FileNotFoundError:
            pass
        except Exception as exc:
            logger.warning("Could not remove EA bridge IPC lock: %s", exc)

    def _clear_command_file(self):
        try:
            with open(self.cmd_file, "w") as f:
                f.write("")
                f.flush()
                os.fsync(f.fileno())
        except Exception as exc:
            logger.warning("Could not clear EA bridge command file: %s", exc)

    def start(self):
        logger.info("File IPC Bridge is ready.")

    def start_server(self):
        self.start()

    def send_command(self, cmd_str, timeout=10):
        with self._command_lock:
            lock_fd = self._acquire_ipc_lock(timeout)
            if lock_fd is None:
                return "ERR|LOCK_TIMEOUT"

            try:
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
                except Exception as exc:
                    logger.error("Error writing command file: %s", exc)
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
                            time.sleep(0.05)
                            continue
                    time.sleep(0.1)

                self._clear_command_file()
                return "ERR|TIMEOUT"
            finally:
                self._release_ipc_lock(lock_fd)

    def stop(self):
        pass


ea_bridge = EABridgeServer()
