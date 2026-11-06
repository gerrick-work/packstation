"""Bottle labels: one fixed template (the boxed Atlas Origins design from Label Expert, Oct 2026),
filled per product. Every line has a fixed font size and position, so labels look identical
whatever the product; a long title wraps onto more lines (never cut off with "...").

The product list lives in data/products.csv (edited from the Labels screen; updates never touch
it). On first run it is seeded from labels/products.csv:
    sku,title,size,brand,icon,bottles
    ML-SANDAL-3G,Sandalwood,3g,,corrosive,
An empty brand uses the default brand ("atlas origins"). `icon` is a file in assets/icons/
(without .png), or empty. Products missing from the list fall back to their marketplace name,
with a trailing size such as "3g" or "10ml" split off.

Bundles: a product sold as several bottles ("Hedione (15x2) Best Value", "15g x 2", "2 x 15g")
prints one label per bottle with the bottle's size (2 labels of 15g, not 1 of 30g). `bottles` in
the product list sets this; left empty, it is read from the marketplace name.
"""
from __future__ import annotations

import csv
import re
import shutil
from dataclasses import dataclass, replace
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
UNIT = r"(?:kg|mg|ml|g|l|oz)"
NUM = r"\d+(?:\.\d+)?"
# "2 x 15g" (count first, the size has a unit), then "15x2" / "15g x 2" / "15g*2" (size first).
PACK_COUNT_FIRST = re.compile(rf"(?<![\d.])(\d+)\s*[x×*]\s*({NUM})\s?({UNIT})\b", re.I)
PACK_SIZE_FIRST = re.compile(rf"(?<![\d.])({NUM})\s?({UNIT})?\s*[x×*]\s*(\d+)(?![\d.])(?!\s?{UNIT}\b)", re.I)
ANY_SIZE = re.compile(rf"(?<![\d.])({NUM})\s?({UNIT})\b", re.I)
FILLER = re.compile(r"\b(best\s*value|value\s*pack|bundle|combo|promo|twin\s*pack|\d+\s*-?\s*pack|units?|pcs|"
                    r"pieces|bottles?)\b", re.I)


def _num(v: float) -> str:
    return f"{v:g}"


def parse_pack(name: str) -> tuple[str, int, tuple[int, int]] | None:
    """A multi-bottle pack in a product name: (size of one bottle, number of bottles, where it was
    found), e.g. "Hedione (15x2) Best Value 30g" -> ("15g", 2, ...). None for a single bottle."""
    m = PACK_COUNT_FIRST.search(name)
    if m:
        count, size, unit = int(m.group(1)), float(m.group(2)), m.group(3)
    else:
        m = PACK_SIZE_FIRST.search(name)
        if not m:
            return None
        size, unit, count = float(m.group(1)), m.group(2), float(m.group(3))
        if not unit and count > size:  # "2x15": the smaller number is the bottle count
            size, count = count, size
        if count != int(count):
            return None
        count = int(count)
        if not unit:  # take the unit from another size in the name ("... 30g"), else grams
            other = next((u for u in ANY_SIZE.finditer(name) if not m.start() <= u.start() < m.end()), None)
            unit = other.group(2) if other else "g"
    if not 2 <= count <= 50:
        return None
    return _num(size) + unit.lower(), count, m.span()


def _pack_title(name: str, span: tuple[int, int]) -> str:
    """The product name without the pack, its sizes and words like "Best Value"."""
    t = name[:span[0]] + " " + name[span[1]:]
    t = FILLER.sub(" ", ANY_SIZE.sub(" ", t))
    t = re.sub(r"\(\s*\)|\[[^\]]*\]", " ", t)   # empty brackets, and Odoo's "[CODE]"
    return re.sub(r"\s+", " ", t).strip(" -,/()[]")


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
    bottles: int = 0   # labels per unit sold; 0 = not set (1, unless the name shows a pack)


SEED = ROOT / "labels" / "products.csv"


