"""Atlas Origins website orders (Odoo eCommerce, sales team "Website"): listed once paid, bottle labels
printed, a waybill printed only when an SPX waybill PDF is attached to the delivery, and Mark Packed
asks for the SPX tracking number before validating (validating emails it to the customer)."""
from packstation.engine import Engine
from packstation.sources.odoo import OdooSource

from tests.test_fixes import LiveShapedOdoo, write_cfg


class WebsiteOdoo(LiveShapedOdoo):
    def __init__(self):
        super().__init__()
        base = dict(self.pickings[90])
        for pid, sid, name in ((95, 20, "WH/OUT/00173"), (96, 21, "WH/OUT/00174"), (97, 22, "WH/OUT/00175")):
            self.pickings[pid] = {**base, "id": pid, "name": name, "origin": f"S{sid}", "sale_id": [sid, f"S{sid}"],
                                  "move_ids": [pid], "carrier_tracking_ref": False}
        self.tags = {20: [], 21: [6], 22: [4]}        # 21 awaiting payment, 22 a test order
        self.written = []
        self.validated = []

    def __call__(self, model, method, *args, **kw):
        if model == "sale.order" and method == "read" and any(i >= 20 for i in args[0]):
            team = {9: "TikTok Shop", 10: "Shopee", 11: "Shopee", 12: "Sales", 20: "Website", 21: "Website",
                    22: "Website"}
            return [{"id": i, "client_order_ref": False, "team_id": [1, team[i]], "order_line": [i * 10],
                     "tag_ids": self.tags.get(i, []), "amount_total": 17.8, "currency_id": [1, "MYR"]}
                    for i in args[0]]
        if model == "crm.tag" and method == "read":
            names = {4: "Test", 5: "Receipt uploaded", 6: "Awaiting payment"}
            return [{"id": i, "name": names[i]} for i in args[0]]
        if model == "ir.attachment" and method == "search_read" and self.files:
            from tests.test_fixes import FakeOdoo
            return FakeOdoo.__call__(self, model, method, *args, **kw)
        if model == "ir.attachment" and method == "read":
            return [{"id": i, "raw": self.att[i]} for i in args[0]]
        if model == "stock.move" and method == "search_read":
            return []          # no "TikTok Shop Sale" placeholder moves on website deliveries
        if model == "stock.picking" and method == "write":
            self.written.append((args[0], args[1]))
            for i in args[0]:
                self.pickings[i].update(args[1])
            return True
        if model == "stock.picking" and method == "button_validate":
            self.validated.append(args[0][0])
            self.pickings[args[0][0]]["state"] = "done"
            return True
        return super().__call__(model, method, *args, **kw)


def test_paid_website_orders_listed_unpaid_and_test_orders_left_off():
    src = OdooSource({}, rpc=WebsiteOdoo())
    orders = {o.order_id: o for o in src.list_orders()}
    assert "WH/OUT/00173" in orders
    assert "WH/OUT/00174" not in orders           # Awaiting payment
    assert "WH/OUT/00175" not in orders           # Test
    assert "WH/OUT/00090" not in orders           # B2B Sales team still skipped
    web = orders["WH/OUT/00173"]
    assert web.platform == "website" and not web.has_waybill and not web.waybill_missing
    assert web.status == "website: book SPX"
    assert [i.name for i in web.items] == ["Bottle 1100 3g"]


def test_installed_config_with_old_team_list_still_gets_website():
    src = OdooSource({"only_sales_teams": ["Shopee", "TikTok Shop"]}, rpc=WebsiteOdoo())
    assert "WH/OUT/00173" in {o.order_id for o in src.list_orders()}


def test_attached_spx_waybill_is_printed_but_payment_receipt_is_not():
    fake = WebsiteOdoo()
    fake.files = [{"id": 777, "name": "receipt.pdf", "res_model": "sale.order", "res_id": 20,
                   "mimetype": "application/pdf", "create_date": "2026-11-05"}]
    web = {o.order_id: o for o in OdooSource({}, rpc=fake).list_orders()}["WH/OUT/00173"]
    assert not web.has_waybill
    fake.files.append({"id": 501, "name": "SPX MY06155308820B.pdf", "res_model": "stock.picking", "res_id": 95,
                       "mimetype": "application/pdf", "create_date": "2026-11-06"})
    src = OdooSource({}, rpc=fake)
    web = {o.order_id: o for o in src.list_orders()}["WH/OUT/00173"]
    assert web.has_waybill and web.waybill_ready and web.status == "website: SPX waybill attached"
    assert src.get_waybill(web).startswith(b"%PDF")


def test_print_website_order_prints_labels_only(tmp_path):
    fake = WebsiteOdoo()
    eng = Engine(write_cfg(tmp_path), sources=[OdooSource({}, rpc=fake)])
    eng.scan()
    o = next(o for o in eng.orders if o.order_id == "WH/OUT/00173")
    res = eng.print_order(o)
    assert res.ok and res.message.startswith("1 label")
    assert "odoo:WH/OUT/00173" in eng.store.printed()
    assert any("no SPX waybill" in m for m in eng.missing_info(o))


def test_mark_packed_asks_for_tracking_then_saves_and_validates(tmp_path):
    from packstation.web import create_app
    fake = WebsiteOdoo()
    eng = Engine(write_cfg(tmp_path), sources=[OdooSource({}, rpc=fake)])
    eng.scan()
    c = create_app(eng).test_client()
    key = "odoo:WH/OUT/00173"
    r = c.post(f"/api/packed/{key}", json={"packed": True}).get_json()
    assert r["needs_tracking"] and r["error"]["code"] == "TRACKING-NEEDED"
    assert fake.validated == [] and key not in eng.store.packed()
    r = c.post(f"/api/packed/{key}", json={"packed": True, "tracking": " my06155308820b "}).get_json()
    assert r["ok"] and r["validated"]
    assert fake.written == [([95], {"carrier_tracking_ref": "MY06155308820B"})]
    assert fake.validated == [95] and key in eng.store.packed()


def test_marketplace_orders_never_asked_for_tracking(tmp_path):
    from packstation.web import create_app
    fake = WebsiteOdoo()
    eng = Engine(write_cfg(tmp_path), sources=[OdooSource({}, rpc=fake)])
    eng.scan()
    c = create_app(eng).test_client()
    r = c.post("/api/packed/odoo:WH/OUT/00081", json={"packed": True}).get_json()
    assert not r.get("needs_tracking")
