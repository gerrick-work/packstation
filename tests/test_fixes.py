"""Tests for the 12 Oct 2026 Pack Station fix list (one section per fix)."""
import base64
import io
import time
try:
    import tomllib
except ModuleNotFoundError:  # Python 3.9 on the Mac
    import tomli as tomllib
import xmlrpc.client

import pytest
from PIL import Image, ImageDraw

from packstation import config as config_mod
from packstation.engine import Busy, Engine
from packstation.errors import PackError
from packstation.labels import LabelData, render_product_label
from packstation.models import Item, Order
from packstation.printers import build_printer, calibration_image, tspl_feed
from packstation.sources.demo import DemoSource
from packstation.sources.odoo import OdooSource


def pdf_bytes(w_mm=105, h_mm=148, bar_from_left_mm=10):
    """An A6 'waybill' PDF with a 1mm black bar a known distance from the left edge."""
    dpi = 203
    w, h = round(w_mm / 25.4 * dpi), round(h_mm / 25.4 * dpi)
    im = Image.new("L", (w, h), 255)
    x = round(bar_from_left_mm / 25.4 * dpi)
    ImageDraw.Draw(im).rectangle((x, 100, x + 7, 300), fill=0)
    buf = io.BytesIO()
    im.save(buf, "PDF", resolution=dpi)
    return buf.getvalue()


def write_cfg(tmp_path, extra=""):
    p = tmp_path / "config.toml"
    p.write_text(f'''[app]
data_dir = "{tmp_path / 'data'}"

[printers.waybill]
backend = "file"
width_mm = 100
height_mm = 150

[printers.label]
backend = "file"
width_mm = 40
height_mm = 20
{extra}''')
    return config_mod.load(p)


# ---------------------------------------------------------------- fake Odoo with both connectors