class Catalog:
    def __init__(self, csv_path: str | Path | None = None, default_brand: str = DEFAULT_BRAND,
                 data_dir: str | Path | None = None, default_icon: str = "corrosive"):
        self.default_icon = default_icon or ""
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
        # data/label_names.json: {"Orange Blossom (Neroli)": "Neroli", ...} shorter names for the label
        self.renames: dict[str, str] = {}
        names = Path(data_dir) / "label_names.json" if data_dir else None
        if names and names.exists():
            try:
                import json
                self.renames = {str(k).strip().lower(): str(v).strip()
                                for k, v in json.loads(names.read_text(encoding="utf-8")).items()}
            except ValueError:
                self.renames = {}
        if self.path.exists():
            with open(self.path, newline="", encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    sku = (r.get("sku") or "").strip()
                    if sku:
                        self.rows[sku.upper()] = LabelData(
                            r.get("title", "").strip(), r.get("size", "").strip(),
                            (r.get("brand") or "").strip() or self.default_brand,
                            (r.get("icon") or "").strip() or self.default_icon,
                            _bottles(r.get("bottles")))

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
                                   str(r.get("icon", "")).strip(), _bottles(r.get("bottles")))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["sku", "title", "size", "brand", "icon", "bottles"])
            for sku, d in sorted(clean.items()):
                w.writerow([sku, d.title, d.size, d.brand, d.icon, d.bottles or ""])
        tmp.replace(self.path)
        self.rows = clean

    def lookup(self, item: Item) -> tuple[LabelData, bool]:
        """Returns the label data and whether it came from the catalog (False = guessed from the name).
        `bottles` says how many labels one unit needs (a "15x2" pack: 2 labels of 15g)."""
        hit = self.rows.get(item.sku.upper()) if item.sku else None
        name = re.sub(r"^\s*\[[^\]]*\]\s*", "", item.name).strip()   # Odoo puts "[SKU] " in front
        pack = parse_pack(name)
        m = SIZE_RE.match(name)
        if not hit:  # no SKU match (e.g. a marketplace item Odoo couldn't link): try the product's name
            if pack:
                hit = self._by_title(_pack_title(name, pack[2]), "")
            else:
                hit = self._by_title(m.group(1).strip() if m else name, m.group(2) if m else "")
        if hit:
            hit = self._renamed(hit)
            if hit.bottles or not pack:
                return hit, True
            return replace(hit, size=pack[0], bottles=pack[1]), True
        if pack:
            return self._renamed(LabelData(_pack_title(name, pack[2]) or item.sku, pack[0], self.default_brand,
                                           self.default_icon, pack[1])), False
        title, size = (m.group(1).strip(), m.group(2).replace(" ", "")) if m else (name, "")
        return self._renamed(LabelData(title or item.sku, size, self.default_brand, self.default_icon)), False

    def _renamed(self, d: LabelData) -> LabelData:
        new = self.renames.get(d.title.strip().lower())
        return replace(d, title=new) if new else d

    def _by_title(self, title: str, size: str) -> LabelData | None:
        """A product-list row with this title (and size, when the name has one), if exactly one."""
        t, z = title.lower(), size.lower().replace(" ", "")
        hits = [d for d in self.rows.values() if d.title.lower() == t and (not z or d.size.lower() == z)]
        return hits[0] if len(hits) == 1 and t else None

    def label_count(self, order: Order) -> int:
        return sum(max(i.qty, 0) * max(self.lookup(i)[0].bottles, 1) for i in order.items)


def _bottles(v) -> int:
    try:
        return max(int(str(v).strip() or 0), 0)
    except ValueError:
        return 0


def _mm_px(mm: float, dpi: int) -> int:
    return max(1, round(mm / 25.4 * dpi))


def _ellipsize(draw: ImageDraw.ImageDraw, text: str, font, max_w: int) -> tuple[str, bool]:
    if draw.textlength(text, font=font) <= max_w:
        return text, True
    while text and draw.textlength(text + "…", font=font) > max_w:
        text = text[:-1]
    return text.rstrip() + "…", False


