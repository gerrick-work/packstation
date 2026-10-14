"""Odoo (Online, standard) through its external API, with the Odoo S.A. Shopee and TikTok connectors
(sale_shopee, sale_tiktok). Ready-to-ship deliveries are the orders; the connectors' own fields say
whether the waybill exists:

  TikTok : tiktok_label_attachment_id (an ir.attachment). While tiktok_delivery_status is
           "confirmed" and there is no label, shipment must be arranged first (books the courier):
           action_tiktok_ship_order -> tiktok.picking.ship.wizard -> action_tiktok_sync_pickings.
  Shopee : shopee_label_status == "stored" plus the connector's label attachment field.

Refresh runs the connectors' scheduled jobs straight away (ir.cron.method_direct_trigger, needs an
Odoo user with Settings/Administrator rights) so Odoo catches up with Shopee/TikTok first.
"""
from __future__ import annotations

import base64
import re
import socket
import time
import xmlrpc.client
from datetime import datetime

from ..errors import PackError
from ..models import Item, Order
from . import Source

DEFAULT_CRONS = [73, 77, 74]  # Shopee sync orders, TikTok sync orders, Shopee sync shipping labels


class _TimeoutTransport(xmlrpc.client.SafeTransport):
    def __init__(self, timeout: float):
        super().__init__()
        self.timeout = timeout

    def make_connection(self, host):
        conn = super().make_connection(host)
        conn.timeout = self.timeout
        return conn


