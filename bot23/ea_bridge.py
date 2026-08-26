# -*- coding: utf-8 -*-
"""File IPC bridge template for botNN."""

from __future__ import annotations

import os
import logging
import re
import threading
import time


DIAGNOSTIC_COMMANDS = {"HIST", "INFO", "ORDERS", "POSITIONS"}
HEALTH_CONTINUATION_LOG_SECONDS = 60.0


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
        self._command_health: dict[str, dict[str, float | int | str]] = {}

    @staticmethod
    def _command_name(cmd_str: str) -> str:
        return str(cmd_str or "").split("|", 1)[0].strip().upper() or "UNKNOWN"

    @staticmethod
    def _error_code(result: str) -> str | None:
        if result and not result.startswith("ERR|"):
            return None
        raw = result.split("|", 2)[1] if result.startswith("ERR|") and "|" in result else "EMPTY_RESPONSE"
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", raw.strip())[:64]
        return safe or "UNKNOWN_ERROR"

    def _record_command_health(
        self,
        command: str,
        result: str,
        started_at: float,
        finished_at: float,
    ) -> None:
        if command not in DIAGNOSTIC_COMMANDS:
            return
        elapsed = max(0.0, finished_at - started_at)
        error = self._error_code(result)
        active = self._command_health.get(command)
        if error is None:
            if active is not None:
                logging.info(
                    "S23 bridge recovered: command=%s outage_seconds=%.3f attempts=%d last_error=%s recovery_elapsed_seconds=%.3f",
                    command,
                    max(0.0, finished_at - float(active["started_at"])),
                    int(active["attempts"]),
                    str(active["last_error"]),
                    elapsed,
                )
                self._command_health.pop(command, None)
            return

        if active is None:
            self._command_health[command] = {
                "started_at": started_at,
                "last_logged_at": finished_at,
                "attempts": 1,
                "last_error": error,
            }
            logging.error(
                "S23 bridge command failed: command=%s error=%s elapsed_seconds=%.3f consecutive_failures=1",
                command,
                error,
                elapsed,
            )
            return

        active["attempts"] = int(active["attempts"]) + 1
        prior_error = str(active["last_error"])
        active["last_error"] = error
        should_log = (
            error != prior_error
            or finished_at - float(active["last_logged_at"]) >= HEALTH_CONTINUATION_LOG_SECONDS
        )
        if should_log:
            active["last_logged_at"] = finished_at
            logging.warning(
                "S23 bridge command still failing: command=%s outage_seconds=%.3f attempts=%d last_error=%s elapsed_seconds=%.3f",
                command,
                max(0.0, finished_at - float(active["started_at"])),
                int(active["attempts"]),
                error,
                elapsed,
            )

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
            command = self._command_name(cmd_str)
            started_at = time.monotonic()

            def finish(result: str) -> str:
                self._record_command_health(command, result, started_at, time.monotonic())
                return result

            fd = self._acquire_ipc_lock(timeout)
            if fd is None:
                return finish("ERR|LOCK_TIMEOUT")
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
                    return finish("ERR|WRITE_FAILED")
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
                                return finish(res)
                        except OSError:
                            time.sleep(0.05)
                            continue
                    time.sleep(0.1)
                return finish("ERR|TIMEOUT")
            finally:
                self._release_ipc_lock(fd)


ea_bridge = EABridgeServer()