def _wrap(draw, words: list[str], font, max_w: int, n: int) -> list[str] | None:
    """Split words into exactly n lines as evenly as possible; None if any line is too wide."""
    if n == 1:
        line = " ".join(words)
        return [line] if draw.textlength(line, font=font) <= max_w else None
    best = None
    for i in range(1, len(words) - n + 2):
        head = " ".join(words[:i])
        if draw.textlength(head, font=font) > max_w:
            break
        rest = _wrap(draw, words[i:], font, max_w, n - 1)
        if rest:
            cand = [head] + rest
            widest = max(draw.textlength(x, font=font) for x in cand)
            if best is None or widest < best[0]:
                best = (widest, cand)
    return best[1] if best else None


def _char_wrap(draw, text: str, font, max_w: int) -> list[str]:
    """Last resort for a single very long word: break it between letters."""
    lines, cur = [], ""
    for ch in text:
        if cur and draw.textlength(cur + ch, font=font) > max_w:
            lines.append(cur)
            cur = ch.lstrip()
        else:
            cur += ch
    return lines + ([cur] if cur else [])


def fit_title(draw, title: str, font_path: str | None, full_px: int, max_w: int, max_h: int):
    """Largest font at which the full title fits in max_w x max_h, on as few lines as possible.
    Prefers one line down to 80% of the normal size before wrapping."""
    words = title.split() or [title]
    for size in range(full_px, max(1, round(full_px * 0.8)) - 1, -1):
        f = load_font(font_path, size)
        if draw.textlength(title, font=f) <= max_w:
            return [title], f
    for size in range(full_px, 5, -1):
        f = load_font(font_path, size)
        step = round(size * 1.08)
        ink = f.getbbox("Hgjpy()")[3]
        for n in range(2, len(words) + 1):
            if step * (n - 1) + ink > max_h:
                break
            lines = _wrap(draw, words, f, max_w, n)
            if lines:
                return lines, f
        lines = _char_wrap(draw, title, f, max_w)
        if step * (len(lines) - 1) + ink <= max_h and size <= full_px * 0.6:
            return lines, f
    f = load_font(font_path, 6)
    return _char_wrap(draw, title, f, max_w), f


# ---- Bottle label design (Oct 2026, "Atlas Origins" boxed template) -------------------------------
# Rounded black frame. Left column: the Atlas Origins "A" mark with the bottle size under it.
# Right side: product name (caps) on top, "Atlas Origins(TM)" in the bottom row.
# Positions in mm from the label's top-left, measured from the Label Expert templates (40 x 20 mm).
BOX = {
    "frame_x": 1.4, "frame_y": 1.0, "frame_w": 0.5, "radius": 1.2,
    "col_x": 12.3, "row_y": 12.1, "rule_w": 0.25,
    "logo": (2.9, 2.8, 11.3, 12.4),          # box the mark is fitted into (fills the column)
    "title_x": 14.1, "title_top": 3.3, "title_cap": 2.6, "title_right_pad": 1.0, "title_bottom_pad": 1.0,
    "foot_top": 13.6, "size_cap": 2.45, "brand_cap": 2.88,
}
FONT_DIR = ROOT / "assets" / "fonts"
TITLE_FONT = FONT_DIR / "BarlowCondensed-SemiBold.ttf"
BRAND_FONT = FONT_DIR / "BarlowCondensed-Medium.ttf"
MARK = ROOT / "assets" / "brand" / "atlas-origins-mark.png"


def _font_path(p: Path, override: str | None) -> str | None:
    if override and Path(override).exists():
        return override
    return str(p) if p.exists() else None


def font_for_cap(path: str | None, cap_px: float, bold: bool = True):
    """Font whose capital H is cap_px tall."""
    f = load_font(path, 40, bold)
    h = f.getbbox("H", anchor="ls")
    ratio = (h[3] - h[1]) / 40 if h[3] > h[1] else 0.7
    return load_font(path, max(6, round(cap_px / ratio)), bold)


def _cap(f) -> int:
    b = f.getbbox("H", anchor="ls")
    return b[3] - b[1]


