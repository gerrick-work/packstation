---
name: atlas-documents
description: Issue Atlas DSF business documents (invoice, pro forma invoice, quotation, official receipt, delivery order, credit note) as finished PDFs in the one approved design, copied from Odoo invoice ATL/2026/1001. Use whenever Gerrick asks for any of these documents for a client, or to "issue", "send", "make" or "redo" one.
---

# Atlas DSF documents

Every client document uses ONE design: the Odoo invoice ATL/2026/1001 (Atlas-DSF logo top left,
company block beside it, BILL TO block on the right, Lato, thin black rules, "Page 1 / 1" footer).
Documents differ only in wording and content. Never invent branding, colours, gradients, fonts,
headers or layouts, and never restyle a document "to look nicer".

The design lives in `documents/render.py` (repo root). Do not hand-write HTML or CSS for a document.

## Steps

1. **Pick the type** (wording for each is fixed in `DOC_TYPES` in render.py):

   | Situation | type | Number |
   |---|---|---|
   | Client hasn't paid yet, needs something to pay against | `proforma` | `PF/2026/xxxx` |
   | Payment received, or a normal sale booked in Odoo | `invoice` | `ATL/2026/xxxx`, taken from Odoo |
   | Price offer before the client commits | `quotation` | `QT/2026/xxxx` |
   | Confirming money received | `receipt` | `OR/2026/xxxx` |
   | Goods going out (no prices) | `delivery_order` | `DO/2026/xxxx` |
   | Refund or correction against an invoice | `credit_note` | `RATL/2026/xxxx`, taken from Odoo |

   Rule of thumb: not paid yet = pro forma. Paid = invoice (then a receipt if asked).
   Invoice and credit note numbers come from Odoo. Never make one up. If Odoo can't be read,
   say so and ask for the number.

2. **Gather the facts, never guess them.** Client legal name, attention, tel, email, the
   project source line, line items, quantities and unit prices. Look in Odoo (when connected),
   then Google Drive (e.g. the "Fresh Collection: Internal Key" docs and the
   "Atlas_DSF_Catalogue_and_Quote_Engine" sheet), then earlier documents for that client.
   Reuse the client's details exactly as on their previous document.
   Anything you still had to assume (a unit price split, a deposit being paid) must be
   listed back to Gerrick when you deliver the PDF.

3. **Write the JSON** (copy the matching file in `documents/examples/`). Fields:
   `type`, `number`, `date` (DD/MM/YYYY), the type's info fields (`due_date`, `valid_until`,
   `payment_method`, `deliver_to`, `original_invoice`, `reason`), `source`,
   `bill_to {name, attn, tel, email, address[]}`, `lines [{description, quantity, uom, unit_price}]`,
   `payment_terms`, optional `notes []` and `payment_reference`.
   - Deposits already paid go in as a negative line: `"Less: sample deposit paid (Invoice ATL/2026/1001)"`.
   - Amounts are RM; totals are calculated by the renderer, never typed.
   - No em dashes or en dashes in any wording.

4. **Render**: `python documents/render.py client.json -o out.pdf` (`--check` validates only).
   It refuses missing fields, wrong number prefixes and dashes.

5. **Look at it** (render page 1 to PNG and view it) before sending it to Gerrick.

6. **Deliver** the PDF to the chat. Keep client documents and their JSON out of git; they
   contain client contact details. Only the made-up examples belong in the repo.

## Fixed company details (in render.py, change only there)

Atlas DSF Fragrance and Beauty Sdn. Bhd., Company No. 202601036648 (1698743-U),
BO1-A-9, Menara 2, KL Eco City, 3 Jalan Bangsar, 59200 Kuala Lumpur, Malaysia.
Bank: 35600176786 - Hong Leong Bank. Issuer is always Atlas DSF, never Pangaea Labs.

## Adding a new document type

Add one entry to `DOC_TYPES` and one to `NUMBER_PREFIX`, plus an example JSON. Only the title,
info-row labels and notes may differ. Do not add new styling.
