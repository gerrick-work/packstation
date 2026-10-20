import json
from datetime import date

from packstation.engine import Engine
from packstation.sources.manual import ManualSource

from tests.test_fixes import pdf_bytes, write_cfg


def test_manual_order_prints_waybill_and_label(tmp_path):
    cfg = write_cfg(tmp_path)
    data = tmp_path / "data"
    (data / "manual").mkdir(parents=True, exist_ok=True)
    (data / "manual" / "SG1.pdf").write_bytes(pdf_bytes(100, 150))
    (data / "manual_orders.json").write_text(json.dumps([{
        "order_id": "261014JTXG926Q", "platform": "shopee SG", "tracking": "SG260080596808C",
        "waybill": "manual/SG1.pdf", "added": date.today().isoformat(),
        "items": [{"sku": "SP025-3G", "name": "Galaxolide 50 3g", "qty": 1}]}]))
    eng = Engine(cfg, sources=[ManualSource(data)])
    eng.scan()
    [o] = eng.orders
    assert o.platform == "shopee SG" and o.waybill_ready and o.match_code == "808C"
    r = eng.print_order(o)
    assert r.ok and r.labels == 1, r.message


def test_old_manual_orders_drop_off_and_no_file_is_fine(tmp_path):
    assert ManualSource(tmp_path).list_orders() == []
    (tmp_path / "manual_orders.json").write_text(json.dumps([{"order_id": "X", "added": "2020-01-01", "items": []}]))
    assert ManualSource(tmp_path).list_orders() == []


def test_combined_parcel_covers_two_odoo_deliveries(tmp_path):
    import json as _json
    from packstation.engine import Engine
    from packstation.sources.manual import ManualSource
    from packstation.sources.odoo import OdooSource
    from packstation.web import create_app
    from tests.test_fixes import FakeOdoo, pdf_bytes, write_cfg

    data = tmp_path / "data"
    (data / "manual").mkdir(parents=True)
    (data / "manual" / "combo.pdf").write_bytes(pdf_bytes())
    (data / "manual_orders.json").write_text(_json.dumps([{
        "order_id": "586590149256119944", "platform": "tiktok", "tracking": "MYNJV70014477258",
        "waybill": "manual/combo.pdf", "covers": ["WH/OUT/00073", "WH/OUT/00080"],
        "items": [{"sku": "SP030-3G", "name": "Hedione 3g", "qty": 1}, {"sku": "SP034-3G", "name": "Iso E Super 3g", "qty": 1}]}]))
    fake = FakeOdoo()
    validated = []
    fake.validate_calls = validated
    odoo = OdooSource({"only_sales_teams": []}, rpc=fake)
    odoo.validate = lambda pid: validated.append(pid)
    eng = Engine(write_cfg(tmp_path), sources=[ManualSource(str(data)), odoo])
    eng.scan()
    names = [o.order_id for o in eng.orders]
    assert "WH/OUT/00073" not in names and "WH/OUT/00080" not in names
    combo = next(o for o in eng.orders if o.order_id == "586590149256119944")
    assert combo.waybill_ready and len(combo.items) == 2
    assert sorted(combo.ref["odoo_picking_ids"]) == [73, 80]
    r = create_app(eng).test_client().post("/api/packed/" + combo.key, json={"packed": True}).get_json()
    assert r["validated"] and sorted(validated) == [73, 80]


def test_item_fixes_replace_placeholder_products(tmp_path):
    import json as _json
    from packstation.engine import Engine
    from packstation.sources.odoo import OdooSource
    from tests.test_fixes import FakeOdoo, write_cfg

    eng = Engine(write_cfg(tmp_path), sources=[OdooSource({"only_sales_teams": []}, rpc=FakeOdoo())])
    (tmp_path / "data").mkdir(exist_ok=True)
    (tmp_path / "data" / "item_fixes.json").write_text(_json.dumps(
        {"WH/OUT/00073": [{"sku": "SP030-15G", "name": "[SP030-15G] Hedione® 15g", "qty": 2}]}))
    eng.scan()
    o = next(x for x in eng.orders if x.order_id == "WH/OUT/00073")
    assert [(i.sku, i.qty) for i in o.items] == [("SP030-15G", 2)]


def test_label_renames_and_long_names_shrink_instead_of_dots(tmp_path):
    import json as _json
    from packstation.labels import Catalog, LabelData, render_product_label
    from packstation.models import Item

    (tmp_path / "label_names.json").write_text(_json.dumps(
        {"Orange Blossom (Neroli)": "Neroli", "EDP Fixative (WOODY)": "Woody Fixative",
         "Pink Pepper Essential Oil": "Pink Pepper EO"}))
    cat = Catalog(data_dir=tmp_path, default_brand="atlas origins")
    assert cat.lookup(Item("SP042-3G", "[SP042-3G] Orange Blossom (Neroli) 3g"))[0].title == "Neroli"
    d = cat.lookup(Item("", "EDP Fixative (WOODY) 15g"))[0]
    assert (d.title, d.size) == ("Woody Fixative", "15g")
    assert cat.lookup(Item("SP101-3G", "[SP101-3G] Pink Pepper Essential Oil 3g"))[0].title == "Pink Pepper EO"
    for t in ("Woody Fixative", "Pink Pepper EO", "Orange Blossom (Neroli)"):
        w = []
        render_product_label(LabelData(t, "3g", "atlas origins", "corrosive"), None, 40, 20, warnings=w)
        assert not w, (t, w)