def _fit_caps(d, text: str, path, cap_px: float, max_w: int, max_h: int):
    """Largest size (from cap_px down) at which the whole text fits; one line preferred down to 80%."""
    words = text.split() or [text]
    full = font_for_cap(path, cap_px)
    for size in range(full.size, max(6, round(full.size * 0.8)) - 1, -1):
        f = load_font(path, size, True)
        if d.textlength(text, font=f) <= max_w and _cap(f) <= max_h:
            return [text], f
    for size in range(full.size, 5, -1):
        f = load_font(path, size, True)
        step = round(size * 1.05)
        for n in range(2, len(words) + 1):
            if _cap(f) + step * (n - 1) > max_h:
                break
            lines = _wrap(d, words, f, max_w, n)
            if lines:
                return lines, f
        lines = _char_wrap(d, text, f, max_w)
        if _cap(f) + step * (len(lines) - 1) <= max_h and size <= full.size * 0.6:
            return lines, f
    f = load_font(path, 6, True)
    return _char_wrap(d, text, f, max_w), f


def _size_text(size: str) -> str:
    """"15g" -> "15 g", as on the printed design."""
    m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*([a-zA-Z]+)\s*$", size or "")
    return f"{m.group(1)} {m.group(2).lower()}" if m else (size or "").strip()


def _brand_text(brand: str) -> tuple[str, bool]:
    """Display text for the brand row, and whether it carries the TM mark."""
    b = (brand or "").strip()
    if b.lower() in ("atlas origins", "atlas origin"):
        return "Atlas Origins", True
    return (b.title() if b.islower() else b), False


