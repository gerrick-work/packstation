"""Tests for the 20 Oct 2026 change list: order totals, reprinting one part of an order, and the
"Hold orders with missing info" switch."""
import pytest

from packstation.engine import Engine
from packstation.labels import Catalog, labels_for_order, parse_pack
from packstation.models import Item, Order
from packstation.sources.demo import DemoSource
from packstation.sources.odoo import OdooSource

try:
    from tests.test_fixes import FakeOdoo, LiveShapedOdoo, by_name, write_cfg
except ImportError:
    from test_fixes import FakeOdoo, LiveShapedOdoo, by_name, write_cfg


def demo_engine(tmp_path):
    eng = Engine(write_cfg(tmp_path), sources=[DemoSource()])
    # DEMO10001 and DEMO10002 are in the label list; DEMO10003's Vanillin is not.
    eng.catalog.path = tmp_path / "products.csv"
    eng.catalog.save([{"sku": s, "title": t, "size": z} for s, t, z in (
        ("ML-SANDAL-3G", "Sandalwood", "3g"), ("ML-ISOAMYL-3G", "Isoamyl Acetate", "3g"),
        ("ML-LINALOOL-5G", "Linalool", "5g"))])
    eng.scan()
    return eng


def outputs(tmp_path):
    return sorted(p.name for p in (tmp_path / "data" / "output").rglob("*") if p.is_file())


# ---------------------------------------------------------------- grand total

def test_odoo_orders_carry_the_sale_order_total(tmp_path):
    fake = FakeOdoo()
    real = fake.__call__

    def with_totals(model, method, *a, **kw):
        if model == "sale.order" and method == "read":
            assert {"amount_total", "currency_id"} <= set(kw["fields"])
            return [dict(s, amount_total=88.5, currency_id=[1, "MYR"], team_id=s.get("team_id") or [1, "Shopee"])
                    for s in real(model, method, *a, **kw)]
        return real(model, method, *a, **kw)

    eng = Engine(write_cfg(tmp_path), sources=[OdooSource({}, rpc=with_totals, sleep=lambda s: None)])
    eng.scan()
    o = by_name(eng)["WH/OUT/00073"]
    assert (o.total, o.currency) == (88.5, "MYR")


def test_total_from_odoo_fills_in_for_the_platform_twin():
    plat = Order("shopee", "SP1", [Item("A", "A", 1)], platform="shopee")
    odoo = Order("odoo", "WH/OUT/1", [Item("A", "A", 1)], odoo_picking_id=5, ref={"platform_ref": "SP1"},
                 total=20.0, currency="MYR")
    merged = Engine._merge([plat, odoo])
    assert len(merged) == 1 and (merged[0].total, merged[0].currency) == (20.0, "MYR")


# ---------------------------------------------------------------- reprint one part

def test_reprint_waybill_only_and_labels_only_change_nothing_on_record(tmp_path):
    eng = demo_engine(tmp_path)
    job = eng.wait(eng.start_reprint("demo:DEMO10001", "waybill"))
    assert job.state == "done" and job.summary == "waybill reprinted"
    assert outputs(tmp_path) and all("waybill" in n for n in outputs(tmp_path))
    assert "demo:DEMO10001" not in eng.store.printed()          # not marked printed by a part reprint

    job = eng.wait(eng.start_reprint("demo:DEMO10002", "labels"))
    assert job.state == "done" and job.summary == "2 labels reprinted"
    new = [n for n in outputs(tmp_path) if "DEMO10002" in n]
    assert new and not any("waybill" in n for n in new)
    assert "demo:DEMO10002" not in eng.store.printed()


def test_reprint_waybill_refused_when_there_is_none(tmp_path):
    eng = demo_engine(tmp_path)
    eng.orders[0].waybill_ready = False
    job = eng.wait(eng.start_reprint("demo:DEMO10001", "waybill"))
    assert job.error["code"] == "WAYBILL-MISSING"


# ---------------------------------------------------------------- hold orders with missing info

def test_hold_switch_off_prints_everything_as_before(tmp_path):
    eng = demo_engine(tmp_path)
    assert not eng.hold_incomplete
    assert eng.plan()["held"] == [] and len(eng.plan()["ready"]) == 3


