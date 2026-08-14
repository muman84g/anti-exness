# -*- coding: utf-8 -*-
"""File IPC bridge template for botNN."""

from __future__ import annotations

import os
import threading
import time


def resolve_files_dir() -> str:
    for env_name in ("EA_BRIDGE_FILES_DIR", "MT5_FILES_DIR"):
        value = os.environ.get(env_name)
        if value:
            return os.path.expandvars(os.path.expanduser(value))
    try:
        from live_config import EA_BRIDGE_FILES_DIR  # type: ignore
    except Exception:
        EA_BRIDGE_FILES_DIR = ""
    if EA_BRIDGE_FILES_DIR:
        return os.path.expandvars(os.path.expanduser(str(EA_BRIDGE_FILES_DIR)))
    return "/root/.wine/drive_c/Program Files/MetaTrader 5/MQL5/Files"


class EABridgeServer:
    def __init__(self, bot_suffix: str | None = None, files_dir: str | None = None):
        suffix = bot_suffix or os.environ.get("BOT_SUFFIX", "sNN")
        self.bridge_dir = files_dir or resolve_files_dir()
        self.cmd_file = os.path.join(self.bridge_dir, os.environ.get("EA_BRIDGE_COMMAND_FILE", f"cmd_{suffix}.txt"))
        self.res_file = os.path.join(self.bridge_dir, os.environ.get("EA_BRIDGE_RESPONSE_FILE", f"res_{suffix}.txt"))
        self.lock_file = os.path.join(self.bridge_dir, os.environ.get("EA_BRIDGE_LOCK_FILE", f"ea_bridge_{suffix}.lock"))
        self._command_lock = threading.Lock()

    def start(self) -> None:
        if not os.path.isdir(self.bridge_dir):
            raise FileNotFoundError(self.bridge_dir)

    def start_server(self) -> None:
        self.start()

    def _acquire_ipc_lock(self, timeout: float) -> int | None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                return os.open(self.lock_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                time.sleep(0.05)
        return None

    def _release_ipc_lock(self, fd: int | None) -> None:
        if fd is None:
            return
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.remove(self.lock_file)
        except FileNotFoundError:
            pass

    def send_command(self, cmd_str: str, timeout: float = 10) -> str:
        with self._command_lock:
            fd = self._acquire_ipc_lock(timeout)
            if fd is None:
                return "ERR|LOCK_TIMEOUT"
            try:
                if os.path.exists(self.res_file):
                    try:
                        os.remove(self.res_file)
                    except OSError:
                        pass
                try:
                    with open(self.cmd_file, "w", encoding="utf-8") as f:
                        f.write(cmd_str)
                        f.flush()
                        os.fsync(f.fileno())
                    written_at = time.time()
                except OSError:
                    return "ERR|WRITE_FAILED"
                deadline = time.time() + timeout
                while time.time() < deadline:
                    if os.path.exists(self.res_file):
                        try:
                            if os.path.getmtime(self.res_file) + 0.001 < written_at:
                                os.remove(self.res_file)
                                continue
                            with open(self.res_file, "r", encoding="utf-8", errors="replace") as f:
                                res = f.read().strip()
                            os.remove(self.res_file)
                            if res:
                                return res
                        except OSError:
                            time.sleep(0.05)
                            continue
                    time.sleep(0.1)
                return "ERR|TIMEOUT"
            finally:
                self._release_ipc_lock(fd)


ea_bridge = EABridgeServer()
