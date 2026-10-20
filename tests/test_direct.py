"""Shopee/TikTok asked directly when Odoo falls short; no "TikTok Shop Sale" labels; stock fix on pack."""
from __future__ import annotations

import pytest

from packstation.engine import Engine
from packstation.errors import PackError
from packstation.models import Item, Order
from packstation.sources.direct import TikTokDirect, WaybillCache
from packstation.sources.odoo import OdooSource

try:
    from tests.test_fixes import FakeOdoo, by_name, pdf_bytes, write_cfg
except ImportError:
    from test_fixes import FakeOdoo, by_name, pdf_bytes, write_cfg


class FakeTT:
    def __init__(self, status="AWAITING_COLLECTION", lines=None):
        self.status, self.shipped = status, []
        self.lines = lines or [{"sku_id": "999", "product_name": "Galaxolide 50 – Premium Aroma Chemical | Perfume",
                                "sku_name": "15g x2", "seller_sku": ""}]

    def order(self, ref, max_age=60):
        return {"id": ref, "status": self.status, "tracking_number": "680068472406118",
                "packages": [{"id": "PKG1"}], "line_items": self.lines}

    def items(self, ref, sku_products=None):
        return TikTokDirect.items(self, ref, sku_products)

    def label(self, ref, pkg=""):
        return None if self.status == "AWAITING_SHIPMENT" else pdf_bytes()

    def ship(self, ref, pkg="", method="dropoff"):
        self.shipped.append((ref, method))
        self.status = "AWAITING_COLLECTION"


class FakeSP:
    def __init__(self, tracking=None):
        self.tn, self.shipped = tracking, []

    def tracking(self, sn):
        return self.tn

    def label(self, sn, tracking=None):
        return pdf_bytes() if (tracking or self.tn) else None

    def ship(self, sn, method="dropoff"):
        self.shipped.append(sn)
        self.tn = "SPXMY0000NEW1"
        return self.tn


def odoo_with(fake, tmp_path, tt=None, sp=None):
    src = OdooSource({"only_sales_teams": []}, rpc=fake, sleep=lambda s: None)
    src.attach(tmp_path / "data")
    src._tt_api, src._sp_api = tt, sp
    return src


def tiktok_fake(placeholder=True):
    fake = FakeOdoo()
    p = fake.pickings[73]
    p.update(tiktok_order_ref="5866", tiktok_packing_ref="PKG1")
    for pk in fake.pickings.values():
        pk.setdefault("tiktok_order_ref", False)
        pk.setdefault("tiktok_packing_ref", False)
        pk.setdefault("shopee_order_ref", False)
    fake.pickings[81]["shopee_order_ref"] = "2610192WGATVAX"
    real = fake.__call__

    def rpc(model, method, *a, **kw):
        if model == "product.product" and method == "read" and placeholder:
            return [{"id": i, "default_code": "", "display_name": "TikTok Shop Sale" if i == 101 else f"Linalool {i} 3g"}
                    for i in a[0]]
        if model == "tiktok.item" and method == "search_read":
            return []
        if model == "sale.order.line":
            return []
        return real(model, method, *a, **kw)
    rpc.calls = fake.calls
    rpc.pickings = fake.pickings
    return rpc, fake


# ---------------------------------------------------------------- TikTok items

def test_tiktok_placeholder_gets_the_real_bottles_from_tiktok(tmp_path):
    rpc, fake = tiktok_fake()
    src = odoo_with(rpc, tmp_path, tt=FakeTT())
    o = next(o for o in src.list_orders() if o.order_id == "WH/OUT/00073")
    assert [(i.name, i.qty) for i in o.items] == [("Galaxolide 50 15g x2", 1)]
    assert o.ref["items_from"] == "tiktok"


def test_tiktok_variant_linked_in_odoo_uses_the_odoo_product(tmp_path):
    tt = FakeTT(lines=[{"sku_id": "1736", "product_name": "Iso E Super", "sku_name": "15g"}] * 2)
    items = tt.items("x", {"1736": {"default_code": "SP034-15G", "display_name": "[SP034-15G] Iso E Super 15g"}})
    assert [(i.sku, i.name, i.qty) for i in items] == [("SP034-15G", "[SP034-15G] Iso E Super 15g", 2)]


