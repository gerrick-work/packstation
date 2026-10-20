"""Printer backends. Each printer in config.toml picks one:

  file      : save each label/waybill into data/output/ (test mode, nothing is printed)
  cups      : print through a normal macOS printer queue (needs a working driver for the printer)
  tspl      : TSPL label commands straight to the printer over USB, through a raw macOS queue
              (`queue`) or a device path (`device`)
  bluetooth : TSPL over Bluetooth Low Energy (`ble_address`, set up by the setup helper)

Per-printer settings (all in mm): width_mm, height_mm, gap_mm, offset_x_mm, offset_y_mm.
Offsets move the whole print right/down (negative = left/up); set them on the Calibrate screen.
The label printer also has `separator` = blank | none | divider, fed after each order's labels.
"""
from __future__ import annotations

import io
import subprocess
import tempfile
import threading
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw

from .errors import PackError


def pdf_to_image(pdf: bytes, dpi: int, box_mm: tuple[float, float] | None = None, supersample: int = 3) -> Image.Image:
    """Render page 1 for a thermal printer. Drawn at 3x the printer's resolution and shrunk down, so
    letter edges are averaged properly before the 1-bit step (smoother, fuller strokes, less grain).
    With box_mm the page is scaled to fit inside that box."""
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(pdf)
    page = doc[0]
    scale = dpi / 72
    if box_mm:
        pw, ph = page.get_size()  # points
        bw, bh = (v / 25.4 * dpi for v in box_mm)
        scale = min(bw / pw, bh / ph)
    target = page.render(scale=scale).to_pil().size
    big = page.render(scale=scale * supersample).to_pil().convert("L")
    return big.resize(target, Image.LANCZOS)


def to_1bit(img: Image.Image, cut: int = 160) -> Image.Image:
    """Grey to black/white. A higher cut makes strokes fuller (more grey counts as black)."""
    return img.convert("L").point(lambda v: 0 if v < cut else 255).convert("1")


