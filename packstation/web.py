"""The Pack Station screen (packstation/static/index.html) and the small JSON API behind it.
Served on http://localhost:8765 on the packing Mac."""
from __future__ import annotations

import io
from datetime import datetime, timedelta

from flask import Flask, jsonify, request, send_file
from PIL import Image

from .engine import Engine, Result
from .labels import LabelData, icon_names, labels_for_order, render_product_label
from .models import Order


def _png(img: Image.Image):
    buf = io.BytesIO()
    img.convert("L").save(buf, "PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png", max_age=0)


def _result(r: Result) -> dict:
    return {"key": r.key, "ok": r.ok, "message": r.message, "labels": r.labels, "guessed": r.guessed_skus}


def create_app(engine: Engine) -> Flask:
    app = Flask(__name__, static_folder="static", static_url_path="/static")

    def order_json(o: Order, printed: dict, packed: dict) -> dict:
        if o.key in packed:
            state = "packed"
        elif o.key in printed:
            state = "printed"
        elif o.needs_arrange:
            state = "arrange"
        else:
            state = "ready"
        return {
            "key": o.key, "source": o.source, "order_id": o.order_id, "tracking": o.tracking,
            "match": o.match_code, "status": o.status, "state": state,
            "ship_by": o.ship_by.isoformat() if o.ship_by else None,
            "items": [{"sku": i.sku, "name": i.name, "qty": i.qty,
                       "known": engine.catalog.lookup(i)[1]} for i in o.items],
            "units": o.unit_count, "has_waybill": o.has_waybill,
            "printed_at": printed.get(o.key), "packed_at": packed.get(o.key),
        }

    @app.get("/")
    def index():
        return app.send_static_file("index.html")

    @app.get("/api/state")
    def state():
        if not engine.job.running and (not engine.last_scan
                                       or datetime.now() - engine.last_scan > timedelta(minutes=2)):
            engine.scan()
        printed, packed = engine.store.printed(), engine.store.packed()
        pending = engine.pending()
        job = engine.job
        return jsonify({
            "orders": [order_json(o, printed, packed) for o in engine.orders],
            "pending": {"orders": len(pending), "labels": sum(o.unit_count for o in pending)},
            "sources": [{"name": s.name, "error": engine.source_errors.get(s.name)} for s in engine.sources],
            "printers": {
                name: {"backend": p.cfg.get("backend", "file"), "queue": p.cfg.get("queue"),
                       "size": f"{p.width_mm:g} × {p.height_mm:g} mm"}
                for name, p in (("waybill", engine.waybill_printer), ("label", engine.label_printer))
            },
            "last_scan": engine.last_scan.isoformat() if engine.last_scan else None,
            "job": {"running": job.running, "total": job.total, "done": job.done, "current": job.current,
                    "results": [_result(r) for r in job.results],
                    "finished_at": job.finished_at.isoformat() if job.finished_at else None},
        })

    @app.post("/api/scan")
    def scan():
        if not engine.job.running:
            engine.scan()
        return jsonify(ok=True)

    @app.post("/api/print-all")
    def print_all():
        return jsonify(started=engine.start_print_all())

    @app.post("/api/print/<path:key>")
    def print_one(key):
        if engine.job.running:
            return jsonify(_result(Result(key, False, "A print run is in progress"))), 409
        engine.store.unmark(key)
        return jsonify(_result(engine.reprint(key)))

    @app.post("/api/packed/<path:key>")
    def packed(key):
        engine.store.mark_packed(key, bool((request.get_json(silent=True) or {}).get("packed", True)))
        return jsonify(ok=True)

    @app.get("/api/find")
    def find():
        o = engine.find(request.args.get("code", ""))
        if not o:
            return jsonify(found=False)
        return jsonify(found=True, order=order_json(o, engine.store.printed(), engine.store.packed()))

    @app.get("/api/preview/<path:key>")
    def preview(key):
        order = next((o for o in engine.orders if o.key == key), None)
        if not order or not order.items:
            return "No such order", 404
        seen, imgs = set(), []
        for img, item, _ in labels_for_order(order, engine.catalog, engine.label_cfg):
            if (item.sku, item.name) not in seen:
                seen.add((item.sku, item.name))
                imgs.append(img.convert("L"))
        gap = 16
        sheet = Image.new("L", (max(i.width for i in imgs), sum(i.height for i in imgs) + gap * (len(imgs) - 1)), 235)
        y = 0
        for i in imgs:
            sheet.paste(i, (0, y))
            y += i.height + gap
        return _png(sheet)

    # ---- label library ----------------------------------------------------------

    @app.get("/api/labels")
    def labels():
        return jsonify(rows=engine.catalog.all_rows(), icons=icon_names(), brand=engine.catalog.default_brand)

    @app.put("/api/labels")
    def save_labels():
        rows = (request.get_json(silent=True) or {}).get("rows", [])
        engine.catalog.save(rows)
        return jsonify(ok=True, count=len(engine.catalog.rows))

    def _label_from_args() -> LabelData:
        a = request.args
        return LabelData(a.get("title", "") or "Product", a.get("size", ""),
                         a.get("brand", "") or engine.catalog.default_brand, a.get("icon", ""))

    @app.get("/api/label.png")
    def label_png():
        c = engine.label_cfg
        img = render_product_label(_label_from_args(), request.args.get("code") or None,
                                   float(c["width_mm"]), float(c["height_mm"]), int(c.get("dpi", 203)),
                                   c.get("font"))
        return _png(img)

    # ---- printer tests ----------------------------------------------------------

    @app.post("/api/test/label")
    def test_label():
        c = engine.label_cfg
        body = request.get_json(silent=True) or {}
        data = LabelData(body.get("title") or "Sandalwood", body.get("size", "3g"),
                         body.get("brand") or engine.catalog.default_brand, body.get("icon", "corrosive"))
        try:
            img = render_product_label(data, "TEST", float(c["width_mm"]), float(c["height_mm"]),
                                       int(c.get("dpi", 203)), c.get("font"))
            return jsonify(ok=True, message=f"Sent to label printer: {engine.label_printer.print_image(img, 'test-label')}")
        except Exception as e:
            return jsonify(ok=False, message=str(e)), 500

    @app.post("/api/test/waybill")
    def test_waybill():
        from .sources.demo import DemoSource
        try:
            demo = DemoSource()
            pdf = demo.get_waybill(demo.list_orders()[0])
            return jsonify(ok=True, message=f"Sent to waybill printer: {engine.waybill_printer.print_pdf(pdf, 'test-waybill')}")
        except Exception as e:
            return jsonify(ok=False, message=str(e)), 500

    return app
