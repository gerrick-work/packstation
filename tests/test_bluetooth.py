import sys
import types

import pytest
from PIL import Image

from packstation import bluetooth
from packstation.printers import build_printer


class Char:
    def __init__(self, uuid, props):
        self.uuid, self.properties = uuid, props


class Svc:
    def __init__(self, uuid, chars):
        self.uuid, self.characteristics = uuid, chars


DEVICE_INFO = Svc("0000180a-0000-1000-8000-00805f9b34fb", [Char("00002a29-0000-1000-8000-00805f9b34fb", ["read", "write"])])


def test_picks_known_printer_characteristic():
    svcs = [DEVICE_INFO,
            Svc("0000ff00-0000-1000-8000-00805f9b34fb", [Char("0000ff01-0000-1000-8000-00805f9b34fb", ["notify"]),
                                                          Char("0000ff02-0000-1000-8000-00805f9b34fb", ["write", "write-without-response"])])]
    assert bluetooth.pick_write_char(svcs) == ("0000ff02-0000-1000-8000-00805f9b34fb", True)


def test_falls_back_to_any_writable_custom_characteristic():
    svcs = [DEVICE_INFO, Svc("e7810a71-73ae-499d-8c15-faa9aef0c3f2",
                             [Char("bef8d6c9-9c21-4c9e-b632-bd58c1009f9f", ["write"])])]
    assert bluetooth.pick_write_char(svcs) == ("bef8d6c9-9c21-4c9e-b632-bd58c1009f9f", False)


def test_no_writable_characteristic_is_a_clear_error():
    with pytest.raises(RuntimeError, match="nothing to print to"):
        bluetooth.pick_write_char([DEVICE_INFO])


def test_printer_names_sort_first():
    assert bluetooth.looks_like_printer("AiYin E40 Pro") and not bluetooth.looks_like_printer("Gerrick's AirPods")


@pytest.fixture
def fake_bleak(monkeypatch):
    written = []

    class Client:
        def __init__(self, dev):
            self.is_connected = False
            self.mtu_size = 185
            self.services = [DEVICE_INFO, Svc("49535343-fe7d-4ae5-8fa9-9fafd205e455", [
                Char("49535343-8841-43f4-a8d4-ecbe34729bb3", ["write", "write-without-response"])])]

        async def connect(self):
            self.is_connected = True

        async def write_gatt_char(self, uuid, data, response):
            written.append((uuid, bytes(data), response))

    class Scanner:
        @staticmethod
        async def find_device_by_address(addr, timeout=10):
            return object() if addr == "PRINTER-UUID" else None

    monkeypatch.setitem(sys.modules, "bleak", types.SimpleNamespace(BleakClient=Client, BleakScanner=Scanner))
    return written


def test_bluetooth_printer_sends_whole_tspl_job_in_chunks(fake_bleak, tmp_path):
    p = build_printer("label", {"backend": "bluetooth", "ble_address": "PRINTER-UUID", "ble_name": "AiYin",
                                "width_mm": 50, "height_mm": 25, "ble_delay_ms": 0}, str(tmp_path))
    assert p.print_image(Image.new("1", (400, 200), 1), "x") == "bluetooth AiYin"
    data = b"".join(d for _, d, _ in fake_bleak)
    assert data.startswith(b"SIZE 50 mm,25 mm") and data.endswith(b"PRINT 1,1\r\n")
    assert all(len(d) <= 180 for _, d, _ in fake_bleak) and len(fake_bleak) > 1
    assert {u for u, _, _ in fake_bleak} == {"49535343-8841-43f4-a8d4-ecbe34729bb3"}
    assert not any(r for _, _, r in fake_bleak)  # write-without-response used


def test_missing_printer_gives_plain_advice(fake_bleak, tmp_path):
    p = build_printer("label", {"backend": "bluetooth", "ble_address": "OTHER", "ble_delay_ms": 0}, str(tmp_path))
    from packstation.errors import PackError
    with pytest.raises(PackError, match="Label Expert") as e:
        p.print_image(Image.new("1", (400, 200), 1), "x")
    assert e.value.code == "BT-NOTFOUND"
