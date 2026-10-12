# Atlas DSF document templates

One design for every client document, copied from the Odoo invoice ATL/2026/1001. Only
wording and content change between document types.

| Type | Title on the page | Numbering |
|---|---|---|
| `invoice` | Invoice | `ATL/2026/xxxx` (from Odoo) |
| `proforma` | Pro Forma Invoice | `PF/2026/xxxx` |
| `quotation` | Quotation | `QT/2026/xxxx` |
| `receipt` | Official Receipt | `OR/2026/xxxx` |
| `delivery_order` | Delivery Order (no prices, signature lines) | `DO/2026/xxxx` |
| `credit_note` | Credit Note | `RATL/2026/xxxx` (from Odoo) |

## Make a document

```
python documents/render.py documents/examples/proforma.json -o Pro_Forma.pdf
python documents/render.py my.json --check      # validate only
```

Copy the example for the type you need, change the content and render it. The renderer works
out the totals and refuses missing fields, wrong number prefixes and em/en dashes.

Needs Python with `playwright` and a Chromium browser (`pip install playwright && playwright install chromium`).

## Rules

- Never restyle. The layout, logo, Lato font and rules are fixed in `render.py`.
- Company and bank details are set once in `render.py` (`COMPANY`).
- Client documents and their JSON stay out of git. Only the made-up examples live here.
- Full instructions for Claude: `.claude/skills/atlas-documents/SKILL.md`.
