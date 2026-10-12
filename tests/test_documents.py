import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "documents"))
import render  # noqa: E402

EXAMPLES = sorted((ROOT / "documents" / "examples").glob("*.json"))


def load(name):
    return json.loads((ROOT / "documents" / "examples" / f"{name}.json").read_text())


def test_every_type_has_an_example():
    assert {p.stem for p in EXAMPLES} == set(render.DOC_TYPES)


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.stem)
def test_examples_are_valid(path):
    assert render.check(json.loads(path.read_text())) == []


def test_totals_and_deposit_line():
    html = render.build_html(load("proforma"))
    assert "Pro Forma Invoice PF/2026/1099" in html
    assert "RM 1,200.00" in html and "RM -100.00" in html
    assert "<span>Total</span><span>RM 1,100.00</span>" in html
    assert "Payment Communication: <b>PF/2026/1099</b>" in html
    assert "not a tax invoice" in html


def test_delivery_order_has_no_prices():
    html = render.build_html(load("delivery_order"))
    assert "Unit Price" not in html and "RM " not in html.split("<table>")[1]
    assert "Received in good order by" in html


def test_check_catches_mistakes():
    doc = load("invoice")
    doc["number"] = "PF/2026/1"
    doc["lines"][0]["description"] = "Gift — 5ml"
    del doc["due_date"]
    errs = render.check(doc)
    assert any("start with ATL/" in e for e in errs)
    assert any("dashes" in e for e in errs)
    assert any("due_date" in e for e in errs)


def test_client_text_is_escaped():
    doc = load("invoice")
    doc["bill_to"]["name"] = "<script>x</script>"
    assert "<script>x" not in render.build_html(doc)
