"""Install a new version from a GitHub ZIP chosen in Settings (no Terminal needed).

Keeps everything that belongs to this Mac: config.toml, data/ (printed history, product list, log,
platform tokens) and the Python environment (.venv). The product list is moved into data/ first
if it still lives in labels/, so an update can never overwrite it. Then installs any new Python
packages and the caller restarts Pack Station.
"""
from __future__ import annotations

import io
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

from .config import ROOT
from .errors import PackError

KEEP = {"config.toml", "data", ".venv", ".git"}


def install_zip(blob: bytes, root: Path = ROOT, pip: bool = True) -> dict:
    try:
        zf = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile as e:
        raise PackError("CONFIG", "reading the update", "that file isn't a ZIP") from e
    names = [n for n in zf.namelist() if not n.endswith("/")]
    main = [n for n in names if n.endswith("packstation/__main__.py")]
    if not main:
        raise PackError("CONFIG", "reading the update", "this ZIP doesn't contain Pack Station")
    prefix = main[0][: -len("packstation/__main__.py")]

    data = root / "data"
    data.mkdir(exist_ok=True)
    old_list = root / "labels" / "products.csv"
    if old_list.exists() and not (data / "products.csv").exists():
        shutil.copy(old_list, data / "products.csv")

    old_req = (root / "requirements.txt").read_text() if (root / "requirements.txt").exists() else ""
    written = 0
    for n in names:
        if not n.startswith(prefix):
            continue
        rel = Path(n[len(prefix):])
        if not rel.parts or rel.parts[0] in KEEP or ".." in rel.parts:
            continue
        dest = root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".new")
        tmp.write_bytes(zf.read(n))
        tmp.replace(dest)
        if rel.suffix in (".command", ".sh"):
            dest.chmod(0o755)
        written += 1

    new_req = (root / "requirements.txt").read_text() if (root / "requirements.txt").exists() else ""
    installed = False
    if pip and new_req != old_req:
        r = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-r", str(root / "requirements.txt")],
                           capture_output=True, text=True, timeout=900)
        if r.returncode:
            raise PackError("CONFIG", "installing new parts for the update", r.stderr.strip()[-300:])
        installed = True
    return {"files": written, "installed_packages": installed}
