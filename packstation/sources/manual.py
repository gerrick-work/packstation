"""Orders added by hand, with a waybill PDF downloaded from Seller Centre. For orders no connector
fetches yet (e.g. the Shopee Singapore shop).

data/manual_orders.json:
    [{"order_id": "261014JTXG926Q", "platform": "shopee SG", "tracking": "SG260080596808C",
      "waybill": "manual/261014JTXG926Q.pdf", "added": "2026-10-14",
      "items": [{"sku": "SP025-3G", "name": "Galaxolide 50 3g", "qty": 1}]}]
`waybill` is relative to the data folder. Entries older than `keep_days` drop off the list.
Optional `covers`: Odoo delivery names this parcel replaces, e.g. a TikTok combined shipment
["WH/OUT/00086", "WH/OUT/00087"]: those leave the list and are validated together when packed.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

from ..errors import PackError
from ..models import Item, Order
from . import Source


class ManualSource(Source):
    name = "manual"

    def __init__(self, data_dir: str | Path, keep_days: int = 14):
        self.dir = Path(data_dir)
        self.path = self.dir / "manual_orders.json"
        self.keep_days = keep_days

    def _rows(self) -> list[dict]:
        if not self.path.exists():
            return []
        try:
            rows = json.loads(self.path.read_text(encoding="utf-8"))
        except ValueError as e:
            raise PackError("CONFIG", "reading manual_orders.json", str(e)[:160]) from e
        cutoff = date.today() - timedelta(days=self.keep_days)
        out = []
        for r in rows if isinstance(rows, list) else []:
            try:
                added = date.fromisoformat(str(r.get("added", "")))
            except ValueError:
                added = date.today()
            if added >= cutoff and r.get("order_id"):
                out.append(r)
        return out

    def list_orders(self) -> list[Order]:
        orders = []
        for r in self._rows():
            pdf = self.dir / str(r.get("waybill") or "")
            has_pdf = bool(r.get("waybill")) and pdf.is_file()
            ship_by = r.get("ship_by")
            orders.append(Order(
                source=self.name,
                order_id=str(r["order_id"]),
                items=[Item(str(i.get("sku", "")), str(i.get("name", "")), int(i.get("qty", 1)))
                       for i in r.get("items") or []],
                status="manual",
                ship_by=datetime.fromisoformat(ship_by) if ship_by else None,
                tracking=r.get("tracking") or None,
                has_waybill=bool(r.get("waybill")),
                waybill_ready=has_pdf,
                platform=str(r.get("platform") or "manual"),
                ref={"waybill": str(pdf), "covers": [str(c) for c in r.get("covers") or []]},
            ))
        return orders

    def get_waybill(self, order: Order) -> bytes:
        p = Path(order.ref.get("waybill", ""))
        if not p.is_file():
            raise PackError("WAYBILL-MISSING", "reading the manual waybill PDF", f"{p.name} not found")
        return p.read_bytes()
