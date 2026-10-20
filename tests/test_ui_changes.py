"""Tests for the 20 Oct 2026 change list: order totals, reprinting one part of an order, and the
"Hold orders with missing info" switch."""
from packstation.engine import Engine
from packstation.models import Item, Order
from packstation.sources.demo import DemoSource
from packstation.sources.odoo import OdooSource

from test_fixes import FakeOdoo, by_name, write_cfg


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
            return [{"id": i, "client_order_ref": f"PLAT{i}", "amount_total": 88.5, "currency_id": [1, "MYR"]}
                    for i in a[0]]
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
