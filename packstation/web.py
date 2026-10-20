"""The Pack Station screen (packstation/static/index.html) and the JSON API behind it.

Served on port 8765 of the Mac to every device on the home Wi-Fi. All state lives on the Mac, so
every phone/iPad sees the same live status. Optional 4-digit PIN (Settings) for other devices.
"""
from __future__ import annotations

import hashlib
import hmac
import io
import os
import socket
import sys
import threading
import time

from flask import Flask, jsonify, request, send_file
from PIL import Image, ImageDraw

from .engine import Busy, Engine
from .errors import CODES, PackError
from .labels import LabelData, icon_names, labels_for_order, render_product_label

LOCAL = {"127.0.0.1", "::1"}


def _png(img: Image.Image):
    buf = io.BytesIO()
    img.save(buf, "PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png", max_age=0)


def lan_addresses(port: int) -> list[str]:
    """How other devices on the Wi-Fi reach this Mac: name.local first (survives IP changes), then IP."""
    out = []
    host = socket.gethostname()
    if host and host != "localhost":
        out.append(f"http://{host if host.endswith('.local') else host + '.local'}:{port}")
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        if not ip.startswith("127."):
            out.append(f"http://{ip}:{port}")
    except OSError:
        pass
    return out


def app_icon(size: int) -> Image.Image:
    from .icon import bean_icon
    return bean_icon(size)