class FakeOdoo:
    def __init__(self):
        self.calls = []
        self.att = {501: base64.b64encode(pdf_bytes()).decode(), 777: base64.b64encode(pdf_bytes()).decode()}
        self.pickings = {
            73: {"id": 73, "name": "WH/OUT/00073", "origin": "S00073", "scheduled_date": "2026-10-12 10:00:00",
                 "date_deadline": False, "sale_id": [9, "S00073"], "move_ids": [1],
                 "carrier_tracking_ref": False, "tiktok_delivery_status": "confirmed",
                 "tiktok_label_attachment_id": False, "shopee_label_status": False, "shopee_label_attachment_id": False},
            80: {"id": 80, "name": "WH/OUT/00080", "origin": "S00080", "scheduled_date": "2026-10-12 09:00:00",
                 "date_deadline": False, "sale_id": [10, "S00080"], "move_ids": [2],
                 "carrier_tracking_ref": "SPXMY0601278A", "tiktok_delivery_status": False,
                 "tiktok_label_attachment_id": False, "shopee_label_status": "stored",
                 "shopee_label_attachment_id": [501, "label.pdf"]},
            81: {"id": 81, "name": "WH/OUT/00081", "origin": "S00081", "scheduled_date": "2026-10-12 11:00:00",
                 "date_deadline": False, "sale_id": [11, "S00081"], "move_ids": [3],
                 "carrier_tracking_ref": False, "tiktok_delivery_status": False,
                 "tiktok_label_attachment_id": False, "shopee_label_status": "pending",
                 "shopee_label_attachment_id": False},
            90: {"id": 90, "name": "WH/OUT/00090", "origin": "S00090", "scheduled_date": "2026-10-12 12:00:00",
                 "date_deadline": False, "sale_id": False, "move_ids": [4],
                 "carrier_tracking_ref": False, "tiktok_delivery_status": False,
                 "tiktok_label_attachment_id": False, "shopee_label_status": False,
                 "shopee_label_attachment_id": False},
        }
        for pk in self.pickings.values():
            pk.setdefault("state", "assigned")
            pk.setdefault("company_id", [1, "Atlas DSF (MY)"])
        self.cron_fail = None
        self.wizard_vals = None
        self.files = []  # plain attachments: {id, name, res_model, res_id, mimetype, create_date}

    def __call__(self, model, method, *args, **kw):
        self.calls.append((model, method, args, kw))
        if model == "stock.picking" and method == "fields_get":
            return {n: {"type": "many2one", "relation": "ir.attachment"} if n.endswith("attachment_id")
                    else {"type": "char"} for n in self.pickings[73]}
        if model == "res.users" and method == "read":
            return [{"id": 2, "company_ids": [1, 2]}]
        if model == "res.company" and method == "read":
            return [{"id": i, "name": {1: "Atlas DSF (MY)", 2: "Atlas DSF (SG)"}[i]} for i in args[0]]
        if model == "sale.order" and method == "search_read":
            return [{"id": 9, "company_id": [1, "Atlas DSF (MY)"], "team_id": [5, "Shopee MY"], "picking_ids": [80], "state": "sale"},
                    {"id": 30, "company_id": [2, "Atlas DSF (SG)"], "team_id": [6, "Shopee SG"], "picking_ids": [201], "state": "sale"},
                    {"id": 31, "company_id": [2, "Atlas DSF (SG)"], "team_id": [6, "Shopee SG"], "picking_ids": [], "state": "sale"}]
        if model == "stock.picking" and method == "read":
            return [dict(self.pickings[i], picking_type_code=self.pickings[i].get("picking_type_code", "outgoing"))
                    for i in args[0] if i in self.pickings]
        if model == "stock.picking" and method == "search_read":
            out = []
            for pk in self.pickings.values():
                ok = True
                for f, op, v in args[0]:
                    if f == "state" and op == "in" and pk["state"] not in v:
                        ok = False
                    if f == "state" and op == "not in" and pk["state"] in v:
                        ok = False
                    code = pk.get("picking_type_code", "outgoing")
                    if f == "picking_type_code" and ((op == "in" and code not in v) or (op == "=" and code != v)
                                                     or (op == "!=" and code == v)):
                        ok = False
                if kw.get("context", {}).get("allowed_company_ids") != [1, 2] and pk["company_id"][0] != 1:
                    ok = False  # like Odoo: other companies hidden unless allowed in the context
                if ok:
                    out.append(dict(pk, picking_type_code=pk.get("picking_type_code", "outgoing")))
            return out
        if model == "stock.move" and method == "read":
            return [{"id": i, "product_id": [100 + i, "x"], "product_uom_qty": 2.0 if i == 1 else 1.0} for i in args[0]]
        if model == "product.product" and method == "read":
            return [{"id": i, "default_code": f"ML-{i}", "display_name": f"Linalool {i} 3g"} for i in args[0]]
        if model == "sale.order" and method == "read":
            return [{"id": i, "client_order_ref": f"PLAT{i}"} for i in args[0]]
        if model == "ir.attachment" and method == "fields_get":
            return {"datas": {"type": "binary"}, "name": {"type": "char"}}
        if model == "ir.attachment" and method == "search_read":
            pick = sale = []
            dom = args[0]
            for i, t in enumerate(dom):
                if t == ("res_model", "=", "stock.picking"):
                    pick = dom[i + 1][2]
                if t == ("res_model", "=", "sale.order"):
                    sale = dom[i + 1][2]
            return [a for a in self.files if (a["res_model"] == "stock.picking" and a["res_id"] in pick)
                    or (a["res_model"] == "sale.order" and a["res_id"] in sale)]
        if model == "ir.attachment" and method == "read":
            return [{"id": i, "datas": self.att[i], "mimetype": "application/pdf", "name": "l.pdf"} for i in args[0]]
        if model == "stock.picking" and method == "action_tiktok_ship_order":
            return {"type": "ir.actions.act_window", "res_model": "tiktok.picking.ship.wizard",
                    "context": {"default_picking_id": args[0][0]}}
        if model == "tiktok.picking.ship.wizard" and method == "fields_get":
            return {"handover_method": {"type": "selection", "selection": [["drop_off", "Drop off"], ["pickup", "Pickup"]]},
                    "note": {"type": "char"}}
        if model == "tiktok.picking.ship.wizard" and method == "create":
            self.wizard_vals = args[0]
            return 5
        if model == "tiktok.picking.ship.wizard" and method == "get_views":
            return {"views": {"form": {"arch": '<form><footer><button name="action_cancel" type="object"/>'
                                               '<button name="action_confirm_ship" type="object" class="btn-primary"/></footer></form>'}}}
        if model == "tiktok.picking.ship.wizard" and method == "action_confirm_ship":
            p = self.pickings[73]
            p.update(tiktok_delivery_status="ready_to_ship", carrier_tracking_ref="MYTT00099X")
            return True
        if model == "stock.picking" and method == "action_tiktok_sync_pickings":
            p = self.pickings[73]
            if p["tiktok_delivery_status"] == "ready_to_ship":
                p["tiktok_label_attachment_id"] = [777, "tt.pdf"]
            return True
        if model == "ir.cron" and method == "method_direct_trigger":
            if self.cron_fail:
                raise self.cron_fail
            return True
        if model == "ir.cron" and method == "read":
            return [{"id": i, "cron_name": {73: "Shopee: sync orders", 77: "TikTok: sync orders",
                                            74: "Shopee: sync shipping labels"}[i]} for i in args[0]]
        if method == "message_post":
            return 1
        raise AssertionError((model, method))


@pytest.fixture
def odoo_engine(tmp_path):
    fake = FakeOdoo()
    src = OdooSource({"only_sales_teams": []}, rpc=fake, sleep=lambda s: None)
    eng = Engine(write_cfg(tmp_path), sources=[src])
    eng.scan()
    return eng, fake


def by_name(eng):
    return {o.order_id: o for o in eng.orders}


# ---------------------------------------------------------------- 1. margins / template / native waybill

