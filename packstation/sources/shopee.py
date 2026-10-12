"""Shopee Open Platform v2: to-pack orders and SPX / courier thermal waybills.

Flow per order (Shopee's own rules):
  READY_TO_SHIP  -> shipment not arranged yet, no tracking number, no waybill.
                    `arrange()` calls ship_order (drop-off or pickup) when enabled in config.
  PROCESSED      -> arranged, has a tracking number, waybill can be created and downloaded.
"""
from __future__ import annotations

import hashlib
import hmac
import time
from datetime import datetime

import requests

from ..models import Item, Order
from . import Source
from .tokens import TokenFile

HOST = "https://partner.shopeemobile.com"
DOC_TYPE = "THERMAL_AIR_WAYBILL"


def sign(partner_key: str, base: str) -> str:
    return hmac.new(partner_key.encode(), base.encode(), hashlib.sha256).hexdigest()


class ShopeeSource(Source):
    name = "shopee"

    def __init__(self, cfg: dict, data_dir: str, session: requests.Session | None = None):
        self.partner_id = int(cfg["partner_id"])
        self.partner_key = cfg["partner_key"]
        self.shop_id = int(cfg["shop_id"])
        self.lookback_days = int(cfg.get("lookback_days", 14))
        self.arrange_mode = cfg.get("arrange", "off")   # "off", "dropoff" or "pickup"
        self.http = session or requests.Session()
        self.tokens = TokenFile(data_dir, "shopee")
        saved = self.tokens.load()
        self.access_token = saved.get("access_token", cfg.get("access_token", ""))
        self.refresh_token = saved.get("refresh_token", cfg.get("refresh_token", ""))
        self.expires_at = saved.get("expires_at", 0)

    # ---- signing / transport -------------------------------------------------

    def _common(self, path: str) -> dict:
        ts = int(time.time())
        base = f"{self.partner_id}{path}{ts}{self.access_token}{self.shop_id}"
        return {
            "partner_id": self.partner_id,
            "timestamp": ts,
            "access_token": self.access_token,
            "shop_id": self.shop_id,
            "sign": sign(self.partner_key, base),
        }

    def _refresh_if_needed(self) -> None:
        if self.access_token and time.time() < self.expires_at - 300:
            return
        if not self.refresh_token:
            return  # use the configured access token as is
        path = "/api/v2/auth/access_token/get"
        ts = int(time.time())
        params = {
            "partner_id": self.partner_id,
            "timestamp": ts,
            "sign": sign(self.partner_key, f"{self.partner_id}{path}{ts}"),
        }
        body = {"refresh_token": self.refresh_token, "partner_id": self.partner_id, "shop_id": self.shop_id}
        data = self.http.post(HOST + path, params=params, json=body, timeout=30).json()
        if data.get("error"):
            raise RuntimeError(f"Shopee token refresh failed: {data.get('error')} {data.get('message')}")
        self.access_token = data["access_token"]
        self.refresh_token = data["refresh_token"]
        self.expires_at = int(time.time()) + int(data.get("expire_in", 14400))
        self.tokens.save({
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
        })

    def _call(self, method: str, path: str, params: dict | None = None, body: dict | None = None,
              raw: bool = False):
        self._refresh_if_needed()
        q = self._common(path)
        q.update(params or {})
        resp = self.http.request(method, HOST + path, params=q, json=body, timeout=60)
        if raw and resp.content[:4] == b"%PDF":
            return resp.content
        data = resp.json()
        if data.get("error"):
            raise RuntimeError(f"Shopee {path}: {data.get('error')} {data.get('message')}")
        return data.get("response", {})

    # ---- orders ----------------------------------------------------------------

    def _order_sns(self, status: str) -> list[str]:
        now = int(time.time())
        # Shopee allows at most a 15-day window per call.
        start = now - min(self.lookback_days, 15) * 86400
        sns, cursor = [], ""
        while True:
            r = self._call("GET", "/api/v2/order/get_order_list", {
                "time_range_field": "create_time", "time_from": start, "time_to": now,
                "page_size": 100, "cursor": cursor, "order_status": status,
            })
            sns += [o["order_sn"] for o in r.get("order_list", [])]
            if not r.get("more"):
                return sns
            cursor = r.get("next_cursor", "")

    def _details(self, sns: list[str]) -> list[dict]:
        out = []
        for i in range(0, len(sns), 50):
            r = self._call("GET", "/api/v2/order/get_order_detail", {
                "order_sn_list": ",".join(sns[i:i + 50]),
                "response_optional_fields": "item_list,package_list,shipping_carrier",
            })
            out += r.get("order_list", [])
        return out

    def _tracking(self, sn: str) -> str | None:
        try:
            return self._call("GET", "/api/v2/logistics/get_tracking_number", {"order_sn": sn}).get(
                "tracking_number") or None
        except RuntimeError:
            return None

    def _to_order(self, d: dict) -> Order:
        items = [
            Item(
                sku=i.get("model_sku") or i.get("item_sku") or "",
                name=" ".join(x for x in (i.get("item_name"), i.get("model_name")) if x),
                qty=int(i.get("model_quantity_purchased", 1)),
            )
            for i in d.get("item_list", [])
        ]
        status = d.get("order_status", "")
        packages = d.get("package_list") or []
        ship_by = d.get("ship_by_date")
        order = Order(
            source=self.name,
            order_id=d["order_sn"],
            items=items,
            status=status,
            ship_by=datetime.fromtimestamp(ship_by) if ship_by else None,
            needs_arrange=status == "READY_TO_SHIP",
            ref={"package_number": packages[0].get("package_number") if len(packages) == 1 else None,
                 "carrier": d.get("shipping_carrier", "")},
        )
        if not order.needs_arrange:
            order.tracking = self._tracking(order.order_id)
        return order

    def list_orders(self) -> list[Order]:
        sns = self._order_sns("READY_TO_SHIP") + self._order_sns("PROCESSED")
        return [self._to_order(d) for d in self._details(sns)]

    # ---- shipment + waybill ----------------------------------------------------

    def arrange(self, order: Order) -> Order:
        if self.arrange_mode not in ("dropoff", "pickup"):
            raise RuntimeError("Arrange shipment in Shopee Seller Centre (auto-arrange is off in config)")
        params = self._call("GET", "/api/v2/logistics/get_shipping_parameter", {"order_sn": order.order_id})
        body: dict = {"order_sn": order.order_id}
        if self.arrange_mode == "pickup":
            addr = params["pickup"]["address_list"][0]
            body["pickup"] = {"address_id": addr["address_id"],
                              "pickup_time_id": addr["time_slot_list"][0]["pickup_time_id"]}
        else:
            body["dropoff"] = {}
        self._call("POST", "/api/v2/logistics/ship_order", body=body)
        for _ in range(10):  # tracking number is issued asynchronously
            time.sleep(2)
            tn = self._tracking(order.order_id)
            if tn:
                order.tracking, order.needs_arrange, order.status = tn, False, "PROCESSED"
                return order
        raise RuntimeError("Shipment arranged but Shopee has not issued a tracking number yet; try again shortly")

    def get_waybill(self, order: Order) -> bytes:
        entry = {"order_sn": order.order_id}
        if order.ref.get("package_number"):
            entry["package_number"] = order.ref["package_number"]
        create = dict(entry, shipping_document_type=DOC_TYPE)
        if order.tracking:
            create["tracking_number"] = order.tracking
        self._call("POST", "/api/v2/logistics/create_shipping_document", body={"order_list": [create]})
        for _ in range(15):
            r = self._call("POST", "/api/v2/logistics/get_shipping_document_result",
                           body={"order_list": [dict(entry, shipping_document_type=DOC_TYPE)]})
            status = (r.get("result_list") or [{}])[0].get("status")
            if status == "READY":
                break
            if status == "FAILED":
                raise RuntimeError("Shopee failed to generate the waybill")
            time.sleep(2)
        else:
            raise RuntimeError("Shopee waybill still processing; try again shortly")
        pdf = self._call("POST", "/api/v2/logistics/download_shipping_document",
                         body={"shipping_document_type": DOC_TYPE, "order_list": [entry]}, raw=True)
        if not isinstance(pdf, bytes):
            raise RuntimeError("Shopee did not return a PDF waybill")
        return pdf
