import pytest
import base64
try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib

from packstation.autosetup import assign, write_printer
from packstation.sources.odoo import OdooSource


def test_assign_uses_name_hints():
    found = [("A", "E40 Pro_A2AD", -50), ("B", "E40 Pro_0214", -60), ("C", "JBL Speaker", -30)]
    roles = assign(found, {"waybill": "0214", "label": "A2AD"})
    assert roles == {"waybill": ("B", "E40 Pro_0214"), "label": ("A", "E40 Pro_A2AD")}


def test_assign_falls_back_to_order_found():
    roles = assign([("A", "E40 Pro_X", -50)], {"waybill": "", "label": ""})
    assert roles == {"waybill": ("A", "E40 Pro_X")}


def test_write_printer_keeps_rest_of_config():
    text = '[app]\nhost = "0.0.0.0"\n\n[printers.label]\nbackend = "file"\nwidth_mm = 40\n\n[odoo]\nenabled = true\n'
    out = write_printer(text, "label", {"backend": "bluetooth", "ble_address": "X", "width_mm": 40.0, "invert": False})
    cfg = tomllib.loads(out)
    assert cfg["printers"]["label"] == {"backend": "bluetooth", "ble_address": "X", "width_mm": 40, "invert": False}
    assert cfg["odoo"]["enabled"] and cfg["app"]["host"] == "0.0.0.0"


class FakeOdoo(OdooSource):
    def __init__(self):
        super().__init__({"url": "https://x.odoo.com", "db": "x", "username": "u", "api_key": "k"})
        self._uid = 1

    def _x(self, model, method, *args, **kw):
        if method == "fields_get":
            return {"carrier_tracking_ref": {}, "name": {}}
        if model == "stock.picking":
            return [{"id": 5, "name": "WH/OUT/5", "origin": "S1", "scheduled_date": "2026-10-12 10:00:00",
                     "date_deadline": False, "sale_id": False, "move_ids": [9], "carrier_tracking_ref": "SPX123871A"}]
        if model == "stock.move":
            return [{"id": 9, "product_id": [3, "Amber 3g"], "product_uom_qty": 2.0}]
        if model == "product.product":
            return [{"id": 3, "default_code": "AMB3", "display_name": "Amber 3g"}]
        if model == "ir.attachment" and method == "search_read":
            return [{"id": 77, "res_id": 5, "name": "Shipping Label SPX123871A.pdf"}]
        if model == "ir.attachment" and method == "read":
            return [{"datas": base64.b64encode(b"%PDF-1.4 x").decode()}]
        raise AssertionError((model, method))


@pytest.mark.skip(reason="old waybill lookup by attachment name; replaced by the connector-field tests in test_fixes.py")
def test_odoo_reads_tracking_and_attached_waybill():
    src = FakeOdoo()
    [o] = src.list_orders()
    assert o.tracking == "SPX123871A" and o.match_code == "871A"
    assert o.has_waybill and o.items[0].qty == 2
    assert src.get_waybill(o).startswith(b"%PDF")
