"""Collects to-pack orders from every source and prints them: waybill on one printer, then that
order's product labels on the other, order by order so the two stacks come out in the same order."""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime

from .labels import Catalog, labels_for_order
from .models import Order
from .printers import build_printer
from .sources import Source, build_sources
from .sources.odoo import OdooSource
from .store import Store


@dataclass
class Result:
    key: str
    ok: bool
    message: str
    labels: int = 0
    guessed_skus: list[str] = field(default_factory=list)


class Engine:
    def __init__(self, cfg: dict, sources: list[Source] | None = None):
        self.cfg = cfg
        data_dir = cfg["app"]["data_dir"]
        self.sources = sources if sources is not None else build_sources(cfg)
        self.store = Store(data_dir)
        self.catalog = Catalog(cfg.get("labels", {}).get("catalog"),
                               cfg.get("labels", {}).get("brand", "moleculelabs"))
        printers = cfg.get("printers", {})
        self.waybill_printer = build_printer("waybill", printers.get("waybill", {}), data_dir)
        self.label_printer = build_printer("label", {"width_mm": 50, "height_mm": 25,
                                                     **printers.get("label", {})}, data_dir)
        self.orders: list[Order] = []
        self.source_errors: dict[str, str] = {}
        self.last_scan: datetime | None = None
        self.lock = threading.Lock()

    # ---- scanning ---------------------------------------------------------------

    def _by_name(self, name: str) -> Source | None:
        return next((s for s in self.sources if s.name == name), None)

    def scan(self) -> list[Order]:
        found: list[Order] = []
        self.source_errors = {}
        for s in self.sources:
            try:
                found += s.list_orders()
            except Exception as e:  # one platform being down must not block the others
                self.source_errors[s.name] = str(e)
        self.orders = self._merge(found)
        self.last_scan = datetime.now()
        return self.orders

    @staticmethod
    def _merge(orders: list[Order]) -> list[Order]:
        """Odoo deliveries for Shopee/TikTok orders are the same parcel: keep the platform order
        (it has the waybill) and remember the Odoo delivery so it can be noted as printed."""
        platform = {o.order_id: o for o in orders if o.source != "odoo"}
        merged = [o for o in orders if o.source != "odoo"]
        for o in orders:
            if o.source != "odoo":
                continue
            twin = platform.get(o.ref.get("platform_ref", ""))
            if twin:
                twin.odoo_picking_id = o.odoo_picking_id
            else:
                merged.append(o)
        far = datetime.max
        return sorted(merged, key=lambda o: (o.ship_by or far, o.source, o.order_id))

    def pending(self) -> list[Order]:
        printed = self.store.printed()
        todo = [o for o in self.orders if o.key not in printed]
        if self.cfg.get("app", {}).get("only_due_today"):
            end = datetime.now().replace(hour=23, minute=59, second=59)
            todo = [o for o in todo if not o.ship_by or o.ship_by <= end]
        return todo

    # ---- printing ---------------------------------------------------------------

    def print_order(self, order: Order) -> Result:
        src = self._by_name(order.source)
        try:
            if order.has_waybill:
                if order.needs_arrange:
                    order = src.arrange(order)
                pdf = src.get_waybill(order)
                self.waybill_printer.print_pdf(pdf, f"{order.source}-{order.order_id}-waybill")
            labels = labels_for_order(order, self.catalog, self.cfg.get("printers", {}).get("label", {}))
            for n, (img, item, _) in enumerate(labels, 1):
                self.label_printer.print_image(img, f"{order.source}-{order.order_id}-{n:02d}")
        except Exception as e:
            return Result(order.key, False, str(e))

        self.store.mark(order.key, order.tracking, len(labels))
        odoo = self._by_name("odoo")
        if isinstance(odoo, OdooSource) and order.odoo_picking_id:
            try:
                odoo.note_printed(order.odoo_picking_id,
                                  f"Pack Station printed waybill {order.tracking or '-'} and {len(labels)} labels"
                                  f" (match code #{order.match_code}).")
            except Exception:
                pass  # printing succeeded; a missing Odoo note is not worth failing the order
        guessed = sorted({item.sku or item.name for _, item, known in labels if not known})
        return Result(order.key, True, f"{'waybill + ' if order.has_waybill else ''}{len(labels)} labels",
                      len(labels), guessed)

    def print_all(self) -> list[Result]:
        with self.lock:
            return [self.print_order(o) for o in self.pending()]

    def reprint(self, key: str) -> Result:
        with self.lock:
            order = next((o for o in self.orders if o.key == key), None)
            if not order:
                return Result(key, False, "Order not in the current list; scan again")
            return self.print_order(order)
