"""Atlas DSF business documents, all in the one approved design (Odoo invoice ATL/2026/1001).

Usage:
    python documents/render.py documents/examples/proforma.json            -> PDF next to the JSON
    python documents/render.py my.json -o out/Invoice.pdf

The design is fixed in this file. Only wording and content change between document types,
and every document type is defined once in DOC_TYPES below. Do not restyle per document.
"""
from __future__ import annotations

import argparse
import html
import json
import sys
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

HERE = Path(__file__).resolve().parent
ASSETS = HERE / "assets"

COMPANY = {
    "name": "Atlas DSF Fragrance and Beauty Sdn. Bhd.",
    "lines": ["Company No. 202601036648 (1698743-U)", "BO1-A-9, Menara 2, KL Eco City", "3 Jalan Bangsar",
              "59200 Kuala Lumpur, Malaysia"],
    "bank": "35600176786 - Hong Leong Bank",
}

# Everything that differs between document types. Wording only; the layout never changes.
DOC_TYPES = {
    "invoice": {
        "title": "Invoice",
        "info": [("Invoice Date", "date"), ("Due Date", "due_date"), ("Source", "source")],
        "prices": True, "total_label": "Total", "show_payment": True,
        "terms_label": "Payment terms",
    },
    "proforma": {
        "title": "Pro Forma Invoice",
        "info": [("Invoice Date", "date"), ("Due Date", "due_date"), ("Source", "source")],
        "prices": True, "total_label": "Total", "show_payment": True,
        "terms_label": "Payment terms",
        "fixed_note": "This is a pro forma invoice and not a tax invoice. A final invoice will be issued once payment is received.",
    },
    "quotation": {
        "title": "Quotation",
        "info": [("Quotation Date", "date"), ("Valid Until", "valid_until"), ("Source", "source")],
        "prices": True, "total_label": "Total", "show_payment": False,
        "terms_label": "Payment terms",
        "fixed_note": "This quotation is valid until the date above. Prices are in Malaysian Ringgit.",
    },
    "receipt": {
        "title": "Official Receipt",
        "info": [("Receipt Date", "date"), ("Payment Method", "payment_method"), ("For Invoice", "source")],
        "prices": True, "total_label": "Amount Received", "show_payment": False,
        "terms_label": None,
        "fixed_note": "Received with thanks.",
    },
    "delivery_order": {
        "title": "Delivery Order",
        "info": [("Delivery Date", "date"), ("Reference", "source"), ("Deliver To", "deliver_to")],
        "prices": False, "total_label": None, "show_payment": False,
        "terms_label": None, "signatures": True,
    },
    "credit_note": {
        "title": "Credit Note",
        "info": [("Credit Note Date", "date"), ("Original Invoice", "original_invoice"), ("Reason", "reason")],
        "prices": True, "total_label": "Total Credit", "show_payment": False,
        "terms_label": None,
    },
}

NUMBER_PREFIX = {  # house numbering; real invoices and credit notes take their number from Odoo
    "invoice": "ATL/", "proforma": "PF/", "quotation": "QT/", "receipt": "OR/",
    "delivery_order": "DO/", "credit_note": "RATL/",
}


def money(v: Decimal) -> str:
    return f"{v:,.2f}"


