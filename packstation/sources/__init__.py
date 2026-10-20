"""Order sources. Each one lists to-pack orders and, where the platform issues one, fetches the waybill PDF."""
from __future__ import annotations

from ..models import Order


class Source:
    name = "base"

    def list_orders(self) -> list[Order]:
        raise NotImplementedError

    def get_waybill(self, order: Order) -> bytes:
        """Return the waybill as PDF bytes."""
        raise NotImplementedError

    def arrange(self, order: Order) -> Order:
        """Choose pickup/drop-off so the platform issues a tracking number. Returns the refreshed order."""
        raise NotImplementedError(f"{self.name} cannot arrange shipment")


def build_sources(cfg: dict) -> list[Source]:
    from .demo import DemoSource
    from .odoo import OdooSource
    from .shopee import ShopeeSource
    from .tiktok import TikTokSource

    data_dir = cfg["app"]["data_dir"]
    sources: list[Source] = []
    if cfg.get("demo", {}).get("enabled"):
        sources.append(DemoSource())
    if cfg.get("shopee", {}).get("enabled"):
        sources.append(ShopeeSource(cfg["shopee"], data_dir))
    if cfg.get("tiktok", {}).get("enabled"):
        sources.append(TikTokSource(cfg["tiktok"], data_dir))
    from .manual import ManualSource
    sources.append(ManualSource(data_dir))   # data/manual_orders.json; empty unless orders were added by hand
    if cfg.get("odoo", {}).get("enabled"):
        odoo = OdooSource(cfg["odoo"])
        odoo.attach(data_dir)
        sources.append(odoo)
        # Shopee cross-border shops (e.g. Singapore) that Odoo's Shopee connector doesn't fetch
        regions = cfg["odoo"].get("shopee_sip_regions") or []
        if regions:
            from .shopee_sip import ShopeeSipSource
            sources.append(ShopeeSipSource(odoo, regions, data_dir=data_dir))
    return sources
