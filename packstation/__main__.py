"""Run the Pack Station:  python -m packstation            (opens the one-button page)
                         python -m packstation print-all  (prints everything pending, no browser)
                         python -m packstation test-label  (prints one sample product label)"""
from __future__ import annotations

import argparse
import webbrowser

from . import config
from .engine import Engine
from .labels import LabelData, render_product_label


def main() -> None:
    p = argparse.ArgumentParser(prog="packstation")
    p.add_argument("command", nargs="?", default="serve", choices=["serve", "scan", "print-all", "test-label"])
    p.add_argument("--config")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-browser", action="store_true")
    args = p.parse_args()

    cfg = config.load(args.config)
    engine = Engine(cfg)

    if args.command == "scan":
        for o in engine.scan():
            print(f"{o.key:32} #{o.match_code}  {o.status:20} {', '.join(f'{i.qty}x {i.name}' for i in o.items)}")
        for name, err in engine.source_errors.items():
            print(f"!! {name}: {err}")
    elif args.command == "print-all":
        engine.scan()
        for r in engine.print_all():
            print(("OK  " if r.ok else "ERR ") + f"{r.key}: {r.message}")
    elif args.command == "test-label":
        lcfg = cfg.get("printers", {}).get("label", {})
        img = render_product_label(LabelData("Sandalwood", "3g", "moleculelabs", "corrosive"), "871A",
                                   float(lcfg.get("width_mm", 50)), float(lcfg.get("height_mm", 25)),
                                   int(lcfg.get("dpi", 203)), lcfg.get("font"))
        print(engine.label_printer.print_image(img, "test-label"))
    else:
        from .web import create_app

        url = f"http://localhost:{args.port}"
        if not args.no_browser:
            webbrowser.open(url)
        print(f"Pack Station running at {url}  (Ctrl+C to stop)")
        create_app(engine).run(host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
