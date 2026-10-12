"""Guided printer setup for the Pack Station (macOS). Run it with:  bash scripts/setup-printers-mac.command

It finds the AiYin printers on USB, connects each one as a direct (raw) macOS printer, prints a big
test number on each so you can tell them apart, asks which is which, and saves the answers in
config.toml. Nothing is changed in config.toml until every test has printed correctly.
"""
from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PIL import Image, ImageDraw  # noqa: E402

from packstation.labels import load_font  # noqa: E402
from packstation.printers import fit_to_label, tspl_job  # noqa: E402

CONFIG = ROOT / "config.toml"
QUEUE_PREFIX = "PackStation_USB"


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def say(text: str = "") -> None:
    print(text, flush=True)


def ask(question: str, choices: dict[str, str]) -> str:
    options = " / ".join(f"{k} = {v}" for k, v in choices.items())
    while True:
        a = input(f"\n{question}\n   ({options}): ").strip().lower()
        if a in choices:
            return a
        say("   Please type one of: " + ", ".join(choices))


def usb_printers() -> list[str]:
    out = run(["lpinfo", "-v"]).stdout
    uris = [line.split(None, 1)[1].strip() for line in out.splitlines()
            if line.strip().startswith("direct usb://")]
    aiyin = [u for u in uris if re.search(r"aiyin|e40", u, re.I)]
    return aiyin or uris


def connect(n: int, uri: str) -> str:
    queue = f"{QUEUE_PREFIX}{n}"
    r = run(["sudo", "lpadmin", "-p", queue, "-E", "-v", uri, "-m", "raw"])
    if r.returncode:
        raise SystemExit(f"Could not connect printer {n}: {r.stderr.strip()}")
    return queue


def number_card(text: str, w_mm: float, h_mm: float, invert: bool = False) -> bytes:
    """A big black number on white, sized for the label, as a TSPL job."""
    w, h = round(w_mm / 25.4 * 203), round(h_mm / 25.4 * 203)
    im = Image.new("L", (w, h), 255)
    d = ImageDraw.Draw(im)
    font = load_font(None, int(h * 0.6), bold=True)
    tw = d.textlength(text, font=font)
    d.text(((w - tw) / 2, h * 0.12), text, font=font, fill=0)
    d.rectangle((4, 4, w - 5, h - 5), outline=0, width=4)
    return tspl_job(fit_to_label(im, w, h), w_mm, h_mm, invert=invert)


def send(queue: str, job: bytes) -> None:
    with tempfile.NamedTemporaryFile(suffix=".tspl", delete=False) as f:
        f.write(job)
    r = run(["lp", "-d", queue, "-o", "raw", f.name])
    if r.returncode:
        raise SystemExit(f"Could not send the test to {queue}: {r.stderr.strip()}")


def write_printer(text: str, section: str, values: dict) -> str:
    """Replace one [printers.x] block in config.toml text, keeping everything else as is."""
    lines = [f"[printers.{section}]"]
    for k, v in values.items():
        lines.append(f"{k} = " + (f'"{v}"' if isinstance(v, str) else str(v).lower() if isinstance(v, bool) else f"{v:g}"))
    block = "\n".join(lines) + "\n"
    pattern = re.compile(rf"^\[printers\.{section}\]\n(?:(?!^\[).*\n?)*", re.M)
    if pattern.search(text):
        return pattern.sub(block + "\n", text, count=1)
    return text.rstrip("\n") + "\n\n" + block


def main() -> None:
    if not CONFIG.exists():
        (ROOT / "config.toml").write_text((ROOT / "config.example.toml").read_text())

    say("\n=== Pack Station printer setup ===")
    say("1. Plug BOTH AiYin printers into this Mac with USB cables.")
    say("2. Switch both printers on and make sure each has its labels loaded.")
    input("\nPress Enter when both are on... ")

    uris = usb_printers()
    if not uris:
        say("\nThe Mac can't see any USB printer.")
        say("Check the cables (try another USB port or adapter), switch the printers off and on, then run this again.")
        sys.exit(1)
    say(f"\nFound {len(uris)} USB printer(s).")
    if len(uris) > 2:
        say("More than 2 USB printers are connected. Unplug any other printers and run this again.")
        sys.exit(1)

    say("\nConnecting them now. If the Mac asks for a Password, type your Mac login password and press Enter")
    say("(nothing shows on screen while you type, that is normal).")
    queues = [connect(i + 1, u) for i, u in enumerate(uris)]

    roles: dict[str, dict] = {}
    for i, q in enumerate(queues, 1):
        say(f"\nPrinting a test card with a big number {i} ...")
        send(q, number_card(str(i), 50, 25))
        printed = ask(f"Did a label with the number {i} come out of one of the printers?",
                      {"y": "yes", "n": "no"})
        if printed == "n":
            say("\nThis printer did not accept direct printing. Nothing has been changed in your settings.")
            say("Take a photo of the printer's model sticker and send it, plus a screenshot of this window, to Claude.")
            sys.exit(1)
        invert = ask("Was it a BLACK number on a WHITE label?", {"y": "yes", "n": "no, it was white on black"}) == "n"
        if invert:
            send(q, number_card(str(i), 50, 25, invert=True))
            if ask("A second one printed. Is THAT one black on white?", {"y": "yes", "n": "no"}) == "n":
                say("\nThe colours are still wrong. Nothing has been changed. Send Claude a photo of both labels.")
                sys.exit(1)
        role = ask(f"Which printer printed number {i}?", {"w": "the BIG waybill printer", "s": "the SMALL product label printer"})
        roles["waybill" if role == "w" else "label"] = {"queue": q, "invert": invert}

    if len(queues) == 2 and len(roles) != 2:
        say("\nYou picked the same printer twice. Run this again and choose carefully.")
        sys.exit(1)

    size = "50x25"
    if "label" in roles:
        size = input("\nWhat size are your SMALL product labels? Look on the roll's box, e.g. 50x25 (press Enter for 50x25): ").strip() or "50x25"
        m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*[x×*]\s*(\d+(?:\.\d+)?)\s*$", size)
        if not m:
            say("That size wasn't understood, so 50x25 is used. You can change it later in config.toml.")
            size = "50x25"

    text = CONFIG.read_text()
    if "waybill" in roles:
        text = write_printer(text, "waybill", {"backend": "tspl", "queue": roles["waybill"]["queue"],
                                               "width_mm": 100, "height_mm": 150, "dpi": 203,
                                               "invert": roles["waybill"]["invert"]})
    if "label" in roles:
        lw, lh = (float(x) for x in re.split(r"[x×*]", size.replace(" ", "")))
        text = write_printer(text, "label", {"backend": "tspl", "queue": roles["label"]["queue"],
                                             "width_mm": lw, "height_mm": lh, "dpi": 203,
                                             "invert": roles["label"]["invert"], "match_code": True})
    CONFIG.write_text(text)

    say("\n=== Done! Your printers are set up. ===")
    say("Next: start Pack Station again (bash scripts/start-mac.command), click 'Printers' on the left,")
    say("and press 'Print a test label' and 'Print a test waybill'. Then press Print all to print the 3 test orders.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        say("\nStopped. Nothing more was changed.")
