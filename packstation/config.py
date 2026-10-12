"""Loads config.toml (secrets and printer setup live there, never in git)."""
from __future__ import annotations

import os
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11 (older macOS system Python)
    import tomli as tomllib

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PATH = ROOT / "config.toml"


def load(path: str | os.PathLike | None = None) -> dict:
    path = Path(path or os.environ.get("PACKSTATION_CONFIG", DEFAULT_PATH))
    if not path.exists():
        raise SystemExit(
            f"No config at {path}. Copy config.example.toml to config.toml and fill it in."
        )
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    cfg.setdefault("app", {})
    cfg["app"].setdefault("data_dir", str(ROOT / "data"))
    Path(cfg["app"]["data_dir"]).mkdir(parents=True, exist_ok=True)
    return cfg
