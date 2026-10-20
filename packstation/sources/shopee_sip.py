"""Shopee cross-border (SIP) shops, e.g. the Singapore shop moleculelabs.co.sg under the Malaysian
main shop. Odoo's Shopee connector only fetches the main shop's orders, so this reads the SIP shops'
orders straight from Shopee and prints their waybills. A stop-gap until Odoo's connector handles them.

The app key (partner id/key) is read from Odoo's Shopee connector on every scan; Pack Station
never stores it. "Direct" shops such as Singapore need their own authorisation: Settings >
Connect Singapore shop sends you to Shopee once, and the shop's own access/refresh token is kept
in data/shopee_direct_tokens.json and refreshed here (Odoo's main-shop token is never touched).
Without that, it falls back to the main shop's token, which Shopee rejects for Direct shops.
These orders exist only on Shopee, so Odoo gets no delivery, stock move or note for them.
"""
from __future__ import annotations

import re
import time

from ..errors import PackError
from ..models import Order
from .shopee import HOST, ShopeeSource, sign
from .tokens import TokenFile

_TITLE = re.compile(r"^(.+?)\s+[\u2013\u2014-]\s+.*?\s*(\d+(?:\.\d+)?\s?(?:g|kg|ml))?$", re.I)


def short_name(name: str) -> str:
    """Shopee listing titles carry SEO words after the name ("Orange Blossom (Neroli) – Premium Aroma
    Chemical | Essential Oil | ... 15g"); a bottle label needs "Orange Blossom (Neroli) 15g"."""
    m = _TITLE.match(name or "")
    if not m:
        return (name or "").split(" | ")[0].strip()
    return f"{m.group(1).strip()} {m.group(2).replace(' ', '')}" if m.group(2) else m.group(1).strip()


AUTH_PATH = "/api/v2/shop/auth_partner"
TOKEN_GET = "/api/v2/auth/token/get"
TOKEN_REFRESH = "/api/v2/auth/access_token/get"


