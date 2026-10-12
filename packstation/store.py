"""Remembers which orders were printed so Print All never prints an order twice."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path


class Store:
    def __init__(self, data_dir: str | Path):
        self.path = Path(data_dir) / "packstation.db"
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS printed ("
            " key TEXT PRIMARY KEY, printed_at TEXT, tracking TEXT, labels INTEGER)"
        )
        self.db.commit()

    def printed(self) -> dict[str, str]:
        return {k: t for k, t in self.db.execute("SELECT key, printed_at FROM printed")}

    def mark(self, key: str, tracking: str | None, labels: int) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO printed VALUES (?, ?, ?, ?)",
            (key, datetime.now().isoformat(timespec="seconds"), tracking, labels),
        )
        self.db.commit()

    def unmark(self, key: str) -> None:
        self.db.execute("DELETE FROM printed WHERE key = ?", (key,))
        self.db.commit()