def test_offsets_move_the_print_and_calibration_page(tmp_path):
    p = build_printer("label", {"backend": "file", "width_mm": 40, "height_mm": 20, "offset_x_mm": 2,
                                "offset_y_mm": -1}, str(tmp_path))
    im = Image.new("L", (320, 160), 255)
    ImageDraw.Draw(im).rectangle((0, 20, 3, 23), fill=0)
    page = p.prepare(im)
    xs = [x for x in range(page.width) for y in range(page.height) if page.getpixel((x, y)) == 0]
    assert min(xs) == round(2 / 25.4 * 203)                  # 2 mm right
    cal = calibration_image(320, 160, 203, "X +0.0")
    assert cal.size == (320, 160) and cal.getpixel((0, 80)) == 0   # border at the edge


def test_waybill_prints_at_native_size_not_rescaled(tmp_path):
    p = build_printer("waybill", {"backend": "file", "width_mm": 100, "height_mm": 150}, str(tmp_path))
    from packstation.printers import pdf_to_image
    page = p.prepare(pdf_to_image(pdf_bytes(bar_from_left_mm=10), 203), native=True)
    assert page.size == p.dots == (799, 1199)               # 100 x 150 mm at 203 dpi
    xs = [x for x in range(page.width) if page.getpixel((x, 200)) == 0]
    # A6 is 105mm wide, so 2.5mm is trimmed from each side: bar lands at 10 - 2.5 = 7.5mm, width kept (8 dots)
    assert abs(min(xs) - round(7.5 / 25.4 * 203)) <= 1 and 7 <= len(xs) <= 9


def test_bottle_label_template_is_fixed_whatever_the_title():
    a = render_product_label(LabelData("Sandalwood", "3g", "atlas origins", ""), "278A", 40, 20)
    b = render_product_label(LabelData("Isoamyl Acetate Natural", "3g", "atlas origins", ""), "278A", 40, 20)
    c = render_product_label(LabelData("A Very Long Material Name Indeed", "3g", "atlas origins", ""), "278A", 40, 20)
    lower = (0, 78, 320, 160)   # size line, brand line, match code: identical positions
    assert a.crop(lower).tobytes() == b.crop(lower).tobytes() == c.crop(lower).tobytes()


def test_long_label_title_wraps_in_full_never_ellipsized():
    from PIL import Image, ImageDraw
    from packstation.labels import fit_title
    d = ImageDraw.Draw(Image.new("L", (10, 10)))
    for name in ["A Very Long Material Name Indeed", "Organic Tea Tree Essential Oil", "Hydroxycitronellalextraordinaire"]:
        lines, font = fit_title(d, name, None, 34, 288, 66)
        assert "".join(lines).replace(" ", "") == name.replace(" ", "")
        assert not any("\u2026" in x or "..." in x for x in lines)
        assert all(d.textlength(x, font=font) <= 288 for x in lines)


def test_default_brand_is_atlas_origins(tmp_path):
    from packstation.labels import Catalog
    cat = Catalog(data_dir=tmp_path)
    data, _ = cat.lookup(Item("NEW-SKU", "Vanillin 10g", 1))
    assert data.brand == "atlas origins" and (tmp_path / "products.csv").exists()


def test_printer_settings_saved_from_ui_hot_reload(tmp_path):
    from packstation.web import create_app
    eng = Engine(write_cfg(tmp_path), sources=[DemoSource()])
    c = create_app(eng).test_client()
    r = c.post("/api/printer/label", json={"offset_x_mm": 0.5, "offset_y_mm": -0.5, "gap_mm": 3, "separator": "none"})
    assert r.get_json()["ok"]
    saved = tomllib.loads((tmp_path / "config.toml").read_text())["printers"]["label"]
    assert saved["offset_x_mm"] == 0.5 and saved["gap_mm"] == 3 and saved["separator"] == "none"
    assert eng.label_printer.offset_x_mm == 0.5 and eng.label_cfg["separator"] == "none"
    assert c.post("/api/printer/label", json={"offset_x_mm": 99}).status_code == 400
    job = c.post("/api/calibrate/label").get_json()["job"]
    eng.wait(eng.jobs[0])
    assert eng.jobs[0].state == "done" and "calibration" in eng.jobs[0].summary.lower(), job


# ---------------------------------------------------------------- 2. TikTok / Shopee waybills from connector fields

def test_waybills_come_from_connector_fields(odoo_engine):
    eng, fake = odoo_engine
    o = by_name(eng)
    assert o["WH/OUT/00080"].platform == "shopee" and o["WH/OUT/00080"].waybill_ready
    assert o["WH/OUT/00073"].platform == "tiktok" and o["WH/OUT/00073"].needs_arrange
    assert o["WH/OUT/00081"].waybill_missing                 # shopee label not "stored"
    assert not o["WH/OUT/00090"].has_waybill                  # Odoo-only order
    assert eng.odoo.get_waybill(o["WH/OUT/00080"])[:4] == b"%PDF"


