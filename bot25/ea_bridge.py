# -*- coding: utf-8 -*-
"""File IPC bridge template for botNN."""

from __future__ import annotations

import os
import logging
import re
import secrets
import threading
import time
from typing import BinaryIO


DIAGNOSTIC_COMMANDS = {"HIST", "INFO", "ORDERS", "POSITIONS"}
HEALTH_CONTINUATION_LOG_SECONDS = 60.0


def _local_ipc_name(value: object, label: str) -> str:
    name = value if isinstance(value, str) else ""
    if (
        not name
        or name in {".", ".."}
        or not name.isascii()
        or any(not (char.isalnum() or char in "._-") for char in name)
    ):
        raise ValueError(f"invalid EA bridge {label}")
    return name


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
        suffix = _local_ipc_name(bot_suffix or os.environ.get("BOT_SUFFIX", "sNN"), "bot suffix")
        self.bridge_dir = files_dir or resolve_files_dir()
        command_name = _local_ipc_name(os.environ.get("EA_BRIDGE_COMMAND_FILE", f"cmd_{suffix}.txt"), "command filename")
        response_name = _local_ipc_name(os.environ.get("EA_BRIDGE_RESPONSE_FILE", f"res_{suffix}.txt"), "response filename")
        lock_name = _local_ipc_name(os.environ.get("EA_BRIDGE_LOCK_FILE", f"ea_bridge_{suffix}.lock"), "lock filename")
        if len({command_name.casefold(), response_name.casefold(), lock_name.casefold()}) != 3:
            raise ValueError("EA bridge command, response, and lock filenames must be distinct")
        self.cmd_file = os.path.join(self.bridge_dir, command_name)
        self.res_file = os.path.join(self.bridge_dir, response_name)
        self.lock_file = os.path.join(self.bridge_dir, lock_name)
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
                    "S25 bridge recovered: command=%s outage_seconds=%.3f attempts=%d last_error=%s recovery_elapsed_seconds=%.3f",
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
                "S25 bridge command failed: command=%s error=%s elapsed_seconds=%.3f consecutive_failures=1",
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
                "S25 bridge command still failing: command=%s outage_seconds=%.3f attempts=%d last_error=%s elapsed_seconds=%.3f",
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

    def _acquire_ipc_lock(self, timeout: float) -> BinaryIO | None:
        os.makedirs(self.bridge_dir, exist_ok=True)
        handle = open(self.lock_file, "a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
            os.fsync(handle.fileno())
        deadline = time.monotonic() + timeout
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

    def _release_ipc_lock(self, handle: BinaryIO | None) -> None:
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
        finally:
            handle.close()

    def send_command(self, cmd_str: str, timeout: float = 10) -> str:
        with self._command_lock:
            command = self._command_name(cmd_str)
            started_at = time.monotonic()

            def finish(result: str) -> str:
                self._record_command_health(command, result, started_at, time.monotonic())
                return result

            try:
                timeout_value = float(timeout)
            except (TypeError, ValueError, OverflowError):
                return finish("ERR|INVALID_TIMEOUT")
            if not (0.0 < timeout_value <= 300.0):
                return finish("ERR|INVALID_TIMEOUT")
            if not cmd_str or any(char in str(cmd_str) for char in ("\r", "\n")):
                return finish("ERR|INVALID_COMMAND")
            fd = self._acquire_ipc_lock(timeout_value)
            if fd is None:
                return finish("ERR|LOCK_TIMEOUT")
            temp_cmd_file: str | None = None
            try:
                if os.path.exists(self.cmd_file):
                    try:
                        # Bridge v7 acknowledged a command by truncating this
                        # file.  A zero-byte remnant cannot contain a live
                        # request and would otherwise wedge v8 permanently
                        # after an in-place upgrade.
                        if os.path.getsize(self.cmd_file) == 0:
                            os.remove(self.cmd_file)
                        else:
                            return finish("ERR|COMMAND_BUSY")
                    except OSError:
                        return finish("ERR|COMMAND_BUSY")
                if os.path.exists(self.res_file):
                    try:
                        os.remove(self.res_file)
                    except OSError:
                        return finish("ERR|RESPONSE_BUSY")
                try:
                    request_id = secrets.token_hex(16)
                    # The EA compares with a one-second UTC clock and floors
                    # this millisecond value. Early expiry is acceptable;
                    # execution after the caller has timed out is not.
                    deadline_msc = int((time.time() + timeout_value) * 1000)
                    framed_command = f"REQ|{request_id}|{deadline_msc}|{cmd_str}"
                    temp_cmd_file = f"{self.cmd_file}.{os.getpid()}.{threading.get_ident()}.tmp"
                    with open(temp_cmd_file, "x", encoding="utf-8") as f:
                        f.write(framed_command)
                        f.flush()
                        os.fsync(f.fileno())
                    os.replace(temp_cmd_file, self.cmd_file)
                    temp_cmd_file = None
                except OSError:
                    return finish("ERR|WRITE_FAILED")
                deadline = time.monotonic() + timeout_value
                while time.monotonic() < deadline:
                    if os.path.exists(self.res_file):
                        try:
                            with open(self.res_file, "r", encoding="utf-8", errors="replace") as f:
                                res = f.read().strip()
                            if not (res.startswith("RES|") and res.endswith("|ENDRES")):
                                time.sleep(0.05)
                                continue
                            body = res[4:-7]
                            os.remove(self.res_file)
                            if not body.startswith("RID|"):
                                return finish("ERR|RESPONSE_ID_MISSING")
                            response_parts = body.split("|", 2)
                            if len(response_parts) != 3:
                                return finish("ERR|RESPONSE_ID_MALFORMED")
                            if response_parts[1] != request_id:
                                continue
                            if response_parts[2]:
                                return finish(response_parts[2])
                        except OSError:
                            time.sleep(0.05)
                            continue
                    time.sleep(0.1)
                return finish("ERR|TIMEOUT")
            finally:
                if temp_cmd_file is not None:
                    try:
                        os.remove(temp_cmd_file)
                    except FileNotFoundError:
                        pass
                self._release_ipc_lock(fd)


ea_bridge = EABridgeServer()
