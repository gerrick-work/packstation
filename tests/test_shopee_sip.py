"""Shopee cross-border (Singapore) shop read with the token Odoo's Shopee connector keeps."""
import time

import pytest

from packstation.engine import Engine
from packstation.errors import PackError
from packstation.sources.odoo import OdooSource
from packstation.sources.shopee_sip import ShopeeSipSource

from tests.test_fixes import FakeOdoo, pdf_bytes, write_cfg


class Resp:
    def __init__(self, data=None, content=b""):
        self._d, self.content = data, content or b"{}"

    def json(self):
        return self._d


class FakeShopee:
    """Answers like Shopee: the Malaysian main shop 111 lists Singapore shop 222 as a SIP shop."""

    def __init__(self, token_ok="TOKEN-FROM-ODOO"):
        self.calls, self.token_ok, self.doc_types = [], token_ok, []

    def request(self, method, url, params=None, json=None, timeout=None):
        path = url.split(".com", 1)[1]
        self.calls.append((path, params.get("shop_id"), params.get("access_token")))
        if params.get("access_token") != self.token_ok:
            return Resp({"error": "error_auth", "message": "Invalid access_token."})
        if path == "/api/v2/shop/get_shop_info":
            return Resp({"error": "", "shop_name": "moleculelabs.co", "region": "MY",
                         "sip_affi_shops": [{"affi_shop_id": 222, "region": "SG"},
                                            {"affi_shop_id": 333, "region": "TH"}]})
        assert params["shop_id"] == 222, "Singapore calls must use the Singapore shop id"
        if path == "/api/v2/order/get_order_list":
            sns = {"READY_TO_SHIP": ["SGNEW1"], "PROCESSED": ["261014JTXG926Q"]}[params["order_status"]]
            return Resp({"error": "", "response": {"order_list": [{"order_sn": s} for s in sns], "more": False}})
        if path == "/api/v2/order/get_order_detail":
            out = []
            for sn in params["order_sn_list"].split(","):
                out.append({"order_sn": sn, "order_status": "READY_TO_SHIP" if sn == "SGNEW1" else "PROCESSED",
                            "ship_by_date": int(time.time()) + 86400, "package_list": [{"package_number": "P1"}],
                            "item_list": [{"model_sku": "SP025-3G", "item_name": "Galaxolide 50", "model_name": "3g",
                                           "model_quantity_purchased": 1}]})
            return Resp({"error": "", "response": {"order_list": out}})
        if path == "/api/v2/logistics/get_tracking_number":
            return Resp({"error": "", "response": {"tracking_number": "SG2600805968808C"}})
        if path == "/api/v2/logistics/get_shipping_document_parameter":
            return Resp({"error": "", "response": {"result_list": [{"suggest_shipping_document_type": "NORMAL_AIR_WAYBILL",
                                                                    "selectable_shipping_document_type": ["NORMAL_AIR_WAYBILL"]}]}})
        if path == "/api/v2/logistics/create_shipping_document":
            self.doc_types.append(json["order_list"][0]["shipping_document_type"])
            return Resp({"error": "", "response": {}})
        if path == "/api/v2/logistics/get_shipping_document_result":
            return Resp({"error": "", "response": {"result_list": [{"status": "READY"}]}})
        if path == "/api/v2/logistics/download_shipping_document":
            return Resp(content=pdf_bytes())
        raise AssertionError(path)


class OdooWithShopee(FakeOdoo):
    def __call__(self, model, method, *args, **kw):
        if model == "shopee.account":
            return [{"id": 1, "partner_identifier": 2045486, "partner_key": "KEY"}]
        if model == "shopee.shop":
            return [{"id": 1, "shop_identifier": 111, "access_token": "TOKEN-FROM-ODOO"}]
        return super().__call__(model, method, *args, **kw)


def sip(shopee=None):
    odoo = OdooSource({"only_sales_teams": []}, rpc=OdooWithShopee())
    return ShopeeSipSource(odoo, ["SG"], session=shopee or FakeShopee())


def test_singapore_orders_listed_with_odoos_token_and_sg_shop_id():
    shopee = FakeShopee()
    orders = {o.order_id: o for o in sip(shopee).list_orders()}
    o = orders["261014JTXG926Q"]
    assert o.platform == "shopee SG" and o.tracking == "SG2600805968808C" and o.waybill_ready
    assert o.items[0].sku == "SP025-3G"
    new = orders["SGNEW1"]   # not arranged yet: shown as waybill missing, never booked by Pack Station
    assert not new.needs_arrange and not new.waybill_ready and new.waybill_missing
    assert {c[2] for c in shopee.calls} == {"TOKEN-FROM-ODOO"}
    assert not any("access_token/get" in c[0] for c in shopee.calls)   # never refreshes Odoo's token


