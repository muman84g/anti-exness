"""Authenticated container healthcheck for the bot0 dashboard."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def _credentials() -> tuple[str, str]:
    auth_file_value = os.environ.get("BOT0_AUTH_FILE", "").strip()
    if not auth_file_value:
        raise RuntimeError("BOT0_AUTH_FILE is required")
    try:
        payload = json.loads(Path(auth_file_value).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("invalid BOT0_AUTH_FILE") from exc
    username = payload.get("username") if isinstance(payload, dict) else None
    password = payload.get("password") if isinstance(payload, dict) else None
    expected_username = os.environ.get("BOT0_AUTH_USER", "bot0").strip()
    if not isinstance(username, str) or username != expected_username or not username:
        raise RuntimeError("invalid BOT0_AUTH_FILE username")
    if not isinstance(password, str) or not password.strip():
        raise RuntimeError("BOT0_AUTH_FILE password is required")
    return username, password


def main() -> None:
    username, password = _credentials()
    port = os.environ.get("BOT0_PORT", "8230")
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    request = Request(
        f"http://127.0.0.1:{port}/api/health",
        headers={"Authorization": f"Basic {token}"},
    )
    try:
        with urlopen(request, timeout=2) as response:
            if response.status != 200:
                raise RuntimeError("healthcheck failed")
    except (HTTPError, URLError, OSError) as exc:
        raise RuntimeError("healthcheck failed") from exc


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from None
