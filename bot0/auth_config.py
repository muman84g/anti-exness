"""Shared file-authoritative credentials for bot0 and its healthcheck."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

_USERNAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


def load_auth_credentials() -> tuple[str, str]:
    """Read and validate the sole Basic-auth identity from BOT0_AUTH_FILE."""
    auth_file_value = os.environ.get("BOT0_AUTH_FILE", "").strip()
    if not auth_file_value:
        raise RuntimeError("BOT0_AUTH_FILE is required")
    try:
        payload = json.loads(Path(auth_file_value).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("invalid BOT0_AUTH_FILE") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("invalid BOT0_AUTH_FILE")
    username = payload.get("username")
    password = payload.get("password")
    if not isinstance(username, str) or _USERNAME_PATTERN.fullmatch(username) is None:
        raise RuntimeError("invalid BOT0_AUTH_FILE username")
    if not isinstance(password, str) or not password.strip():
        raise RuntimeError("BOT0_AUTH_FILE password is required")
    return username, password
