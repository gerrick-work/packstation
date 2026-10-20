"""Straight to Shopee / TikTok, for when Odoo's connectors fall short.

Odoo's connectors sometimes miss a waybill (the label sync hasn't run, or the shipment was arranged
in Seller Centre instead of from Odoo), and file TikTok items whose variant isn't linked in Odoo
under a catch-all product ("TikTok Shop Sale"). Pack Station then asks the platform itself, with the
app keys and tokens Odoo's connectors already hold. Nothing is stored except the waybill PDFs
(data/waybills/), and Odoo stays the owner of the tokens (it refreshes them; we only read them).
"""
from __future__ import annotations

import json
import re
import time
from collections import Counter
from pathlib import Path

from ..errors import PackError
from ..models import Item, Order
from . import tiktok as tt

UNIT_RE = re.compile(r"(\d+(?:\.\d+)?)\s?(g|kg|ml|l)\b", re.I)


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_")


class WaybillCache:
    """data/waybills/<order>.pdf: waybills fetched straight from a platform."""

    def __init__(self, data_dir: str | Path | None):
        self.dir = Path(data_dir) / "waybills" if data_dir else None

    def path(self, order_id: str) -> Path | None:
        return self.dir / f"{_safe(order_id)}.pdf" if self.dir else None

    def get(self, order_id: str) -> Path | None:
        p = self.path(order_id)
        return p if p and p.is_file() and p.stat().st_size > 100 else None

    def put(self, order_id: str, pdf: bytes) -> Path | None:
        p = self.path(order_id)
        if not p:
            return None
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_bytes(pdf)
        tmp.replace(p)
        return p


class TikTokDirect:
    """TikTok Shop API with the keys of Odoo's TikTok connector (model tiktok.shop)."""

    def __init__(self, odoo, session=None):
        import requests

        self.odoo = odoo
        self.http = session or requests.Session()
        self._creds: dict | None = None
        self._loaded = 0.0
        self._orders: dict[str, tuple[float, dict]] = {}

    def _load(self) -> dict:
        if self._creds and time.time() - self._loaded < 300:
            return self._creds
        rows = self.odoo._x("tiktok.shop", "search_read", [], fields=["app_key", "app_secret", "access_token",
                                                                      "tiktok_cipher"], step="reading TikTok keys from Odoo")
        rows = [r for r in rows if r.get("app_key") and r.get("access_token")]
        if not rows:
            raise PackError("TIKTOK-API", "reading TikTok keys from Odoo", "Odoo's TikTok connector has no authorised shop")
        self._creds, self._loaded = rows[0], time.time()
        return self._creds

    def call(self, method: str, path: str, params: dict | None = None, body: dict | None = None) -> dict:
        c = self._load()
        q = {"app_key": c["app_key"], "timestamp": int(time.time())}
        if c.get("tiktok_cipher"):
            q["shop_cipher"] = c["tiktok_cipher"]
        q.update(params or {})
        body_str = json.dumps(body, separators=(",", ":")) if body is not None else ""
        q["sign"] = tt.sign(c["app_secret"], path, q, body_str)
        try:
            resp = self.http.request(method, tt.HOST + path, params=q, data=body_str or None, timeout=60,
                                     headers={"x-tts-access-token": c["access_token"],
                                              "content-type": "application/json"})
            data = resp.json()
        except Exception as e:
            raise PackError("TIKTOK-API", f"TikTok {path.rsplit('/', 1)[-1]}", str(e)[:160]) from e
        if data.get("code"):
            if data.get("code") in (105001, 105002, 36004004):   # token expired/invalid: reread from Odoo next time
                self._creds = None
            raise PackError("TIKTOK-API", f"TikTok {path.rsplit('/', 1)[-1]}",
                            f"{data.get('code')} {data.get('message')}"[:200])
        return data.get("data") or {}

    def order(self, order_ref: str, max_age: float = 60) -> dict:
        hit = self._orders.get(order_ref)
        if hit and time.time() - hit[0] < max_age:
            return hit[1]
        d = self.call("GET", "/order/202309/orders", {"ids": order_ref})
        orders = d.get("orders") or []
        if not orders:
            raise PackError("TIKTOK-API", "reading the TikTok order", f"TikTok has no order {order_ref}")
        self._orders[order_ref] = (time.time(), orders[0])
        return orders[0]

    def items(self, order_ref: str, sku_products: dict[str, dict] | None = None) -> list[Item]:
        """The order's real bottles. A variant linked in Odoo (tiktok.item) gives its Odoo product;
        otherwise the listing name and variant ("Galaxolide 50 ... " + "15g x2")."""
        from .odoo import PLACEHOLDER_RE, _short_title

        counts: Counter = Counter()
        info: dict[str, Item] = {}
        for li in self.order(order_ref).get("line_items") or []:
            sku_id = str(li.get("sku_id") or "")
            prod = (sku_products or {}).get(sku_id)
            if prod and PLACEHOLDER_RE.match(prod.get("display_name") or ""):
                prod = None   # the connector links unmatched variants to "TikTok Shop Sale" itself
            if prod:
                key, it = sku_id, Item(prod.get("default_code") or "", prod.get("display_name") or "", 1)
            else:
                title = _short_title(re.sub(r"^\[[^\]]*\]\s*", "", li.get("product_name") or "").strip())
                variant = (li.get("sku_name") or "").strip()
                name = f"{title} {variant}".strip() if variant and variant.lower() not in title.lower() else title
                key, it = sku_id or name, Item(li.get("seller_sku") or "", name, 1)
            counts[key] += int(li.get("quantity") or 1)
            info[key] = it
        return [Item(info[k].sku, info[k].name, q) for k, q in counts.items() if info[k].name]

    def label(self, order_ref: str, package_id: str = "") -> bytes | None:
        """The shipping label, or None while the shipment isn't arranged yet."""
        o = self.order(order_ref, max_age=0)
        if o.get("status") in ("AWAITING_SHIPMENT", "UNPAID", "ON_HOLD", "CANCELLED"):
            return None
        pids = [p.get("id") for p in o.get("packages") or [] if p.get("id")] or ([package_id] if package_id else [])
        if not pids:
            return None
        d = self.call("GET", f"/fulfillment/202309/packages/{pids[0]}/shipping_documents",
                      {"document_type": "SHIPPING_LABEL", "document_size": "A6"})
        url = d.get("doc_url")
        if not url:
            return None
        pdf = self.http.get(url, timeout=60).content
        return pdf if pdf[:4] == b"%PDF" else None

    def ship(self, order_ref: str, package_id: str = "", method: str = "dropoff") -> None:
        o = self.order(order_ref, max_age=0)
        if o.get("status") != "AWAITING_SHIPMENT":
            return
        pids = [p.get("id") for p in o.get("packages") or [] if p.get("id")] or ([package_id] if package_id else [])
        for pid in pids:
            self.call("POST", f"/fulfillment/202309/packages/{pid}/ship",
                      body={"handover_method": "PICKUP" if method == "pickup" else "DROP_OFF"})


