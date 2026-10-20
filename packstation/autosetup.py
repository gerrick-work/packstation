"""One-click printer setup from the Pack Station screen: finds the AiYin printers over Bluetooth by
name and saves them in config.toml. Which printer is which comes from the end of its Bluetooth name
(config `name_hint`, e.g. "0214" for the waybill printer); if no hints match, the first two found are used."""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

from .bluetooth import looks_like_printer, scan


def write_printer(text: str, section: str, values: dict) -> str:
    """Replace one [printers.x] block in config.toml text, keeping everything else as is."""
    lines = [f"[printers.{section}]"]
    for k, v in values.items():
        if isinstance(v, bool):
            lines.append(f"{k} = {str(v).lower()}")
        elif isinstance(v, str):
            lines.append(f'{k} = "{v}"')
        else:
            lines.append(f"{k} = {v:g}")
    block = "\n".join(lines) + "\n"
    pattern = re.compile(rf"^\[printers\.{section}\]\n(?:(?!^\[).*\n?)*", re.M)
    if pattern.search(text):
        return pattern.sub(block + "\n", text, count=1)
    return text.rstrip("\n") + "\n\n" + block


def assign(found: list[tuple[str, str, int]], hints: dict[str, str]) -> dict[str, tuple[str, str]]:
    """Map 'waybill'/'label' to (address, name) from a Bluetooth scan."""
    printers = [(a, n) for a, n, _ in found if looks_like_printer(n)]
    out: dict[str, tuple[str, str]] = {}
    for role, hint in hints.items():
        if hint:
            hit = next((p for p in printers if hint.lower() in p[1].lower()), None)
            if hit:
                out[role] = hit
    rest = [p for p in printers if p not in out.values()]
    for role in ("waybill", "label"):
        if role not in out and rest:
            out[role] = rest.pop(0)
    return out


def find_and_save(cfg: dict, config_path: Path, seconds: float = 10) -> dict[str, str]:
    """Scan, save the printers into config.toml, and return {role: printer name}."""
    pcfg = cfg.get("printers", {})
    hints = {r: str(pcfg.get(r, {}).get("name_hint", "")) for r in ("waybill", "label")}
    found = asyncio.run(scan(seconds))
    roles = assign(found, hints)
    if not roles:
        raise RuntimeError("No printers found. Switch both printers on, close Label Expert on your "
                           "phone and Mac, make sure Bluetooth is on, then try again.")
    text = config_path.read_text()
    for role, (addr, name) in roles.items():
        current = dict(pcfg.get(role, {}))
        current.update(backend="bluetooth", ble_address=addr, ble_name=name)
        current.setdefault("invert", False)
        text = write_printer(text, role, current)
    config_path.write_text(text)
    return {r: n for r, (_, n) in roles.items()}
