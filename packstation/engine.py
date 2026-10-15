"""The Pack Station's brain: finds to-pack orders and runs every printer/Odoo action as a job on one
queue, so phones, iPads and the Mac all see the same live status and nothing prints twice.

Print run per order: (TikTok: book the courier first, only when confirmed on screen) -> waybill on
the waybill printer -> that order's bottle labels on the label printer -> one blank label. An order
is marked printed only when all of that was sent. Marketplace orders with no waybill are never
printed or marked, and get no Odoo note.
"""
from __future__ import annotations

import os
import queue
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime

from . import config as config_mod
from .errors import PackError, EventLog, classify
from .labels import Catalog, labels_for_order
from .models import Order
from .printers import build_printer
from .sources import Source, build_sources
from .sources.odoo import OdooSource
from .store import Store


class Busy(Exception):
    """Another job already has this order queued or printing."""


@dataclass
class Result:
    key: str
    ok: bool
    message: str
    labels: int = 0
    guessed_skus: list[str] = field(default_factory=list)
    error: dict | None = None
    at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))


@dataclass
class Job:
    kind: str
    title: str
    keys: set = field(default_factory=set)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    state: str = "queued"           # queued | running | done | error
    steps: list = field(default_factory=list)   # [{"name", "state": pending|running|done|error|skipped}]
    total: int = 0
    done: int = 0
    current: str = ""
    results: list = field(default_factory=list)
    summary: str = ""
    error: dict | None = None
    queued_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    started_at: str | None = None
    finished_at: str | None = None

    def step(self, name: str, state: str = "running"):
        for s in self.steps:
            if s["name"] == name:
                s["state"] = state
                return
        self.steps.append({"name": name, "state": state})

    def as_dict(self) -> dict:
        return {
            "id": self.id, "kind": self.kind, "title": self.title, "state": self.state, "steps": self.steps,
            "total": self.total, "done": self.done, "current": self.current, "summary": self.summary,
            "error": self.error, "queued_at": self.queued_at, "started_at": self.started_at,
            "finished_at": self.finished_at, "keys": sorted(self.keys),
            "results": [vars(r) for r in self.results],
        }