def test_waybill_uses_the_type_shopee_suggests():
    shopee = FakeShopee()
    src = sip(shopee)
    o = next(x for x in src.list_orders() if x.order_id == "261014JTXG926Q")
    assert src.get_waybill(o).startswith(b"%PDF")
    assert shopee.doc_types == ["NORMAL_AIR_WAYBILL"]


def test_bad_token_is_a_clear_singapore_error_and_odoo_orders_still_show(tmp_path):
    odoo = OdooSource({"only_sales_teams": []}, rpc=OdooWithShopee())
    sg = ShopeeSipSource(odoo, ["SG"], session=FakeShopee(token_ok="OTHER"))
    eng = Engine(write_cfg(tmp_path), sources=[odoo, sg])
    eng.scan()
    assert eng.source_errors["shopee-sg"]["code"] == "SHOPEE-SG"
    assert any(o.source == "odoo" for o in eng.orders)


def test_missing_region_says_so():
    src = ShopeeSipSource(OdooSource({"only_sales_teams": []}, rpc=OdooWithShopee()), ["PH"], session=FakeShopee())
    with pytest.raises(PackError) as e:
        src.list_orders()
    assert e.value.code == "SHOPEE-SG" and "PH" in e.value.detail


def test_engine_prints_singapore_order(tmp_path):
    odoo = OdooSource({"only_sales_teams": []}, rpc=OdooWithShopee())
    sg = ShopeeSipSource(odoo, ["SG"], session=FakeShopee())
    eng = Engine(write_cfg(tmp_path), sources=[odoo, sg])
    eng.scan()
    o = next(x for x in eng.orders if x.order_id == "261014JTXG926Q")
    r = eng.print_order(o)
    assert r.ok, r.message


def test_off_by_default():
    from packstation.sources import build_sources
    cfg = {"app": {"data_dir": "."}, "odoo": {"enabled": True, "url": "https://x.odoo.com", "db": "d", "username": "x", "api_key": "k"}}
    assert [s.name for s in build_sources(cfg)] == ["manual", "odoo"]
    cfg["odoo"]["shopee_sip_regions"] = ["SG"]
    assert [s.name for s in build_sources(cfg)] == ["manual", "odoo", "shopee-sg"]


def test_direct_shop_list_format_is_understood():
    class DirectShopee(FakeShopee):
        def request(self, method, url, params=None, json=None, timeout=None):
            if url.endswith("/api/v2/shop/get_shop_info"):
                self.calls.append(("/api/v2/shop/get_shop_info", params.get("shop_id"), params.get("access_token")))
                return Resp({"error": "", "shop_name": "moleculelabs.co", "region": "MY", "is_main_shop": True,
                             "linked_direct_shop_list": [{"direct_shop_id": 222, "direct_shop_region": "SG"}]})
            return super().request(method, url, params, json, timeout)
    orders = sip(DirectShopee()).list_orders()
    assert any(o.order_id == "261014JTXG926Q" for o in orders)


# ---------------------------------------------------------------- Singapore's own authorisation

class OwnTokenShopee(FakeShopee):
    """Shopee for a Direct shop: the main shop's token is refused, the Singapore shop's own works."""

    def __init__(self):
        super().__init__(token_ok="SG-ACCESS-1")
        self.posts = []

    def post(self, url, params=None, json=None, timeout=None):
        path = url.split(".com", 1)[1]
        self.posts.append((path, json))
        if path == "/api/v2/auth/token/get":
            assert json["code"] == "CODE1" and json["shop_id"] == 222 and json["partner_id"] == 2045486
            return Resp({"error": "", "access_token": "SG-ACCESS-1", "refresh_token": "SG-REFRESH-1", "expire_in": 14400})
        if path == "/api/v2/auth/access_token/get":
            assert json["refresh_token"] == "SG-REFRESH-1" and json["shop_id"] == 222
            self.token_ok = "SG-ACCESS-2"
            return Resp({"error": "", "access_token": "SG-ACCESS-2", "refresh_token": "SG-REFRESH-2", "expire_in": 14400})
        raise AssertionError(path)

    def request(self, method, url, params=None, json=None, timeout=None):
        if url.endswith("/api/v2/shop/get_shop_info") and params.get("shop_id") == 222:
            self.calls.append(("/api/v2/shop/get_shop_info", 222, params.get("access_token")))
            if params.get("access_token") != self.token_ok:
                return Resp({"error": "error_auth", "message": "Invalid access_token."})
            return Resp({"error": "", "shop_name": "moleculelabs.co.sg", "region": "SG"})
        return super().request(method, url, params, json, timeout)


def own_sip(tmp_path, shopee):
    odoo = OdooSource({"only_sales_teams": []}, rpc=OdooWithShopee())
    return ShopeeSipSource(odoo, ["SG"], session=shopee, data_dir=str(tmp_path))