class ShopeeDirect:
    """Shopee Open Platform with the Malaysian main shop's keys from Odoo's Shopee connector."""

    def __init__(self, odoo, data_dir=None, session=None):
        from .shopee_sip import ShopeeSipSource

        self.api = ShopeeSipSource(odoo, regions=[], data_dir=data_dir, session=session)

    def _main(self) -> None:
        self.api._load_from_odoo()
        self.api.shop_id = self.api.main_shop_id

    def _order(self, sn: str, tracking: str | None) -> Order:
        return Order(source="shopee", order_id=sn, items=[], status="", tracking=tracking, platform="shopee")

    def tracking(self, sn: str) -> str | None:
        self._main()
        return self.api._tracking(sn)

    def label(self, sn: str, tracking: str | None = None) -> bytes | None:
        """The waybill, or None while Shopee has no tracking number (shipment not arranged)."""
        from . import shopee as base

        self._main()
        tracking = tracking or self.api._tracking(sn)
        if not tracking:
            return None
        o = self._order(sn, tracking)
        doc = self.api._doc_type(o)
        old, base.DOC_TYPE = base.DOC_TYPE, doc
        try:
            return base.ShopeeSource.get_waybill(self.api, o)
        except RuntimeError as e:
            raise PackError("WAYBILL-MISSING", "fetching the waybill from Shopee", str(e)[:200]) from e
        finally:
            base.DOC_TYPE = old

    def ship(self, sn: str, method: str = "dropoff") -> str | None:
        """Arrange the shipment (drop-off unless Settings says pickup); returns the tracking number."""
        from . import shopee as base

        self._main()
        if self.api._tracking(sn):
            return self.api._tracking(sn)
        self.api.arrange_mode = "pickup" if method == "pickup" else "dropoff"
        try:
            return base.ShopeeSource.arrange(self.api, self._order(sn, None)).tracking
        except RuntimeError as e:
            raise PackError("WAYBILL-MISSING", "arranging the Shopee shipment", str(e)[:200]) from e
