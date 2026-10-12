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
    has_waybill: bool = True         # False for orders (e.g. Odoo-only) that have no platform waybill
    odoo_picking_id: int | None = None
    ref: dict = field(default_factory=dict)   # source-specific ids (package ids etc.)

    @property
    def key(self) -> str:
        return f"{self.source}:{self.order_id}"

    @property
    def match_code(self) -> str:
        """Short code printed on every product label so it can be matched to its waybill.

        The last 4 characters of the tracking number (printed big on the waybill),
        falling back to the last 4 of the order id.
        """
        base = self.tracking or self.order_id
        return base[-4:].upper()

    @property
    def unit_count(self) -> int:
        return sum(i.qty for i in self.items)
