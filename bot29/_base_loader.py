from __future__ import annotations

from pathlib import Path


def execute_base(globals_dict: dict[str, object], mounted_name: str, source_name: str) -> None:
    local = Path(__file__).with_name(mounted_name)
    source = local if local.exists() else Path(__file__).parents[1] / "bot27" / source_name
    exec(compile(source.read_bytes(), str(source), "exec"), globals_dict, globals_dict)
