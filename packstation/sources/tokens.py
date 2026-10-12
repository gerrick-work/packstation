"""Keeps refreshed platform access tokens on disk (Shopee and TikTok tokens expire every few hours)."""
from __future__ import annotations

import json
from pathlib import Path


class TokenFile:
    def __init__(self, data_dir: str | Path, name: str):
        self.path = Path(data_dir) / f"{name}_tokens.json"

    def load(self) -> dict:
        if self.path.exists():
            return json.loads(self.path.read_text())
        return {}

    def save(self, tokens: dict) -> None:
        self.path.write_text(json.dumps(tokens, indent=2))
        self.path.chmod(0o600)