def test_tiktok_needs_on_screen_confirmation_then_books_courier(odoo_engine, tmp_path):
    eng, fake = odoo_engine
    assert "odoo:WH/OUT/00073" in eng.plan()["arrange"]
    eng.wait(eng.start_print_all(arrange=False))
    assert not any(c[1] == "action_tiktok_ship_order" for c in fake.calls)   # nothing booked without a yes
    assert "odoo:WH/OUT/00073" not in eng.store.printed()

    job = eng.wait(eng.start_print_one("odoo:WH/OUT/00073", arrange=True))
    assert job.state == "done", job.as_dict()
    assert fake.wizard_vals == {"handover_method": "drop_off"}               # ship method from settings
    assert [c[1] for c in fake.calls if c[0] == "tiktok.picking.ship.wizard"][-1] == "action_confirm_ship"
    assert eng.store.printed_rows()["odoo:WH/OUT/00073"]["tracking"] == "MYTT00099X"
    assert by_name(eng)["WH/OUT/00073"].match_code == "099X"


def test_tiktok_label_never_arrives_gives_tiktok_arrange(odoo_engine):
    eng, fake = odoo_engine
    fake.att.pop(777)
    orig = fake.__call__

    def no_label(model, method, *a, **kw):
        if method == "action_tiktok_sync_pickings":
            fake.calls.append((model, method, a, kw))
            return True
        return orig(model, method, *a, **kw)

    eng.odoo._rpc = no_label
    job = eng.wait(eng.start_print_one("odoo:WH/OUT/00073", arrange=True))
    assert job.error["code"] == "TIKTOK-ARRANGE"
    assert "odoo:WH/OUT/00073" not in eng.store.printed()


# ---------------------------------------------------------------- 3. no-waybill orders never "printed"

def test_waybill_missing_never_printed_marked_or_noted(odoo_engine, tmp_path):
    eng, fake = odoo_engine
    job = eng.wait(eng.start_print_all())
    res = {r.key: r for r in job.results}
    assert res["odoo:WH/OUT/00081"].error["code"] == "WAYBILL-MISSING"
    printed = eng.store.printed()
    assert "odoo:WH/OUT/00081" not in printed and "odoo:WH/OUT/00073" not in printed
    assert "odoo:WH/OUT/00080" in printed and "odoo:WH/OUT/00090" in printed
    notes = [c for c in fake.calls if c[1] == "message_post"]
    assert [c[2][0] for c in notes] == [[80]]                 # only the order with a real waybill
    assert "SPXMY0601278A" in notes[0][3]["body"] and "#278A" in notes[0][3]["body"]
    assert by_name(eng)["WH/OUT/00081"].match_code == ""        # no made-up code like #0081


def test_bad_printed_record_for_00073_is_removed(tmp_path):
    from packstation.store import Store
    (tmp_path / "data").mkdir()
    st = Store(tmp_path / "data")
    st.mark("odoo:WH/OUT/00073", None, 2)
    st.mark("odoo:WH/OUT/00011", None, 1)
    eng = Engine(write_cfg(tmp_path), sources=[OdooSource({"only_sales_teams": []}, rpc=FakeOdoo())])
    assert "odoo:WH/OUT/00073" not in eng.store.printed()
    assert "odoo:WH/OUT/00011" in eng.store.printed()            # unrelated record left alone


# ---------------------------------------------------------------- 4. refresh runs the whole chain

def test_refresh_triggers_crons_in_order_then_rescans(odoo_engine):
    eng, fake = odoo_engine
    fake.calls.clear()
    job = eng.wait(eng.start_refresh())
    seq = [(c[1], c[2][0] if c[2] else None) for c in fake.calls
           if c[1] in ("method_direct_trigger", "action_tiktok_sync_pickings")]
    assert seq == [("method_direct_trigger", [73]), ("method_direct_trigger", [77]),
                   ("action_tiktok_sync_pickings", [73]), ("method_direct_trigger", [74])]
    assert job.state == "done" and job.summary.startswith("Refresh successful") and "0 new orders" in job.summary
    assert [s["state"] for s in job.steps] == ["done"] * 5
    assert job.steps[0]["name"] == "Odoo: Shopee: sync orders"


def test_refresh_without_admin_rights_is_odoo_cron(odoo_engine):
    eng, fake = odoo_engine
    fake.cron_fail = xmlrpc.client.Fault(1, "AccessError: You are not allowed to modify 'Scheduled Actions'")
    job = eng.wait(eng.start_refresh())
    assert job.state == "error" and job.error["code"] == "ODOO-CRON"
    assert "Administrator" in job.error["detail"] and job.steps[0]["state"] == "error"
    assert not any(c[1] == "action_tiktok_sync_pickings" for c in fake.calls[-3:])   # stopped at the failed step


# ---------------------------------------------------------------- 5. blank label between orders

def test_blank_label_after_each_order(tmp_path):
    eng = Engine(write_cfg(tmp_path), sources=[DemoSource()])
    eng.scan()
    eng.wait(eng.start_print_all())
    files = sorted(p.name for p in (tmp_path / "data" / "output").glob("*/label/*"))
    seps = [i for i, n in enumerate(files) if "blank-label" in n]
    assert len(seps) == 3                                   # one after each of the 3 orders
    assert "DEMO10001" in files[seps[0] - 1] and "DEMO10002" in files[seps[0] + 1]
    assert tspl_feed(40, 20, 2).endswith(b"FORMFEED\r\n")


