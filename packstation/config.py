"""Loads config.toml (secrets and printer setup live there, never in git) and writes settings back
from the Settings screen, keeping every other line of the file as it is."""
from __future__ import annotations

import os
import re
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11 (older macOS system Python)
    import tomli as tomllib

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PATH = ROOT / "config.toml"


def path_of(path: str | os.PathLike | None = None) -> Path:
    return Path(path or os.environ.get("PACKSTATION_CONFIG", DEFAULT_PATH))


def load(path: str | os.PathLike | None = None) -> dict:
    path = path_of(path)
    if not path.exists():
        raise SystemExit(
            f"No config at {path}. Copy config.example.toml to config.toml and fill it in."
        )
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    cfg.setdefault("app", {})
    cfg["app"].setdefault("data_dir", str(ROOT / "data"))
    cfg["_path"] = str(path)
    Path(cfg["app"]["data_dir"]).mkdir(parents=True, exist_ok=True)
    return cfg


def _fmt(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return f"{v:g}" if isinstance(v, float) else str(v)
    if isinstance(v, list):
        return "[" + ", ".join(_fmt(x) for x in v) + "]"
    return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'


def set_values(path: str | os.PathLike, section: str, values: dict) -> None:
    """Set keys in one [section] of config.toml. Other keys, sections and comments are kept.
    Missing sections are added at the end. Writes atomically."""
    path = Path(path)
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    header = re.compile(rf"^\[{re.escape(section)}\][ \t]*(#.*)?$", re.M)
    m = header.search(text)
    if not m:
        text = text.rstrip("\n") + f"\n\n[{section}]\n"
        m = header.search(text)
    start = m.end()
    nxt = re.compile(r"^\[", re.M).search(text, start)
    end = nxt.start() if nxt else len(text)
    body = text[start:end]
    for key, val in values.items():
        line = f"{key} = {_fmt(val)}"
        pat = re.compile(rf"^[ \t]*#?[ \t]*{re.escape(key)}[ \t]*=.*$", re.M)
        live = re.compile(rf"^[ \t]*{re.escape(key)}[ \t]*=.*$", re.M)
        if live.search(body):
            body = live.sub(line, body, count=1)
        elif pat.search(body):  # commented-out example line: replace it
            body = pat.sub(line, body, count=1)
        else:
            body = body.rstrip("\n") + "\n" + line + "\n"
    if not body.endswith("\n"):
        body += "\n"
    if nxt and not body.endswith("\n\n"):
        body += "\n"
    new = text[:start] + body if body.startswith("\n") else text[:start] + "\n" + body.lstrip("\n")
    new += text[end:]
    tmp = path.with_suffix(".tmp")
    tmp.write_text(new, encoding="utf-8")
    tomllib.loads(new)  # never write a file that won't load
    tmp.replace(path)
