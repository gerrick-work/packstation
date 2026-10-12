#!/usr/bin/env python3
"""Set up the Charlie Yin / 6 Degrees corporate gifting project in Odoo and file its documents.

Run on your own Mac (Python 3, nothing to install):

    python3 scripts/odoo_charlie_project.py ~/Downloads/Pro_Forma_Invoice_PF-2026-1001_-_6_Degrees_Sdn_Bhd.pdf \
        ~/Downloads/Charlie_gift_card_artwork_Miniature_Perfume.png

It asks for your Odoo login and API key (Odoo: Preferences > Account Security > New API Key).
They are used for this run only and never saved. It first shows what it will do and changes
nothing until you type "yes".

What it does (safe to run twice: anything already there is left alone):
  - finds the customer 6 Degrees Sdn Bhd and the deposit invoice ATL/2026/1001 (read only)
  - creates the project "Charlie Yin: corporate gifting 400 x 5ml" (deadline 10/11/2026)
  - creates the job's tasks
  - attaches the files you give it, unchanged: the pro forma PDF to the project and its task,
    artwork images to the project and the Production task.
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
TASKS = [  # (key, name, deadline or None, description)
    ("samples", "Samples: 3 x 5ml (deposit invoice ATL/2026/1001, RM 100)", None,
     "<p>Sample deposit invoiced on ATL/2026/1001. Charlie picked Linen Tide, Pulau Breeze and Harbour Hour on 5 Oct.</p>"),
    ("proforma", "Pro forma PF/2026/1001: collect RM 1,100", "2026-10-26",
     "<p>Pro forma issued 12/10/2026, valid to 26/10/2026. Once paid, issue the final invoice in Odoo "
     "and mark this done.</p>"),
    ("production", "Production: 400 x 5ml (Linen Tide, Pulau Breeze, Harbour Hour)", "2026-11-05",
     "<p>Starts once the pro forma is paid.</p>"),
    ("delivery", "Delivery to 6 Degrees Sdn Bhd", DEADLINE, "<p>Deliver by 10/11/2026.</p>"),
]

# Which task each kind of file is filed on (besides the project itself), and the chatter note.
FILE_KINDS = {
    ".pdf": ("proforma", "application/pdf", "Pro forma PF/2026/1001 filed (as issued 12/10/2026)."),
    ".png": ("production", "image/png", "Gift card artwork filed: Miniature Perfume, \"A little scent of celebration\"."),
    ".jpg": ("production", "image/jpeg", "Artwork filed."),
    ".jpeg": ("production", "image/jpeg", "Artwork filed."),
}


class Odoo:
    def __init__(self, url, db, login, key):
        self.db, self.key = db, key
        self.uid = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/common").authenticate(db, login, key, {})
        if not self.uid:
            raise SystemExit("Odoo login failed: check the email and API key.")
        self.obj = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/object", allow_none=True)

    def __call__(self, model, method, *args, **kw):
        return self.obj.execute_kw(self.db, self.uid, self.key, model, method, list(args), kw)


def plan(o: Odoo, files: list[Path]):
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
        for f in files:
            attached |= {(f.name, a["res_model"], a["res_id"]) for a in o(
                "ir.attachment", "search_read", [("name", "=", f.name)], fields=["res_model", "res_id"])}
    return partner, (inv[0] if inv else None), (project[0]["id"] if project else None), existing_tasks, attached


def main():
    files = [Path(a).expanduser() for a in sys.argv[1:]]
    for f in files:
        if not f.exists() or f.suffix.lower() not in FILE_KINDS:
            raise SystemExit(f"Can't file {f}: give existing .pdf, .png or .jpg files")
    if not files:
        print("No files given: the project and tasks will be set up without attachments.\n")

    print(f"Odoo: {URL}  (database {DB})")
    login = input("Odoo login email: ").strip()
    key = getpass.getpass("Odoo API key (hidden): ").strip()
    o = Odoo(URL, DB, login, key)

    partner, inv, project_id, existing_tasks, attached = plan(o, files)
    print(f"\nCustomer: {partner['name']} (id {partner['id']})")
    print(f"Deposit invoice {DEPOSIT_INVOICE}: " + (f"found, {inv['state']}, payment {inv['payment_state']}" if inv else "not found (only mentioned in the notes)"))
    todo = []
    print(f"\nProject '{PROJECT}': " + ("already exists, keeping it" if project_id else "will be CREATED"))
    if not project_id:
        todo.append("project")
    for _, name, *_ in TASKS:
        new = name not in existing_tasks
        print(f"  task: {name}: " + ("will be CREATED" if new else "already there"))
        if new:
            todo.append(name)
    task_names = {k: n for k, n, *_ in TASKS}
    for f in files:
        if project_id and (f.name, "project.project", project_id) in attached:
            print(f"  {f.name}: already on the project")
            continue
        task = task_names[FILE_KINDS[f.suffix.lower()][0]]
        print(f"  attach {f.name} to the project and the task '{task}' (file unchanged)")
        todo.append(f.name)
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
    for _, name, deadline, desc in TASKS:
        if name not in task_ids:
            vals = {"name": name, "project_id": project_id, "partner_id": partner["id"], "description": desc}
            if deadline:
                vals["date_deadline"] = deadline
            task_ids[name] = o("project.task", "create", vals)
            print(f"Created task: {name}")

    for f in files:
        task_key, mimetype, note = FILE_KINDS[f.suffix.lower()]
        data = base64.b64encode(f.read_bytes()).decode()
        for model, rid in (("project.project", project_id), ("project.task", task_ids[task_names[task_key]])):
            if (f.name, model, rid) in attached:
                print(f"{f.name} already on {model} {rid}")
                continue
            att_id = o("ir.attachment", "create", {"name": f.name, "datas": data, "res_model": model,
                                                   "res_id": rid, "mimetype": mimetype})
            o(model, "message_post", [rid], body=note, attachment_ids=[att_id])
            print(f"Attached {f.name} to {model} {rid}")
    print(f"\nDone. In Odoo, open Project > '{PROJECT}' (id {project_id}).")


if __name__ == "__main__":
    main()
