"""TikTok Shop Partner API (version 202309): to-pack orders and shipping labels.

AWAITING_SHIPMENT   -> handover (pickup / drop-off) not chosen yet; `arrange()` ships the package when enabled.
AWAITING_COLLECTION -> arranged, shipping label available.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections import Counter
from datetime import datetime

import requests

from ..models import Item, Order
from . import Source
from .tokens import TokenFile

HOST = "https://open-api.tiktokglobalshop.com"
AUTH_HOST = "https://auth.tiktok-shops.com"


def sign(app_secret: str, path: str, params: dict, body: str = "") -> str:
    """TikTok Shop request signature: secret + path + sorted key/values + body + secret, HMAC-SHA256."""
    keys = sorted(k for k in params if k not in ("sign", "access_token"))
    payload = path + "".join(f"{k}{params[k]}" for k in keys) + body
    payload = app_secret + payload + app_secret
    return hmac.new(app_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


class TikTokSource(Source):
    name = "tiktok"

    def __init__(self, cfg: dict, data_dir: str, session: requests.Session | None = None):
        self.app_key = cfg["app_key"]
        self.app_secret = cfg["app_secret"]
        self.shop_cipher = cfg["shop_cipher"]
        self.arrange_mode = cfg.get("arrange", "off")  # "off", "dropoff" or "pickup"
        self.http = session or requests.Session()
        self.tokens = TokenFile(data_dir, "tiktok")
        saved = self.tokens.load()
        self.access_token = saved.get("access_token", cfg.get("access_token", ""))
        self.refresh_token = saved.get("refresh_token", cfg.get("refresh_token", ""))
        self.expires_at = saved.get("expires_at", 0)

    def _refresh_if_needed(self) -> None:
        if self.access_token and time.time() < self.expires_at - 300:
            return
        if not self.refresh_token:
            return
        data = self.http.get(AUTH_HOST + "/api/v2/token/refresh", params={
            "app_key": self.app_key, "app_secret": self.app_secret,
            "refresh_token": self.refresh_token, "grant_type": "refresh_token",
        }, timeout=30).json()
        if data.get("code"):
            raise RuntimeError(f"TikTok token refresh failed: {data.get('message')}")
        d = data["data"]
        self.access_token, self.refresh_token = d["access_token"], d["refresh_token"]
        self.expires_at = int(d["access_token_expire_in"])
        self.tokens.save({"access_token": self.access_token, "refresh_token": self.refresh_token,
                          "expires_at": self.expires_at})

    def _call(self, method: str, path: str, params: dict | None = None, body: dict | None = None) -> dict:
        self._refresh_if_needed()
        q = {"app_key": self.app_key, "timestamp": int(time.time()), "shop_cipher": self.shop_cipher}
        q.update(params or {})
        body_str = json.dumps(body, separators=(",", ":")) if body is not None else ""
        q["sign"] = sign(self.app_secret, path, q, body_str)
        resp = self.http.request(
            method, HOST + path, params=q, data=body_str or None, timeout=60,
            headers={"x-tts-access-token": self.access_token, "content-type": "application/json"},
        )
        data = resp.json()
        if data.get("code"):
            raise RuntimeError(f"TikTok {path}: {data.get('code')} {data.get('message')}")
        return data.get("data", {})

    # ---- orders ----------------------------------------------------------------

    def _search(self, status: str) -> list[dict]:
        orders, token = [], ""
        while True:
            params = {"page_size": 50}
            if token:
                params["page_token"] = token
            d = self._call("POST", "/order/202309/orders/search", params, {"order_status": status})
            orders += d.get("orders", [])
            token = d.get("next_page_token")
            if not token:
                return orders

    def _to_order(self, d: dict) -> Order:
        # TikTok returns one line item per unit; group them back into quantities.
        counts: Counter = Counter()
        names: dict[str, str] = {}
        for li in d.get("line_items", []):
            sku = li.get("seller_sku") or li.get("sku_id", "")
            counts[sku] += 1
            names[sku] = " ".join(x for x in (li.get("product_name"), li.get("sku_name")) if x)
        packages = [p["id"] for p in d.get("packages", []) if p.get("id")]
        status = d.get("status", "")
        due = d.get("rts_sla_time") or d.get("shipping_due_time")
        pay = d.get("payment") or {}
        return Order(
            source=self.name,
            order_id=str(d["id"]),
            items=[Item(sku=s, name=names[s], qty=q) for s, q in counts.items()],
            status=status,
            ship_by=datetime.fromtimestamp(int(due)) if due else None,
            tracking=d.get("tracking_number") or None,
            needs_arrange=status == "AWAITING_SHIPMENT",
            waybill_ready=status != "AWAITING_SHIPMENT",
            platform="tiktok",
            ref={"package_ids": packages},
            total=float(pay["total_amount"]) if pay.get("total_amount") not in (None, "") else None,
            currency=pay.get("currency", ""),
        )

    def list_orders(self) -> list[Order]:
        raw = self._search("AWAITING_SHIPMENT") + self._search("AWAITING_COLLECTION")
        return [self._to_order(d) for d in raw]

    # ---- shipment + waybill ----------------------------------------------------

    def arrange(self, order: Order) -> Order:
        if self.arrange_mode not in ("dropoff", "pickup"):
            raise RuntimeError("Arrange shipment in TikTok Seller Center (auto-arrange is off in config)")
        method = "PICKUP" if self.arrange_mode == "pickup" else "DROP_OFF"
        for pid in order.ref.get("package_ids", []):
            self._call("POST", f"/fulfillment/202309/packages/{pid}/ship", body={"handover_method": method})
        order.needs_arrange, order.status, order.waybill_ready = False, "AWAITING_COLLECTION", True
        return order

    def get_waybill(self, order: Order) -> bytes:
        pids = order.ref.get("package_ids") or []
        if not pids:
            raise RuntimeError("TikTok order has no package yet")
        if len(pids) > 1:
            raise RuntimeError("TikTok order is split into several packages; print it from Seller Center")
        d = self._call("GET", f"/fulfillment/202309/packages/{pids[0]}/shipping_documents",
                       {"document_type": "SHIPPING_LABEL", "document_size": "A6"})
        url = d.get("doc_url")
        if not url:
            raise RuntimeError("TikTok did not return a shipping label")
        pdf = self.http.get(url, timeout=60).content
        if not pdf.startswith(b"%PDF"):
            raise RuntimeError("TikTok shipping label download was not a PDF")
        return pdf
