# atlasdsf

## Pack Station: one-click waybills and bottle labels

A touch-friendly station that runs on the packing Mac and opens on any phone or iPad on the same
Wi-Fi (Settings shows a QR code; "Add to Home Screen" gives it an app icon). It reads ready-to-ship
deliveries from Odoo, with waybills from the Odoo Shopee and TikTok connectors, and prints them on
two AiYin E40 Pro printers over Bluetooth.

- **Refresh** runs Odoo's Shopee/TikTok sync jobs first (Shopee orders, TikTok orders, TikTok
  delivery sync, Shopee labels), then re-reads everything and says "Refresh successful HH:MM:SS,
  N new orders". The Odoo user needs Settings/Administrator rights for this.
- **Print all** prints, order by order: the waybill (at its real size, no rescaling) on the waybill
  printer, then that order's bottle labels, then one blank label. An order is marked printed only
  when all of it was sent. TikTok orders still needing a courier are booked only after you confirm
  on screen (drop-off or pickup, set in Settings).
- Orders with no waybill show **WAYBILL-MISSING**: never printed, never marked, no Odoo note.
- **Hold orders with missing info** (switch under Print all): Print all also skips any order with no
  waybill (e.g. Odoo-only orders), no tracking number, or a product that isn't in the label list
  (**INFO-MISSING**). Printing one of those on its own asks first ("Print anyway").
- Each order shows its **grand total** (from Shopee, TikTok or the Odoo sale order). Orders can be
  shown as **cards or a list** (the toggle top right of the orders; each device remembers its choice).
- **Reprint one part**: from a selected order, reprint just its waybill or just all its bottle
  labels. A part reprint doesn't change whether the order counts as printed.
- **Bottle labels** use one fixed template (Label Expert "Atlas Origin Finalv1"); the product list
  is edited on the Labels screen and kept in `data/products.csv`. Default brand: "atlas origins".
- **Printers** screen: calibration page (border, mm ruler, centre cross), nudge buttons for
  left/right, up/down and label gap, separator choice, test prints.
- Live status per printer ("Order #278A, label 2 of 3", sending %), persistent results with times,
  a lock per order across all devices, and "Already printed at HH:MM, print again?".
- Fixed **error codes** (ODOO-AUTH, ODOO-NET, ODOO-CRON, WAYBILL-MISSING, TIKTOK-ARRANGE,
  BT-NOTFOUND, BT-PERMISSION, BT-SEND, PRINT-PARTIAL, CONFIG, INFO-MISSING) on cards, the status bar, the Log
  screen and `data/packstation.log`.
- Settings: TikTok courier type, default brand, optional 4-digit PIN for other devices,
  **Install an update** (choose the GitHub ZIP; keeps config.toml and data/) and **Restart**.

### Start it

Double-click `scripts/start-mac.command`. The first run installs what it needs (Python 3 comes
with macOS developer tools; run `xcode-select --install` once if it's missing). Then it opens
http://localhost:8765. Use the browser's full-screen mode for a kiosk feel. It starts in **demo mode**: made-up orders, and "printing" just saves
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
- `bluetooth`: the same, over Bluetooth (no pairing needed). Slower: a big waybill takes around
  15 to 25 seconds. Close Label Expert on your phone first; a printer talks to one device at a time
- `cups`: an ordinary macOS printer queue, if a working driver exists

To set up the printers (Bluetooth or USB), switch them on and run:

```
bash scripts/setup-printers-mac.command
```

It finds the printers, connects them, prints a big test number on each, asks which printer is
which (and whether the print came out the right way round), and saves the settings in
`config.toml`. If a printer doesn't accept direct printing, it stops without changing anything.

### Tests

```
python -m venv .venv && .venv/bin/pip install -r requirements.txt pytest
.venv/bin/python -m pytest -q
```