class Engine:
    def __init__(self, cfg: dict, sources: list[Source] | None = None):
        self._fixed_sources = sources
        self.cfg_path = cfg.get("_path")
        self.cfg_mtime = os.path.getmtime(self.cfg_path) if self.cfg_path and os.path.exists(self.cfg_path) else None
        self.data_dir = cfg["app"]["data_dir"]
        self.log = EventLog(self.data_dir)
        self.store = Store(self.data_dir)
        self.orders: list[Order] = []
        self.source_errors: dict[str, dict] = {}
        self.last_scan: datetime | None = None
        self.jobs: list[Job] = []          # newest first, kept for the screen
        self.busy_keys: set[str] = set()
        self.lock = threading.Lock()       # guards jobs / busy_keys / orders
        self.q: queue.Queue = queue.Queue()
        self.config_error: dict | None = None
        self.waybill_printer = self.label_printer = None
        self._apply(cfg)
        self.store.run_once("2026-10-12-unprint-WH-OUT-00073",
                            lambda: self.store.unmark("odoo:WH/OUT/00073"))
        self.store.run_once("2026-10-12-brand-atlas-origins", self._brand_fix)
        threading.Thread(target=self._worker, daemon=True).start()

    def _brand_fix(self) -> None:
        """Bottle labels used a mix of "moleculelabs" and "atlas origins": use the default brand."""
        if str(self.cfg.get("labels", {}).get("brand", "")).lower() == "moleculelabs" and self.cfg_path:
            self.save_settings("labels", {"brand": "atlas origins"})
        rows = self.catalog.all_rows()
        if any(r["brand"].lower() == "moleculelabs" for r in rows):
            for r in rows:
                if r["brand"].lower() == "moleculelabs":
                    r["brand"] = ""
            self.catalog.save(rows)
            self.log.add("info", f"Product list: brand 'moleculelabs' changed to '{self.catalog.default_brand}'")

    # ---- settings / hot reload ------------------------------------------------------------

    def _apply(self, cfg: dict) -> None:
        self.cfg = cfg
        try:
            self.sources = self._fixed_sources if self._fixed_sources is not None else build_sources(cfg)
            lab = cfg.get("labels", {})
            self.catalog = Catalog(lab.get("catalog"), lab.get("brand") or None, data_dir=self.data_dir)
            printers = cfg.get("printers", {})
            for p in (self.waybill_printer, self.label_printer):
                if p is not None and hasattr(p, "close"):
                    p.close()
            self.waybill_printer = build_printer("waybill", printers.get("waybill", {}), self.data_dir)
            self.label_cfg = {"width_mm": 40, "height_mm": 20, "separator": "blank", **printers.get("label", {})}
            if lab.get("template"):
                self.label_cfg["template"] = lab["template"]
            self.label_printer = build_printer("label", self.label_cfg, self.data_dir)
            self.config_error = None
        except PackError as e:
            self.config_error = e.as_dict()
            self.log.add("error", e.text, e.code)
            if self.waybill_printer is None or self.label_printer is None:
                self.waybill_printer = build_printer("waybill", {}, self.data_dir)
                self.label_printer = build_printer("label", {"width_mm": 40, "height_mm": 20}, self.data_dir)
                self.label_cfg = {"width_mm": 40, "height_mm": 20, "separator": "blank"}
            if not hasattr(self, "sources"):
                self.sources = []
            if not hasattr(self, "catalog"):
                self.catalog = Catalog(data_dir=self.data_dir)

    def reload_if_changed(self) -> bool:
        """Config hot-reload: pick up config.toml edits (from Settings or by hand) between jobs."""
        if not self.cfg_path or not os.path.exists(self.cfg_path):
            return False
        m = os.path.getmtime(self.cfg_path)
        if m == self.cfg_mtime or self.running():
            return False
        self.cfg_mtime = m
        try:
            self._apply(config_mod.load(self.cfg_path))
            self.log.add("info", "Settings reloaded from config.toml")
        except Exception as e:
            err = classify(e, "loading settings")
            self.config_error = err.as_dict()
            self.log.add("error", err.text, err.code)
        return True

    def save_settings(self, section: str, values: dict) -> None:
        if not self.cfg_path:
            raise PackError("CONFIG", "saving settings", "no config.toml path")
        config_mod.set_values(self.cfg_path, section, values)
        self.cfg_mtime = None  # force reload
        self.reload_if_changed()

    @property
    def ship_method(self) -> str:
        return (self.cfg.get("odoo", {}).get("tiktok_ship_method") or "dropoff").lower()

    # ---- scanning --------------------------------------------------------------------------

    def _by_name(self, name: str) -> Source | None:
        return next((s for s in self.sources if s.name == name), None)

    @property
    def odoo(self) -> OdooSource | None:
        s = self._by_name("odoo")
        return s if isinstance(s, OdooSource) else None

    def scan(self) -> list[Order]:
        found: list[Order] = []
        errors = {}
        for s in self.sources:
            try:
                found += s.list_orders()
            except Exception as e:  # one platform being down must not block the others
                err = classify(e, f"reading {s.name}")
                errors[s.name] = err.as_dict()
                self.log.add("error", err.text, err.code)
        orders = self._merge(found)
        self._unprint_waybill_less(orders)
        with self.lock:
            self.orders = orders
            self.source_errors = errors
            self.last_scan = datetime.now()
        return orders

    def _unprint_waybill_less(self, orders: list[Order]) -> None:
        """Marketplace orders recorded as printed without a waybill/tracking were never really done."""
        rows = self.store.printed_rows()
        for o in orders:
            r = rows.get(o.key)
            if r and o.has_waybill and not r.get("tracking"):
                self.store.unmark(o.key)
                self.log.add("info", "Was marked printed without a waybill; back in the to-print list.", key=o.key)

    @staticmethod
    def _merge(orders: list[Order]) -> list[Order]:
        """Odoo deliveries for Shopee/TikTok orders read directly from the platforms are the same
        parcel: keep the platform order (it has the waybill) and remember the Odoo delivery."""
        platform = {o.order_id: o for o in orders if o.source != "odoo"}
        merged = [o for o in orders if o.source != "odoo"]
        for o in orders:
            if o.source != "odoo":
                continue
            twin = platform.get(o.ref.get("platform_ref", ""))
            if twin:
                twin.odoo_picking_id = o.odoo_picking_id
            else:
                merged.append(o)
        far = datetime.max
        return sorted(merged, key=lambda o: (o.ship_by or far, o.source, o.order_id))

    def state_of(self, o: Order, printed: dict, packed: dict) -> str:
        if o.key in packed:
            return "packed"
        if o.key in printed:
            return "printed"
        if o.key in self.busy_keys:
            return "busy"
        if o.needs_arrange:
            return "arrange"
        if o.waybill_missing:
            return "waybill_missing"
        return "ready"

    def pending(self, include_arrange: bool = False) -> list[Order]:
        printed = self.store.printed()
        todo = [o for o in self.orders if o.key not in printed and not o.waybill_missing
                and (include_arrange or not o.needs_arrange)]
        if self.cfg.get("app", {}).get("only_due_today"):
            end = datetime.now().replace(hour=23, minute=59, second=59)
            todo = [o for o in todo if not o.ship_by or o.ship_by <= end]
        return todo

    def plan(self) -> dict:
        printed = self.store.printed()
        unprinted = [o for o in self.orders if o.key not in printed]
        ready = self.pending()
        return {
            "ready": [o.key for o in ready],
            "labels": sum(o.unit_count for o in ready),
            "arrange": [o.key for o in unprinted if o.needs_arrange],
            "missing": [o.key for o in unprinted if o.waybill_missing],
            "ship_method": self.ship_method,
        }

    def find(self, code: str) -> Order | None:
        """Look an order up by what a packer can scan or type: tracking number, order id or match code."""
        c = code.strip().upper().lstrip("#")
        if not c:
            return None
        for o in self.orders:
            if c in ((o.tracking or "").upper(), o.order_id.upper()):
                return o
        hits = [o for o in self.orders if o.match_code and o.match_code == c]
        return hits[0] if len(hits) == 1 else None

    # ---- job queue --------------------------------------------------------------------------

    def running(self) -> bool:
        return any(j.state in ("queued", "running") for j in self.jobs)

    def submit(self, kind: str, title: str, fn, keys: set | None = None) -> Job:
        keys = set(keys or ())
        with self.lock:
            clash = keys & self.busy_keys
            if clash:
                raise Busy(f"Already queued or printing: {', '.join(sorted(clash))}")
            if kind in ("refresh", "print_all") and any(j.kind == kind and j.state in ("queued", "running")
                                                        for j in self.jobs):
                raise Busy(f"{title} is already running")
            job = Job(kind, title, keys)
            self.busy_keys |= keys
            self.jobs.insert(0, job)
            del self.jobs[30:]
        self.q.put((job, fn))
        return job

    def _worker(self):
        while True:
            job, fn = self.q.get()
            job.state, job.started_at = "running", datetime.now().isoformat(timespec="seconds")
            try:
                fn(job)
                job.state = "error" if job.error else "done"
            except Exception as e:
                err = classify(e, job.current or job.title)
                job.error, job.state = err.as_dict(), "error"
                job.summary = job.summary or err.text
                self.log.add("error", f"{job.title}: {err.text}", err.code)
            finally:
                job.finished_at = datetime.now().isoformat(timespec="seconds")
                job.current = ""
                with self.lock:
                    self.busy_keys -= job.keys
                for p in (self.waybill_printer, self.label_printer):
                    if p.status.state in ("connecting", "sending"):
                        p.status.set("idle")
                self.q.task_done()

    def wait(self, job: Job, timeout: float = 30) -> Job:
        """For scripts and tests: block until a job has finished."""
        import time
        end = time.time() + timeout
        while job.state in ("queued", "running") and time.time() < end:
            time.sleep(0.02)
        return job

    # ---- printing -----------------------------------------------------------------------------

    def print_order(self, order: Order, job: Job | None = None, arrange_ok: bool = False) -> Result:
        src = self._by_name(order.source)
        code = order.match_code
        tag = f"Order #{code}" if code else f"Order {order.order_id}"
        sent_any = False
        step = "starting"
        try:
            if order.has_waybill:
                if order.needs_arrange:
                    if not arrange_ok:
                        raise PackError("TIKTOK-ARRANGE", "booking courier", "not confirmed on screen")
                    step = "booking TikTok courier"
                    if job:
                        job.current = f"{tag}: booking courier"
                    order = (src.arrange(order, self.ship_method) if isinstance(src, OdooSource)
                             else src.arrange(order))
                    self._replace_order(order)
                    code = order.match_code
                    tag = f"Order #{code}" if code else f"Order {order.order_id}"
                if not order.waybill_ready:
                    raise PackError("WAYBILL-MISSING", "fetching waybill")
                step = "fetching waybill"
                waybill = src.get_waybill(order)
                if not order.tracking:  # the waybill itself carries it; Odoo's copy can lag behind
                    self.log.add("info", "Waybill found, but Odoo has no tracking number for it yet. Printed anyway.",
                                 key=order.key)
                step = "printing waybill on the waybill printer"
                if job:
                    job.current = f"{tag}: waybill"
                if waybill[:4] == b"%PDF":
                    self.waybill_printer.print_pdf(waybill, f"{order.source}-{order.order_id}-waybill",
                                                   detail=f"{tag}, waybill")
                else:
                    import io
                    from PIL import Image
                    self.waybill_printer.print_image(Image.open(io.BytesIO(waybill)),
                                                     f"{order.source}-{order.order_id}-waybill",
                                                     detail=f"{tag}, waybill", native=True)
                sent_any = True
            labels = labels_for_order(order, self.catalog, self.label_cfg)
            for n, (img, item, _) in enumerate(labels, 1):
                step = f"printing label {n} of {len(labels)} on the label printer"
                if job:
                    job.current = f"{tag}: label {n} of {len(labels)}"
                self.label_printer.print_image(img, f"{order.source}-{order.order_id}-{n:02d}",
                                               detail=f"{tag}, label {n} of {len(labels)}")
                sent_any = True
            if labels:
                step = "feeding separator label"
                self.label_printer.feed_separator(self.label_cfg.get("separator", "blank"), detail=f"{tag}, done")
        except Exception as e:
            err = classify(e, step)
            if sent_any:
                err = PackError("PRINT-PARTIAL", step, f"{err.code}: {err.detail or err.step}")
            self.log.add("error", err.text, err.code, order.key)
            return Result(order.key, False, err.text, error=err.as_dict())

        self.store.mark(order.key, order.tracking, len(labels))
        if self.odoo and order.odoo_picking_id and order.tracking:
            try:
                self.odoo.note_printed(order.odoo_picking_id,
                                       f"Pack Station printed waybill {order.tracking} and {len(labels)} labels"
                                       f" (match code #{code}).")
            except Exception as e:
                self.log.add("error", f"Printed, but the Odoo note failed: {e}", "ODOO-NET", order.key)
        guessed = sorted({item.sku or item.name for _, item, known in labels if not known})
        msg = f"{'waybill + ' if order.has_waybill else ''}{len(labels)} labels"
        self.log.add("info", f"Printed {msg}", key=order.key)
        return Result(order.key, True, msg, len(labels), guessed)

    def _replace_order(self, order: Order) -> None:
        with self.lock:
            self.orders = [order if o.key == order.key else o for o in self.orders]

    def _run_orders(self, job: Job, orders: list[Order], arrange_ok: bool) -> None:
        job.total = len(orders)
        for o in orders:
            job.current = o.key
            job.results.append(self.print_order(o, job, arrange_ok))
            job.done += 1
        bad = [r for r in job.results if not r.ok]
        job.summary = (f"Printed {job.done - len(bad)} of {job.total} orders"
                       + (f", {len(bad)} with problems" if bad else ""))
        if bad and len(bad) == len(job.results):
            job.error = bad[0].error

    def start_print_all(self, arrange: bool = False) -> Job:
        """Print every pending order. Orders needing a TikTok courier booking are included only when
        `arrange` (the user confirmed booking on screen). Waybill-missing orders are reported, not printed."""
        orders = self.pending(include_arrange=arrange)
        missing = [o for o in self.orders if o.waybill_missing and o.key not in self.store.printed()]

        def work(job: Job):
            for o in missing:
                err = PackError("WAYBILL-MISSING", "checking waybill")
                job.results.append(Result(o.key, False, err.text, error=err.as_dict()))
                self.log.add("error", "Skipped: " + err.text, err.code, o.key)
            self._run_orders(job, orders, arrange)
            if missing:
                job.summary += f"; {len(missing)} waiting for a waybill"

        return self.submit("print_all", "Print all", work, {o.key for o in orders})

    def start_print_one(self, key: str, force: bool = False, arrange: bool = False) -> Job | dict:
        order = next((o for o in self.orders if o.key == key), None)
        if not order:
            raise PackError("CONFIG", "finding order", "order not in the current list; press Refresh")
        printed_at = self.store.printed().get(key)
        if printed_at and not force:
            return {"needs_confirm": True, "printed_at": printed_at}

        def work(job: Job):
            # The printed record is only replaced when the new print fully succeeds, so a failed
            # reprint leaves the earlier successful print on record.
            r = self.print_order(order, job, arrange)
            job.results.append(r)
            job.total = job.done = 1
            job.summary = r.message
            if not r.ok:
                job.error = r.error

        return self.submit("print_one", f"{'Reprint' if printed_at else 'Print'} {key.split(':', 1)[-1]}",
                           work, {key})

    # ---- refresh chain ------------------------------------------------------------------------

    def start_refresh(self) -> Job:
        def work(job: Job):
            before = {o.key for o in self.orders}
            odoo = self.odoo
            if odoo:
                names = odoo.cron_names()
                for cid in odoo.crons[:2]:
                    name = f"Odoo: {names.get(cid, f'sync job {cid}')}"
                    job.step(name)
                    job.current = name
                    try:
                        odoo.run_cron(cid)
                    except PackError:
                        job.step(name, "error")
                        raise
                    job.step(name, "done")
                tt = [o.odoo_picking_id for o in self.orders if o.platform == "tiktok" and not o.waybill_ready]
                name = "Odoo: TikTok delivery sync"
                job.step(name)
                job.current = name
                odoo.sync_tiktok(tt)
                job.step(name, "done" if tt else "skipped")
                for cid in odoo.crons[2:]:
                    name = f"Odoo: {names.get(cid, f'sync job {cid}')}"
                    job.step(name)
                    job.current = name
                    try:
                        odoo.run_cron(cid)
                    except PackError:
                        job.step(name, "error")
                        raise
                    job.step(name, "done")
            name = "Reading orders and waybills"
            job.step(name)
            job.current = name
            self.scan()
            job.step(name, "done")
            new = len({o.key for o in self.orders} - before)
            if self.source_errors:
                first = next(iter(self.source_errors.values()))
                job.error = first
                job.summary = f"Refresh finished with problems at {datetime.now():%H:%M:%S}"
            else:
                job.summary = f"Refresh successful {datetime.now():%H:%M:%S}, {new} new order{'s' if new != 1 else ''}"
            self.log.add("info", job.summary)

        return self.submit("refresh", "Refresh", work)

    # ---- printer tools -----------------------------------------------------------------------

    def printer(self, name: str):
        return {"waybill": self.waybill_printer, "label": self.label_printer}[name]

    def start_calibration(self, name: str) -> Job:
        from .printers import calibration_image

        def work(job: Job):
            p = self.printer(name)
            w, h = p.dots
            note = f"X {p.offset_x_mm:+.1f}  Y {p.offset_y_mm:+.1f}  GAP {p.gap_mm:g}"
            job.current = f"{name} printer: calibration page"
            p.print_image(calibration_image(w, h, p.dpi, note), "calibration", detail="Calibration page")
            job.summary = f"Calibration page sent to the {name} printer ({note})"

        return self.submit("calibrate", f"Calibrate {name} printer", work)

    def start_test(self, name: str, data=None) -> Job:
        from .labels import LabelData, render_product_label

        def work(job: Job):
            p = self.printer(name)
            job.current = f"{name} printer: test"
            if name == "label":
                d = data or LabelData("Sandalwood", "3g", self.catalog.default_brand, "corrosive")
                c = self.label_cfg
                img = render_product_label(d, None, float(c["width_mm"]), float(c["height_mm"]),
                                           int(c.get("dpi", 203)), c.get("font"), c.get("template"))
                p.print_image(img, "test-label", detail="Test label")
            else:
                from .sources.demo import DemoSource
                demo = DemoSource()
                p.print_pdf(demo.get_waybill(demo.list_orders()[0]), "test-waybill", detail="Test waybill")
            job.summary = f"Test sent to the {name} printer"

        return self.submit("test", f"Test {name} printer", work)
