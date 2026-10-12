"""Printer backends. Each printer in config.toml picks one:

  file : save each label/waybill into data/output/ (test mode, nothing is printed)
  cups : print through a normal macOS printer queue (needs a working driver for the printer)
  tspl : send TSPL label commands straight to the printer, no driver and no Label Expert needed.
         Goes through a raw macOS queue (`queue`) or a device path (`device`).
"""
from __future__ import annotations

import io
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

from PIL import Image


def pdf_to_image(pdf: bytes, dpi: int) -> Image.Image:
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(pdf)
    page = doc[0]
    return page.render(scale=dpi / 72).to_pil().convert("L")


def fit_to_label(img: Image.Image, w: int, h: int) -> Image.Image:
    """Scale to fit the label, keep aspect ratio, centre, threshold to 1-bit (keeps barcodes crisp)."""
    img = img.convert("L")
    scale = min(w / img.width, h / img.height)
    resized = img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))), Image.LANCZOS)
    canvas = Image.new("L", (w, h), 255)
    canvas.paste(resized, ((w - resized.width) // 2, (h - resized.height) // 2))
    return canvas.point(lambda v: 0 if v < 160 else 255).convert("1")


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


class Printer:
    def __init__(self, name: str, cfg: dict, data_dir: str):
        self.name = name
        self.cfg = cfg
        self.width_mm = float(cfg.get("width_mm", 100))
        self.height_mm = float(cfg.get("height_mm", 150))
        self.dpi = int(cfg.get("dpi", 203))
        self.data_dir = Path(data_dir)

    @property
    def dots(self) -> tuple[int, int]:
        return round(self.width_mm / 25.4 * self.dpi), round(self.height_mm / 25.4 * self.dpi)

    def print_pdf(self, pdf: bytes, label: str) -> str:
        return self.print_image(pdf_to_image(pdf, self.dpi), label)

    def print_image(self, img: Image.Image, label: str) -> str:
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

    def print_pdf(self, pdf: bytes, label: str) -> str:
        self.counter += 1
        path = self._dir() / f"{datetime.now():%H%M%S}-{self.counter:03d}-{label}.pdf"
        path.write_bytes(pdf)
        return str(path)

    def print_image(self, img: Image.Image, label: str) -> str:
        self.counter += 1
        path = self._dir() / f"{datetime.now():%H%M%S}-{self.counter:03d}-{label}.png"
        fit_to_label(img, *self.dots).save(path)
        return str(path)


class CupsPrinter(Printer):
    def _lp(self, path: str) -> str:
        cmd = ["lp", "-d", self.cfg["queue"], "-o", f"media=Custom.{self.width_mm:g}x{self.height_mm:g}mm",
               "-o", "fit-to-page", path]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if out.returncode:
            raise RuntimeError(f"lp failed on {self.cfg['queue']}: {out.stderr.strip()}")
        return out.stdout.strip()

    def print_pdf(self, pdf: bytes, label: str) -> str:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(pdf)
        return self._lp(f.name)

    def print_image(self, img: Image.Image, label: str) -> str:
        buf = io.BytesIO()
        fit_to_label(img, *self.dots).convert("L").save(buf, "PDF", resolution=self.dpi)
        return self.print_pdf(buf.getvalue(), label)


class TsplPrinter(Printer):
    def print_image(self, img: Image.Image, label: str) -> str:
        job = tspl_job(fit_to_label(img, *self.dots), self.width_mm, self.height_mm,
                       float(self.cfg.get("gap_mm", 2)), bool(self.cfg.get("invert", False)))
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


BACKENDS = {"file": FilePrinter, "cups": CupsPrinter, "tspl": TsplPrinter}


def build_printer(name: str, cfg: dict, data_dir: str) -> Printer:
    kind = cfg.get("backend", "file")
    if kind not in BACKENDS:
        raise SystemExit(f"Unknown printer backend '{kind}' for {name}; use one of {', '.join(BACKENDS)}")
    return BACKENDS[kind](name, cfg, data_dir)
