"""Bottle labels: one fixed template (matching the Label Expert design "Atlas Origin Finalv1"),
filled per product. Every line has a fixed font size and position, so labels look identical
whatever the product; a title too long for the label is shortened and flagged in the Labels screen.

The product list lives in data/products.csv (edited from the Labels screen; updates never touch
it). On first run it is seeded from labels/products.csv:
    sku,title,size,brand,icon
    ML-SANDAL-3G,Sandalwood,3g,,corrosive
An empty brand uses the default brand ("atlas origins"). `icon` is a file in assets/icons/
(without .png), or empty. Products missing from the list fall back to their marketplace name,
with a trailing size such as "3g" or "10ml" split off.
"""
from __future__ import annotations

import csv
import re
import shutil
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
DEFAULT_BRAND = "atlas origins"

# Fixed layout, in mm from the label's top-left corner. Font sizes are cap-to-descender heights in mm.
# Override any key under [labels.template] in config.toml.
TEMPLATE = {
    "margin_x": 2.0,
    "title_y": 1.4, "title_size": 4.2,
    "size_y": 10.2, "size_size": 2.4,
    "brand_y": 13.8, "brand_size": 2.4,
    "icon_size": 5.6, "icon_margin": 1.4,
    "code_size": 2.4,
}

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


SEED = ROOT / "labels" / "products.csv"


class Catalog:
    def __init__(self, csv_path: str | Path | None = None, default_brand: str = DEFAULT_BRAND,
                 data_dir: str | Path | None = None):
        if csv_path:
            self.path = Path(csv_path)
        elif data_dir:
            self.path = Path(data_dir) / "products.csv"
            if not self.path.exists() and SEED.exists():
                shutil.copy(SEED, self.path)
        else:
            self.path = SEED
        self.default_brand = default_brand or DEFAULT_BRAND
        self.rows: dict[str, LabelData] = {}
        if self.path.exists():
            with open(self.path, newline="", encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    sku = (r.get("sku") or "").strip()
                    if sku:
                        self.rows[sku.upper()] = LabelData(
                            r.get("title", "").strip(), r.get("size", "").strip(),
                            (r.get("brand") or "").strip() or self.default_brand, (r.get("icon") or "").strip())

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


def _mm_px(mm: float, dpi: int) -> int:
    return max(1, round(mm / 25.4 * dpi))


def _ellipsize(draw: ImageDraw.ImageDraw, text: str, font, max_w: int) -> tuple[str, bool]:
    if draw.textlength(text, font=font) <= max_w:
        return text, True
    while text and draw.textlength(text + "…", font=font) > max_w:
        text = text[:-1]
    return text.rstrip() + "…", False


def render_product_label(data: LabelData, match_code: str | None, width_mm: float, height_mm: float,
                         dpi: int = 203, font_path: str | None = None, template: dict | None = None,
                         warnings: list | None = None) -> Image.Image:
    """1-bit image of one bottle label in the fixed template, sized to the label roll."""
    t = {**TEMPLATE, **(template or {})}
    px = lambda mm: _mm_px(mm, dpi)  # noqa: E731
    w, h = px(width_mm), px(height_mm)
    im = Image.new("L", (w, h), 255)
    d = ImageDraw.Draw(im)
    left, right = px(t["margin_x"]), w - px(t["margin_x"])

    icon_side = px(t["icon_size"])
    icon_x = w - px(t["icon_margin"]) - icon_side
    has_icon = bool(data.icon) and (ROOT / "assets" / "icons" / f"{data.icon}.png").exists()

    title_font = load_font(font_path, px(t["title_size"]))
    title, fits = _ellipsize(d, data.title, title_font, right - left)
    d.text((left, px(t["title_y"])), title, font=title_font, fill=0)
    if not fits and warnings is not None:
        warnings.append(f"Title too long for the label, printed as '{title}'. Shorten it under Labels.")

    small = load_font(font_path, px(t["size_size"]))
    if data.size:
        d.text((left, px(t["size_y"])), data.size, font=small, fill=0)
    d.text((left, px(t["brand_y"])), data.brand, font=load_font(font_path, px(t["brand_size"])), fill=0)

    code_right = right
    if has_icon:
        icon = Image.open(ROOT / "assets" / "icons" / f"{data.icon}.png").convert("L")
        icon = icon.resize((icon_side, icon_side), Image.LANCZOS)
        im.paste(icon, (icon_x, h - px(t["icon_margin"]) - icon_side))
        code_right = icon_x - px(1.0)

    if match_code:
        # Packing match code: the last 4 characters of the waybill's tracking number.
        code_font = load_font(font_path, px(t["code_size"]), bold=True)
        text = f"#{match_code}"
        d.text((code_right - d.textlength(text, font=code_font), px(t["brand_y"])), text, font=code_font, fill=0)

    return im.point(lambda v: 0 if v < 160 else 255).convert("1")


def icon_names() -> list[str]:
    return sorted(p.stem for p in (ROOT / "assets" / "icons").glob("*.png"))


def labels_for_order(order: Order, catalog: Catalog, cfg: dict) -> list[tuple[Image.Image, Item, bool]]:
    """One label per unit, in item order. Each entry: (image, item, found_in_catalog)."""
    out = []
    code = None  # bottle labels carry no order/match code (the waybill alone identifies the parcel)
    for item in order.items:
        data, known = catalog.lookup(item)
        img = render_product_label(data, code, float(cfg.get("width_mm", 40)), float(cfg.get("height_mm", 20)),
                                   int(cfg.get("dpi", 203)), cfg.get("font"), cfg.get("template"))
        out += [(img, item, known)] * max(item.qty, 0)
    return out
