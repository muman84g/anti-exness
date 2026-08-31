# -*- coding: utf-8 -*-
"""File IPC bridge template for botNN."""

from __future__ import annotations

import os
import logging
import math
import re
import threading
import time
import uuid


DIAGNOSTIC_COMMANDS = {"HIST", "HISTPAGE", "INFO", "ORDERS", "POSITIONS"}
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
        self.claim_file = os.path.join(self.bridge_dir, os.environ.get("EA_BRIDGE_CLAIM_FILE", f"claim_{suffix}.txt"))
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

    def _acquire_ipc_lock(self, timeout: float):
        deadline = time.monotonic() + timeout
        os.makedirs(self.bridge_dir, exist_ok=True)
        handle = open(self.lock_file, "a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
            os.fsync(handle.fileno())
        while time.monotonic() < deadline:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return handle
            except (OSError, IOError):
                time.sleep(0.05)
        handle.close()
        return None

    def _release_ipc_lock(self, handle) -> None:
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except (OSError, IOError):
            pass
        handle.close()

    def send_command(self, cmd_str: str, timeout: float = 10) -> str:
        with self._command_lock:
            command = self._command_name(cmd_str)
            started_at = time.monotonic()

            def finish(result: str) -> str:
                self._record_command_health(command, result, started_at, time.monotonic())
                return result

            if (
                isinstance(timeout, bool)
                or not isinstance(timeout, (int, float))
                or not math.isfinite(float(timeout))
                or float(timeout) <= 0.0
                or float(timeout) > 300.0
            ):
                return finish("ERR|INVALID_TIMEOUT")
            timeout = float(timeout)

            lock_handle = self._acquire_ipc_lock(timeout)
            if lock_handle is None:
                return finish("ERR|LOCK_TIMEOUT")
            request_id = uuid.uuid4().hex
            deadline_msc = int((time.time() + timeout) * 1000)
            envelope = f"REQ|{request_id}|{deadline_msc}|{cmd_str}"
            temp_cmd = f"{self.cmd_file}.{request_id}.tmp"
            try:
                # Never overwrite or queue behind an unclaimed command or a
                # durable EA claim. The EA must drain the single mutation slot
                # before another correlated request can be published.
                if os.path.exists(self.claim_file):
                    return finish("ERR|CLAIM_BUSY")
                if os.path.exists(self.cmd_file):
                    return finish("ERR|COMMAND_BUSY")
                if os.path.exists(self.res_file):
                    try:
                        os.remove(self.res_file)
                    except OSError:
                        if os.path.exists(self.res_file):
                            return finish("ERR|RESPONSE_BUSY")
                try:
                    with open(temp_cmd, "x", encoding="utf-8") as f:
                        f.write(envelope)
                        f.flush()
                        os.fsync(f.fileno())
                    os.replace(temp_cmd, self.cmd_file)
                except OSError:
                    try:
                        os.remove(temp_cmd)
                    except OSError:
                        pass
                    return finish("ERR|WRITE_FAILED")
                wait_deadline = time.monotonic() + timeout
                while time.monotonic() < wait_deadline:
                    if os.path.exists(self.res_file):
                        try:
                            with open(self.res_file, "r", encoding="utf-8", errors="replace") as f:
                                res = f.read().strip()
                            prefix = f"RES|{request_id}|"
                            suffix = "|ENDRES"
                            if res.startswith(prefix) and res.endswith(suffix):
                                try:
                                    os.remove(self.res_file)
                                except OSError:
                                    pass
                                return finish(res[len(prefix):-len(suffix)])
                        except OSError:
                            time.sleep(0.05)
                            continue
                    time.sleep(0.1)
                # Leave a published timeout in place. The EA owns expiry and
                # correlated cleanup; deleting here races with durable claim.
                return finish("ERR|TIMEOUT")
            finally:
                self._release_ipc_lock(lock_handle)


ea_bridge = EABridgeServer()
