"""Remembers which orders were printed (so Print All never prints twice) and which were packed."""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime
from pathlib import Path


class Store:
    def __init__(self, data_dir: str | Path):
        self.path = Path(data_dir) / "packstation.db"
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.lock = threading.Lock()
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS printed ("
            " key TEXT PRIMARY KEY, printed_at TEXT, tracking TEXT, labels INTEGER)"
        )
        self.db.execute("CREATE TABLE IF NOT EXISTS packed (key TEXT PRIMARY KEY, packed_at TEXT)")
        self.db.commit()

    def _now(self) -> str:
        return datetime.now().isoformat(timespec="seconds")

    def printed(self) -> dict[str, str]:
        with self.lock:
            return {k: t for k, t in self.db.execute("SELECT key, printed_at FROM printed")}

    def mark(self, key: str, tracking: str | None, labels: int) -> None:
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO printed VALUES (?, ?, ?, ?)",
                            (key, self._now(), tracking, labels))
            self.db.commit()

    def unmark(self, key: str) -> None:
        with self.lock:
            self.db.execute("DELETE FROM printed WHERE key = ?", (key,))
            self.db.commit()

    def packed(self) -> dict[str, str]:
        with self.lock:
            return {k: t for k, t in self.db.execute("SELECT key, packed_at FROM packed")}

    def mark_packed(self, key: str, packed: bool = True) -> None:
        with self.lock:
            if packed:
                self.db.execute("INSERT OR REPLACE INTO packed VALUES (?, ?)", (key, self._now()))
            else:
                self.db.execute("DELETE FROM packed WHERE key = ?", (key,))
            self.db.commit()