class OdooSource(Source):
    name = "odoo"

    def __init__(self, cfg: dict, rpc=None, sleep=time.sleep):
        self.url = cfg.get("url", "").rstrip("/")
        self.db = cfg.get("db", "")
        self.user = cfg.get("username", "")
        self.key = cfg.get("api_key", "")
        self.ref_field = cfg.get("platform_ref_field", "client_order_ref")
        self.post_note = bool(cfg.get("note_when_printed", True))
        self.crons = [int(c) for c in cfg.get("refresh_crons", DEFAULT_CRONS)]
        self.f_tracking = cfg.get("tracking_field", "carrier_tracking_ref")
        self.f_tt_status = cfg.get("tiktok_status_field", "tiktok_delivery_status")
        self.f_tt_label = cfg.get("tiktok_label_field", "tiktok_label_attachment_id")
        self.f_sp_status = cfg.get("shopee_status_field", "shopee_label_status")
        self.f_sp_label = cfg.get("shopee_label_field", "")  # found automatically when empty
        self.tt_arrange_states = cfg.get("tiktok_arrange_states", ["confirmed"])
        # Which delivery statuses count as "to pack". Odoo: assigned = Ready, confirmed = Waiting,
        # waiting = Waiting another operation. Settings can add "Waiting" for warehouses without
        # reserved stock (e.g. a Singapore warehouse fulfilled from Malaysia).
        self.states = list(cfg.get("picking_states", ["assigned"]))
        self.all_companies = bool(cfg.get("all_companies", True))
        self._ctx = None
        self.sleep = sleep
        self._uid = None
        self._fields = None
        self._rpc = rpc
        if rpc is None and not (self.url and self.db and self.user and self.key):
            raise PackError("CONFIG", "Odoo settings", "url, db, username and api_key are needed")
        if rpc is None:
            self.models = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/object", allow_none=True,
                                                    transport=_TimeoutTransport(60))
            self.slow = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/object", allow_none=True,
                                                  transport=_TimeoutTransport(600))

    # ---- transport ------------------------------------------------------------------

    @property
    def uid(self) -> int:
        if self._uid is None:
            try:
                common = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/common", transport=_TimeoutTransport(30))
                self._uid = common.authenticate(self.db, self.user, self.key, {})
            except (OSError, socket.timeout) as e:
                raise PackError("ODOO-NET", "Odoo login", str(e)[:160]) from e
            except xmlrpc.client.Fault as e:
                raise PackError("ODOO-AUTH", "Odoo login", e.faultString[:160]) from e
            if not self._uid:
                raise PackError("ODOO-AUTH", "Odoo login")
        return self._uid

    def _x(self, model: str, method: str, *args, slow: bool = False, step: str = "reading Odoo", **kw):
        if self._rpc is not None:
            return self._rpc(model, method, *args, **kw)
        proxy = self.slow if slow else self.models
        try:
            return proxy.execute_kw(self.db, self.uid, self.key, model, method, list(args), kw)
        except PackError:
            raise
        except xmlrpc.client.Fault as e:
            low = e.faultString.lower()
            if "access" in low or "not allowed" in low or "permission" in low:
                raise PackError("ODOO-AUTH" if step == "reading Odoo" else "ODOO-CRON", step,
                                e.faultString.strip().splitlines()[-1][:200]) from e
            raise
        except (OSError, socket.timeout, xmlrpc.client.ProtocolError) as e:
            raise PackError("ODOO-NET", step, str(e)[:160]) from e

    def ctx(self) -> dict:
        """Read across every company this Odoo user can access (e.g. Malaysia and Singapore),
        not only the login's current company."""
        if self._ctx is None:
            self._ctx = {}
            if self.all_companies:
                try:
                    me = self._x("res.users", "read", [self.uid if self._rpc is None else 0],
                                 fields=["company_ids"])
                    ids = (me[0].get("company_ids") if me else None) or []
                    if ids:
                        self._ctx = {"allowed_company_ids": ids}
                except PackError:
                    raise
                except Exception:
                    self._ctx = {}
        return self._ctx

    def fields(self) -> dict:
        if self._fields is None:
            self._fields = self._x("stock.picking", "fields_get", attributes=["type", "relation"])
            if not self.f_sp_label:
                cands = [n for n, f in self._fields.items()
                         if "shopee" in n and f.get("type") == "many2one" and f.get("relation") == "ir.attachment"]
                cands.sort(key=lambda n: ("label" not in n, n))
                self.f_sp_label = cands[0] if cands else ""
        return self._fields

    # ---- orders -----------------------------------------------------------------------

    def list_orders(self) -> list[Order]:
        f = self.fields()
        wanted = ["name", "origin", "scheduled_date", "date_deadline", "sale_id", "move_ids",
                  self.f_tracking, self.f_tt_status, self.f_tt_label, self.f_sp_status, self.f_sp_label]
        platform_fields = [n for n in f if (n.startswith("tiktok_") or n.startswith("shopee_"))
                           and f[n].get("type") in ("char", "selection", "many2one", "integer")]
        read = sorted({n for n in wanted + platform_fields if n and n in f})
        ctx = self.ctx()
        if "company_id" in f:
            read.append("company_id")
        pickings = self._x("stock.picking", "search_read",
                           [("picking_type_code", "=", "outgoing"), ("state", "in", self.states)],
                           fields=read, context=ctx)
        move_ids = [m for p in pickings for m in p.get("move_ids", [])]
        moves = {m["id"]: m for m in self._x("stock.move", "read", move_ids,
                                             fields=["product_id", "product_uom_qty"], context=ctx)} if move_ids else {}
        prod_ids = list({m["product_id"][0] for m in moves.values() if m.get("product_id")})
        prods = {p["id"]: p for p in self._x("product.product", "read", prod_ids,
                                             fields=["default_code", "display_name"], context=ctx)} if prod_ids else {}
        sale_ids = [p["sale_id"][0] for p in pickings if p.get("sale_id")]
        refs = {s["id"]: s.get(self.ref_field) or "" for s in self._x(
            "sale.order", "read", sale_ids, fields=[self.ref_field], context=ctx)} if sale_ids else {}
        return [self._to_order(p, moves, prods, refs) for p in pickings]

    def check(self) -> list[dict]:
        """Every open outgoing delivery, counted by company, operation type and status, and whether
        Pack Station lists it. Read only; shown on the Settings screen to explain missing orders."""
        ctx = self.ctx()
        f = self.fields()
        fields = ["state", "picking_type_id"] + (["company_id"] if "company_id" in f else [])
        rows = self._x("stock.picking", "search_read",
                       [("picking_type_code", "=", "outgoing"), ("state", "not in", ["done", "cancel"])],
                       fields=fields, context=ctx)
        labels = {"draft": "Draft", "waiting": "Waiting another operation", "confirmed": "Waiting (no stock reserved)",
                  "assigned": "Ready"}
        groups: dict[tuple, int] = {}
        for r in rows:
            key = ((r.get("company_id") or [0, "?"])[1], (r.get("picking_type_id") or [0, "?"])[1], r["state"])
            groups[key] = groups.get(key, 0) + 1
        return [{"company": c, "operation": t, "state": s, "state_label": labels.get(s, s), "count": n,
                 "listed": s in self.states}
                for (c, t, s), n in sorted(groups.items())]

    def _platform(self, p: dict) -> str:
        def truthy(v):
            return v not in (False, None, "", 0)
        if truthy(p.get(self.f_tt_status)) or truthy(p.get(self.f_tt_label)) or any(
                truthy(v) for k, v in p.items() if k.startswith("tiktok_")):
            return "tiktok"
        if truthy(p.get(self.f_sp_status)) or any(truthy(v) for k, v in p.items() if k.startswith("shopee_")):
            return "shopee"
        return ""

    def _to_order(self, p: dict, moves: dict, prods: dict, refs: dict) -> Order:
        items = []
        for mid in p.get("move_ids", []):
            m = moves.get(mid)
            if not m or not m.get("product_id"):
                continue
            prod = prods.get(m["product_id"][0], {})
            items.append(Item(sku=prod.get("default_code") or "", name=prod.get("display_name", ""),
                              qty=int(round(m["product_uom_qty"]))))
        when = p.get("date_deadline") or p.get("scheduled_date")
        platform = self._platform(p)
        label_id = None
        needs_arrange = False
        status = "ready"
        if platform == "tiktok":
            label_id = (p.get(self.f_tt_label) or [None])[0] if p.get(self.f_tt_label) else None
            tt = p.get(self.f_tt_status) or ""
            status = f"tiktok: {tt}" if tt else "tiktok"
            needs_arrange = not label_id and tt in self.tt_arrange_states
        elif platform == "shopee":
            sp = p.get(self.f_sp_status) or ""
            status = f"shopee label: {sp}" if sp else "shopee"
            if sp == "stored" and self.f_sp_label and p.get(self.f_sp_label):
                label_id = p[self.f_sp_label][0]
        tracking = p.get(self.f_tracking) or None
        if not tracking and platform:  # connector's own tracking field, e.g. shopee_tracking_number
            tracking = next((v for k, v in p.items() if k.startswith(platform + "_") and "tracking" in k
                             and isinstance(v, str) and v), None)
        ref = refs.get(p["sale_id"][0], "") if p.get("sale_id") else ""
        return Order(
            source=self.name,
            order_id=p["name"],
            items=items,
            status=status,
            ship_by=datetime.fromisoformat(str(when)) if when else None,
            tracking=tracking,
            needs_arrange=needs_arrange,
            has_waybill=bool(platform),
            waybill_ready=bool(label_id),
            platform=platform,
            odoo_picking_id=p["id"],
            ref={"platform_ref": str(ref).strip(), "origin": p.get("origin") or "", "label_attachment_id": label_id},
        )

    def _reload(self, order: Order) -> Order:
        f = self.fields()
        read = [n for n in ["name", "move_ids", "sale_id", "scheduled_date", "date_deadline", self.f_tracking,
                            self.f_tt_status, self.f_tt_label, self.f_sp_status, self.f_sp_label] if n and n in f]
        read += [n for n in f if (n.startswith("tiktok_") or n.startswith("shopee_")) and "tracking" in n]
        p = self._x("stock.picking", "read", [order.odoo_picking_id], fields=read, context=self.ctx())[0]
        fresh = self._to_order(p, {}, {}, {})
        fresh.items, fresh.ref["platform_ref"] = order.items, order.ref.get("platform_ref", "")
        return fresh

    # ---- waybills ---------------------------------------------------------------------

    def get_waybill(self, order: Order) -> bytes:
        att = order.ref.get("label_attachment_id")
        if not att:
            raise PackError("WAYBILL-MISSING", "fetching waybill from Odoo")
        rows = self._x("ir.attachment", "read", [att], fields=["datas", "mimetype", "name"], context=self.ctx())
        if not rows or not rows[0].get("datas"):
            raise PackError("WAYBILL-MISSING", "fetching waybill from Odoo", "attachment is empty")
        return base64.b64decode(rows[0]["datas"])

    # ---- TikTok: arrange shipment -------------------------------------------------------

    def arrange(self, order: Order, method: str = "dropoff") -> Order:
        """Book the TikTok courier, sync, and wait for the label. Raises TIKTOK-ARRANGE."""
        step = "arranging TikTok shipment"
        pid = order.odoo_picking_id
        try:
            action = self._x("stock.picking", "action_tiktok_ship_order", [pid], step=step, context=self.ctx())
            if isinstance(action, dict) and action.get("res_model"):
                self._run_wizard(action, method, step)
            self._x("stock.picking", "action_tiktok_sync_pickings", [pid], step=step, context=self.ctx())
        except PackError as e:
            if e.code in ("ODOO-NET", "ODOO-AUTH"):
                raise
            raise PackError("TIKTOK-ARRANGE", step, e.detail or e.code) from e
        except xmlrpc.client.Fault as e:
            raise PackError("TIKTOK-ARRANGE", step, e.faultString.strip().splitlines()[-1][:200]) from e
        for _ in range(8):  # TikTok issues the label a few seconds after the booking
            fresh = self._reload(order)
            if fresh.waybill_ready:
                return fresh
            self.sleep(4)
            try:
                self._x("stock.picking", "action_tiktok_sync_pickings", [pid], step=step, context=self.ctx())
            except Exception:
                pass
        raise PackError("TIKTOK-ARRANGE", "waiting for TikTok label",
                        "courier booked; TikTok hasn't sent the label yet. Press Refresh in a minute.")

    def _run_wizard(self, action: dict, method: str, step: str) -> None:
        model = action["res_model"]
        ctx = action.get("context") or {}
        if isinstance(ctx, str):
            ctx = {}
        ctx = {**self.ctx(), **ctx}
        wf = self._x(model, "fields_get", attributes=["type", "selection"], step=step)
        vals = {}
        want = re.sub(r"[^a-z]", "", method.lower())
        for name, f in wf.items():
            if f.get("type") != "selection":
                continue
            keys = [k for k, _ in (f.get("selection") or [])]
            norm = {re.sub(r"[^a-z]", "", str(k).lower()): k for k in keys}
            if any(("drop" in k or "pick" in k) for k in norm):
                match = next((v for k, v in norm.items() if want in k or k in want), None)
                if match is None:
                    raise PackError("TIKTOK-ARRANGE", step, f"ship method '{method}' not offered ({keys})")
                vals[name] = match
                break
        if action.get("res_id"):
            wiz_id = action["res_id"]
            if vals:
                self._x(model, "write", [wiz_id], vals, context=ctx, step=step)
        else:
            wiz_id = self._x(model, "create", vals, context=ctx, step=step)
            if isinstance(wiz_id, list):
                wiz_id = wiz_id[0]
        views = self._x(model, "get_views", [[False, "form"]], step=step)
        arch = ((views or {}).get("views", {}).get("form", {}) or {}).get("arch", "")
        button = _primary_button(arch)
        if not button:
            raise PackError("TIKTOK-ARRANGE", step, "couldn't find the confirm button on the TikTok ship form")
        self._x(model, button, [wiz_id], context=ctx, step=step)

    # ---- refresh chain --------------------------------------------------------------------

    def run_cron(self, cron_id: int) -> None:
        try:
            if self._rpc is not None:
                self._rpc("ir.cron", "method_direct_trigger", [cron_id])
            else:
                self.slow.execute_kw(self.db, self.uid, self.key, "ir.cron", "method_direct_trigger", [[cron_id]], {})
        except PackError:
            raise
        except xmlrpc.client.Fault as e:
            low = e.faultString.lower()
            if "access" in low or "not allowed" in low or "administrator" in low:
                raise PackError("ODOO-CRON", f"running Odoo sync job {cron_id}",
                                "the Odoo user in Settings needs Administrator (Settings) rights") from e
            raise PackError("ODOO-CRON", f"running Odoo sync job {cron_id}",
                            e.faultString.strip().splitlines()[-1][:200]) from e
        except (OSError, socket.timeout, xmlrpc.client.ProtocolError) as e:
            raise PackError("ODOO-NET", f"running Odoo sync job {cron_id}", str(e)[:160]) from e

    def cron_names(self) -> dict[int, str]:
        try:
            return {c["id"]: c.get("cron_name") or c.get("name") or str(c["id"])
                    for c in self._x("ir.cron", "read", self.crons, fields=["cron_name"])}
        except Exception:
            return {}

    def sync_tiktok(self, picking_ids: list[int]) -> None:
        if picking_ids:
            self._x("stock.picking", "action_tiktok_sync_pickings", picking_ids, step="syncing TikTok deliveries",
                    context=self.ctx())

    def note_printed(self, picking_id: int, text: str) -> None:
        if self.post_note:
            self._x("stock.picking", "message_post", [picking_id], body=text, context=self.ctx())


def _primary_button(arch: str) -> str | None:
    buttons = re.findall(r"<button\b([^>]*)>", arch or "")
    found = []
    for attrs in buttons:
        name = re.search(r'\bname="([^"]+)"', attrs)
        typ = re.search(r'\btype="([^"]+)"', attrs)
        if not name or not typ or typ.group(1) != "object":
            continue
        cls = re.search(r'\bclass="([^"]*)"', attrs)
        primary = bool(cls and ("oe_highlight" in cls.group(1) or "btn-primary" in cls.group(1)))
        found.append((not primary, name.group(1)))
    found.sort()
    return found[0][1] if found else None
