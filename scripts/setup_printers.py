"""Guided printer setup for the Pack Station (macOS). Run it with:  bash scripts/setup-printers-mac.command

It finds the AiYin printers over Bluetooth or USB, prints a big test number on each so you can tell
them apart, asks which is which, and saves the answers in config.toml. Nothing is changed in
config.toml until every test has printed correctly.
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


def usb_targets() -> list[tuple[str, callable, dict]]:
    say("1. Plug BOTH AiYin printers into this Mac with USB cables.")
    say("2. Switch both printers on and make sure each has its labels loaded.")
    input("\nPress Enter when both are on... ")
    uris = usb_printers()
    if not uris:
        say("\nThe Mac can't see any USB printer.")
        say("Check the cables (try another USB port or adapter), switch the printers off and on, then run this again.")
        sys.exit(1)
    if len(uris) > 2:
        say("More than 2 USB printers are connected. Unplug any other printers and run this again.")
        sys.exit(1)
    say(f"\nFound {len(uris)} USB printer(s).")
    say("\nConnecting them now. If the Mac asks for a Password, type your Mac login password and press Enter")
    say("(nothing shows on screen while you type, that is normal).")
    out = []
    for i, uri in enumerate(uris, 1):
        q = connect(i, uri)
        out.append((f"USB printer {i}", lambda job, q=q: send(q, job), {"backend": "tspl", "queue": q}))
    return out


def bluetooth_targets() -> list[tuple[str, callable, dict]]:
    import asyncio

    from packstation.bluetooth import BleLink, scan

    say("1. Switch BOTH AiYin printers on, with their labels loaded.")
    say("2. On your phone, CLOSE the Label Expert app (or turn the phone's Bluetooth off).")
    say("   A printer can only talk to one device at a time.")
    say("3. If the Mac asks 'Terminal would like to use Bluetooth', click Allow.")
    input("\nPress Enter when ready... ")
    say("\nSearching for Bluetooth printers (about 10 seconds)...")
    found = asyncio.run(scan(10))[:12]
    if not found:
        say("\nNo Bluetooth devices found. Check Bluetooth is on (top-right menu bar), the printers are on")
        say("and not connected to the phone, then run this again.")
        sys.exit(1)
    for n, (addr, name, rssi) in enumerate(found, 1):
        say(f"   {n}. {name}")
    picks: list[int] = []
    while len(picks) < 2:
        a = input(f"\nType the number of {'a' if not picks else 'the other'} printer"
                  f"{'' if not picks else ' (or just press Enter if you only have one)'}: ").strip()
        if not a and picks:
            break
        if a.isdigit() and 1 <= int(a) <= len(found) and int(a) - 1 not in picks:
            picks.append(int(a) - 1)
        else:
            say("   Please type one of the numbers in the list.")
    out = []
    for i in picks:
        addr, name, _ = found[i]
        link = BleLink(addr)
        out.append((name, link.send, {"backend": "bluetooth", "ble_address": addr, "ble_name": name}))
    return out


def main() -> None:
    if not CONFIG.exists():
        (ROOT / "config.toml").write_text((ROOT / "config.example.toml").read_text())

    say("\n=== Pack Station printer setup ===")
    mode = ask("How are the printers connected to this Mac?", {"b": "Bluetooth", "u": "USB cable"})
    say("")
    targets = bluetooth_targets() if mode == "b" else usb_targets()

    roles: dict[str, dict] = {}
    for i, (name, send_job, conf) in enumerate(targets, 1):
        say(f"\nPrinting a test card with a big number {i} on {name} ...")
        try:
            send_job(number_card(str(i), 50, 25))
        except Exception as e:
            say(f"\nCould not send to {name}: {e}")
            say("Nothing has been changed in your settings. Send Claude a screenshot of this window.")
            sys.exit(1)
        if ask(f"Did a label with the number {i} come out of one of the printers?", {"y": "yes", "n": "no"}) == "n":
            say("\nThis printer did not accept direct printing. Nothing has been changed in your settings.")
            say("Take a photo of the printer's model sticker and send it, plus a screenshot of this window, to Claude.")
            sys.exit(1)
        invert = ask("Was it a BLACK number on a WHITE label?", {"y": "yes", "n": "no, it was white on black"}) == "n"
        if invert:
            send_job(number_card(str(i), 50, 25, invert=True))
            if ask("A second one printed. Is THAT one black on white?", {"y": "yes", "n": "no"}) == "n":
                say("\nThe colours are still wrong. Nothing has been changed. Send Claude a photo of both labels.")
                sys.exit(1)
        role = ask(f"Which printer printed number {i}?", {"w": "the BIG waybill printer", "s": "the SMALL product label printer"})
        roles["waybill" if role == "w" else "label"] = dict(conf, invert=invert)

    if len(targets) == 2 and len(roles) != 2:
        say("\nYou picked the same printer twice. Run this again and choose carefully.")
        sys.exit(1)

    size = "40x20"
    if "label" in roles:
        size = input("\nWhat size are your SMALL product labels? Look on the roll's box, e.g. 40x20 (press Enter for 40x20): ").strip() or "40x20"
        if not re.match(r"^\s*(\d+(?:\.\d+)?)\s*[x×*]\s*(\d+(?:\.\d+)?)\s*$", size):
            say("That size wasn't understood, so 40x20 is used. You can change it later in config.toml.")
            size = "40x20"

    from packstation.config import set_values

    if "waybill" in roles:
        r = roles["waybill"]
        set_values(CONFIG, "printers.waybill", {**{k: v for k, v in r.items() if k != "invert"},
                                                "width_mm": 100, "height_mm": 150, "dpi": 203, "invert": r["invert"]})
    if "label" in roles:
        r = roles["label"]
        lw, lh = (float(x) for x in re.split(r"[x×*]", size.replace(" ", "")))
        set_values(CONFIG, "printers.label", {**{k: v for k, v in r.items() if k != "invert"},
                                              "width_mm": lw, "height_mm": lh, "dpi": 203,
                                              "invert": r["invert"]})

    say("\n=== Done! Your printers are set up. ===")
    if mode == "b":
        say("Over Bluetooth a big waybill takes a while to send (around 15 to 25 seconds). That's normal.")
    say("Next: start Pack Station again (bash scripts/start-mac.command), click 'Printers' on the left,")
    say("and press 'Print a test label' and 'Print a test waybill'. Then press Print all to print the 3 test orders.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        say("\nStopped. Nothing more was changed.")
