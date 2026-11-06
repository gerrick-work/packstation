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


def test_direct_order_validates_odoo_delivery_outside_synced_teams(tmp_path):
    """Website / WhatsApp orders are on the 'Sales' team, which the Odoo source skips, so their
    delivery is never folded in by the scan. Mark Packed must still find it by name and validate it."""
    import json as _json
    from packstation.sources.odoo import OdooSource
    from packstation.web import create_app
    from tests.test_fixes import write_cfg

    data = tmp_path / "data"
    data.mkdir()
    (data / "manual_orders.json").write_text(_json.dumps([{
        "order_id": "S00157", "platform": "direct", "added": date.today().isoformat(),
        "covers": ["WH/OUT/00140"], "items": [{"sku": "SP008-3G", "name": "Cardamom 3g", "qty": 1}]}]))

    def rpc(model, method, *args, **kw):
        if model == "stock.picking" and method == "search_read" and args and args[0] == [("name", "in", ["WH/OUT/00140"])]:
            return [{"id": 141, "name": "WH/OUT/00140"}]
        return []

    odoo = OdooSource({"only_sales_teams": ["Shopee"]}, rpc=rpc)
    odoo.list_orders = lambda: []
    validated = []
    odoo.validate = lambda pid: validated.append(pid)
    odoo.deduct_real_items = lambda order: None
    eng = Engine(write_cfg(tmp_path), sources=[ManualSource(str(data)), odoo])
    eng.scan()
    [o] = eng.orders
    assert o.odoo_picking_id is None
    r = create_app(eng).test_client().post("/api/packed/" + o.key, json={"packed": True}).get_json()
    assert r["validated"] and validated == [141], r


def test_direct_order_with_unknown_delivery_is_not_marked_packed(tmp_path):
    import json as _json
    from packstation.sources.odoo import OdooSource
    from packstation.web import create_app
    from tests.test_fixes import write_cfg

    data = tmp_path / "data"
    data.mkdir()
    (data / "manual_orders.json").write_text(_json.dumps([{
        "order_id": "S00999", "platform": "direct", "added": date.today().isoformat(),
        "covers": ["WH/OUT/09999"], "items": [{"sku": "SP008-3G", "name": "Cardamom 3g", "qty": 1}]}]))
    odoo = OdooSource({"only_sales_teams": ["Shopee"]}, rpc=lambda *a, **k: [])
    odoo.list_orders = lambda: []
    eng = Engine(write_cfg(tmp_path), sources=[ManualSource(str(data)), odoo])
    eng.scan()
    [o] = eng.orders
    r = create_app(eng).test_client().post("/api/packed/" + o.key, json={"packed": True}).get_json()
    assert r["ok"] is False and o.key not in eng.store.packed()


def test_shopee_trial_set_expands_to_the_variant_bottles(tmp_path):
    import json as _json
    from packstation.models import Item, Order
    from packstation.sources.odoo import OdooSource

    (tmp_path / "bundles.json").write_text(_json.dumps({"555": {"match": "perfume trial set", "models": {
        "9": {"variant": "Set 5 + free Ylang", "items": [{"sku": "SP011-3G", "name": "Chrysanthemum 3g", "qty": 1},
                                                          {"sku": "SP050-3G", "name": "Ylang 3g", "qty": 1}]}}}}))
    odoo = OdooSource({"only_sales_teams": []}, rpc=lambda *a, **k: [])
    odoo.data_dir = str(tmp_path)

    class Api:
        def _details(self, sns):
            return [{"item_list": [{"item_id": 555, "model_id": 9, "model_quantity_purchased": 2},
                                   {"item_id": 1, "model_id": 2, "model_quantity_purchased": 1}]}]

    class Direct:
        api = Api()

        def _main(self):
            pass

    odoo._sp_api = Direct()
    o = Order(source="odoo", order_id="WH/OUT/1", platform="shopee", status="", ref={"sp_ref": "SN1"},
              items=[Item("", "Perfume Trial Set Fragrance Oil", 2), Item("SP008-3G", "Cardamom 3g", 1)])
    odoo._expand_bundles(o)
    assert [(i.sku, i.qty) for i in o.items] == [("SP008-3G", 1), ("SP011-3G", 2), ("SP050-3G", 2)]