def test_hold_switch_skips_incomplete_orders_in_print_all(tmp_path):
    eng = demo_engine(tmp_path)
    eng.save_settings("app", {"hold_incomplete": True})
    assert eng.hold_incomplete
    plan = eng.plan()
    assert plan["held"] == ["demo:DEMO10003"] and "demo:DEMO10003" not in plan["ready"]
    assert eng.state_of(by_name(eng)["DEMO10003"], {}, {}) == "held"

    job = eng.wait(eng.start_print_all())
    assert job.summary == "Printed 2 of 2 orders; 1 held for missing info"   # skipped is not a failure
    held = next(r for r in job.results if r.key == "demo:DEMO10003")
    assert not held.ok and held.error["code"] == "INFO-MISSING" and "ML-VANILLIN-10G" in held.error["detail"]
    assert set(eng.store.printed()) == {"demo:DEMO10001", "demo:DEMO10002"}


def test_hold_switch_asks_before_printing_one_and_allows_print_anyway(tmp_path):
    eng = demo_engine(tmp_path)
    eng.save_settings("app", {"hold_incomplete": True})
    r = eng.start_print_one("demo:DEMO10003")
    assert r == {"needs_confirm_incomplete": True, "missing": ["not in label list: ML-VANILLIN-10G"]}
    job = eng.wait(eng.start_print_one("demo:DEMO10003", allow_incomplete=True))
    assert job.state == "done" and "demo:DEMO10003" in eng.store.printed()


def test_hold_switch_holds_orders_without_a_waybill_or_tracking(tmp_path):
    eng = demo_engine(tmp_path)
    eng.save_settings("app", {"hold_incomplete": True})
    a, b = by_name(eng)["DEMO10001"], by_name(eng)["DEMO10002"]
    a.has_waybill = False                       # an Odoo-only order: labels but no waybill
    b.tracking = None
    assert eng.missing_info(a) == ["no waybill (not a Shopee/TikTok order)"]
    assert eng.missing_info(b) == ["no tracking number"]
    assert eng.plan()["ready"] == []


def test_print_anyway_also_covers_a_missing_tracking_number(tmp_path):
    eng = demo_engine(tmp_path)
    eng.save_settings("app", {"hold_incomplete": True})
    by_name(eng)["DEMO10001"].tracking = None
    assert eng.start_print_one("demo:DEMO10001")["missing"] == ["no tracking number"]
    job = eng.wait(eng.start_print_one("demo:DEMO10001", allow_incomplete=True))
    assert job.state == "done" and "demo:DEMO10001" in eng.store.printed()


# ---------------------------------------------------------------- "15x2" best-value bundles


@pytest.mark.parametrize("name", ["Hedione (15x2) Best Value", "Hedione Best Value (15x2) 30g",
                                  "Hedione 30g (15g x 2) Best Value", "Hedione 2 x 15g", "Hedione 15g×2",
                                  "Hedione (15X2 Units) Best Value", "[HED-BV] Hedione - 15x2 units - Best Value"])
def test_bundle_names_give_two_15g_labels(tmp_path, name):
    cat = Catalog(tmp_path / "p.csv")
    data, known = cat.lookup(Item("", name, 1))
    assert (data.title, data.size, data.bottles, known) == ("Hedione", "15g", 2, False)


@pytest.mark.parametrize("name", ["Hedione 30g", "Linalool 5g", "Vanillin 10g", "Iso E Super"])
def test_single_bottles_are_not_bundles(name):
    assert parse_pack(name) is None


def test_bundle_in_product_list_prints_one_label_per_bottle(tmp_path):
    cat = Catalog(tmp_path / "p.csv")
    cat.save([{"sku": "HED-BV", "title": "Hedione", "size": "30g"}])   # old row: one 30g label
    order = Order("odoo", "WH/OUT/9", [Item("HED-BV", "Hedione (15x2) Best Value", 3)])
    labels = labels_for_order(order, cat, {"width_mm": 40, "height_mm": 20})
    assert len(labels) == 6 and cat.label_count(order) == 6
    assert cat.lookup(order.items[0])[0].size == "15g"

    cat.save([{"sku": "HED-BV", "title": "Hedione", "size": "30g", "bottles": "1"}])  # explicit: really one
    assert cat.label_count(order) == 3 and cat.lookup(order.items[0])[0].size == "30g"
    assert Catalog(tmp_path / "p.csv").rows["HED-BV"].bottles == 1                    # saved in the CSV