def test_separator_none(tmp_path):
    eng = Engine(write_cfg(tmp_path, 'separator = "none"'), sources=[DemoSource()])
    eng.scan()
    eng.wait(eng.start_print_all())
    assert not list((tmp_path / "data" / "output").glob("*/label/*blank-label*"))


# ---------------------------------------------------------------- 6. status + no double printing

def test_order_lock_blocks_double_print(tmp_path):
    eng = Engine(write_cfg(tmp_path), sources=[DemoSource()])
    eng.scan()
    gate = __import__("threading").Event()
    eng.submit("hold", "hold", lambda job: gate.wait(5), {"demo:DEMO10001"})
    with pytest.raises(Busy):
        eng.start_print_one("demo:DEMO10001")
    assert eng.state_of(eng.orders[0], {}, {}) == "busy"
    gate.set()


def test_marked_printed_only_after_whole_order_sent(tmp_path):
    eng = Engine(write_cfg(tmp_path), sources=[DemoSource()])
    eng.scan()
    sent = []
    real = eng.label_printer._send_page

    def flaky(page, label):
        sent.append(label)
        if len(sent) == 2:
            raise PackError("BT-SEND", "sending to printer")
        return real(page, label)

    eng.label_printer._send_page = flaky
    job = eng.wait(eng.start_print_one("demo:DEMO10001"))
    assert job.error["code"] == "PRINT-PARTIAL" and "label 2 of 2" in job.error["step"]
    assert "demo:DEMO10001" not in eng.store.printed()
    assert eng.label_printer.status.state in ("idle", "done", "sending")


def test_printer_status_reports_progress(tmp_path):
    from packstation.printers import BluetoothPrinter

    class Link:
        def send(self, data, progress):
            progress("connecting", 0)
            progress("sending", 50)
            progress("sending", 100)

        def close(self):
            pass

    p = BluetoothPrinter("label", {"ble_address": "X", "width_mm": 40, "height_mm": 20}, str(tmp_path), link=Link())
    p.print_image(Image.new("L", (320, 160), 255), "x", detail="Order #278A, label 2 of 3")
    st = p.status.as_dict()
    assert st["state"] == "done" and st["detail"] == "Order #278A, label 2 of 3" and st["at"]


# ---------------------------------------------------------------- 7. error codes and log

def test_errors_are_logged_with_codes(odoo_engine, tmp_path):
    eng, fake = odoo_engine
    eng.wait(eng.start_print_all())
    log = (tmp_path / "data" / "packstation.log").read_text()
    assert "WAYBILL-MISSING" in log
    assert any(i["code"] == "WAYBILL-MISSING" for i in eng.log.recent())


def test_odoo_unreachable_is_odoo_net(tmp_path):
    def down(*a, **k):
        raise PackError("ODOO-NET", "reading Odoo", "connection refused")
    eng = Engine(write_cfg(tmp_path), sources=[OdooSource({"only_sales_teams": []}, rpc=down)])
    eng.scan()
    assert eng.source_errors["odoo"]["code"] == "ODOO-NET"


# ---------------------------------------------------------------- 8. multi-device

def test_home_screen_app_qr_pin_and_restart(tmp_path, monkeypatch):
    from packstation import web
    eng = Engine(write_cfg(tmp_path), sources=[DemoSource()])
    c = web.create_app(eng).test_client()
    assert c.get("/manifest.webmanifest").get_json()["display"] == "standalone"
    assert c.get("/icon-192.png").mimetype == "image/png"
    assert c.get("/api/qr.png").mimetype == "image/png"
    assert "addresses" in c.get("/api/state").get_json()

    assert c.post("/api/settings", json={"pin": "12a"}).status_code == 400
    assert c.post("/api/settings", json={"pin": "4321"}).get_json()["ok"]
    remote = {"REMOTE_ADDR": "192.168.1.20"}
    assert c.get("/api/state", environ_base=remote).status_code == 401
    assert "PIN" in c.get("/", environ_base=remote).get_data(as_text=True)
    assert c.post("/api/login", json={"pin": "0000"}, environ_base=remote).status_code == 403
    r = c.post("/api/login", json={"pin": "4321"}, environ_base=remote)
    assert r.status_code == 200
    assert c.get("/api/state", environ_base=remote).status_code == 200      # cookie now set
    assert c.get("/api/state").status_code == 200                             # the Mac itself never asks

    called = []
    monkeypatch.setattr(web.os, "execv", lambda *a: called.append(a))
    assert c.post("/api/restart").get_json()["ok"]
    time.sleep(1.2)
    assert called and called[0][1][1:3] == ["-m", "packstation"]


# ---------------------------------------------------------------- deploy: update from the UI, brand clean-up