def create_app(engine: Engine) -> Flask:
    app = Flask(__name__, static_folder="static", static_url_path="/static")
    secret = os.urandom(16)

    def pin() -> str:
        return str(engine.cfg.get("app", {}).get("pin") or "").strip()

    def token(p: str) -> str:
        return hmac.new(secret, p.encode(), hashlib.sha256).hexdigest()

    @app.before_request
    def gate():
        p = pin()
        if not p or request.remote_addr in LOCAL:
            return None
        open_paths = ("/login", "/api/login", "/manifest.webmanifest", "/icon-", "/apple-touch-icon")
        if request.path.startswith(open_paths) or request.cookies.get("ps_pin") == token(p):
            return None
        if request.path.startswith("/api/"):
            return jsonify(error="PIN required"), 401
        return app.send_static_file("login.html")

    @app.post("/api/login")
    def login():
        p = pin()
        given = str((request.get_json(silent=True) or {}).get("pin", ""))
        if p and hmac.compare_digest(given, p):
            resp = jsonify(ok=True)
            resp.set_cookie("ps_pin", token(p), max_age=60 * 60 * 24 * 365, samesite="Lax")
            return resp
        time.sleep(1)
        return jsonify(ok=False), 403

    def err(e: Exception, status: int = 400):
        if isinstance(e, PackError):
            return jsonify(ok=False, error=e.as_dict()), status
        if isinstance(e, Busy):
            return jsonify(ok=False, busy=True, message=str(e)), 409
        return jsonify(ok=False, error={"code": "CONFIG", "message": CODES["CONFIG"], "detail": str(e)}), 500

    def order_json(o, printed, packed, rows=None):
        rows = rows if rows is not None else engine.store.printed_rows()
        state = engine.state_of(o, printed, packed)
        error = None
        missing = engine.missing_info(o)
        if state == "waybill_missing":
            error = PackError("WAYBILL-MISSING", "checking waybill").as_dict()
        elif state == "held":
            error = PackError("INFO-MISSING", "checking order", "; ".join(missing)).as_dict()
        else:  # last failure for this order in recent jobs
            for j in engine.jobs:
                r = next((r for r in j.results if r.key == o.key), None)
                if r:
                    error = None if r.ok else r.error
                    break
        return {
            "key": o.key, "source": o.source, "platform": o.platform, "order_id": o.order_id,
            "tracking": o.tracking, "match": o.match_code, "status": o.status, "state": state,
            "ship_by": o.ship_by.isoformat() if o.ship_by else None,
            "items": [{"sku": i.sku, "name": i.name, "qty": i.qty, "known": known,
                       "bottles": max(data.bottles, 1), "size": data.size}
                      for i in o.items for data, known in [engine.catalog.lookup(i)]],
            "labels": engine.catalog.label_count(o),
            "units": o.unit_count, "has_waybill": o.has_waybill, "waybill_ready": o.waybill_ready,
            "printed_at": printed.get(o.key), "packed_at": packed.get(o.key), "error": error,
            "printed_labels": (rows.get(o.key) or {}).get("labels"),
            "total": o.total, "currency": o.currency, "missing": missing,
        }

    @app.get("/")
    def index():
        return app.send_static_file("index.html")

    @app.get("/manifest.webmanifest")
    def manifest():
        return jsonify({
            "name": "Pack Station", "short_name": "Pack Station", "start_url": "/", "display": "standalone",
            "background_color": "#FFFFFF", "theme_color": "#FFFFFF",
            "icons": [{"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
                      {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png"}],
        })

    @app.get("/icon-<int:size>.png")
    def icon(size):
        return _png(app_icon(max(32, min(size, 1024))))

    @app.get("/apple-touch-icon.png")
    def apple_icon():
        return _png(app_icon(180))

    @app.get("/api/state")
    def state():
        engine.reload_if_changed()
        if not engine.running() and engine.last_scan is None:
            engine.scan()
        printed, packed, rows = engine.store.printed(), engine.store.packed(), engine.store.printed_rows()
        with engine.lock:
            orders = list(engine.orders)
            jobs = [j.as_dict() for j in engine.jobs[:12]]
        port = int(engine.cfg.get("app", {}).get("port", 8765))
        return jsonify({
            "orders": [order_json(o, printed, packed, rows) for o in orders],
            "plan": engine.plan(),
            "sources": [{"name": s.name, "error": engine.source_errors.get(s.name)} for s in engine.sources],
            "printers": {
                n: {"backend": p.cfg.get("backend", "file"), "name": p.cfg.get("ble_name") or p.cfg.get("queue"),
                    "size": f"{p.width_mm:g} × {p.height_mm:g} mm", "width_mm": p.width_mm, "height_mm": p.height_mm,
                    "offset_x_mm": p.offset_x_mm, "offset_y_mm": p.offset_y_mm, "gap_mm": p.gap_mm,
                    "separator": p.cfg.get("separator", "blank") if n == "label" else None,
                    "status": p.status.as_dict()}
                for n, p in (("waybill", engine.waybill_printer), ("label", engine.label_printer))
            },
            "jobs": jobs,
            "busy": sorted(engine.busy_keys),
            "last_scan": engine.last_scan.isoformat() if engine.last_scan else None,
            "config_error": engine.config_error,
            "settings": {
                "tiktok_ship_method": engine.ship_method,
                "brand": engine.catalog.default_brand,
                "pin_set": bool(pin()),
                "hold_incomplete": engine.hold_incomplete,
                "odoo": bool(engine.odoo),
                "picking_states": engine.odoo.states if engine.odoo else ["assigned"],
                "picking_type_codes": engine.odoo.type_codes if engine.odoo else ["outgoing"],
            },
            "addresses": lan_addresses(port),
            "codes": CODES,
            "server_time": time.strftime("%H:%M:%S"),
            "version": __import__("packstation").__version__,
        })

    @app.post("/api/refresh")
    def refresh():
        try:
            return jsonify(ok=True, job=engine.start_refresh().as_dict())
        except Exception as e:
            return err(e)

    @app.get("/api/plan")
    def plan():
        return jsonify(engine.plan())

    @app.post("/api/print-all")
    def print_all():
        body = request.get_json(silent=True) or {}
        try:
            return jsonify(ok=True, job=engine.start_print_all(arrange=bool(body.get("arrange"))).as_dict())
        except Exception as e:
            return err(e)

    @app.post("/api/print/<path:key>")
    def print_one(key):
        body = request.get_json(silent=True) or {}
        try:
            r = engine.start_print_one(key, force=bool(body.get("force")), arrange=bool(body.get("arrange")),
                                       allow_incomplete=bool(body.get("allow_incomplete")))
        except Exception as e:
            return err(e)
        if isinstance(r, dict):
            return jsonify(ok=False, **r), 409
        return jsonify(ok=True, job=r.as_dict())

    @app.post("/api/unprint/<path:key>")
    def unprint(key):
        """Make an order look new again (testing, or printed by mistake). Platforms and Odoo are untouched."""
        if key in engine.busy_keys:
            return jsonify(ok=False, message="This order is printing right now"), 409
        engine.store.unmark(key)
        engine.store.mark_packed(key, False)
        return jsonify(ok=True, message="Marked as not printed")

    @app.post("/api/reprint/<path:key>")
    def reprint(key):
        part = (request.get_json(silent=True) or {}).get("part", "")
        try:
            return jsonify(ok=True, job=engine.start_reprint(key, part).as_dict())
        except Exception as e:
            return err(e)

    @app.post("/api/packed/<path:key>")
    def packed(key):
        want = bool((request.get_json(silent=True) or {}).get("packed", True))
        validated = False
        if want and engine.cfg.get("odoo", {}).get("validate_when_packed", True):
            order = next((o for o in engine.orders if o.key == key), None)
            odoo = next((s for s in engine.sources if getattr(s, "name", "") == "odoo"), None)
            if order is not None and odoo is not None and order.odoo_picking_id and hasattr(odoo, "validate"):
                try:
                    for pid in order.ref.get("odoo_picking_ids") or [order.odoo_picking_id]:
                        odoo.validate(pid)
                    validated = True
                    engine.log.add("info", f"Packed and validated in Odoo: {order.order_id}", None, key)
                except Exception as e:
                    return err(e)
        engine.store.mark_packed(key, want)
        return jsonify(ok=True, validated=validated,
                       message="Packed and validated in Odoo" if validated else ("Packed" if want else "Not packed"))

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

    # ---- label library ----------------------------------------------------------------------

    def label_warnings(row: dict) -> list[str]:
        c = engine.label_cfg
        w: list[str] = []
        render_product_label(LabelData(row.get("title") or "", row.get("size") or "",
                                       row.get("brand") or engine.catalog.default_brand, row.get("icon") or ""),
                             None, float(c["width_mm"]), float(c["height_mm"]), int(c.get("dpi", 203)),
                             c.get("font"), c.get("template"), warnings=w)
        return w

    @app.get("/api/labels")
    def labels():
        rows = engine.catalog.all_rows()
        for r in rows:
            r["warnings"] = label_warnings(r)
        return jsonify(rows=rows, icons=icon_names(), brand=engine.catalog.default_brand)

    @app.put("/api/labels")
    def save_labels():
        rows = (request.get_json(silent=True) or {}).get("rows", [])
        engine.catalog.save(rows)
        engine.log.add("info", f"Label list saved ({len(engine.catalog.rows)} products)")
        return jsonify(ok=True, count=len(engine.catalog.rows))

    @app.get("/api/label.png")
    def label_png():
        c = engine.label_cfg
        a = request.args
        data = LabelData(a.get("title", "") or "Product", a.get("size", ""),
                         a.get("brand", "") or engine.catalog.default_brand, a.get("icon", ""))
        img = render_product_label(data, a.get("code") or None, float(c["width_mm"]), float(c["height_mm"]),
                                   int(c.get("dpi", 203)), c.get("font"), c.get("template"))
        return _png(img.convert("L"))

    # ---- printers: tests, calibration, settings ------------------------------------------------

    @app.post("/api/test/<name>")
    def test(name):
        if name not in ("waybill", "label"):
            return "no such printer", 404
        body = request.get_json(silent=True) or {}
        data = None
        if name == "label" and body.get("title"):
            data = LabelData(body["title"], body.get("size", ""), body.get("brand") or engine.catalog.default_brand,
                             body.get("icon", ""))
        try:
            return jsonify(ok=True, job=engine.start_test(name, data).as_dict())
        except Exception as e:
            return err(e)

    @app.post("/api/print-labels")
    def print_labels():
        """Body {items: [{sku, name, qty}], title}: those bottle labels only, in one run."""
        from .models import Item

        body = request.get_json(silent=True) or {}
        items = [Item(str(i.get("sku", "")), str(i.get("name", "")), int(i.get("qty", 1)))
                 for i in body.get("items") or [] if i.get("name") or i.get("sku")]
        if not items:
            return err(PackError("CONFIG", "printing labels", "no items given"))
        try:
            return jsonify(ok=True, job=engine.start_print_labels(items, str(body.get("title") or "Bottle labels")).as_dict())
        except Exception as e:
            return err(e)

    @app.post("/api/calibrate/<name>")
    def calibrate(name):
        if name not in ("waybill", "label"):
            return "no such printer", 404
        try:
            return jsonify(ok=True, job=engine.start_calibration(name).as_dict())
        except Exception as e:
            return err(e)

    @app.post("/api/printer/<name>")
    def printer_settings(name):
        if name not in ("waybill", "label"):
            return "no such printer", 404
        body = request.get_json(silent=True) or {}
        vals = {}
        for k in ("offset_x_mm", "offset_y_mm", "gap_mm"):
            if k in body:
                v = round(float(body[k]), 1)
                if not -20 <= v <= 20:
                    return err(PackError("CONFIG", f"{name} printer {k}", "must be between -20 and 20 mm"))
                vals[k] = v
        if name == "label" and body.get("separator") in ("blank", "none", "divider"):
            vals["separator"] = body["separator"]
        if engine.running():
            return err(Busy("Wait for the current job to finish, then save."))
        try:
            engine.save_settings(f"printers.{name}", vals)
        except Exception as e:
            return err(e)
        engine.log.add("info", f"{name} printer settings saved: {vals}")
        return jsonify(ok=True)

    @app.post("/api/settings")
    def settings():
        body = request.get_json(silent=True) or {}
        if engine.running():
            return err(Busy("Wait for the current job to finish, then save."))
        try:
            if body.get("tiktok_ship_method") in ("dropoff", "pickup"):
                engine.save_settings("odoo", {"tiktok_ship_method": body["tiktok_ship_method"]})
            if body.get("include_waiting") is not None:
                states = ["assigned", "confirmed", "waiting"] if body["include_waiting"] else ["assigned"]
                engine.save_settings("odoo", {"picking_states": states})
            if body.get("include_dropship") is not None:
                codes = ["outgoing", "dropship"] if body["include_dropship"] else ["outgoing"]
                engine.save_settings("odoo", {"picking_type_codes": codes})
            if body.get("hold_incomplete") is not None:
                engine.save_settings("app", {"hold_incomplete": bool(body["hold_incomplete"])})
            if "brand" in body and str(body["brand"]).strip():
                engine.save_settings("labels", {"brand": str(body["brand"]).strip()})
            if "pin" in body:
                p = str(body["pin"]).strip()
                if p and not (p.isdigit() and len(p) == 4):
                    return err(PackError("CONFIG", "PIN", "use exactly 4 digits, or leave empty for no PIN"))
                engine.save_settings("app", {"pin": p})
        except Exception as e:
            return err(e)
        engine.log.add("info", "Settings saved")
        return jsonify(ok=True)

    @app.get("/api/odoo-check")
    def odoo_check():
        if not engine.odoo:
            return err(PackError("CONFIG", "checking Odoo", "Odoo isn't switched on in config.toml"))
        try:
            return jsonify(ok=True, states=engine.odoo.states, **engine.odoo.check())
        except Exception as e:
            from .errors import classify
            return err(classify(e, "checking Odoo"))

    # ---- Shopee Singapore (Direct shop): its own authorisation ------------------------------------

    def sg_source():
        from .sources.shopee_sip import ShopeeSipSource

        s = next((x for x in engine.sources if isinstance(x, ShopeeSipSource)), None)
        if s is None:
            if not engine.odoo:
                raise PackError("CONFIG", "connecting Singapore", "Odoo isn't switched on in config.toml")
            s = ShopeeSipSource(engine.odoo, ["SG"], data_dir=engine.data_dir)
        return s

    def sg_finish(code, shop_id=None, main_account_id=None):
        shops = sg_source().connect(code, shop_id=shop_id, main_account_id=main_account_id)
        regions = sorted({*(engine.cfg.get("odoo", {}).get("shopee_sip_regions") or []), *(x["region"] for x in shops)})
        engine.save_settings("odoo", {"shopee_sip_regions": regions})
        names = ", ".join(f"{x['shop_name'] or x['shop_id']} ({x['region']})" for x in shops)
        engine.log.add("info", f"Shopee shop connected: {names}")
        threading.Thread(target=engine.scan, daemon=True).start()
        return shops

    @app.get("/api/shopee-sg")
    def sg_status():
        try:
            tokens = sg_source().own_tokens()
        except Exception:
            tokens = {}
        t = tokens.get("SG") or {}
        return jsonify(ok=True, connected=bool(t), shop_name=t.get("shop_name", ""), shop_id=t.get("shop_id"),
                       connected_at=t.get("connected_at"), on=("SG" in (engine.cfg.get("odoo", {}).get("shopee_sip_regions") or [])),
                       error=engine.source_errors.get("shopee-sg"))

    @app.get("/shopee-sg/connect")
    def sg_connect():
        from flask import redirect

        try:
            # Shopee only sends you back to the app's registered domain (the Odoo site). Its page there
            # won't know the address, so Settings asks you to paste it ("Finish connecting").
            odoo_url = (engine.cfg.get("odoo", {}).get("url") or "").rstrip("/")
            back = engine.cfg.get("odoo", {}).get("shopee_redirect") or (
                odoo_url + "/packstation-singapore" if odoo_url else request.host_url.rstrip("/") + "/shopee-sg/callback")
            return redirect(sg_source().auth_url(back))
        except Exception as e:
            return f"<p style='font:18px sans-serif;padding:30px'>Couldn't start: {e}</p><p><a href='/'>Back</a></p>", 500

    @app.get("/shopee-sg/callback")
    def sg_callback():
        a = request.args
        page = "<div style='font:18px -apple-system,sans-serif;padding:40px;max-width:560px'>{}<p><a href='/'>Back to Pack Station</a></p></div>"
        if not a.get("code"):
            return page.format("<h2>Not connected</h2><p>Shopee didn't send a code back.</p>"), 400
        try:
            shops = sg_finish(a["code"], a.get("shop_id"), a.get("main_account_id"))
        except Exception as e:
            return page.format(f"<h2>Not connected</h2><p>{e}</p>"), 400
        names = ", ".join(f"{x['shop_name'] or x['shop_id']} ({x['region']})" for x in shops)
        return page.format(f"<h2>Connected ✓</h2><p>{names}. Its orders now show in Pack Station with the others.</p>")

    @app.post("/api/shopee-sg/paste")
    def sg_paste():
        """Fallback when Shopee sends you to another site: paste the address you landed on."""
        from urllib.parse import parse_qs, urlparse

        q = {k: v[0] for k, v in parse_qs(urlparse(str((request.get_json(silent=True) or {}).get("url", ""))).query).items()}
        if not q.get("code"):
            return err(PackError("SHOPEE-SG", "connecting Singapore", "that address has no code= in it"))
        try:
            return jsonify(ok=True, shops=sg_finish(q["code"], q.get("shop_id"), q.get("main_account_id")))
        except Exception as e:
            return err(e if isinstance(e, PackError) else PackError("SHOPEE-SG", "connecting Singapore", str(e)[:200]))

    @app.get("/api/log")
    def log():
        return jsonify(items=engine.log.recent(300))

    @app.get("/api/qr.png")
    def qr():
        import qrcode

        port = int(engine.cfg.get("app", {}).get("port", 8765))
        addrs = lan_addresses(port)
        target = request.args.get("url") or (addrs[0] if addrs else f"http://localhost:{port}")
        img = qrcode.make(target, box_size=8, border=2)
        return _png(img.convert("L"))

    @app.post("/api/update")
    def update():
        from .updater import install_zip

        if request.remote_addr not in LOCAL:
            return err(PackError("CONFIG", "installing update", "updates can only be installed on the Mac itself"), 403)
        if engine.running():
            return err(Busy("Wait for the current job to finish, then update."))
        f = request.files.get("file")
        if not f:
            return err(PackError("CONFIG", "installing update", "choose the ZIP file first"))
        try:
            info = install_zip(f.read())
        except Exception as e:
            return err(e if isinstance(e, PackError) else PackError("CONFIG", "installing update", str(e)[:200]))
        engine.log.add("info", f"Update installed ({info['files']} files); restarting")
        return restart()

    @app.post("/api/restart")
    def restart():
        if engine.running():
            return err(Busy("Wait for the current job to finish, then restart."))
        engine.log.add("info", "Restarting Pack Station")

        def again():
            time.sleep(0.8)
            srv = app.config.get("SERVER")
            if srv is not None:
                try:
                    srv.socket.close()  # free the port for the new copy
                except OSError:
                    pass
            os.environ["PACKSTATION_RESTARTED"] = "1"
            args = [a for a in sys.argv[1:] if a != "--no-browser"] + ["--no-browser"]
            os.execv(sys.executable, [sys.executable, "-m", "packstation", *args])

        threading.Thread(target=again, daemon=True).start()
        return jsonify(ok=True)

    return app