def test_placeholder_label_is_never_printed(tmp_path):
    rpc, fake = tiktok_fake()
    src = odoo_with(rpc, tmp_path, tt=None)
    src.direct = False                                   # TikTok unreachable: the name stays unknown
    eng = Engine(write_cfg(tmp_path), sources=[src])
    eng.scan()
    o = by_name(eng)["WH/OUT/00073"]
    assert o.items[0].name == "TikTok Shop Sale"
    job = eng.wait(eng.start_print_one(o.key))
    assert job.error["code"] == "PRODUCT-UNKNOWN"
    assert o.key not in eng.store.printed()
    assert not list((tmp_path / "data" / "output").rglob("*.png")) if (tmp_path / "data" / "output").exists() else True


# ---------------------------------------------------------------- waybills

def test_tiktok_arranged_in_seller_center_gets_its_label_on_refresh(tmp_path):
    rpc, fake = tiktok_fake(placeholder=False)
    src = odoo_with(rpc, tmp_path, tt=FakeTT("AWAITING_COLLECTION"))
    o = next(o for o in src.list_orders() if o.order_id == "WH/OUT/00073")
    assert o.waybill_ready and o.ref["label"][0] == "file"
    assert o.tracking == "680068472406118"
    assert src.get_waybill(o)[:4] == b"%PDF"
    assert WaybillCache(tmp_path / "data").get("WH/OUT/00073")


def test_shopee_label_missing_in_odoo_comes_from_shopee(tmp_path):
    rpc, fake = tiktok_fake(placeholder=False)
    sp = FakeSP(tracking="SPXMY06846803469A")
    src = odoo_with(rpc, tmp_path, tt=FakeTT("AWAITING_SHIPMENT"), sp=sp)
    o = next(o for o in src.list_orders() if o.order_id == "WH/OUT/00081")
    assert o.waybill_ready and o.tracking == "SPXMY06846803469A"
    assert not sp.shipped                                 # refresh never books anything


def test_shopee_is_never_arranged_by_pack_station(tmp_path):
    rpc, fake = tiktok_fake(placeholder=False)
    sp = FakeSP(tracking=None)
    src = odoo_with(rpc, tmp_path, tt=FakeTT("AWAITING_SHIPMENT"), sp=sp)
    eng = Engine(write_cfg(tmp_path), sources=[src])
    eng.scan()
    o = by_name(eng)["WH/OUT/00081"]
    assert o.key in eng.plan()["ready"]                    # Print all still tries it
    job = eng.wait(eng.start_print_one(o.key))
    assert job.error["code"] == "WAYBILL-MISSING"
    assert not sp.shipped                                  # Odoo's connector arranges Shopee, not us
    assert any(c[1] == "method_direct_trigger" and c[2] == ([74],) for c in fake.calls)   # label sync nudged
    sp.tn = "SPXMY0000NEW1"                                # Odoo arranged it meanwhile
    job = eng.wait(eng.start_print_one(o.key))
    assert job.state == "done" and not job.error, job.as_dict()
    assert eng.store.printed_rows()[o.key]["tracking"] == "SPXMY0000NEW1"


def test_tiktok_falls_back_to_tiktok_when_odoo_never_gets_the_label(tmp_path):
    rpc, fake = tiktok_fake(placeholder=False)
    fake.att.pop(777)

    def no_label(model, method, *a, **kw):
        if method == "action_tiktok_sync_pickings":
            return True
        return rpc(model, method, *a, **kw)

    tt = FakeTT("AWAITING_SHIPMENT")
    src = odoo_with(no_label, tmp_path, tt=tt)
    eng = Engine(write_cfg(tmp_path), sources=[src])
    eng.scan()
    job = eng.wait(eng.start_print_one("odoo:WH/OUT/00073"))
    assert job.state == "done" and not job.error, job.as_dict()
    assert fake.wizard_vals == {"handover_method": "drop_off"}   # booked through Odoo first


# ---------------------------------------------------------------- stock fix on pack