def test_install_update_zip_keeps_mac_files(tmp_path):
    import zipfile
    from packstation.updater import install_zip

    root = tmp_path / "PackStation"
    (root / "labels").mkdir(parents=True)
    (root / "config.toml").write_text("SECRET")
    (root / "labels" / "products.csv").write_text("sku,title,size,brand,icon\nMY-EDIT,Mine,3g,,\n")
    (root / "requirements.txt").write_text("flask\n")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("atlasdsf-branch/packstation/__main__.py", "print('new')\n")
        z.writestr("atlasdsf-branch/labels/products.csv", "sku,title,size,brand,icon\nSEED,Seed,3g,,\n")
        z.writestr("atlasdsf-branch/config.toml", "OVERWRITE")
        z.writestr("atlasdsf-branch/data/packstation.db", "OVERWRITE")
        z.writestr("atlasdsf-branch/requirements.txt", "flask\n")
    info = install_zip(buf.getvalue(), root=root, pip=False)
    assert (root / "packstation" / "__main__.py").read_text() == "print('new')\n"
    assert (root / "config.toml").read_text() == "SECRET"                       # kept
    assert not (root / "data" / "packstation.db").exists()                      # data/ never touched
    assert "MY-EDIT" in (root / "data" / "products.csv").read_text()            # edits moved to data/ first
    assert info["files"] == 3 and not info["installed_packages"]
    with pytest.raises(PackError):
        install_zip(b"not a zip", root=root, pip=False)


def test_update_only_from_the_mac_itself(tmp_path):
    from packstation import web
    eng = Engine(write_cfg(tmp_path), sources=[DemoSource()])
    c = web.create_app(eng).test_client()
    r = c.post("/api/update", environ_base={"REMOTE_ADDR": "192.168.1.20"})
    assert r.status_code == 403


def test_moleculelabs_brand_switched_to_atlas_origins_once(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "products.csv").write_text("sku,title,size,brand,icon\nA,Sandalwood,3g,moleculelabs,\nB,Rose,3g,other brand,\n")
    cfg = write_cfg(tmp_path)
    config_mod.set_values(cfg["_path"], "labels", {"brand": "moleculelabs"})
    eng = Engine(config_mod.load(cfg["_path"]), sources=[DemoSource()])
    rows = {r["sku"]: r["brand"] for r in eng.catalog.all_rows()}
    assert rows == {"A": "atlas origins", "B": "other brand"}
    assert eng.catalog.default_brand == "atlas origins"
    assert tomllib.loads((tmp_path / "config.toml").read_text())["labels"]["brand"] == "atlas origins"


def test_tracking_falls_back_to_connector_field():
    fake = FakeOdoo()
    p = fake.pickings[80]
    p["carrier_tracking_ref"] = False
    p["shopee_tracking_number"] = "SPXMY0999111Z"
    for q in fake.pickings.values():
        q.setdefault("shopee_tracking_number", False)
    src = OdooSource({"only_sales_teams": []}, rpc=fake)
    o = {x.order_id: x for x in src.list_orders()}["WH/OUT/00080"]
    assert o.tracking == "SPXMY0999111Z" and o.match_code == "111Z"


