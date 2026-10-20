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
import urllib.request
import xmlrpc.client
from datetime import datetime

from ..errors import PackError
from ..models import Item, Order
from . import Source

DEFAULT_CRONS = [73, 77, 74]  # Shopee sync orders, TikTok sync orders, Shopee sync shipping labels
# The connectors put marketplace items they can't match to an Odoo product (by SKU) on a catch-all
# product such as "TikTok Shop Sale". The real item is then only in the sale order line's description.
PLACEHOLDER_RE = re.compile(r"^\s*(\[[^\]]*\]\s*)?(tik\s*tok|shopee|lazada|marketplace)\b.*\b(sale|sales|product|item|order)s?\s*$",
                            re.I)


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
        # Only deliveries whose sale order is in these sales teams (empty list = all). Keeps bulk B2B
        # orders (e.g. 400 corporate gift sets) off the label printer, and says which platform it is.
        self.teams = {t.lower() for t in cfg.get("only_sales_teams", ["Shopee", "TikTok Shop"])}
        # Which delivery statuses count as "to pack". Odoo: assigned = Ready, confirmed = Waiting,
        # waiting = Waiting another operation. Settings can add "Waiting" for warehouses without
        # reserved stock (e.g. a Singapore warehouse fulfilled from Malaysia).
        self.states = list(cfg.get("picking_states", ["assigned"]))
        self.all_companies = bool(cfg.get("all_companies", True))
        # Odoo operation types to read: "outgoing" = Delivery. Add e.g. "dropship" if a shop ships that way.
        self.type_codes = list(cfg.get("picking_type_codes", ["outgoing"]))
        self.placeholders = {str(n).strip().lower() for n in cfg.get("placeholder_products", [])}
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
            if "cannot marshal none" in low:
                return None  # the button worked; Odoo just can't send back its empty reply over XML-RPC
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
            if self.f_tt_label not in self._fields:  # connector versions name it differently
                self.f_tt_label = next(iter(self._label_fields("tiktok")), self.f_tt_label)
        return self._fields

    def _label_fields(self, platform: str) -> list[str]:
        """Fields on the delivery that can hold a waybill: attachment links, files, or a URL."""
        f = self._fields or {}
        out = [n for n, d in f.items() if n.startswith(platform + "_")
               and (d.get("relation") == "ir.attachment" or d.get("type") == "binary"
                    or (d.get("type") == "char" and any(w in n for w in ("url", "link"))
                        and any(w in n for w in ("label", "waybill", "awb", "document"))))]
        out.sort(key=lambda n: (not any(w in n for w in ("label", "waybill", "awb")), n))
        return out

    # ---- orders -----------------------------------------------------------------------

    def list_orders(self) -> list[Order]:
        f = self.fields()
        wanted = ["name", "origin", "scheduled_date", "date_deadline", "sale_id", "move_ids",
                  self.f_tracking, self.f_tt_status, self.f_tt_label, self.f_sp_status, self.f_sp_label]
        platform_fields = [n for n in f if (n.startswith("tiktok_") or n.startswith("shopee_"))
                           and f[n].get("type") in ("char", "selection", "many2one", "integer")]
        read = sorted({n for n in wanted + platform_fields + self._label_fields("tiktok")
                       + self._label_fields("shopee") if n and n in f})
        ctx = self.ctx()
        bctx = dict(ctx, bin_size=True)
        if "company_id" in f:
            read.append("company_id")
        pickings = self._x("stock.picking", "search_read",
                           [("picking_type_code", "in", self.type_codes), ("state", "in", self.states)],
                           fields=read, context=bctx)
        move_ids = [m for p in pickings for m in p.get("move_ids", [])]
        moves = {m["id"]: m for m in self._x("stock.move", "read", move_ids,
                                             fields=["product_id", "product_uom_qty", "sale_line_id"],
                                             context=ctx)} if move_ids else {}
        prod_ids = list({m["product_id"][0] for m in moves.values() if m.get("product_id")})
        prods = {p["id"]: p for p in self._x("product.product", "read", prod_ids,
                                             fields=["default_code", "display_name"], context=ctx)} if prod_ids else {}
        self._real_items(moves, prods, ctx)
        sale_ids = list({p["sale_id"][0] for p in pickings if p.get("sale_id")})
        sales = {s["id"]: s for s in self._x("sale.order", "read", sale_ids,
                                             fields=[self.ref_field, "team_id", "order_line", "amount_total",
                                                     "currency_id"], context=ctx)} if sale_ids else {}
        refs = {sid: s.get(self.ref_field) or "" for sid, s in sales.items()}
        totals = {sid: (s.get("amount_total"), (s.get("currency_id") or [0, ""])[1]) for sid, s in sales.items()}
        self._team = {sid: (s["team_id"][1] if s.get("team_id") else "") for sid, s in sales.items()}
        if self.teams:
            pickings = [p for p in pickings if p.get("sale_id")
                        and self._team.get(p["sale_id"][0], "").lower() in self.teams]
        self._sold = self._sold_items(sales)
        self._sp_files = self._shopee_files([p["id"] for p in pickings])
        found = self._attached_labels(pickings)
        return [self._to_order(p, moves, prods, refs, found.get(p["id"]), totals) for p in pickings]

    def is_placeholder(self, prod: dict) -> bool:
        name = (prod.get("display_name") or "").strip()
        return name.lower() in self.placeholders or bool(PLACEHOLDER_RE.match(name))

    def _real_items(self, moves: dict, prods: dict, ctx: dict) -> None:
        """Moves on a catch-all product ("TikTok Shop Sale") get the item's real name, and SKU when
        given as "[SKU] name", from their sale order line. Stored on the move as _name/_sku."""
        todo = {m["sale_line_id"][0]: m for m in moves.values()
                if m.get("product_id") and m.get("sale_line_id")
                and self.is_placeholder(prods.get(m["product_id"][0], {}))}
        if not todo:
            return
        for line in self._x("sale.order.line", "read", list(todo), fields=["name"], context=ctx):
            prod = prods.get(todo[line["id"]]["product_id"][0], {})
            sku, name = _line_item(line.get("name") or "", prod.get("display_name") or "")
            if name:
                todo[line["id"]].update(_name=name, _sku=sku)

    _LABEL_WORDS = ("label", "waybill", "awb", "airway", "shipping", "consign", "tiktok", "shopee")

    def _attached_labels(self, pickings: list[dict]) -> dict:
        """Waybills the connector saved as plain attachments (on the delivery or its sale order)
        instead of in a label field. Returns {picking id: attachment id}; PDFs whose name looks like
        a label win, then the newest."""
        need = [p for p in pickings if self._platform(p) and not self._field_label(p)]
        if not need:
            return {}
        sale_of = {p["sale_id"][0]: p["id"] for p in need if p.get("sale_id")}
        dom = ["|", "&", ("res_model", "=", "stock.picking"), ("res_id", "in", [p["id"] for p in need]),
               "&", ("res_model", "=", "sale.order"), ("res_id", "in", list(sale_of) or [0]),
               ("mimetype", "in", ["application/pdf", "image/png", "image/jpeg"])]
        try:
            atts = self._x("ir.attachment", "search_read", dom,
                           fields=["id", "name", "res_model", "res_id", "mimetype", "create_date"],
                           order="create_date desc, id desc", context=self.ctx())
        except Exception:  # no right to read attachments: fall back to the label fields only
            return {}
        best: dict[int, tuple] = {}
        for a in atts:
            pid = a["res_id"] if a["res_model"] == "stock.picking" else sale_of.get(a["res_id"])
            if not pid:
                continue
            name = (a.get("name") or "").lower()
            looks = any(w in name for w in self._LABEL_WORDS)
            if a["res_model"] == "sale.order" and not looks:
                continue  # a sale order's quotation PDF is not a waybill
            rank = (looks, a["res_model"] == "stock.picking", a.get("mimetype") == "application/pdf")
            if pid not in best or rank > best[pid][0]:
                best[pid] = (rank, a["id"])
        return {pid: aid for pid, (_, aid) in best.items()}

    def _field_label(self, p: dict):
        """The waybill held in one of the delivery's label fields, as a ref: ("att", id), ("bin", field)
        or ("url", link). None when no field holds one."""
        platform = self._platform(p)
        if not platform:
            return None
        names = self._label_fields(platform)
        main = self.f_tt_label if platform == "tiktok" else self.f_sp_label
        if main in names:
            names.remove(main)
            names.insert(0, main)
        if platform == "shopee" and self.f_sp_status in p and p.get(self.f_sp_status) not in ("stored", False, None, ""):
            return None  # Shopee is still preparing the label; an older one may be stale
        f = self._fields or {}
        for n in names:
            v = p.get(n)
            if not v:
                continue
            d = f.get(n, {})
            if d.get("relation") == "ir.attachment":
                if isinstance(v, list) and v and isinstance(v[0], int):
                    return ("att", v[0] if d.get("type") == "many2one" else v[-1])
            elif d.get("type") == "binary":
                return ("bin", n)
            elif isinstance(v, str) and v.startswith("http"):
                return ("url", v)
        return None

    def _sold_items(self, sales: dict) -> dict[int, list[Item]]:
        """What the customer bought (bottles), per sale order. Delivery moves can hold raw-material
        grams instead when products are kits (0.3 g Labdanum for a 3 g bottle), so they're a fallback."""
        line_ids = [lid for s in sales.values() for lid in s.get("order_line", [])]
        if not line_ids:
            return {}
        lines = self._x("sale.order.line", "read", line_ids,
                        fields=["order_id", "product_id", "product_uom_qty", "display_type", "is_delivery", "name"],
                        context=self.ctx())
        pids = list({l["product_id"][0] for l in lines if l.get("product_id")})
        prods = {pp["id"]: pp for pp in self._x("product.product", "read", pids,
                                                 fields=["default_code", "display_name", "type"], context=self.ctx())} if pids else {}
        out: dict[int, list[Item]] = {}
        for l in lines:
            if l.get("display_type") or l.get("is_delivery") or not l.get("product_id"):
                continue
            prod = prods.get(l["product_id"][0], {})
            qty = int(round(l.get("product_uom_qty") or 0))
            if prod.get("type") == "service" or qty <= 0:
                continue
            sku, name = prod.get("default_code") or "", prod.get("display_name", "")
            if self.is_placeholder(prod):  # "TikTok Shop Sale": the real item is in the line's description
                real = _line_item(l.get("name") or "", name)
                if real[1]:
                    sku, name = real
            out.setdefault(l["order_id"][0], []).append(Item(sku=sku, name=name, qty=qty))
        return out

    def _shopee_files(self, picking_ids: list[int]) -> dict[int, int]:
        """The Shopee connector stores the waybill as an attachment named Shopee_Label_<delivery>.pdf
        (there is no attachment field on the delivery). Newest wins."""
        if not picking_ids or self.f_sp_label:
            return {}
        atts = self._x("ir.attachment", "search_read",
                       [("res_model", "=", "stock.picking"), ("res_id", "in", picking_ids),
                        ("name", "ilike", "shopee"), ("mimetype", "=", "application/pdf")],
                       fields=["res_id"], order="id desc", context=self.ctx())
        out: dict[int, int] = {}
        for a in atts:
            out.setdefault(a["res_id"], a["id"])
        return out

    def check(self, days: int = 14) -> dict:
        """Where are the orders? Read only; shown on the Settings screen to explain missing orders.
        - companies: the Odoo companies this login can see
        - deliveries: every open stock transfer except receipts, by company, operation, type and status,
          and whether Pack Station lists it (only "Delivery" type, in the chosen statuses)
        - sales: sale orders of the last `days` days by company and sales team/channel, and how many
          have a delivery Pack Station can list"""
        from datetime import date, timedelta

        ctx = self.ctx()
        f = self.fields()
        comp_ids = ctx.get("allowed_company_ids") or []
        companies = [c["name"] for c in self._x("res.company", "read", comp_ids, fields=["name"], context=ctx)] \
            if comp_ids else []
        fields = ["state", "picking_type_id", "picking_type_code"] + (["company_id"] if "company_id" in f else [])
        rows = self._x("stock.picking", "search_read",
                       [("picking_type_code", "!=", "incoming"), ("state", "not in", ["done", "cancel"])],
                       fields=fields, context=ctx)
        labels = {"draft": "Draft", "waiting": "Waiting another operation", "confirmed": "Waiting (no stock reserved)",
                  "assigned": "Ready"}
        groups: dict[tuple, int] = {}
        for r in rows:
            key = ((r.get("company_id") or [0, "?"])[1], (r.get("picking_type_id") or [0, "?"])[1],
                   r.get("picking_type_code") or "?", r["state"])
            groups[key] = groups.get(key, 0) + 1
        deliveries = [{"company": c, "operation": t, "type": code, "state": s, "state_label": labels.get(s, s),
                       "count": n, "listed": code in self.type_codes and s in self.states}
                      for (c, t, code, s), n in sorted(groups.items())]

        since = (date.today() - timedelta(days=days)).isoformat()
        orders = self._x("sale.order", "search_read", [("date_order", ">=", since), ("state", "!=", "cancel")],
                         fields=["company_id", "team_id", "picking_ids", "state"], context=ctx)
        pick_ids = [i for o in orders for i in o.get("picking_ids") or []]
        picks = {p["id"]: p for p in self._x("stock.picking", "read", pick_ids,
                                             fields=["state", "picking_type_code"], context=ctx)} if pick_ids else {}
        sales: dict[tuple, list] = {}
        team_skip: dict[tuple, bool] = {}
        for o in orders:
            key = ((o.get("company_id") or [0, "?"])[1], (o.get("team_id") or [0, "No sales team"])[1])
            team_ok = not self.teams or key[1].lower() in self.teams   # B2B teams are left off on purpose
            listable = team_ok and any(picks.get(i, {}).get("picking_type_code") in self.type_codes
                           and picks.get(i, {}).get("state") in self.states for i in o.get("picking_ids") or [])
            no_delivery = not o.get("picking_ids")
            team_skip[key] = not team_ok
            g = sales.setdefault(key, [0, 0, 0])
            g[0] += 1
            g[1] += listable
            g[2] += no_delivery
        return {
            "waybills": self._check_waybills(),
            "companies": companies,
            "deliveries": deliveries,
            "sales": [{"company": c, "team": t, "orders": n, "listed": l, "no_delivery": nd,
                       "team_skipped": team_skip.get((c, t), False)}
                      for (c, t), (n, l, nd) in sorted(sales.items())],
            "teams": sorted(self.teams),
            "days": days,
        }

    def _check_waybills(self) -> list[dict]:
        """Per platform: how many to-pack deliveries have a waybill Pack Station can fetch, and where
        it was found. Lists the connector's label fields so a missing one can be spotted."""
        try:
            orders = self.list_orders()
        except PackError:
            return []
        out = {}
        for o in orders:
            if not o.platform:
                continue
            g = out.setdefault(o.platform, {"platform": o.platform, "total": 0, "found": 0, "missing": [],
                                            "where": {}, "fields": self._label_fields(o.platform)})
            g["total"] += 1
            lab = o.ref.get("label")
            if lab:
                g["found"] += 1
                kind = {"att": "file", "bin": "file field", "url": "link"}[lab[0]]
                g["where"][kind] = g["where"].get(kind, 0) + 1
            elif len(g["missing"]) < 8:
                g["missing"].append(f"{o.order_id} ({o.status})")
        return list(out.values())

    def _platform(self, p: dict) -> str:
        team = getattr(self, "_team", {}).get(p["sale_id"][0], "").lower() if p.get("sale_id") else ""
        if "tiktok" in team:
            return "tiktok"
        if "shopee" in team:
            return "shopee"
        def truthy(v):
            return v not in (False, None, "", 0)
        if truthy(p.get(self.f_tt_status)) or truthy(p.get(self.f_tt_label)) or any(
                truthy(v) for k, v in p.items() if k.startswith("tiktok_")):
            return "tiktok"
        sp = p.get(self.f_sp_status)
        if (truthy(sp) and sp != "not available") or truthy(p.get("shopee_order_ref")):
            return "shopee"
        return ""

    def _to_order(self, p: dict, moves: dict, prods: dict, refs: dict, attached: int | None = None,
                  totals: dict | None = None) -> Order:
        sold = getattr(self, "_sold", {}).get(p["sale_id"][0]) if p.get("sale_id") else None
        items = list(sold) if sold else []
        for mid in ([] if items else p.get("move_ids", [])):
            m = moves.get(mid)
            if not m or not m.get("product_id"):
                continue
            prod = prods.get(m["product_id"][0], {})
            items.append(Item(sku=m.get("_sku", prod.get("default_code") or ""),
                              name=m.get("_name") or prod.get("display_name", ""),
                              qty=int(round(m["product_uom_qty"]))))
        when = p.get("date_deadline") or p.get("scheduled_date")
        platform = self._platform(p)
        label = self._field_label(p) or (("att", attached) if attached else None)
        needs_arrange = False
        status = "ready"
        if platform == "tiktok":
            tt = p.get(self.f_tt_status) or ""
            status = f"tiktok: {tt}" if tt else "tiktok"
            needs_arrange = not label and tt in self.tt_arrange_states
        elif platform == "shopee":
            sp = p.get(self.f_sp_status) or ""
            status = f"shopee label: {sp}" if sp else "shopee"
            if sp and sp != "stored":
                label = None  # Shopee is still preparing the label; an older file may be stale
            elif not label and getattr(self, "_sp_files", {}).get(p["id"]):
                label = ("att", self._sp_files[p["id"]])
        tracking = p.get(self.f_tracking) or None
        if not tracking and platform:  # connector's own tracking field, e.g. shopee_tracking_number
            tracking = next((v for k, v in p.items() if k.startswith(platform + "_") and "tracking" in k
                             and isinstance(v, str) and v), None)
        ref = refs.get(p["sale_id"][0], "") if p.get("sale_id") else ""
        total, currency = (totals or {}).get(p["sale_id"][0], (None, "")) if p.get("sale_id") else (None, "")
        return Order(
            source=self.name,
            order_id=p["name"],
            items=items,
            status=status,
            ship_by=datetime.fromisoformat(str(when)) if when else None,
            tracking=tracking,
            needs_arrange=needs_arrange,
            has_waybill=bool(platform),
            waybill_ready=bool(label),
            platform=platform,
            odoo_picking_id=p["id"],
            ref={"platform_ref": str(ref).strip(), "origin": p.get("origin") or "", "label": list(label) if label else None,
                 "label_attachment_id": label[1] if label and label[0] == "att" else None},
            total=float(total) if total is not None and total is not False else None,
            currency=currency or "",
        )

    def _reload(self, order: Order) -> Order:
        f = self.fields()
        read = [n for n in ["name", "move_ids", "sale_id", "scheduled_date", "date_deadline", self.f_tracking,
                            self.f_tt_status, self.f_tt_label, self.f_sp_status, self.f_sp_label] if n and n in f]
        read += [n for n in f if (n.startswith("tiktok_") or n.startswith("shopee_")) and "tracking" in n]
        read += [n for n in self._label_fields("tiktok") + self._label_fields("shopee") if n not in read]
        p = self._x("stock.picking", "read", [order.odoo_picking_id], fields=read,
                    context=dict(self.ctx(), bin_size=True))[0]
        fresh = self._to_order(p, {}, {}, {}, self._attached_labels([p]).get(p["id"]))
        fresh.items, fresh.ref["platform_ref"] = order.items, order.ref.get("platform_ref", "")
        fresh.total, fresh.currency = order.total, order.currency
        return fresh

    # ---- waybills ---------------------------------------------------------------------

    def get_waybill(self, order: Order) -> bytes:
        label = order.ref.get("label")
        if not label and order.ref.get("label_attachment_id"):
            label = ["att", order.ref["label_attachment_id"]]
        if not label:
            raise PackError("WAYBILL-MISSING", "fetching waybill from Odoo")
        kind, where = label
        step = "fetching waybill from Odoo"
        if kind == "att":
            if getattr(self, "_att_field", None) is None:
                have = self._x("ir.attachment", "fields_get", attributes=["type"], context=self.ctx())
                self._att_field = "datas" if "datas" in have else "raw"   # Odoo 19.4+ renamed it
            rows = self._x("ir.attachment", "read", [where], fields=[self._att_field, "mimetype", "name"], context=self.ctx())
            data = rows[0].get(self._att_field) if rows else None
            if not data:
                raise PackError("WAYBILL-MISSING", step, "attachment is empty")
            return base64.b64decode(data)
        if kind == "bin":
            rows = self._x("stock.picking", "read", [order.odoo_picking_id], fields=[where], context=self.ctx())
            data = rows[0].get(where) if rows else None
            if not data:
                raise PackError("WAYBILL-MISSING", step, f"{where} is empty")
            return base64.b64decode(data)
        try:  # a link the platform gave the connector
            with urllib.request.urlopen(where, timeout=60) as r:
                data = r.read()
        except OSError as e:
            raise PackError("WAYBILL-MISSING", "downloading waybill", str(e)) from e
        if not data:
            raise PackError("WAYBILL-MISSING", "downloading waybill", "empty download")
        return data

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
            if e.code == "ODOO-AUTH":
                raise
            if "already scheduled" not in (e.detail or "").lower():
                raise PackError("TIKTOK-ARRANGE", step, e.detail or e.code) from e
        except xmlrpc.client.Fault as e:
            if "already scheduled" not in e.faultString.lower():  # booked earlier: just fetch the label
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

    def validate(self, picking_id: int) -> None:
        """Validate the delivery in Odoo (stock leaves, connector marks it shipped). Raises ODOO-VALIDATE.
        Already-validated deliveries are fine. A wizard (backorder, missing quantities) is not answered
        automatically: that needs a person in Odoo."""
        step = "validating the Odoo delivery"
        rows = self._x("stock.picking", "read", [picking_id], fields=["state"], step=step, context=self.ctx())
        if rows and rows[0].get("state") == "done":
            return
        try:
            res = self._x("stock.picking", "button_validate", [picking_id], step=step, context=self.ctx())
        except PackError as e:
            if e.code == "ODOO-AUTH":
                raise
            raise PackError("ODOO-VALIDATE", step, e.detail or e.code) from e
        except xmlrpc.client.Fault as e:
            raise PackError("ODOO-VALIDATE", step, e.faultString.strip().splitlines()[-1][:200]) from e
        if isinstance(res, dict) and res.get("res_model"):
            raise PackError("ODOO-VALIDATE", step, f"Odoo wants a decision first ({res.get('name') or res['res_model']})")
        rows = self._x("stock.picking", "read", [picking_id], fields=["state"], step=step, context=self.ctx())
        if rows and rows[0].get("state") != "done":
            raise PackError("ODOO-VALIDATE", step, f"delivery is still '{rows[0].get('state')}'")

    def note_printed(self, picking_id: int, text: str) -> None:
        if self.post_note:
            self._x("stock.picking", "message_post", [picking_id], body=text, context=self.ctx())


def _line_item(text: str, placeholder: str) -> tuple[str, str]:
    """(sku, name) of the marketplace item in a sale order line description, skipping the catch-all
    product's own name. "[ML-HED-BV] Hedione (15x2) Best Value" -> ("ML-HED-BV", "Hedione (15x2) Best Value")."""
    skip = {placeholder.strip().lower(), re.sub(r"^\[[^\]]*\]\s*", "", placeholder).strip().lower()}
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln or ln.lower() in skip or PLACEHOLDER_RE.match(ln):
            continue
        m = re.match(r"^\[([^\]]+)\]\s*(.*)$", ln)
        return (m.group(1).strip(), m.group(2).strip()) if m else ("", ln)
    return "", ""


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