def fit_to_label(img: Image.Image, w: int, h: int, cut: int = 160) -> Image.Image:
    """Scale to fit the label, keep aspect ratio, centre, threshold to 1-bit (keeps barcodes crisp)."""
    img = img.convert("L")
    scale = min(w / img.width, h / img.height)
    resized = img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))), Image.LANCZOS)
    canvas = Image.new("L", (w, h), 255)
    canvas.paste(resized, ((w - resized.width) // 2, (h - resized.height) // 2))
    return to_1bit(canvas, cut)


def place_native(img: Image.Image, w: int, h: int, cut: int = 185) -> Image.Image:
    """Put an image already rendered at the printer's dpi onto the label with no rescaling, centred
    both ways; anything larger than the label is trimmed equally on each side."""
    img = img.convert("L")
    canvas = Image.new("L", (w, h), 255)
    canvas.paste(img, ((w - img.width) // 2, max(0, (h - img.height) // 2)))
    return to_1bit(canvas, cut)


def shift(img: Image.Image, dx: int, dy: int) -> Image.Image:
    """Move the whole print by (dx, dy) dots, keeping the label size; uncovered edges stay white."""
    if not dx and not dy:
        return img
    out = Image.new(img.mode, img.size, 1 if img.mode == "1" else 255)
    out.paste(img, (dx, dy))
    return out


def tspl_job(img: Image.Image, width_mm: float, height_mm: float, gap_mm: float = 2,
             invert: bool = False, copies: int = 1) -> bytes:
    """TSPL program printing one 1-bit image. In TSPL BITMAP data a 0 bit is a printed (black) dot,
    which is exactly how Pillow packs mode "1" images (black = 0)."""
    img = img.convert("1")
    data = img.tobytes()
    if invert:
        data = bytes(b ^ 0xFF for b in data)
    width_bytes = (img.width + 7) // 8
    head = (
        f"SIZE {width_mm:g} mm,{height_mm:g} mm\r\n"
        f"GAP {gap_mm:g} mm,0 mm\r\n"
        "DIRECTION 1\r\nREFERENCE 0,0\r\nCLS\r\n"
        f"BITMAP 0,0,{width_bytes},{img.height},0,"
    ).encode()
    return head + data + f"\r\nPRINT 1,{copies}\r\n".encode()


def tspl_feed(width_mm: float, height_mm: float, gap_mm: float = 2) -> bytes:
    """Feed one blank label."""
    return (f"SIZE {width_mm:g} mm,{height_mm:g} mm\r\nGAP {gap_mm:g} mm,0 mm\r\nFORMFEED\r\n").encode()


def calibration_image(w: int, h: int, dpi: int, note: str) -> Image.Image:
    """Border 1 dot in from each edge, mm ruler along the top and left, centre cross, settings text."""
    from .labels import load_font

    im = Image.new("L", (w, h), 255)
    d = ImageDraw.Draw(im)
    d.rectangle((0, 0, w - 1, h - 1), outline=0, width=2)
    mm = dpi / 25.4
    small = load_font(None, max(10, int(1.8 * mm)))
    x = 0
    i = 0
    while x < w:
        tick = int(2.5 * mm) if i % 5 == 0 else int(1.2 * mm)
        d.line((x, 0, x, tick), fill=0, width=1)
        if i % 10 == 0 and i:
            d.text((x + 2, tick), str(i), font=small, fill=0)
        i += 1
        x = round(i * mm)
    y, i = 0, 0
    while y < h:
        tick = int(2.5 * mm) if i % 5 == 0 else int(1.2 * mm)
        d.line((0, y, tick, y), fill=0, width=1)
        if i % 10 == 0 and i:
            d.text((tick + 2, y - int(0.9 * mm)), str(i), font=small, fill=0)
        i += 1
        y = round(i * mm)
    cx, cy = w // 2, h // 2
    arm = int(min(w, h) * 0.12)
    d.line((cx - arm, cy, cx + arm, cy), fill=0, width=2)
    d.line((cx, cy - arm, cx, cy + arm), fill=0, width=2)
    f = load_font(None, max(12, int(min(w, h) * 0.07)), bold=True)
    tw = d.textlength(note, font=f)
    d.text(((w - tw) / 2, cy + arm + int(1.5 * mm)), note, font=f, fill=0)
    return to_1bit(im)


class Status:
    """Live state of one printer, shared with every device viewing the station."""

    def __init__(self):
        self.lock = threading.Lock()
        self.state, self.pct, self.detail, self.error, self.at = "idle", 0, "", None, None

    def set(self, state: str, detail: str | None = None, pct: int | None = None, error: dict | None = None):
        with self.lock:
            self.state = state
            if detail is not None:
                self.detail = detail
            if pct is not None:
                self.pct = pct
            self.error = error
            self.at = datetime.now().isoformat(timespec="seconds")

    def as_dict(self) -> dict:
        with self.lock:
            return {"state": self.state, "pct": self.pct, "detail": self.detail, "error": self.error, "at": self.at}


class Printer:
    def __init__(self, name: str, cfg: dict, data_dir: str):
        self.name = name
        self.cfg = cfg
        self.width_mm = float(cfg.get("width_mm", 100))
        self.height_mm = float(cfg.get("height_mm", 150))
        self.dpi = int(cfg.get("dpi", 203))
        self.gap_mm = float(cfg.get("gap_mm", 2))
        self.offset_x_mm = float(cfg.get("offset_x_mm", 0))
        self.offset_y_mm = float(cfg.get("offset_y_mm", 0))
        self.data_dir = Path(data_dir)
        self.status = Status()

    @property
    def dots(self) -> tuple[int, int]:
        return round(self.width_mm / 25.4 * self.dpi), round(self.height_mm / 25.4 * self.dpi)

    def _mm(self, v: float) -> int:
        return round(v / 25.4 * self.dpi)

    def prepare(self, img: Image.Image, native: bool = False) -> Image.Image:
        w, h = self.dots
        if native:
            page = place_native(img, w, h, int(self.cfg.get("darkness", 185)))
        else:
            page = fit_to_label(img, w, h, int(self.cfg.get("darkness", 160)))
        return shift(page, self._mm(self.offset_x_mm), self._mm(self.offset_y_mm))

    def print_pdf(self, pdf: bytes, label: str, detail: str = "") -> str:
        """Waybills: rendered at the printer's dpi and printed at their real size (no rescale)."""
        box = (float(self.cfg.get("content_width_mm", 94)), float(self.cfg.get("content_height_mm", 136)))
        return self.print_image(pdf_to_image(pdf, self.dpi, box), label, detail=detail, native=True)

    def print_image(self, img: Image.Image, label: str, detail: str = "", native: bool = False) -> str:
        page = self.prepare(img, native)
        self.status.set("sending", detail, 0)
        out = self._send_page(page, label)
        self.status.set("done", detail, 100)
        return out

    def feed_separator(self, kind: str, detail: str = "") -> None:
        if kind == "none":
            return
        if kind == "divider":
            w, h = self.dots
            im = Image.new("L", (w, h), 255)
            d = ImageDraw.Draw(im)
            for x in range(0, w, 24):
                d.line((x, h // 2, x + 12, h // 2), fill=0, width=3)
            self.print_image(im, "separator", detail)
            return
        self.status.set("sending", detail, 0)
        self._feed_blank()
        self.status.set("done", detail, 100)

    def _job(self, page: Image.Image) -> bytes:
        return tspl_job(page, self.width_mm, self.height_mm, self.gap_mm, bool(self.cfg.get("invert", False)))

    def _send_page(self, page: Image.Image, label: str) -> str:
        raise NotImplementedError

    def _feed_blank(self) -> None:
        raise NotImplementedError


class FilePrinter(Printer):
    """Test mode: writes what would be printed to data/output/<today>/<printer>/."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.counter = 0

    def _dir(self) -> Path:
        d = self.data_dir / "output" / datetime.now().strftime("%Y-%m-%d") / self.name
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _name(self, label: str, ext: str) -> Path:
        self.counter += 1
        safe = "".join(c if c.isalnum() or c in "-_." else "-" for c in label)  # "WH/OUT/00080" -> "WH-OUT-00080"
        return self._dir() / f"{datetime.now():%H%M%S}-{self.counter:03d}-{safe}.{ext}"

    def print_pdf(self, pdf: bytes, label: str, detail: str = "") -> str:
        self.status.set("sending", detail, 0)
        path = self._name(label, "pdf")
        path.write_bytes(pdf)
        self.status.set("done", detail, 100)
        return str(path)

    def _send_page(self, page, label):
        path = self._name(label, "png")
        page.save(path)
        return str(path)

    def _feed_blank(self):
        self._name("blank-label", "txt").write_text("one blank label fed here\n")


class CupsPrinter(Printer):
    def _lp(self, path: str) -> str:
        cmd = ["lp", "-d", self.cfg["queue"], "-o", f"media=Custom.{self.width_mm:g}x{self.height_mm:g}mm",
               "-o", "fit-to-page", path]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if out.returncode:
            raise RuntimeError(f"lp failed on {self.cfg['queue']}: {out.stderr.strip()}")
        return out.stdout.strip()

    def _send_page(self, page, label):
        buf = io.BytesIO()
        page.convert("L").save(buf, "PDF", resolution=self.dpi)
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(buf.getvalue())
        return self._lp(f.name)

    def _feed_blank(self):
        w, h = self.dots
        self._send_page(Image.new("1", (w, h), 1), "blank")


class TsplPrinter(Printer):
    def _raw(self, job: bytes) -> str:
        if self.cfg.get("device"):
            with open(self.cfg["device"], "wb") as dev:
                dev.write(job)
            return self.cfg["device"]
        with tempfile.NamedTemporaryFile(suffix=".tspl", delete=False) as f:
            f.write(job)
        out = subprocess.run(["lp", "-d", self.cfg["queue"], "-o", "raw", f.name],
                             capture_output=True, text=True, timeout=60)
        if out.returncode:
            raise RuntimeError(f"lp failed on {self.cfg['queue']}: {out.stderr.strip()}")
        return out.stdout.strip()

    def _send_page(self, page, label):
        return self._raw(self._job(page))

    def _feed_blank(self):
        self._raw(tspl_feed(self.width_mm, self.height_mm, self.gap_mm))


class BluetoothPrinter(Printer):
    """TSPL over Bluetooth Low Energy (config: ble_address from the setup helper)."""

    def __init__(self, *a, link=None, **kw):
        super().__init__(*a, **kw)
        if link is None:
            from .bluetooth import BleLink

            if not self.cfg.get("ble_address"):
                raise PackError("CONFIG", f"{self.name} printer", "no ble_address; run printer setup")
            link = BleLink(self.cfg["ble_address"], self.cfg.get("ble_char"),
                           int(self.cfg.get("ble_chunk", 180)), float(self.cfg.get("ble_delay_ms", 20)))
        self.link = link

    def _send(self, data: bytes):
        def progress(stage: str, pct: int):
            self.status.set("connecting" if stage == "connecting" else "sending", None, pct)
        self.link.send(data, progress)

    def _send_page(self, page, label):
        self._send(self._job(page))
        return f"bluetooth {self.cfg.get('ble_name') or self.cfg['ble_address']}"

    def _feed_blank(self):
        # An empty printed page, not FORMFEED: the E40 Pro ignores FORMFEED over Bluetooth.
        self._send(self._job(Image.new("1", self.dots, 1)))

    def close(self):
        self.link.close()


BACKENDS = {"file": FilePrinter, "cups": CupsPrinter, "tspl": TsplPrinter, "bluetooth": BluetoothPrinter}


def build_printer(name: str, cfg: dict, data_dir: str) -> Printer:
    kind = cfg.get("backend", "file")
    if kind not in BACKENDS:
        raise PackError("CONFIG", f"{name} printer", f"unknown backend '{kind}'")
    return BACKENDS[kind](name, cfg, data_dir)