class LiveShapedOdoo(FakeOdoo):
    """Matches atlas-dsf.odoo.com (saas~19.4): kit products put raw grams on the delivery, the Shopee
    label is a plain attachment named Shopee_Label_<delivery>.pdf, attachments use "raw", and every
    delivery carries shopee_label_status "not available" by default."""

    def __init__(self):
        super().__init__()
        for p in self.pickings.values():
            p.pop("shopee_label_attachment_id", None)
            if not p["shopee_label_status"]:
                p["shopee_label_status"] = "not available"
        self.pickings[90]["sale_id"] = [12, "S00090"]   # bulk B2B order in the Sales team

    def __call__(self, model, method, *args, **kw):
        if model == "sale.order" and method == "read":
            team = {9: "TikTok Shop", 10: "Shopee", 11: "Shopee", 12: "Sales"}
            return [{"id": i, "client_order_ref": False, "team_id": [1, team[i]], "order_line": [i * 10]}
                    for i in args[0]]
        if model == "sale.order.line" and method == "read":
            return [{"id": l, "order_id": [l // 10, "S"], "product_id": [900 + l, "x"], "product_uom_qty": 1.0,
                     "display_type": False, "is_delivery": False} for l in args[0]]
        if model == "product.product" and method == "read" and args[0] and args[0][0] >= 900:
            return [{"id": i, "default_code": f"SP-{i}-3G", "display_name": f"Bottle {i} 3g", "type": "consu"}
                    for i in args[0]]
        if model == "stock.move" and method == "read":
            return [{"id": i, "product_id": [100 + i, "x"], "product_uom_qty": 0.3} for i in args[0]]
        if model == "ir.attachment" and method == "fields_get":
            return {"raw": {"type": "binary"}}
        if model == "ir.attachment" and method == "search_read":
            return [{"id": 501, "res_id": 80, "res_model": "stock.picking", "name": "Shopee_Label_WH-OUT-00080.pdf",
                     "mimetype": "application/pdf", "create_date": "2026-10-12"}]
        if model == "ir.attachment" and method == "read":
            return [{"id": i, "raw": self.att[i]} for i in args[0]]
        return super().__call__(model, method, *args, **kw)


def test_live_odoo_shape_bottles_teams_and_shopee_label():
    src = OdooSource({}, rpc=LiveShapedOdoo())
    orders = {o.order_id: o for o in src.list_orders()}
    assert "WH/OUT/00090" not in orders                       # Sales-team bulk order skipped
    assert orders["WH/OUT/00073"].platform == "tiktok"
    shp = orders["WH/OUT/00080"]
    assert shp.platform == "shopee" and shp.waybill_ready
    assert [(i.name, i.qty) for i in shp.items] == [("Bottle 1000 3g", 1)]   # bottle, not 0.3 g
    assert src.get_waybill(shp).startswith(b"%PDF")


def test_odoo_sku_prefix_left_off_label_title():
    from packstation.labels import Catalog
    from packstation.models import Item
    data, known = Catalog(None, "atlas origins").lookup(Item("SP037-3G", "[SP037-3G] Labdanum 3g"))
    assert (data.title, data.size, known) == ("Labdanum", "3g", False)


def test_bottle_labels_have_no_match_code(tmp_path):
    from packstation.labels import Catalog, labels_for_order
    order = Order("demo", "D1", [Item("X", "Sandalwood 3g", 1)], tracking="SPXMY0601278A")
    cat = Catalog(data_dir=tmp_path)
    with_setting = labels_for_order(order, cat, {"width_mm": 40, "height_mm": 20, "match_code": True})[0][0]
    plain = render_product_label(LabelData("Sandalwood", "3g", "atlas origins", "corrosive"), None, 40, 20)
    assert with_setting.tobytes() == plain.tobytes()       # old "match_code = true" settings are ignored



# ---------------------------------------------------------------- Singapore orders (second Odoo company)

def add_singapore(fake):
    base = dict(fake.pickings[80])
    fake.pickings[200] = {**base, "id": 200, "name": "SG/OUT/00012", "carrier_tracking_ref": "SPXSG0400555Q",
                          "company_id": [2, "Atlas DSF (SG)"], "shopee_label_attachment_id": [501, "sg.pdf"]}
    fake.pickings[201] = {**base, "id": 201, "name": "SG/OUT/00013", "carrier_tracking_ref": "SPXSG0400666R",
                          "company_id": [2, "Atlas DSF (SG)"], "state": "confirmed",
                          "shopee_label_attachment_id": [501, "sg.pdf"]}


def test_singapore_company_orders_are_scanned():
    fake = FakeOdoo()
    add_singapore(fake)
    names = {o.order_id for o in OdooSource({"only_sales_teams": []}, rpc=fake).list_orders()}
    assert "SG/OUT/00012" in names                      # second company now read
    assert "SG/OUT/00013" not in names                  # Waiting: only with the setting below
    sr = [c for c in fake.calls if c[1] == "search_read"][-1]
    assert sr[3]["context"] == {"allowed_company_ids": [1, 2]}


def test_ready_plus_waiting_setting_includes_waiting_deliveries():
    fake = FakeOdoo()
    add_singapore(fake)
    names = {o.order_id for o in OdooSource({"only_sales_teams": [], "picking_states": ["assigned", "confirmed", "waiting"]},
                                           rpc=fake).list_orders()}
    assert {"SG/OUT/00012", "SG/OUT/00013"} <= names


def test_odoo_check_explains_where_orders_are(tmp_path):
    from packstation.web import create_app
    fake = FakeOdoo()
    add_singapore(fake)
    eng = Engine(write_cfg(tmp_path), sources=[OdooSource({"only_sales_teams": []}, rpc=fake)])
    c = create_app(eng).test_client()
    r = c.get("/api/odoo-check").get_json()
    sg_wait = [x for x in r["deliveries"] if x["company"] == "Atlas DSF (SG)" and x["state"] == "confirmed"]
    assert sg_wait and sg_wait[0]["count"] == 1 and not sg_wait[0]["listed"] and sg_wait[0]["type"] == "outgoing"
    assert r["companies"] == ["Atlas DSF (MY)", "Atlas DSF (SG)"]
    sg = next(x for x in r["sales"] if x["team"] == "Shopee SG")
    assert sg == {"company": "Atlas DSF (SG)", "team": "Shopee SG", "orders": 2, "listed": 0, "no_delivery": 1,
                  "team_skipped": False}
    assert c.get("/api/state").get_json()["version"]


def test_dropship_type_can_be_included():
    fake = FakeOdoo()
    add_singapore(fake)
    fake.pickings[200]["picking_type_code"] = "dropship"
    assert "SG/OUT/00012" not in {o.order_id for o in OdooSource({"only_sales_teams": []}, rpc=fake).list_orders()}
    src = OdooSource({"only_sales_teams": [], "picking_type_codes": ["outgoing", "dropship"]}, rpc=fake)
    assert "SG/OUT/00012" in {o.order_id for o in src.list_orders()}


def test_waiting_switch_saves_from_settings(tmp_path):
    from packstation.web import create_app
    eng = Engine(write_cfg(tmp_path), sources=[DemoSource()])
    c = create_app(eng).test_client()
    assert c.post("/api/settings", json={"include_waiting": True}).get_json()["ok"]
    saved = tomllib.loads((tmp_path / "config.toml").read_text())["odoo"]["picking_states"]
    assert saved == ["assigned", "confirmed", "waiting"]


def test_dropship_switch_saves_from_settings(tmp_path):
    from packstation.web import create_app
    eng = Engine(write_cfg(tmp_path), sources=[DemoSource()])
    c = create_app(eng).test_client()
    assert c.post("/api/settings", json={"include_dropship": True}).get_json()["ok"]
    saved = tomllib.loads((tmp_path / "config.toml").read_text())["odoo"]["picking_type_codes"]
    assert saved == ["outgoing", "dropship"]


def test_odoo_check_flags_shop_skipped_by_sales_team(tmp_path):
    from packstation.web import create_app
    fake = FakeOdoo()
    add_singapore(fake)
    eng = Engine(write_cfg(tmp_path), sources=[OdooSource({}, rpc=fake)])   # default: Shopee / TikTok Shop teams only
    r = create_app(eng).test_client().get("/api/odoo-check").get_json()
    sg = next(x for x in r["sales"] if x["team"] == "Shopee SG")
    assert sg["team_skipped"] and sg["listed"] == 0
    assert r["teams"] == ["shopee", "tiktok shop"]


# ---------------------------------------------------------------- TikTok waybills saved elsewhere

def tiktok_ready(fake, pid=73):
    fake.pickings[pid].update(tiktok_delivery_status="ready_to_ship", carrier_tracking_ref=False)


def test_tiktok_waybill_saved_as_plain_attachment_is_found(tmp_path):
    fake = FakeOdoo()
    tiktok_ready(fake)
    fake.att[900] = base64.b64encode(pdf_bytes()).decode()
    fake.files = [{"id": 899, "name": "S00073.pdf", "res_model": "sale.order", "res_id": 9,
                   "mimetype": "application/pdf", "create_date": "2026-10-14"},
                  {"id": 900, "name": "TikTok Shipping Label.pdf", "res_model": "stock.picking", "res_id": 73,
                   "mimetype": "application/pdf", "create_date": "2026-10-14"}]
    src = OdooSource({"only_sales_teams": []}, rpc=fake, sleep=lambda s: None)
    eng = Engine(write_cfg(tmp_path), sources=[src])
    eng.scan()
    o = by_name(eng)["WH/OUT/00073"]
    assert o.waybill_ready and not o.needs_arrange
    assert o.ref["label"] == ["att", 900]
    assert src.get_waybill(o)[:4] == b"%PDF"
    job = eng.wait(eng.start_print_one(o.key))  # prints even though Odoo has no tracking number yet
    assert not job.error
    assert o.key in eng.store.printed()


def test_tiktok_waybill_in_a_differently_named_file_field(tmp_path):
    fake = FakeOdoo()
    tiktok_ready(fake)
    for pk in fake.pickings.values():
        pk.pop("tiktok_label_attachment_id")
        pk["tiktok_shipping_label"] = False
    fake.pickings[73]["tiktok_shipping_label"] = "12 kb"  # Odoo sends the size when bin_size is on

    class Fake(FakeOdoo):
        def __call__(self, model, method, *args, **kw):
            if model == "stock.picking" and method == "fields_get":
                d = super().__call__(model, method, *args, **kw)
                d["tiktok_shipping_label"] = {"type": "binary"}
                return d
            if model == "stock.picking" and method == "read" and kw.get("fields") == ["tiktok_shipping_label"]:
                return [{"id": 73, "tiktok_shipping_label": base64.b64encode(pdf_bytes()).decode()}]
            return super().__call__(model, method, *args, **kw)
    f2 = Fake()
    f2.pickings = fake.pickings
    src = OdooSource({"only_sales_teams": []}, rpc=f2, sleep=lambda s: None)
    orders = {o.order_id: o for o in src.list_orders()}
    o = orders["WH/OUT/00073"]
    assert src.f_tt_label == "tiktok_shipping_label"
    assert o.waybill_ready and o.ref["label"] == ["bin", "tiktok_shipping_label"]
    assert src.get_waybill(o)[:4] == b"%PDF"
    assert any(c[2] == [73] or c[3].get("context", {}).get("bin_size") for c in f2.calls
               if c[0] == "stock.picking" and c[1] == "search_read")


def test_odoo_check_reports_waybills_per_platform():
    fake = FakeOdoo()
    src = OdooSource({"only_sales_teams": []}, rpc=fake, sleep=lambda s: None)
    w = {g["platform"]: g for g in src.check()["waybills"]}
    assert w["shopee"]["total"] == 2 and w["shopee"]["found"] == 1
    assert w["tiktok"]["total"] == 1 and w["tiktok"]["found"] == 0
    assert w["tiktok"]["missing"] == ["WH/OUT/00073 (tiktok: confirmed)"]
    assert "tiktok_label_attachment_id" in w["tiktok"]["fields"]
