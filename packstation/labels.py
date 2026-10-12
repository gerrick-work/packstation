"""Product labels: one fixed design (as on the current moleculelabs labels) filled per product.

The product list lives in labels/products.csv (committed to git, so it is the cloud copy):
    sku,title,size,brand,icon
    ML-SANDAL-3G,Sandalwood,3g,moleculelabs,corrosive
`icon` is a file name in assets/icons/ (without .png), or empty for none.
Products missing from the CSV fall back to their marketplace name, with a trailing size such as
"3g" or "10ml" split off automatically.
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .config import ROOT
from .models import Item, Order

FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Futura.ttc",          # macOS, matches the current labels
    "/Library/Fonts/Futura.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]
BOLD_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Futura.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]
SIZE_RE = re.compile(r"^(.*?)[\s\-,(]*(\d+(?:\.\d+)?\s?(?:g|kg|ml|l|mg|oz))\)?\s*$", re.I)


@lru_cache(maxsize=64)
def load_font(path: str | None, size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    for p in ([path] if path else []) + (BOLD_CANDIDATES if bold else FONT_CANDIDATES):
        if p and Path(p).exists():
            return ImageFont.truetype(p, size)
    return ImageFont.load_default(size)


@dataclass
class LabelData:
    title: str
    size: str
    brand: str
    icon: str


class Catalog:
    def __init__(self, csv_path: str | Path | None = None, default_brand: str = "moleculelabs"):
        self.path = Path(csv_path or ROOT / "labels" / "products.csv")
        self.default_brand = default_brand
        self.rows: dict[str, LabelData] = {}
        if self.path.exists():
            with open(self.path, newline="", encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    sku = (r.get("sku") or "").strip()
                    if sku:
                        self.rows[sku.upper()] = LabelData(
                            r.get("title", "").strip(), r.get("size", "").strip(),
                            (r.get("brand") or default_brand).strip(), (r.get("icon") or "").strip())

    def all_rows(self) -> list[dict]:
        return [{"sku": sku, **vars(d)} for sku, d in sorted(self.rows.items())]

    def save(self, rows: list[dict]) -> None:
        """Replace the product list (from the label editor) and write it back to the CSV."""
        clean: dict[str, LabelData] = {}
        for r in rows:
            sku = str(r.get("sku", "")).strip().upper()
            if not sku:
                continue
            clean[sku] = LabelData(str(r.get("title", "")).strip(), str(r.get("size", "")).strip(),
                                   str(r.get("brand") or self.default_brand).strip(),
                                   str(r.get("icon", "")).strip())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["sku", "title", "size", "brand", "icon"])
            for sku, d in sorted(clean.items()):
                w.writerow([sku, d.title, d.size, d.brand, d.icon])
        tmp.replace(self.path)
        self.rows = clean

    def lookup(self, item: Item) -> tuple[LabelData, bool]:
        """Returns the label data and whether it came from the catalog (False = guessed from the name)."""
        hit = self.rows.get(item.sku.upper()) if item.sku else None
        if hit:
            return hit, True
        m = SIZE_RE.match(item.name.strip())
        title, size = (m.group(1).strip(), m.group(2).replace(" ", "")) if m else (item.name.strip(), "")
        return LabelData(title or item.sku, size, self.default_brand, ""), False


def _fit(draw: ImageDraw.ImageDraw, text: str, font_path: str | None, start: int, max_w: int,
         bold: bool = False) -> ImageFont.FreeTypeFont:
    size = start
    while size > 10:
        f = load_font(font_path, size, bold)
        if draw.textlength(text, font=f) <= max_w:
            return f
        size -= 2
    return load_font(font_path, 10, bold)


def render_product_label(data: LabelData, match_code: str | None, width_mm: float, height_mm: float,
                         dpi: int = 203, font_path: str | None = None) -> Image.Image:
    """1-bit image of one product label, sized to the label roll."""
    w, h = round(width_mm / 25.4 * dpi), round(height_mm / 25.4 * dpi)
    im = Image.new("L", (w, h), 255)
    d = ImageDraw.Draw(im)
    left = round(w * 0.09)
    right_edge = w - round(w * 0.06)

    # Title, as large as fits (proportions taken from the current label).
    title_font = _fit(d, data.title, font_path, round(h * 0.27), right_edge - left)
    d.text((left, round(h * 0.08)), data.title, font=title_font, fill=0)

    small = load_font(font_path, round(h * 0.105))
    if data.size:
        d.text((left, round(h * 0.6)), data.size, font=small, fill=0)
    d.text((left, round(h * 0.76)), data.brand, font=small, fill=0)

    code_right = right_edge
    if data.icon:
        icon_path = ROOT / "assets" / "icons" / f"{data.icon}.png"
        if icon_path.exists():
            side = round(h * 0.24)
            icon = Image.open(icon_path).convert("L").resize((side, side), Image.LANCZOS)
            im.paste(icon, (right_edge - side, round(h * 0.64)))
            code_right = right_edge - side - round(w * 0.03)

    if match_code:
        # Packing match code, bottom row: same characters as the end of the waybill's tracking number.
        code_font = load_font(font_path, round(h * 0.1), bold=True)
        text = f"#{match_code}"
        tw = d.textlength(text, font=code_font)
        d.text((code_right - tw, round(h * 0.76)), text, font=code_font, fill=0)

    return im.point(lambda v: 0 if v < 160 else 255).convert("1")


def icon_names() -> list[str]:
    return sorted(p.stem for p in (ROOT / "assets" / "icons").glob("*.png"))


def labels_for_order(order: Order, catalog: Catalog, cfg: dict) -> list[tuple[Image.Image, Item, bool]]:
    """One label per unit, in item order. Each entry: (image, item, found_in_catalog)."""
    out = []
    code = order.match_code if cfg.get("match_code", True) else None
    for item in order.items:
        data, known = catalog.lookup(item)
        img = render_product_label(data, code, float(cfg.get("width_mm", 50)), float(cfg.get("height_mm", 25)),
                                   int(cfg.get("dpi", 203)), cfg.get("font"))
        out += [(img, item, known)] * max(item.qty, 0)
    return out
