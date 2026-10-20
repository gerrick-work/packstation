"""Fixed error codes with a plain-English line, shown on order cards, the status bar and the log."""
from __future__ import annotations

import collections
import logging
import threading
from datetime import datetime
from pathlib import Path

CODES = {
    "ODOO-AUTH": "Odoo refused the login. Check the Odoo email and API key in Settings.",
    "ODOO-NET": "Can't reach Odoo. Check this Mac's internet connection.",
    "ODOO-CRON": "Odoo couldn't run its Shopee/TikTok sync.",
    "ODOO-VALIDATE": "Packed here, but Odoo didn't validate the delivery. Open it in Odoo and press Validate.",
    "ODOO-ERROR": "Odoo answered with an error. The detail says what it was.",
    "WAYBILL-MISSING": "No waybill from Shopee/TikTok yet. Press Refresh, or check the order in Seller Centre.",
    "SHOPEE-SG": "Couldn't read the Shopee Singapore shop. The detail says why; Odoo's own sync is not affected.",
    "TIKTOK-ARRANGE": "Couldn't book the TikTok courier, or TikTok hasn't sent the label yet.",
    "BT-NOTFOUND": "Printer not found. Is it switched on, and is Label Expert on the phone closed?",
    "BT-PERMISSION": "This Mac hasn't allowed Bluetooth for Pack Station. System Settings > Privacy & Security > Bluetooth.",
    "BT-SEND": "The printer stopped receiving part-way. Check it's on and has labels, then print again.",
    "PRINT-PARTIAL": "Only part of this order printed. It is NOT marked printed; print it again.",
    "CONFIG": "A setting is missing or wrong in Settings.",
    "INFO-MISSING": "Held back because something this order needs is missing. Fix it, or turn off "
                    "\"Hold orders with missing info\".",
}


class PackError(Exception):
    """An error with one of the fixed codes, the step that failed and optional detail."""

    def __init__(self, code: str, step: str = "", detail: str = ""):
        assert code in CODES, code
        self.code, self.step, self.detail = code, step, detail
        super().__init__(self.text)

    @property
    def text(self) -> str:
        s = f"{self.code}: {CODES[self.code]}"
        if self.step:
            s += f" (step: {self.step})"
        if self.detail:
            s += f" [{self.detail}]"
        return s

    def as_dict(self) -> dict:
        return {"code": self.code, "message": CODES[self.code], "step": self.step, "detail": self.detail}


def classify(exc: BaseException, step: str) -> PackError:
    """Turn any exception into a PackError with the closest code."""
    if isinstance(exc, PackError):
        if not exc.step:
            exc.step = step
        return exc
    msg = str(exc)
    low = msg.lower()
    import xmlrpc.client
    import socket

    if isinstance(exc, xmlrpc.client.Fault):
        if "access" in low and ("denied" in low or "error" in low) or "session expired" in low:
            return PackError("ODOO-AUTH", step, msg[:200])
        return PackError("ODOO-CRON" if "cron" in step.lower() else "ODOO-ERROR", step, msg[:200])
    if isinstance(exc, (ConnectionError, socket.timeout, socket.gaierror, TimeoutError, OSError)) and "odoo" in step.lower():
        return PackError("ODOO-NET", step, msg[:200])
    if "bluetooth" in step.lower() or "printer" in step.lower():
        if any(w in low for w in ("unauthorized", "not authorized", "permission", "denied")):
            return PackError("BT-PERMISSION", step, msg[:200])
        if any(w in low for w in ("not found", "no device", "could not find")):
            return PackError("BT-NOTFOUND", step, msg[:200])
        return PackError("BT-SEND", step, msg[:200])
    return PackError("CONFIG", step, msg[:200])


class EventLog:
    """Every result and error, newest first, for the Log page; also written to data/packstation.log."""

    def __init__(self, data_dir: str | Path, keep: int = 500):
        self.items: collections.deque = collections.deque(maxlen=keep)
        self.lock = threading.Lock()
        path = (Path(data_dir) / "packstation.log").resolve()
        self.logger = logging.getLogger(f"packstation.{path}")
        self.logger.propagate = False
        if not self.logger.handlers:
            h = logging.FileHandler(path, encoding="utf-8")
            h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            self.logger.addHandler(h)
            self.logger.setLevel(logging.INFO)

    def add(self, level: str, text: str, code: str | None = None, key: str | None = None) -> dict:
        item = {"at": datetime.now().isoformat(timespec="seconds"), "level": level, "text": text,
                "code": code, "key": key}
        with self.lock:
            self.items.appendleft(item)
        self.logger.log(logging.ERROR if level == "error" else logging.INFO,
                        f"{code + ' ' if code else ''}{key + ' ' if key else ''}{text}")
        return item

    def recent(self, n: int = 200) -> list[dict]:
        with self.lock:
            return list(self.items)[:n]
