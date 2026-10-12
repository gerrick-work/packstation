#!/usr/bin/env python3
"""Set up the Charlie Yin / 6 Degrees corporate gifting project in Odoo and file its documents.

Run on your own Mac (Python 3, nothing to install):

    python3 scripts/odoo_charlie_project.py ~/Downloads/Pro_Forma_Invoice_PF-2026-1001_-_6_Degrees_Sdn_Bhd.pdf

It asks for your Odoo login and API key (Odoo: Preferences > Account Security > New API Key).
They are used for this run only and never saved. It first shows what it will do and changes
nothing until you type "yes".

What it does (safe to run twice: anything already there is left alone):
  - finds the customer 6 Degrees Sdn Bhd and the deposit invoice ATL/2026/1001 (read only)
  - creates the project "Charlie Yin: corporate gifting 400 x 5ml" (deadline 10/11/2026)
  - creates the job's tasks
  - attaches the existing pro forma PDF to the project and its task, exactly as issued.
    No invoice is created, re-issued or changed.
"""
from __future__ import annotations

import base64
import getpass
import sys
import xmlrpc.client
from pathlib import Path

URL = "https://atlas-dsf.odoo.com"
DB = "atlas-dsf"

CUSTOMER = "6 Degrees Sdn Bhd"
DEPOSIT_INVOICE = "ATL/2026/1001"
PROJECT = "Charlie Yin: corporate gifting 400 x 5ml"
DEADLINE = "2026-11-10"
DESCRIPTION = (
    "<p>400 x 5ml fragrance for 6 Degrees Sdn Bhd (attn Charlie Yin), due 10/11/2026.</p>"
    "<p>Scents: Linen Tide (FC0041), Pulau Breeze (FC0044), Harbour Hour (FC0049).</p>"
    "<p>Quoted RM 1,200.00 (400 x RM 3.00). Sample deposit RM 100.00 on invoice ATL/2026/1001, "
    "credited on pro forma PF/2026/1001: RM 1,100.00 payable.</p>"
)
TASKS = [  # (name, deadline or None, description, attach the pro forma?)
    ("Samples: 3 x 5ml (deposit invoice ATL/2026/1001, RM 100)", None,
     "<p>Sample deposit invoiced on ATL/2026/1001. Charlie picked Linen Tide, Pulau Breeze and Harbour Hour on 5 Oct.</p>", False),
    ("Pro forma PF/2026/1001: collect RM 1,100", "2026-10-26",
     "<p>Pro forma issued 12/10/2026, valid to 26/10/2026. Once paid, issue the final invoice in Odoo "
     "and mark this done.</p>", True),
    ("Production: 400 x 5ml (Linen Tide, Pulau Breeze, Harbour Hour)", "2026-11-05",
     "<p>Starts once the pro forma is paid.</p>", False),
    ("Delivery to 6 Degrees Sdn Bhd", DEADLINE, "<p>Deliver by 10/11/2026.</p>", False),
]


class Odoo:
    def __init__(self, url, db, login, key):
        self.db, self.key = db, key
        self.uid = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/common").authenticate(db, login, key, {})
        if not self.uid:
            raise SystemExit("Odoo login failed: check the email and API key.")
        self.obj = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/object", allow_none=True)

    def __call__(self, model, method, *args, **kw):
        return self.obj.execute_kw(self.db, self.uid, self.key, model, method, list(args), kw)