def test_print_all_counts_bundle_labels(tmp_path):
    eng = demo_engine(tmp_path)
    by_name(eng)["DEMO10001"].items = [Item("", "Hedione (15x2) Best Value", 1)]
    assert eng.plan()["labels"] == 2 + 2 + 3


# ---------------------------------------------------------------- "TikTok Shop Sale" catch-all product

class CatchAllOdoo(LiveShapedOdoo):
    """Live-shaped Odoo where the TikTok order S00073's item landed on the catch-all "TikTok Shop Sale"."""

    def __init__(self, line_name):
        super().__init__()
        self.line_name = line_name

    def __call__(self, model, method, *args, **kw):
        if model == "sale.order.line" and method == "read":
            self.calls.append((model, method, args, kw))
            return [dict(l, name=self.line_name if l["id"] == 90 else f"Bottle {900 + l['id']} 3g")
                    for l in super().__call__(model, method, *args, **kw)]
        if model == "product.product" and method == "read" and 990 in args[0]:
            return [dict(p, default_code=False, display_name="TikTok Shop Sale") if p["id"] == 990 else p
                    for p in super().__call__(model, method, *args, **kw)]
        return super().__call__(model, method, *args, **kw)


def test_catch_all_product_uses_the_sale_line_item_name():
    fake = CatchAllOdoo("TikTok Shop Sale\n[ML-HED-BV] Hedione (15x2) Best Value")
    orders = {o.order_id: o for o in OdooSource({}, rpc=fake).list_orders()}
    item = orders["WH/OUT/00073"].items[0]
    assert (item.sku, item.name) == ("ML-HED-BV", "Hedione (15x2) Best Value")
    assert orders["WH/OUT/00080"].items[0].name == "Bottle 1000 3g"               # real products untouched
    data, _ = Catalog(None, "atlas origins").lookup(item)
    assert (data.title, data.size, data.bottles) == ("Hedione", "15g", 2)


def test_catch_all_product_without_a_name_is_flagged(tmp_path):
    eng = Engine(write_cfg(tmp_path), sources=[OdooSource({}, rpc=CatchAllOdoo("TikTok Shop Sale"))])
    eng.scan()
    o = by_name(eng)["WH/OUT/00073"]
    assert o.items[0].name == "TikTok Shop Sale"
    assert "Odoo has no product name for: TikTok Shop Sale" in eng.missing_info(o)


def test_catch_all_product_on_delivery_moves_uses_the_sale_line(tmp_path):
    """Fallback when the sale order's lines can't be read: the delivery move's sale line."""
    fake = FakeOdoo()
    real = fake.__call__

    def rpc(model, method, *a, **kw):
        if model == "stock.move" and method == "read":
            return [dict(m, sale_line_id=[700 + m["id"], "line"]) for m in real(model, method, *a, **kw)]
        if model == "product.product" and method == "read":
            return [dict(p, default_code=False, display_name="TikTok Shop Sale") if p["id"] == 101 else p
                    for p in real(model, method, *a, **kw)]
        if model == "sale.order.line" and method == "read":
            fake.calls.append((model, method, a, kw))
            return [{"id": i, "name": "Hedione (15x2) Best Value"} for i in a[0]]
        return real(model, method, *a, **kw)

    eng = Engine(write_cfg(tmp_path), sources=[OdooSource({"only_sales_teams": []}, rpc=rpc, sleep=lambda s: None)])
    eng.scan()
    assert by_name(eng)["WH/OUT/00073"].items[0].name == "Hedione (15x2) Best Value"
    lines = [c for c in fake.calls if c[0] == "sale.order.line"]
    assert len(lines) == 1 and lines[0][2][0] == [701]                        # only the catch-all line read


def test_item_without_sku_matches_the_label_list_by_name(tmp_path):
    cat = Catalog(tmp_path / "p.csv")
    cat.save([{"sku": "ML-HED-15", "title": "Hedione", "size": "15g", "icon": "corrosive"}])
    data, known = cat.lookup(Item("", "Hedione (15x2) Best Value", 1))
    assert known and (data.icon, data.size, data.bottles) == ("corrosive", "15g", 2)
    assert cat.lookup(Item("", "Hedione 15g", 1))[1]
    assert not cat.lookup(Item("", "Hedione 30g", 1))[1]                      # different size: no match