def render_product_label(data: LabelData, match_code: str | None, width_mm: float, height_mm: float,
                         dpi: int = 203, font_path: str | None = None, template: dict | None = None,
                         warnings: list | None = None) -> Image.Image:
    """1-bit image of one bottle label in the boxed Atlas Origins design, sized to the label roll.
    (match_code is accepted for older callers but not printed: the design has no room for it.)"""
    t = {**TEMPLATE, **(template or {})}
    px = lambda mm: _mm_px(mm, dpi)  # noqa: E731
    w, h = px(width_mm), px(height_mm)
    im = Image.new("L", (w, h), 255)
    d = ImageDraw.Draw(im)
    d.fontmode = "1"

    if (data.brand or "").strip() == "-":
        # Plain sticker (brand "-" in the product list, e.g. "Free Gift"): only the title, centred.
        left, right = px(t["margin_x"]), w - px(t["margin_x"])
        lines, f = fit_title(d, data.title, font_path, px(t["title_size"]) * 3 // 2, right - left, h - px(2.0))
        step = round(f.size * 1.08)
        y = (h - step * len(lines)) // 2
        for line in lines:
            d.text(((w - d.textlength(line, font=f)) // 2, y), line, font=f, fill=0)
            y += step
        return im.point(lambda v: 0 if v < 160 else 255).convert("1")

    b = {**BOX, **{k[4:]: v for k, v in t.items() if k.startswith("box_")}}
    sx = width_mm / 40.0     # the design is drawn for 40 x 20 mm; scale for other rolls
    sy = height_mm / 20.0
    X = lambda mm: px(mm * sx)  # noqa: E731
    Y = lambda mm: px(mm * sy)  # noqa: E731
    title_path = _font_path(TITLE_FONT, font_path)
    brand_path = _font_path(BRAND_FONT, font_path) or title_path

    fw = max(2, px(b["frame_w"]))
    rw = max(2, px(b["rule_w"]))
    x0, y0, x1, y1 = X(b["frame_x"]), Y(b["frame_y"]), w - 1 - X(b["frame_x"]), h - 1 - Y(b["frame_y"])
    d.rounded_rectangle((x0, y0, x1, y1), radius=px(b["radius"]), outline=0, width=fw)
    cx = X(b["col_x"])
    d.rectangle((cx, y0, cx + rw - 1, y1), fill=0)                       # column divider
    ry = Y(b["row_y"])
    d.rectangle((cx, ry, x1, ry + rw - 1), fill=0)                       # row divider

    # The Atlas Origins mark, fitted into its box.
    if MARK.exists():
        lx0, ly0, lx1, ly1 = X(b["logo"][0]), Y(b["logo"][1]), X(b["logo"][2]), Y(b["logo"][3])
        mark = Image.open(MARK).convert("L")
        k = min((lx1 - lx0) / mark.width, (ly1 - ly0) / mark.height)
        mw, mh = max(1, round(mark.width * k)), max(1, round(mark.height * k))
        mark = mark.resize((mw, mh), Image.LANCZOS).point(lambda v: 0 if v < 128 else 255)
        im.paste(mark, (lx0 + (lx1 - lx0 - mw) // 2, ly0 + (ly1 - ly0 - mh) // 2))

    # Bottle size, centred under the mark.
    foot_base = Y(b["foot_top"]) + round(px(b["size_cap"]))
    sf = font_for_cap(title_path, px(b["size_cap"]))
    size = _size_text(data.size)
    if size:
        while d.textlength(size, font=sf) > (cx - x0 - fw - px(1.0)) and sf.size > 6:
            sf = load_font(title_path, sf.size - 1, True)
        d.text(((x0 + fw + cx) / 2, foot_base), size, font=sf, fill=0, anchor="ms")

    # Brand row, centred in the bottom-right cell; "TM" drawn small and raised.
    brand, tm = _brand_text(data.brand)
    bf = font_for_cap(brand_path, px(b["brand_cap"]), bold=False)
    tf = font_for_cap(brand_path, px(b["brand_cap"]) * 0.42, bold=False)
    cell_l, cell_r = cx + rw, x1 - fw
    def brand_w(f):
        return d.textlength(brand, font=f) + (px(0.15) + d.textlength("TM", font=tf) if tm else 0)
    while brand_w(bf) > cell_r - cell_l - px(1.4) and bf.size > 6:
        bf = load_font(brand_path, bf.size - 1, False)
    bx = (cell_l + cell_r - brand_w(bf)) / 2
    brand_base = (ry + rw + y1 - fw) // 2 + _cap(bf) // 2     # cap height centred in the cell
    d.text((bx, brand_base), brand, font=bf, fill=0, anchor="ls")
    if tm:
        tx = bx + d.textlength(brand, font=bf) + px(0.15)
        d.text((tx, brand_base - _cap(bf) + _cap(tf)), "TM", font=tf, fill=0, anchor="ls")

    # Product name in capitals: the whole name always prints (smaller, then wrapped), never "...".
    title = (data.title or "").upper()
    tx0 = X(b["title_x"])
    max_w = x1 - fw - X(b["title_right_pad"]) - tx0
    top = Y(b["title_top"])
    max_h = ry - Y(b["title_bottom_pad"]) - top
    lines, f = _fit_caps(d, title, title_path, px(b["title_cap"]), max_w, max_h)
    if warnings is not None and f.size < font_for_cap(title_path, px(b["title_cap"])).size * 0.75:
        warnings.append("name printed smaller to fit")
    base = top + _cap(f)
    for line in lines:
        d.text((tx0, base), line, font=f, fill=0, anchor="ls")
        base += round(f.size * 1.05)

    return im.point(lambda v: 0 if v < 160 else 255).convert("1")


def icon_names() -> list[str]:
    return sorted(p.stem for p in (ROOT / "assets" / "icons").glob("*.png"))


def labels_for_order(order: Order, catalog: Catalog, cfg: dict) -> list[tuple[Image.Image, Item, bool]]:
    """One label per bottle (units × bottles per unit), in item order. Each entry: (image, item, found_in_catalog)."""
    out = []
    code = None  # bottle labels carry no order/match code (the waybill alone identifies the parcel)
    for item in order.items:
        data, known = catalog.lookup(item)
        img = render_product_label(data, code, float(cfg.get("width_mm", 40)), float(cfg.get("height_mm", 20)),
                                   int(cfg.get("dpi", 203)), cfg.get("font"), cfg.get("template"))
        out += [(img, item, known)] * (max(item.qty, 0) * max(data.bottles, 1))
    return out
