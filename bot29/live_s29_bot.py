from __future__ import annotations

import os
from pathlib import Path

os.environ["BOT_SUFFIX"] = "s29"
os.environ["EXPECTED_MAGIC"] = "200029"

local = Path(__file__).with_name("_base_live_bot.py")
source = local if local.exists() else Path(__file__).parents[1] / "bot27" / "live_s27_bot.py"
exec(compile(source.read_bytes(), str(source), "exec"), globals(), globals())
