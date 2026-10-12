"""Odoo (Online / standard) through its external API: ready-to-ship deliveries.

Orders that came from Shopee or TikTok are matched to the platform order through a field on
the sale order (default `client_order_ref`, set `platform_ref_field` in config to whatever your
connector fills). The platform supplies the waybill; Odoo-only orders print product labels only.
"""
from __future__ import annotations

import xmlrpc.client
from datetime import datetime

from ..models import Item, Order
from . import Source


class OdooSource(Source):
    name = "odoo"

    def __init__(self, cfg: dict):
        self.url = cfg["url"].rstrip("/")
        self.db = cfg["db"]
        self.user = cfg["username"]
        self.key = cfg["api_key"]
        self.ref_field = cfg.get("platform_ref_field", "client_order_ref")
        self.post_note = bool(cfg.get("note_when_printed", True))
        self._uid = None
        self.models = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/object", allow_none=True)

    @property
    def uid(self) -> int:
        if self._uid is None:
            common = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/common")
            self._uid = common.authenticate(self.db, self.user, self.key, {})
            if not self._uid:
                raise RuntimeError("Odoo login failed: check url, db, username and api_key")
        return self._uid

    def _x(self, model: str, method: str, *args, **kw):
        return self.models.execute_kw(self.db, self.uid, self.key, model, method, list(args), kw)

    def list_orders(self) -> list[Order]:
        pickings = self._x(
            "stock.picking", "search_read",
            [("picking_type_code", "=", "outgoing"), ("state", "=", "assigned")],
            fields=["name", "origin", "scheduled_date", "date_deadline", "sale_id", "move_ids"],
        )
        move_ids = [m for p in pickings for m in p["move_ids"]]
        moves = {m["id"]: m for m in self._x("stock.move", "read", move_ids,
                                             fields=["product_id", "product_uom_qty"])} if move_ids else {}
        prod_ids = list({m["product_id"][0] for m in moves.values() if m["product_id"]})
        prods = {p["id"]: p for p in self._x("product.product", "read", prod_ids,
                                             fields=["default_code", "display_name"])} if prod_ids else {}
        sale_ids = [p["sale_id"][0] for p in pickings if p.get("sale_id")]
        refs = {s["id"]: s.get(self.ref_field) or "" for s in self._x(
            "sale.order", "read", sale_ids, fields=[self.ref_field])} if sale_ids else {}

        out = []
        for p in pickings:
            items = []
            for mid in p["move_ids"]:
                m = moves.get(mid)
                if not m or not m["product_id"]:
                    continue
                prod = prods.get(m["product_id"][0], {})
                items.append(Item(sku=prod.get("default_code") or "", name=prod.get("display_name", ""),
                                  qty=int(round(m["product_uom_qty"]))))
            when = p.get("date_deadline") or p.get("scheduled_date")
            ref = refs.get(p["sale_id"][0], "") if p.get("sale_id") else ""
            out.append(Order(
                source=self.name,
                order_id=p["name"],
                items=items,
                status="ready",
                ship_by=datetime.fromisoformat(when) if when else None,
                has_waybill=False,
                odoo_picking_id=p["id"],
                ref={"platform_ref": ref.strip(), "origin": p.get("origin") or ""},
            ))
        return out

    def note_printed(self, picking_id: int, text: str) -> None:
        if self.post_note:
            self._x("stock.picking", "message_post", [picking_id], body=text)