def plan(o: Odoo, pdf: Path | None):
    """Work out what exists and what would be created. Reads only."""
    partners = o("res.partner", "search_read", [("name", "ilike", CUSTOMER), ("is_company", "=", True)],
                 fields=["id", "name"], limit=2)
    if not partners:
        partners = o("res.partner", "search_read", [("name", "ilike", CUSTOMER)], fields=["id", "name"], limit=2)
    if len(partners) != 1:
        raise SystemExit(f"Expected one customer named '{CUSTOMER}' in Odoo, found {len(partners)}. "
                         "Fix the name in Odoo or in this script, then run again.")
    partner = partners[0]
    inv = o("account.move", "search_read", [("name", "=", DEPOSIT_INVOICE)], fields=["id", "state", "payment_state"], limit=1)
    project = o("project.project", "search_read", [("name", "=", PROJECT)], fields=["id"], limit=1)
    existing_tasks = set()
    attached = set()
    if project:
        pid = project[0]["id"]
        existing_tasks = {t["name"] for t in o("project.task", "search_read", [("project_id", "=", pid)], fields=["name"])}
        if pdf:
            attached = {(a["res_model"], a["res_id"]) for a in o(
                "ir.attachment", "search_read", [("name", "=", pdf.name)], fields=["res_model", "res_id"])}
    return partner, (inv[0] if inv else None), (project[0]["id"] if project else None), existing_tasks, attached


def main():
    pdf = Path(sys.argv[1]).expanduser() if len(sys.argv) > 1 else None
    if pdf and (not pdf.exists() or pdf.suffix.lower() != ".pdf"):
        raise SystemExit(f"Not a PDF: {pdf}")
    if not pdf:
        print("No pro forma PDF given: the project and tasks will be set up without the attachment.\n")

    print(f"Odoo: {URL}  (database {DB})")
    login = input("Odoo login email: ").strip()
    key = getpass.getpass("Odoo API key (hidden): ").strip()
    o = Odoo(URL, DB, login, key)

    partner, inv, project_id, existing_tasks, attached = plan(o, pdf)
    print(f"\nCustomer: {partner['name']} (id {partner['id']})")
    print(f"Deposit invoice {DEPOSIT_INVOICE}: " + (f"found, {inv['state']}, payment {inv['payment_state']}" if inv else "not found (only mentioned in the notes)"))
    todo = []
    print(f"\nProject '{PROJECT}': " + ("already exists, keeping it" if project_id else "will be CREATED"))
    if not project_id:
        todo.append("project")
    for name, *_ in TASKS:
        new = name not in existing_tasks
        print(f"  task: {name}: " + ("will be CREATED" if new else "already there"))
        if new:
            todo.append(name)
    if pdf and not (project_id and ("project.project", project_id) in attached):
        print(f"  attach {pdf.name} to the project and the pro forma task (unchanged file, nothing re-issued)")
        todo.append("attach")
    if not todo:
        print("\nNothing to do. Odoo is already up to date.")
        return
    if input('\nType "yes" to make these changes in Odoo: ').strip().lower() != "yes":
        print("Nothing changed.")
        return

    if not project_id:
        project_id = o("project.project", "create", {
            "name": PROJECT, "partner_id": partner["id"], "date": DEADLINE, "description": DESCRIPTION})
        print(f"Created project {project_id}")
    task_ids = {t["name"]: t["id"] for t in o("project.task", "search_read", [("project_id", "=", project_id)], fields=["name"])}
    for name, deadline, desc, _ in TASKS:
        if name not in task_ids:
            vals = {"name": name, "project_id": project_id, "partner_id": partner["id"], "description": desc}
            if deadline:
                vals["date_deadline"] = deadline
            task_ids[name] = o("project.task", "create", vals)
            print(f"Created task: {name}")

    if pdf:
        data = base64.b64encode(pdf.read_bytes()).decode()
        targets = [("project.project", project_id)] + [
            ("project.task", task_ids[name]) for name, _, _, att in TASKS if att]
        for model, rid in targets:
            if (model, rid) in attached:
                print(f"{pdf.name} already on {model} {rid}")
                continue
            att_id = o("ir.attachment", "create", {"name": pdf.name, "datas": data, "res_model": model,
                                                   "res_id": rid, "mimetype": "application/pdf"})
            o(model, "message_post", [rid], body="Pro forma PF/2026/1001 filed (as issued 12/10/2026).",
              attachment_ids=[att_id])
            print(f"Attached {pdf.name} to {model} {rid}")
    print(f"\nDone. In Odoo, open Project > '{PROJECT}' (id {project_id}).")


if __name__ == "__main__":
    main()