def test_auth_url_is_signed_for_the_app(tmp_path):
    from urllib.parse import parse_qs, urlparse
    from packstation.sources.shopee import sign

    url = own_sip(tmp_path, OwnTokenShopee()).auth_url("http://localhost:8765/shopee-sg/callback")
    q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
    assert url.startswith("https://partner.shopeemobile.com/api/v2/shop/auth_partner?")
    assert q["partner_id"] == "2045486" and q["redirect"].endswith("/shopee-sg/callback")
    assert q["sign"] == sign("KEY", f"2045486/api/v2/shop/auth_partner{q['timestamp']}")


def test_connect_saves_singapore_token_and_lists_its_orders(tmp_path):
    shopee = OwnTokenShopee()
    src = own_sip(tmp_path, shopee)
    shops = src.connect("CODE1", shop_id=222)
    assert shops == [{"region": "SG", "shop_id": 222, "shop_name": "moleculelabs.co.sg"}]
    saved = src.own_tokens()["SG"]
    assert saved["refresh_token"] == "SG-REFRESH-1"
    orders = {o.order_id: o for o in own_sip(tmp_path, shopee).list_orders()}   # fresh start reads the file
    assert orders["261014JTXG926Q"].platform == "shopee SG"
    order_calls = [c for c in shopee.calls if c[0] == "/api/v2/order/get_order_list"]
    assert order_calls and all(c[1] == 222 and c[2] == "SG-ACCESS-1" for c in order_calls)


def test_singapore_token_refreshes_itself_and_is_saved(tmp_path):
    shopee = OwnTokenShopee()
    src = own_sip(tmp_path, shopee)
    src.connect("CODE1", shop_id=222)
    t = src.own_tokens()
    t["SG"]["expires_at"] = int(time.time()) - 10
    src.own.save(t)
    src2 = own_sip(tmp_path, shopee)
    o = next(x for x in src2.list_orders() if x.order_id == "261014JTXG926Q")
    assert src2.own_tokens()["SG"]["refresh_token"] == "SG-REFRESH-2"
    assert src2.get_waybill(o).startswith(b"%PDF")


def test_main_shop_is_never_given_a_second_token(tmp_path):
    shopee = OwnTokenShopee()
    with pytest.raises(PackError):
        own_sip(tmp_path, shopee).connect("CODE1", shop_id=111)


def test_hand_typed_order_is_not_shown_twice(tmp_path):
    import json as _json
    from packstation.sources.manual import ManualSource

    (tmp_path / "manual_orders.json").write_text(_json.dumps(
        [{"order_id": "261014JTXG926Q", "platform": "shopee SG", "tracking": "SG260080596808C",
          "items": [{"sku": "SP025-3G", "name": "Galaxolide 50 3g", "qty": 1}]}]))
    odoo = OdooSource({"only_sales_teams": []}, rpc=OdooWithShopee())
    sg = ShopeeSipSource(odoo, ["SG"], session=FakeShopee())
    eng = Engine(write_cfg(tmp_path), sources=[ManualSource(str(tmp_path)), odoo, sg])
    eng.scan()
    same = [o for o in eng.orders if o.order_id == "261014JTXG926Q"]
    assert [o.source for o in same] == ["manual"]
    assert any(o.order_id == "SGNEW1" for o in eng.orders)


def test_settings_connect_by_pasting_the_address(tmp_path):
    from packstation.web import create_app

    shopee = OwnTokenShopee()
    odoo = OdooSource({"only_sales_teams": []}, rpc=OdooWithShopee())
    sg = ShopeeSipSource(odoo, ["SG"], session=shopee, data_dir=str(tmp_path / "data"))
    (tmp_path / "data").mkdir()
    eng = Engine(write_cfg(tmp_path, '\n[odoo]\nshopee_sip_regions = []\n'), sources=[odoo, sg])
    c = create_app(eng).test_client()
    assert not c.get("/api/shopee-sg").get_json()["connected"]
    r = c.post("/api/shopee-sg/paste", json={"url": "https://atlas-dsf.odoo.com/x?code=CODE1&shop_id=222"}).get_json()
    assert r["ok"] and r["shops"][0]["region"] == "SG"
    st = c.get("/api/shopee-sg").get_json()
    assert st["connected"] and st["shop_name"] == "moleculelabs.co.sg"


def test_listing_title_trimmed_for_the_bottle_label():
    from packstation.sources.shopee_sip import short_name
    assert short_name("Orange Blossom (Neroli) – Premium Aroma Chemical | Essential Oil | Fragrance Oil | "
                      "Perfumery Grade 15g") == "Orange Blossom (Neroli) 15g"
    assert short_name("Galaxolide 50 3g") == "Galaxolide 50 3g"