class ShopeeSipSource(ShopeeSource):
    """One instance covers every SIP shop in `regions` (default Singapore only)."""

    def __init__(self, odoo, regions: list[str] | None = None, lookback_days: int = 14, session=None,
                 data_dir: str | None = None):
        import requests

        self.odoo = odoo
        self.regions = [r.upper() for r in (regions or ["SG"])]
        self.name = "shopee-" + "-".join(r.lower() for r in self.regions)
        self.lookback_days = int(lookback_days)
        self.arrange_mode = "off"   # arrange in Seller Centre; Pack Station prints once Shopee has a waybill
        self.http = session or requests.Session()
        self.partner_id = 0
        self.partner_key = ""
        self.access_token = ""
        self.refresh_token = ""     # never used: Odoo owns token refreshing
        self.expires_at = 0
        self.main_shop_id = 0
        self.shop_id = 0
        self._affi: dict[str, int] = {}
        self.own = TokenFile(data_dir, "shopee_direct") if data_dir else None
        self._region = ""           # region whose own token is in use, "" = borrowed main-shop token

    # ---- credentials, borrowed from Odoo's Shopee connector on every scan -------------------

    def _load_from_odoo(self) -> None:
        step = "reading Shopee keys from Odoo"
        acc = self.odoo._x("shopee.account", "search_read", [], fields=["partner_identifier", "partner_key"],
                           limit=1, step=step)
        shops = self.odoo._x("shopee.shop", "search_read", [("active", "=", True)],
                             fields=["shop_identifier", "access_token"], step=step)
        if not acc or not acc[0].get("partner_key") or not shops or not shops[0].get("access_token"):
            raise PackError("SHOPEE-SG", step, "Odoo's Shopee connector has no authorised shop")
        self.partner_id = int(acc[0]["partner_identifier"])
        self.partner_key = acc[0]["partner_key"]
        self.main_shop_id = int(shops[0]["shop_identifier"])
        self.access_token = shops[0]["access_token"]
        self.expires_at = time.time() + 3600   # stops ShopeeSource from trying to refresh
        self._region = ""

    # ---- the Direct shop's own authorisation -------------------------------------------------

    def own_tokens(self) -> dict:
        try:
            return self.own.load() if self.own else {}
        except Exception:
            return {}

    def _keys_only(self) -> None:
        step = "reading Shopee keys from Odoo"
        acc = self.odoo._x("shopee.account", "search_read", [], fields=["partner_identifier", "partner_key"],
                           limit=1, step=step)
        if not acc or not acc[0].get("partner_key"):
            raise PackError("SHOPEE-SG", step, "Odoo's Shopee connector has no app key")
        self.partner_id = int(acc[0]["partner_identifier"])
        self.partner_key = acc[0]["partner_key"]

    def auth_url(self, redirect: str) -> str:
        """Shopee's page where the seller logs in and authorises a shop for this app."""
        from urllib.parse import urlencode

        self._keys_only()
        ts = int(time.time())
        q = {"partner_id": self.partner_id, "timestamp": ts,
             "sign": sign(self.partner_key, f"{self.partner_id}{AUTH_PATH}{ts}"), "redirect": redirect}
        return HOST + AUTH_PATH + "?" + urlencode(q)

    def _signed(self, path: str) -> dict:
        ts = int(time.time())
        return {"partner_id": self.partner_id, "timestamp": ts,
                "sign": sign(self.partner_key, f"{self.partner_id}{path}{ts}")}

    def connect(self, code: str, shop_id: int | None = None, main_account_id: int | None = None) -> list[dict]:
        """Swap the code Shopee sent back for the shop's own tokens and save them by region.
        Returns [{region, shop_id, shop_name}] for each shop saved."""
        step = "connecting the Shopee shop"
        self._keys_only()
        main = self.main_shop_id_from_odoo()
        if shop_id and int(shop_id) == main:
            # Don't even swap the code: that would issue the main shop a new token next to Odoo's
            raise PackError("SHOPEE-SG", step, "that's the Malaysian main shop (Odoo looks after it); "
                                               "connect again and choose only the Singapore shop")
        body = {"code": code, "partner_id": self.partner_id}
        if shop_id:
            body["shop_id"] = int(shop_id)
        elif main_account_id:
            body["main_account_id"] = int(main_account_id)
        else:
            raise PackError("SHOPEE-SG", step, "Shopee sent no shop id back")
        data = self.http.post(HOST + TOKEN_GET, params=self._signed(TOKEN_GET), json=body, timeout=30).json()
        if data.get("error"):
            raise PackError("SHOPEE-SG", step, f"{data.get('error')} {data.get('message')}")
        shop_ids = [int(shop_id)] if shop_id else [int(x) for x in data.get("shop_id_list") or []]
        saved = self.own_tokens()
        out = []
        for sid in shop_ids:
            if sid == main:
                continue   # never keep a second token for the main shop: Odoo owns that one
            self.shop_id, self.access_token = sid, data["access_token"]
            info = self._call_plain("GET", "/api/v2/shop/get_shop_info")
            region = str(info.get("region") or "").upper() or "SG"
            saved[region] = {"shop_id": sid, "shop_name": info.get("shop_name") or "",
                             "access_token": data["access_token"], "refresh_token": data["refresh_token"],
                             "expires_at": int(time.time()) + int(data.get("expire_in", 14400)),
                             "connected_at": int(time.time())}
            out.append({"region": region, "shop_id": sid, "shop_name": info.get("shop_name") or ""})
        if not out:
            raise PackError("SHOPEE-SG", step, "only the Malaysian main shop was authorised; choose the Singapore shop")
        self.own.save(saved)
        self._affi = {}
        return out

    def main_shop_id_from_odoo(self) -> int:
        try:
            shops = self.odoo._x("shopee.shop", "search_read", [("active", "=", True)], fields=["shop_identifier"])
            return int(shops[0]["shop_identifier"]) if shops else 0
        except Exception:
            return 0

    def _use_region(self, region: str) -> bool:
        """Switch to the region's own token if it has one (refreshing it when due)."""
        t = self.own_tokens().get(region)
        if not t:
            self._region = ""
            return False
        self._region = region
        self.shop_id = int(t["shop_id"])
        self.access_token, self.refresh_token, self.expires_at = t["access_token"], t["refresh_token"], t["expires_at"]
        self._refresh_if_needed()
        return True

    def _refresh_if_needed(self) -> None:
        if not self._region:
            return  # borrowed main-shop token: Odoo refreshes it; reading it again each scan picks up the new one
        if self.access_token and time.time() < self.expires_at - 300:
            return
        body = {"refresh_token": self.refresh_token, "partner_id": self.partner_id, "shop_id": self.shop_id}
        data = self.http.post(HOST + TOKEN_REFRESH, params=self._signed(TOKEN_REFRESH), json=body, timeout=30).json()
        if data.get("error"):
            raise PackError("SHOPEE-SG", "refreshing the Singapore shop's Shopee login",
                            f"{data.get('error')} {data.get('message')}: reconnect it in Settings")
        self.access_token, self.refresh_token = data["access_token"], data["refresh_token"]
        self.expires_at = int(time.time()) + int(data.get("expire_in", 14400))
        saved = self.own_tokens()
        saved.setdefault(self._region, {}).update(
            shop_id=self.shop_id, access_token=self.access_token, refresh_token=self.refresh_token,
            expires_at=self.expires_at, refreshed_at=int(time.time()))
        self.own.save(saved)

    def _affi_shops(self) -> dict[str, int]:
        if not self._affi:
            self.shop_id = self.main_shop_id
            info = self._call_plain("GET", "/api/v2/shop/get_shop_info")
            found: dict[str, int] = {}
            # Older SIP shops: sip_affi_shops [{affi_shop_id, region}]. SIP "Direct" shops:
            # linked_direct_shop_list [{direct_shop_id, direct_shop_region}]. Accept either.
            for key in ("sip_affi_shops", "linked_direct_shop_list", "direct_shop_list"):
                for sh in info.get(key) or []:
                    sid = sh.get("affi_shop_id") or sh.get("direct_shop_id") or sh.get("shop_id")
                    reg = sh.get("region") or sh.get("direct_shop_region") or sh.get("shop_region") or ""
                    if sid:
                        found[str(reg).upper()] = int(sid)
            self._affi = found
            self._shop_info_keys = sorted(k for k in info if k not in ("request_id", "error", "message"))
        missing = [r for r in self.regions if r not in self._affi]
        if missing:
            raise PackError("SHOPEE-SG", "finding the Shopee cross-border shop",
                            f"no {'/'.join(missing)} shop under the Malaysian main shop "
                            f"(found {sorted(self._affi) or 'none'}; Shopee sent {getattr(self, '_shop_info_keys', [])})")
        return self._affi

    def _call_plain(self, method: str, path: str, params: dict | None = None, body: dict | None = None):
        # ShopeeSource._call returns only "response"; get_shop_info puts its fields at the top level
        q = self._common(path)
        q.update(params or {})
        data = self.http.request(method, "https://partner.shopeemobile.com" + path, params=q, json=body,
                                 timeout=60).json()
        if data.get("error"):
            raise PackError("SHOPEE-SG", f"Shopee {path}", f"{data.get('error')} {data.get('message')}")
        return data.get("response") or data

    # ---- orders -------------------------------------------------------------------------------

    def _shops(self) -> dict[str, int]:
        """Region -> shop id: shops with their own token first, the rest found under the main shop."""
        own = {r: int(t["shop_id"]) for r, t in self.own_tokens().items() if r in self.regions and t.get("shop_id")}
        if all(r in own for r in self.regions):
            return own
        return {**self._affi_shops(), **own}

    def list_orders(self) -> list[Order]:
        self._load_from_odoo()
        out: list[Order] = []
        for region, shop_id in self._shops().items():
            if region not in self.regions:
                continue
            if not self._use_region(region):
                self._load_from_odoo()   # back to the borrowed token
                self.shop_id = shop_id
            sns = self._order_sns("READY_TO_SHIP") + self._order_sns("PROCESSED")
            for d in self._details(sns):
                o = self._to_order(d)
                o.source = self.name
                o.platform = f"shopee {region}"
                o.ref["shop_id"] = shop_id
                o.ref["region"] = region
                for it in o.items:
                    it.name = short_name(it.name)
                if o.needs_arrange:   # not arranged yet: arrange it in Seller Centre, then Refresh
                    o.needs_arrange, o.waybill_ready = False, False
                out.append(o)
        return out

    def _use_shop_of(self, order: Order) -> None:
        region = order.ref.get("region") or (order.platform.split(" ")[-1].upper() if order.platform else "")
        if self._use_region(region):
            return
        if not self.access_token:
            self._load_from_odoo()
        self.shop_id = int(order.ref.get("shop_id") or self.shop_id)

    def _call(self, method, path, params=None, body=None, raw=False):
        try:
            return super()._call(method, path, params, body, raw)
        except RuntimeError as e:
            raise PackError("SHOPEE-SG", f"Shopee {path.rsplit('/', 1)[-1]}", str(e)[:200]) from e

    def _tracking(self, sn: str) -> str | None:
        try:
            return super()._tracking(sn)
        except PackError:
            return None

    def _doc_type(self, order: Order) -> str:
        """The waybill type Shopee suggests for this order's courier (overseas SPX may differ)."""
        entry = {"order_sn": order.order_id}
        if order.ref.get("package_number"):
            entry["package_number"] = order.ref["package_number"]
        try:
            r = self._call("POST", "/api/v2/logistics/get_shipping_document_parameter", body={"order_list": [entry]})
            res = (r.get("result_list") or [{}])[0]
            options = res.get("selectable_shipping_document_type") or []
            if "THERMAL_AIR_WAYBILL" in options:
                return "THERMAL_AIR_WAYBILL"
            return res.get("suggest_shipping_document_type") or "THERMAL_AIR_WAYBILL"
        except PackError:
            return "THERMAL_AIR_WAYBILL"

    def get_waybill(self, order: Order) -> bytes:
        from . import shopee as base

        self._load_from_odoo()   # app key (and the borrowed token, if the shop has no own one)
        self._use_shop_of(order)
        doc = self._doc_type(order)
        old, base.DOC_TYPE = base.DOC_TYPE, doc
        try:
            return super().get_waybill(order)
        except RuntimeError as e:
            raise PackError("WAYBILL-MISSING", "fetching Shopee Singapore waybill", str(e)[:200]) from e
        finally:
            base.DOC_TYPE = old

    def arrange(self, order: Order) -> Order:
        raise PackError("WAYBILL-MISSING", "arranging Shopee Singapore shipment",
                        "arrange it in Shopee Seller Centre (Singapore shop), then press Refresh")