def dec(v) -> Decimal:
    return Decimal(str(v)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def check(doc: dict) -> list[str]:
    """Problems that must be fixed before a document goes out."""
    errs = []
    t = doc.get("type")
    if t not in DOC_TYPES:
        return [f"type must be one of {', '.join(DOC_TYPES)}"]
    spec = DOC_TYPES[t]
    num = str(doc.get("number", ""))
    if not num:
        errs.append("number is missing")
    elif not num.startswith(NUMBER_PREFIX[t]):
        errs.append(f"{t} numbers start with {NUMBER_PREFIX[t]} (got {num})")
    for label, key in spec["info"]:
        if not doc.get(key):
            errs.append(f"'{key}' is missing ({label})")
    bt = doc.get("bill_to") or {}
    if not bt.get("name"):
        errs.append("bill_to.name is missing")
    if not doc.get("lines"):
        errs.append("at least one line is required")
    for i, ln in enumerate(doc.get("lines", []), 1):
        if not ln.get("description"):
            errs.append(f"line {i}: description is missing")
        if spec["prices"] and "unit_price" not in ln:
            errs.append(f"line {i}: unit_price is missing")
    text = json.dumps(doc, ensure_ascii=False)
    if "—" in text or "–" in text:
        errs.append("remove em/en dashes from the wording")
    return errs


def build_html(doc: dict) -> str:
    spec = DOC_TYPES[doc["type"]]
    e = lambda s: html.escape(str(s))  # noqa: E731
    font = ASSETS / "fonts"

    bt = doc["bill_to"]
    bill_lines = [f"<b>{e(bt['name'])}</b>"]
    if bt.get("attn"):
        bill_lines.append(f"Attn: {e(bt['attn'])}")
    for a in bt.get("address", []):
        bill_lines.append(e(a))
    if bt.get("tel"):
        bill_lines.append(f"Tel: {e(bt['tel'])}")
    if bt.get("email"):
        bill_lines.append(f"Email: {e(bt['email'])}")

    info = "".join(f"<div><b>{e(label)}</b>{e(doc[key])}</div>" for label, key in spec["info"])

    rows, total = [], Decimal("0")
    for ln in doc["lines"]:
        qty = dec(ln.get("quantity", 1))
        uom = ln.get("uom", "units")
        if spec["prices"]:
            unit = dec(ln["unit_price"])
            amount = (qty * unit).quantize(Decimal("0.01"))
            total += amount
            rows.append(f"<tr><td>{e(ln['description'])}</td><td class='r'>{money(qty)} {e(uom)}</td>"
                        f"<td class='r'>{money(unit)}</td><td class='r'>RM {money(amount)}</td></tr>")
        else:
            rows.append(f"<tr><td>{e(ln['description'])}</td><td class='r'>{money(qty)} {e(uom)}</td></tr>")
    head = ("<tr><th>Description</th><th class='r' style='width:22mm'>Quantity</th>"
            "<th class='r' style='width:19mm'>Unit Price</th><th class='r' style='width:25mm'>Amount</th></tr>"
            if spec["prices"] else
            "<tr><th>Description</th><th class='r' style='width:30mm'>Quantity</th></tr>")
    total_html = (f"<div class='tot'><div><span>{e(spec['total_label'])}</span><span>RM {money(total)}</span></div></div>"
                  if spec["total_label"] else "")

    after = []
    if spec.get("terms_label") and doc.get("payment_terms"):
        after.append(f"<p style='margin-top:4mm'>{e(spec['terms_label'])}: {e(doc['payment_terms'])}</p>")
    if spec["show_payment"]:
        ref = doc.get("payment_reference") or doc["number"]
        after.append(f"<p style='margin-top:11mm'>Payment Communication: <b>{e(ref)}</b></p>"
                     f"<p>on this account: <b>{e(COMPANY['bank'])}</b></p>")
    notes = list(doc.get("notes", []))
    if spec.get("fixed_note"):
        notes.append(spec["fixed_note"])
    for i, n in enumerate(notes):
        after.append(f"<p class='muted' style='margin-top:{'3.6mm' if i == 0 else '1mm'}'>Note: {e(n)}</p>")
    if spec.get("signatures"):
        after.append("<div class='sign'><div>Issued by<br><br><br>Atlas DSF</div>"
                     "<div>Received in good order by<br><br><br>Name, signature, date and company stamp</div></div>")

    company = "<br>".join([f"<b>{e(COMPANY['name'])}</b>"] + [e(x) for x in COMPANY["lines"]])

    return f"""<!doctype html><html><head><meta charset="utf-8"><title>{e(spec['title'])} {e(doc['number'])}</title><style>
@font-face{{font-family:Lato;font-weight:400;src:url({(font / 'lato-latin-400-normal.woff2').as_uri()})}}
@font-face{{font-family:Lato;font-weight:700;src:url({(font / 'lato-latin-700-normal.woff2').as_uri()})}}
@page{{size:A4;margin:0}}
*{{box-sizing:border-box}}
html,body{{margin:0;width:210mm;height:297mm}}
body{{font:10pt/1.55 Lato,sans-serif;color:#222;position:relative}}
.page{{position:absolute;left:9.4mm;right:9.4mm;top:7.6mm;bottom:0}}
.head{{display:flex;gap:7mm;align-items:flex-start}}
.head img{{width:56mm;margin-top:1.2mm}}
.co{{font-size:9.6pt;line-height:1.6}}
.bill{{margin:25mm 0 0 111.5mm}}
.bill .lab{{font-weight:700;font-size:7.6pt;letter-spacing:1.4px;padding-bottom:1.2mm;border-bottom:1px solid #1e3a2f}}
.bill .who{{font-size:9.6pt;line-height:1.6;margin-top:2mm}}
h1{{font-weight:400;font-size:20.5pt;margin:6.5mm 0 3mm;color:#222}}
.info{{display:grid;grid-template-columns:1fr 1fr 1fr;font-size:9.6pt}}
.info b{{display:block}}
table{{width:100%;border-collapse:collapse;margin-top:9mm;font-size:9.6pt}}
th{{font-weight:400;text-align:left;padding:0 1mm 2.4mm;border-bottom:1px solid #222}}
td{{padding:3.4mm 1mm 1.2mm;vertical-align:top}}
.r{{text-align:right;white-space:nowrap}}
.tot{{display:flex;justify-content:flex-end;margin-top:3.6mm}}
.tot div{{border-top:1px solid #222;padding:2.4mm 1mm 0 4mm;font-weight:700;display:flex;gap:4mm;justify-content:flex-end;white-space:nowrap;min-width:42mm}}
p{{margin:0;font-size:9.6pt}}
.muted{{color:#777}}
.sign{{display:grid;grid-template-columns:1fr 1fr;gap:20mm;margin-top:22mm;font-size:9.6pt}}
.sign div{{border-top:1px solid #222;padding-top:2mm}}
.foot{{position:absolute;left:9.4mm;right:9.4mm;bottom:24.5mm;border-top:1px solid #222;text-align:center;padding-top:2.6mm;font-size:9.6pt;color:#666}}
</style></head><body>
<div class="page">
  <div class="head"><img src="{(ASSETS / 'logo.png').as_uri()}"><div class="co">{company}</div></div>
  <div class="bill"><div class="lab">BILL TO</div><div class="who">{'<br>'.join(bill_lines)}</div></div>
  <h1>{e(spec['title'])} {e(doc['number'])}</h1>
  <div class="info">{info}</div>
  <table>{head}{''.join(rows)}</table>
  {total_html}
  {''.join(after)}
</div>
<div class="foot">Page 1 / 1</div>
</body></html>"""


def render(doc: dict, out: Path) -> Path:
    from playwright.sync_api import sync_playwright

    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".html")
    tmp.write_text(build_html(doc), encoding="utf-8")
    with sync_playwright() as p:
        kw = {}
        for cand in ("/opt/pw-browsers/chromium-1194/chrome-linux/chrome",):
            if Path(cand).exists():
                kw["executable_path"] = cand
        b = p.chromium.launch(args=["--allow-file-access-from-files"], **kw)
        pg = b.new_page()
        pg.goto(tmp.as_uri())
        pg.evaluate("document.fonts.ready")
        pg.pdf(path=str(out), format="A4", print_background=True, prefer_css_page_size=True)
        b.close()
    tmp.unlink()
    return out


def default_name(doc: dict) -> str:
    title = DOC_TYPES[doc["type"]]["title"].replace(" ", "_")
    return f"{title}_{doc['number'].replace('/', '-')}_-_{doc['bill_to']['name'].replace(' ', '_')}.pdf"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("json", nargs="+")
    ap.add_argument("-o", "--out", help="output PDF (only with one JSON)")
    ap.add_argument("--check", action="store_true", help="validate only, do not render")
    a = ap.parse_args()
    bad = False
    for path in a.json:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
        errs = check(doc)
        if errs:
            bad = True
            print(f"{path}:\n  " + "\n  ".join(errs), file=sys.stderr)
            continue
        if a.check:
            print(f"{path}: OK")
            continue
        out = Path(a.out) if a.out and len(a.json) == 1 else Path(path).with_name(default_name(doc))
        print(render(doc, out))
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