class StockOdoo:
    """Just enough Odoo for deduct_real_items."""

    def __init__(self, placeholder=True):
        self.created, self.writes, self.validated, self.notes = [], [], [], []
        self.placeholder = placeholder
        self.state = {}

    def __call__(self, model, method, *a, **kw):
        if model == "res.users":
            return [{"id": 2, "company_ids": [1]}]
        if model == "res.company":
            return [{"id": 1, "name": "Atlas"}]
        if model == "stock.move" and method == "search_read":
            dom = a[0]
            if dom[0][0] == "picking_id" and dom[0][1] == "in":
                return [{"id": 1, "picking_id": [111, "WH/OUT/00110"],
                         "product_id": [5, "TikTok Shop Sale" if self.placeholder else "[SP025-15G] Galaxolide 50 15g"]}]
            return [{"id": 50, "product_uom_qty": 15.0}, {"id": 51, "product_uom_qty": 2.0}]   # kit parts
        if model == "stock.move" and method == "write":
            self.writes.append((a[0], a[1]))
            return True
        if model == "stock.picking" and method == "read":
            if a[0] == [111]:
                return [{"id": 111, "name": "WH/OUT/00110", "picking_type_id": [2, "Delivery"], "location_id": [8, "WH/Stock"],
                         "location_dest_id": [5, "Customers"], "partner_id": [9, "Buyer"], "company_id": [1, "Atlas"],
                         "state": "done"}]
            return [{"id": a[0][0], "name": "WH/OUT/00999", "state": self.state.get(a[0][0], "assigned")}]
        if model == "stock.picking" and method == "search_read":
            return [{"name": "WH/OUT/00999"}] if self.created else []
        if model == "product.product" and method == "search_read":
            dom = a[0]
            if dom[0] == ("name", "=ilike", "Galaxolide 50 15g"):
                return [{"id": 25}]
            return []
        if model == "product.product" and method == "read":
            return [{"id": 25, "uom_id": [1, "Units"], "display_name": "[SP025-15G] Galaxolide 50 15g"}]
        if model == "stock.picking" and method == "create":
            self.created.append(a[0])
            return 999
        if model == "stock.picking" and method == "action_confirm":
            return True
        if model == "stock.picking" and method == "button_validate":
            self.validated.append(a[0])
            self.state[999] = "done"
            return True
        if method == "message_post":
            self.notes.append(kw.get("body"))
            return 1
        raise AssertionError((model, method, a))


def order_110(items):
    return Order(source="odoo", order_id="WH/OUT/00110", items=items, platform="tiktok", odoo_picking_id=111)


def test_pack_takes_the_real_bottles_out_of_stock_once(tmp_path):
    fake = StockOdoo()
    src = OdooSource({}, rpc=fake)
    name = src.deduct_real_items(order_110([Item("", "Galaxolide 50 15g x2", 1)]))
    assert name == "WH/OUT/00999"
    vals = fake.created[0][0]
    assert vals["origin"] == "Pack Station stock fix for WH/OUT/00110"
    assert [(m[2]["product_id"], m[2]["product_uom_qty"]) for m in vals["move_ids"]] == [(25, 2)]   # 2 x 15g bottles
    assert fake.writes == [([50], {"quantity": 15.0, "picked": True}), ([51], {"quantity": 2.0, "picked": True})]
    assert fake.validated == [[999]]
    assert "WH/OUT/00999" in fake.notes[0]
    assert src.deduct_real_items(order_110([Item("", "Galaxolide 50 15g x2", 1)])) == "WH/OUT/00999"
    assert len(fake.created) == 1                                  # never twice


def test_pack_without_placeholder_needs_no_stock_fix():
    fake = StockOdoo(placeholder=False)
    assert OdooSource({}, rpc=fake).deduct_real_items(order_110([Item("SP025-15G", "Galaxolide 50 15g", 2)])) is None
    assert not fake.created


def test_stock_fix_refuses_when_the_product_is_unknown():
    fake = StockOdoo()
    with pytest.raises(PackError) as e:
        OdooSource({}, rpc=fake).deduct_real_items(order_110([Item("", "TikTok Shop Sale", 1)]))
    assert e.value.code == "STOCK-FIX" and not fake.created
