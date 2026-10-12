"""Plain data shapes shared by every order source, the label renderer and the printers."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Item:
    sku: str
    name: str
    qty: int = 1


@dataclass
class Order:
    source: str                      # "shopee", "tiktok", "odoo" or "demo"
    order_id: str                    # the platform's own order number
    items: list[Item]
    status: str = ""
    ship_by: datetime | None = None
    tracking: str | None = None
    needs_arrange: bool = False      # platform still wants pickup/drop-off chosen before a waybill exists
    has_waybill: bool = True         # a platform order that must ship with a waybill (False: Odoo-only)
    waybill_ready: bool = True       # the waybill can be fetched right now
    platform: str = ""               # "shopee" / "tiktok" when the order came from a marketplace
    odoo_picking_id: int | None = None
    ref: dict = field(default_factory=dict)   # source-specific ids (package ids, attachment ids etc.)

    @property
    def key(self) -> str:
        return f"{self.source}:{self.order_id}"

    @property
    def match_code(self) -> str:
        """Short code printed on every product label so it can be matched to its waybill: the last
        4 characters of the tracking number. Empty when there is no tracking number yet (never a
        made-up code that matches no waybill)."""
        return (self.tracking or "")[-4:].upper()

    @property
    def waybill_missing(self) -> bool:
        """A marketplace order whose waybill isn't available and can't be arranged now."""
        return self.has_waybill and not self.waybill_ready and not self.needs_arrange

    @property
    def unit_count(self) -> int:
        return sum(i.qty for i in self.items)
