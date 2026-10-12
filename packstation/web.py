"""The one-button page, served on http://localhost:8765 on the packing Mac."""
from __future__ import annotations

import io
from datetime import datetime, timedelta

from flask import Flask, redirect, render_template_string, send_file, url_for

from .engine import Engine
from .labels import labels_for_order

PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Pack Station</title>
<style>
 :root{--g:#2C463A;--g2:#4F7A60;--bad:#B3261E;--muted:#667;--line:#e4e7e5}
 body{font:15px/1.45 -apple-system,system-ui,sans-serif;margin:0;background:#f6f7f6;color:#111}
 main{max-width:1100px;margin:0 auto;padding:24px 16px}
 h1{margin:0 0 4px;font-size:24px;color:var(--g)} .muted{color:var(--muted)}
 .bar{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin:18px 0}
 button{font:inherit;border:0;border-radius:12px;padding:12px 18px;cursor:pointer;background:#fff;border:1px solid var(--line)}
 .big{font-size:22px;font-weight:600;padding:20px 34px;color:#fff;border:0;
      background:linear-gradient(135deg,#1F3429,#2C463A 45%,#4F7A60)}
 .big:disabled{background:#c9cfcb;cursor:default}
 table{width:100%;border-collapse:collapse;background:#fff;border-radius:14px;overflow:hidden}
 th,td{text-align:left;padding:10px 12px;border-bottom:1px solid var(--line);vertical-align:top}
 th{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted)}
 .code{font:600 18px ui-monospace,Menlo,monospace;color:var(--g)}
 .pill{display:inline-block;border-radius:99px;padding:2px 10px;font-size:12px;background:#eef2ef;color:var(--g)}
 .warn{background:#fdecea;color:var(--bad)} .done{opacity:.55}
 .box{background:#fff;border-radius:14px;padding:12px 16px;margin:12px 0;border-left:4px solid var(--g)}
 .box.err{border-color:var(--bad)} form{display:inline}
</style></head><body><main>
<h1>Pack Station</h1>
<div class="muted">Last scan {{ last_scan or "never" }} · sources: {{ sources|join(", ") or "none enabled" }}</div>

{% for name, err in source_errors.items() %}
 <div class="box err"><b>{{ name }}</b> could not be read: {{ err }}</div>
{% endfor %}
{% if results %}
 <div class="box"><b>Last print run</b><br>
 {% for r in results %}{{ "✔" if r.ok else "✖" }} {{ r.key }}: {{ r.message }}
   {% if r.guessed_skus %}<span class="pill warn">not in label list: {{ r.guessed_skus|join(", ") }}</span>{% endif %}<br>{% endfor %}
 </div>
{% endif %}

<div class="bar">
 <form method="post" action="{{ url_for('print_all') }}"
       onsubmit="this.querySelector('button').disabled=true;this.querySelector('button').innerText='Printing…'">
  <button class="big" {% if not pending %}disabled{% endif %}>
   Print all · {{ pending|length }} orders · {{ pending_labels }} labels</button>
 </form>
 <form method="post" action="{{ url_for('scan') }}"><button>Scan for new orders</button></form>
</div>

<table>
 <tr><th>Ship by</th><th>Order</th><th>Match</th><th>Items</th><th>Status</th><th></th></tr>
 {% for o in orders %}
 {% set when = printed.get(o.key) %}
 <tr class="{{ 'done' if when }}">
  <td>{{ o.ship_by.strftime("%a %d %b %H:%M") if o.ship_by else "-" }}</td>
  <td><span class="pill">{{ o.source }}</span> {{ o.order_id }}<br><span class="muted">{{ o.tracking or "" }}</span></td>
  <td class="code">#{{ o.match_code }}</td>
  <td>{% for i in o.items %}{{ i.qty }} × {{ i.name }}<br>{% endfor %}</td>
  <td>{% if when %}Printed {{ when[11:16] }}
      {% elif o.needs_arrange %}<span class="pill warn">needs arranging</span>
      {% elif not o.has_waybill %}labels only{% else %}ready{% endif %}</td>
  <td><a href="{{ url_for('preview', key=o.key) }}" target="_blank">label</a>
   <form method="post" action="{{ url_for('reprint', key=o.key) }}"><button>{{ "Reprint" if when else "Print" }}</button></form></td>
 </tr>
 {% else %}<tr><td colspan="6" class="muted">Nothing to pack.</td></tr>{% endfor %}
</table>
</main></body></html>"""


def create_app(engine: Engine) -> Flask:
    app = Flask(__name__)
    state = {"results": []}

    def ensure_fresh():
        if not engine.last_scan or datetime.now() - engine.last_scan > timedelta(minutes=2):
            engine.scan()

    @app.get("/")
    def index():
        ensure_fresh()
        pending = engine.pending()
        return render_template_string(
            PAGE, orders=engine.orders, pending=pending, pending_labels=sum(o.unit_count for o in pending),
            printed=engine.store.printed(), results=state["results"], source_errors=engine.source_errors,
            sources=[s.name for s in engine.sources],
            last_scan=engine.last_scan.strftime("%H:%M:%S") if engine.last_scan else None)

    @app.post("/scan")
    def scan():
        engine.scan()
        return redirect(url_for("index"))

    @app.post("/print-all")
    def print_all():
        engine.scan()  # never print from a stale list
        state["results"] = engine.print_all()
        return redirect(url_for("index"))

    @app.post("/reprint/<path:key>")
    def reprint(key):
        engine.store.unmark(key)
        state["results"] = [engine.reprint(key)]
        return redirect(url_for("index"))

    @app.get("/preview/<path:key>")
    def preview(key):
        order = next((o for o in engine.orders if o.key == key), None)
        if not order or not order.items:
            return "No such order", 404
        labels = labels_for_order(order, engine.catalog, engine.cfg.get("printers", {}).get("label", {}))
        # Stack this order's distinct labels vertically for a quick look.
        seen, imgs = set(), []
        for img, item, _ in labels:
            if item.sku not in seen:
                seen.add(item.sku)
                imgs.append(img.convert("L"))
        from PIL import Image
        sheet = Image.new("L", (max(i.width for i in imgs) + 20, sum(i.height + 20 for i in imgs) + 20), 200)
        y = 20
        for i in imgs:
            sheet.paste(i, (10, y))
            y += i.height + 20
        buf = io.BytesIO()
        sheet.save(buf, "PNG")
        buf.seek(0)
        return send_file(buf, mimetype="image/png")

    return app
