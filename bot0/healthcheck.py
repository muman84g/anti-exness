"""Authenticated container healthcheck for the bot0 dashboard."""
from __future__ import annotations

import base64
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from auth_config import load_auth_credentials


def _credentials() -> tuple[str, str]:
    return load_auth_credentials()


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
