import hashlib
import hmac
import json

import pytest
from PIL import Image

from packstation.engine import Engine
from packstation.labels import Catalog, render_product_label, LabelData
from packstation.models import Item, Order
from packstation.printers import tspl_job
from packstation.sources import shopee, tiktok
from packstation.sources.demo import DemoSource


def make_cfg(tmp_path):
    return {"app": {"data_dir": str(tmp_path)}, "printers": {"waybill": {"backend": "file"},
                                                            "label": {"backend": "file"}}}


class FakeResp:
    def __init__(self, data=None, content=b"", ctype="application/json"):
        self._data, self.content, self.headers = data, content, {"Content-Type": ctype}

    def json(self):
        return self._data


class FakeHTTP:
    """Answers by URL path; records every call."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def request(self, method, url, params=None, json=None, data=None, headers=None, timeout=None):
        self.calls.append((method, url, params, json if json is not None else data))
        for path, resp in self.routes.items():
            if url.endswith(path):
                return resp(params, json if json is not None else data) if callable(resp) else resp
        raise AssertionError(f"unexpected call {url}")

    def get(self, url, params=None, timeout=None):
        return self.request("GET", url, params=params)

    def post(self, url, params=None, json=None, timeout=None):
        return self.request("POST", url, params=params, json=json)


# ---- signing -------------------------------------------------------------------

def test_shopee_sign_is_hmac_of_base_string():
    base = "123/api/v2/order/get_order_list1700000000tok456"
    assert shopee.sign("key", base) == hmac.new(b"key", base.encode(), hashlib.sha256).hexdigest()


def test_tiktok_sign_sorts_params_and_skips_sign_and_token():
    params = {"timestamp": 2, "app_key": "a", "sign": "x", "access_token": "t", "shop_cipher": "c"}
    expected_payload = "s" + "/p" + "app_keyashop_cipherctimestamp2" + "{}" + "s"
    expected = hmac.new(b"s", expected_payload.encode(), hashlib.sha256).hexdigest()
    assert tiktok.sign("s", "/p", params, "{}") == expected


# ---- platforms (with fake HTTP) --------------------------------------------------

def test_shopee_lists_orders_and_downloads_waybill(tmp_path):
    def order_list(params, body):
        sns = {"READY_TO_SHIP": [{"order_sn": "A1"}], "PROCESSED": [{"order_sn": "B2"}]}
        return FakeResp({"response": {"order_list": sns[params["order_status"]], "more": False}})

    http = FakeHTTP({
        "/order/get_order_list": order_list,
        "/order/get_order_detail": FakeResp({"response": {"order_list": [
            {"order_sn": "A1", "order_status": "READY_TO_SHIP", "ship_by_date": 1760000000,
             "item_list": [{"item_name": "Sandalwood", "model_name": "3g", "model_sku": "ML-SANDAL-3G",
                            "model_quantity_purchased": 2}]},
            {"order_sn": "B2", "order_status": "PROCESSED", "ship_by_date": 1760000000,
             "package_list": [{"package_number": "P1"}],
             "item_list": [{"item_name": "Linalool 5g", "item_sku": "ML-LINALOOL-5G",
                            "model_quantity_purchased": 1}]},
        ]}}),
        "/logistics/get_tracking_number": FakeResp({"response": {"tracking_number": "SPXMY0001234A"}}),
        "/logistics/create_shipping_document": FakeResp({"response": {}}),
        "/logistics/get_shipping_document_result": FakeResp(
            {"response": {"result_list": [{"order_sn": "B2", "status": "READY"}]}}),
        "/logistics/download_shipping_document": FakeResp(content=b"%PDF-1.4 fake", ctype="application/pdf"),
    })
    src = shopee.ShopeeSource({"partner_id": 1, "partner_key": "k", "shop_id": 2, "access_token": "t"},
                              str(tmp_path), session=http)
    orders = {o.order_id: o for o in src.list_orders()}
    assert orders["A1"].needs_arrange and orders["A1"].tracking is None
    assert orders["A1"].items[0].qty == 2 and orders["A1"].items[0].name == "Sandalwood 3g"
    b2 = orders["B2"]
    assert b2.tracking == "SPXMY0001234A" and b2.match_code == "234A"
    assert src.get_waybill(b2).startswith(b"%PDF")
    create_body = next(c[3] for c in http.calls if c[1].endswith("create_shipping_document"))
    assert create_body["order_list"][0]["package_number"] == "P1"
    with pytest.raises(RuntimeError, match="Seller Centre"):
        src.arrange(orders["A1"])  # auto-arrange is off by default


def test_tiktok_groups_line_items_into_quantities(tmp_path):
    def search(params, body):
        status = json.loads(body)["order_status"]
        orders = [] if status == "AWAITING_SHIPMENT" else [{
            "id": "577", "status": "AWAITING_COLLECTION", "tracking_number": "MYTT00099X",
            "rts_sla_time": 1760000000, "packages": [{"id": "PK1"}],
            "line_items": [{"seller_sku": "ML-SANDAL-3G", "product_name": "Sandalwood", "sku_name": "3g"}] * 3,
        }]
        return FakeResp({"code": 0, "data": {"orders": orders}})

    http = FakeHTTP({
        "/order/202309/orders/search": search,
        "/packages/PK1/shipping_documents": FakeResp({"code": 0, "data": {"doc_url": "https://x/label.pdf"}}),
        "/label.pdf": FakeResp(content=b"%PDF-1.4 tt"),
    })
    src = tiktok.TikTokSource({"app_key": "a", "app_secret": "s", "shop_cipher": "c", "access_token": "t"},
                              str(tmp_path), session=http)
    [o] = src.list_orders()
    assert o.items == [Item("ML-SANDAL-3G", "Sandalwood 3g", 3)]
    assert not o.needs_arrange and o.match_code == "099X"
    assert src.get_waybill(o).startswith(b"%PDF")


# ---- labels and printing -----------------------------------------------------------

def test_catalog_falls_back_to_name_with_size():
    cat = Catalog("/nonexistent.csv")
    data, known = cat.lookup(Item("X", "Vanillin 10g", 1))
    assert (data.title, data.size, known) == ("Vanillin", "10g", False)


def test_label_is_one_bit_at_roll_size():
    img = render_product_label(LabelData("Sandalwood", "3g", "moleculelabs", "corrosive"), "871A", 50, 25)
    assert img.mode == "1" and img.size == (400, 200)


def test_tspl_job_layout():
    img = Image.new("1", (16, 2), 1)
    img.putpixel((0, 0), 0)
    job = tspl_job(img, 50, 25)
    assert job.startswith(b"SIZE 50 mm,25 mm\r\n")
    assert b"BITMAP 0,0,2,2,0," in job and job.endswith(b"PRINT 1,1\r\n")
    data = job.split(b"BITMAP 0,0,2,2,0,")[1][:4]
    assert data == bytes([0x7F, 0xFF, 0xFF, 0xFF])  # black dot = 0 bit


def test_print_all_prints_each_order_once_in_matching_order(tmp_path):
    eng = Engine(make_cfg(tmp_path), sources=[DemoSource()])
    eng.scan()
    results = eng.print_all()
    assert [r.ok for r in results] == [True, True, True]
    assert sum(r.labels for r in results) == 7
    assert eng.print_all() == []        # nothing printed twice
    waybills = sorted((tmp_path / "output").glob("*/waybill/*.pdf"))
    labels = sorted((tmp_path / "output").glob("*/label/*.png"))
    assert len(waybills) == 3 and len(labels) == 7
    assert [p.name.split("-")[3] for p in waybills] == ["DEMO10001", "DEMO10002", "DEMO10003"]


def test_odoo_delivery_merges_into_platform_order(tmp_path):
    shop = Order("shopee", "A1", [Item("S", "Sandalwood 3g")])
    odoo_twin = Order("odoo", "WH/OUT/0001", [Item("S", "Sandalwood 3g")], has_waybill=False,
                      odoo_picking_id=7, ref={"platform_ref": "A1"})
    odoo_only = Order("odoo", "WH/OUT/0002", [Item("S", "Sandalwood 3g")], has_waybill=False,
                      odoo_picking_id=8, ref={"platform_ref": ""})
    merged = Engine._merge([shop, odoo_twin, odoo_only])
    assert [o.key for o in merged] == ["odoo:WH/OUT/0002", "shopee:A1"]
    assert shop.odoo_picking_id == 7


def test_failed_waybill_skips_labels_and_stays_pending(tmp_path):
    class Broken(DemoSource):
        def get_waybill(self, order):
            raise RuntimeError("courier down")

    eng = Engine(make_cfg(tmp_path), sources=[Broken()])
    eng.scan()
    results = eng.print_all()
    assert not any(r.ok for r in results)
    assert not list((tmp_path / "output").glob("*/label/*.png"))
    assert len(eng.pending()) == 3


def test_web_page_and_print_button(tmp_path):
    from packstation.web import create_app

    eng = Engine(make_cfg(tmp_path), sources=[DemoSource()])
    client = create_app(eng).test_client()
    page = client.get("/").get_data(as_text=True)
    assert "Print all · 3 orders · 7 labels" in page
    client.post("/print-all")
    page = client.get("/").get_data(as_text=True)
    assert "Print all · 0 orders" in page and "Printed" in page
    assert client.get("/preview/demo:DEMO10002").mimetype == "image/png"
