"""Bluetooth Low Energy link to a label printer (the way the phone app talks to it).

The printer exposes a "write" characteristic; label commands (TSPL) are sent to it in small chunks.
Uses `bleak` (macOS CoreBluetooth). The connection is kept open between prints so Print all doesn't
reconnect for every label. Pairing in System Settings is not needed.
"""
from __future__ import annotations

import asyncio
import threading

from .errors import PackError

# Write characteristics used by common Bluetooth label/receipt printer modules, best first.
KNOWN_WRITE_CHARS = [
    "49535343-8841-43f4-a8d4-ecbe34729bb3",  # Microchip/ISSC transparent UART
    "0000ff02-0000-1000-8000-00805f9b34fb",
    "0000ae01-0000-1000-8000-00805f9b34fb",
    "0000fff2-0000-1000-8000-00805f9b34fb",
    "0000ffe1-0000-1000-8000-00805f9b34fb",  # HM-10 style
    "0000ff01-0000-1000-8000-00805f9b34fb",
]
STANDARD_SERVICES = ("00001800-", "00001801-", "0000180a-", "0000180f-")
PRINTER_NAME_HINTS = ("aiyin", "e40", "printer", "label", "ay-", "ay_")


def looks_like_printer(name: str | None) -> bool:
    n = (name or "").lower()
    return any(h in n for h in PRINTER_NAME_HINTS)


def pick_write_char(services) -> tuple[str, bool]:
    """Choose the characteristic to send print data to. Returns (uuid, use_write_without_response)."""
    candidates = []
    for svc in services:
        if str(svc.uuid).lower().startswith(STANDARD_SERVICES):
            continue
        for ch in svc.characteristics:
            props = set(ch.properties)
            if props & {"write", "write-without-response"}:
                candidates.append((str(ch.uuid).lower(), "write-without-response" in props))
    if not candidates:
        raise RuntimeError("This Bluetooth device has nothing to print to; is it the printer?")
    for known in KNOWN_WRITE_CHARS:
        for uuid, nores in candidates:
            if uuid == known:
                return uuid, nores
    nores_first = sorted(candidates, key=lambda c: not c[1])
    return nores_first[0]


def chunks(data: bytes, size: int):
    for i in range(0, len(data), size):
        yield data[i:i + size]


async def scan(seconds: float = 8.0) -> list[tuple[str, str, int]]:
    """Nearby Bluetooth devices as (address, name, signal), printers first."""
    from bleak import BleakScanner

    found = await BleakScanner.discover(timeout=seconds, return_adv=True)
    out = [(addr, dev.name or adv.local_name or "", adv.rssi) for addr, (dev, adv) in found.items()]
    out = [d for d in out if d[1]]
    return sorted(out, key=lambda d: (not looks_like_printer(d[1]), -d[2]))


class BleLink:
    """One open connection to one printer, run on its own event loop thread."""

    def __init__(self, address: str, char_uuid: str | None = None, chunk: int = 180,
                 delay_ms: float = 20, timeout: float = 180):
        self.address, self.char_uuid = address, char_uuid
        self.chunk, self.delay, self.timeout = chunk, delay_ms / 1000, timeout
        self.client = None
        self.nores = True
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()

    def _run(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(self.timeout)

    async def _connect(self, progress):
        from bleak import BleakClient, BleakScanner

        if self.client is not None and self.client.is_connected:
            return
        progress("connecting", 0)
        try:
            dev = await BleakScanner.find_device_by_address(self.address, timeout=10)
        except Exception as e:  # CoreBluetooth refuses when Bluetooth permission is missing
            raise _bt_error(e, "connecting to printer") from e
        if dev is None:
            raise PackError("BT-NOTFOUND", "connecting to printer")
        try:
            self.client = BleakClient(dev)
            await self.client.connect()
        except Exception as e:
            raise _bt_error(e, "connecting to printer") from e
        uuid, nores = pick_write_char(self.client.services)
        if self.char_uuid:
            uuid = self.char_uuid
        self.char_uuid, self.nores = uuid, nores

    async def _send(self, data: bytes, progress):
        await self._connect(progress)
        size = max(20, min(self.chunk, (getattr(self.client, "mtu_size", 23) or 23) - 3))
        total = max(1, len(data))
        sent = 0
        progress("sending", 0)
        for part in chunks(data, size):
            await self.client.write_gatt_char(self.char_uuid, part, response=not self.nores)
            sent += len(part)
            progress("sending", int(sent * 100 / total))
            if self.delay:
                await asyncio.sleep(self.delay)

    def send(self, data: bytes, progress=None) -> None:
        progress = progress or (lambda stage, pct: None)
        try:
            self._run(self._send(data, progress))
        except PackError:
            self.client = None
            raise
        except Exception as e:
            self.client = None  # reconnect on the next print
            raise _bt_error(e, "sending to printer") from e

    def close(self) -> None:
        if self.client is not None:
            try:
                self._run(self.client.disconnect())
            except Exception:
                pass
            self.client = None


def _bt_error(e: Exception, step: str) -> PackError:
    low = str(e).lower()
    if any(w in low for w in ("unauthorized", "not authorized", "permission", "denied", "poweredoff", "powered off")):
        return PackError("BT-PERMISSION", step, str(e)[:160])
    if any(w in low for w in ("not found", "no device", "was not found")):
        return PackError("BT-NOTFOUND", step, str(e)[:160])
    return PackError("BT-SEND", step, str(e)[:160])
