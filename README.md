# atlasdsf

## Pack Station: one-click waybills and product labels

A small app for the packing Mac. It scans Shopee, TikTok Shop and Odoo for orders that are ready
to pack. One click on **Print all** then goes through them in ship-by order:

1. the order's **waybill** (fetched from Shopee or TikTok) on the waybill printer
2. that order's **product labels** (one per unit) on the label printer

Because both printers run order by order, the two stacks come out in the same order. Every product
label also carries a **match code**: the last 4 characters of the tracking number, which is
printed on the waybill too (for example `#871A`). Orders are remembered once printed, so Print all
never prints anything twice; use **Reprint** on a row when you need a copy.

![Pack Station](docs/screenshot.png)

### Start it

Double-click `scripts/start-mac.command`. The first run installs what it needs (Python 3 comes
with macOS developer tools; run `xcode-select --install` once if it's missing). Then it opens
http://localhost:8765. It starts in **demo mode**: made-up orders, and "printing" just saves
files into `data/output/`, so you can try it safely.

Terminal alternatives: `python -m packstation scan` lists what it finds;
`python -m packstation print-all` prints without the page; `python -m packstation test-label`
prints one sample product label.

### Set it up for real: `config.toml`

`config.toml` holds your keys and is never committed. The first start creates it from
`config.example.toml`.

| Section | What to fill in |
|---|---|
| `[shopee]` | Shopee Open Platform app `partner_id` and `partner_key`, `shop_id`, and the first `access_token` and `refresh_token` from authorising your shop. Tokens refresh automatically after that. |
| `[tiktok]` | TikTok Shop Partner Center `app_key`, `app_secret`, `shop_cipher` and tokens. |
| `[odoo]` | URL, database, a user's login and an **API key** (Odoo: Preferences → Account Security → New API Key). `platform_ref_field` is the sale order field where your connector stores the Shopee/TikTok order number. |
| `arrange` | Shopee and TikTok only issue a waybill once shipment is arranged. `"off"` (default) means you arrange it in Seller Centre first, and those orders show **needs arranging**. Set `"dropoff"` or `"pickup"` to let Print all arrange it. |

Set `enabled = true` on each source you fill in, and `[demo] enabled = false`.

Odoo deliveries that belong to a Shopee or TikTok order are merged with that order, and Odoo gets
a "printed" note on the delivery. Odoo-only orders (for example WhatsApp sales) print product
labels only.

### Product labels: `labels/products.csv`

One row per product. This file is the master list, kept in git so it's backed up and editable
from GitHub:

```
sku,title,size,brand,icon
ML-SANDAL-3G,Sandalwood,3g,moleculelabs,corrosive
```

`sku` must match the SKU on Shopee, TikTok and Odoo. `icon` is a PNG in `assets/icons/`, or
leave it empty. Products missing from the list still print: the title and size are taken from
the marketplace name, and the print summary flags them so you can add them. The **label** link
on each order previews its labels.

### Printers (AiYin E40 Pro, without Label Expert)

Each printer in `config.toml` has a `backend`:

- `file`: test mode, saves to `data/output/` (default)
- `tspl`: sends label commands straight to the printer over USB, with no driver and no Label Expert
- `cups`: an ordinary macOS printer queue, if a working driver exists

To set up `tspl`:

1. Plug both printers in by USB and switch them on.
2. Run `scripts/printer-check-mac.sh` and copy the `usb://...` address of each printer.
3. Create a raw queue for each (macOS asks for your password):
   ```
   sudo lpadmin -p AiYin_Waybill -E -v "usb://...address of printer 1..." -m raw
   sudo lpadmin -p AiYin_Labels  -E -v "usb://...address of printer 2..." -m raw
   ```
4. In `config.toml` set `backend = "tspl"` and `queue = "AiYin_Waybill"` (or `"AiYin_Labels"`),
   plus the real label sizes in mm.
5. Run `python -m packstation test-label`. If the label prints as a negative, set `invert = true`.
   If it drifts across labels, adjust `gap_mm`.

### Tests

```
python -m venv .venv && .venv/bin/pip install -r requirements.txt pytest
.venv/bin/python -m pytest -q
```
