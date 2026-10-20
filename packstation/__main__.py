"""Run the Pack Station:  python -m packstation            (opens the station screen)
                         python -m packstation scan       (lists what it finds)
                         python -m packstation print-all  (prints everything pending, no browser)"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import webbrowser

from . import config
from .engine import Engine


def keep_awake() -> None:
    """macOS: stop the Mac sleeping while Pack Station runs (ends automatically when it quits)."""
    if sys.platform == "darwin":
        try:
            subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
        except OSError:
            pass


def main() -> None:
    p = argparse.ArgumentParser(prog="packstation")
    p.add_argument("command", nargs="?", default="serve", choices=["serve", "scan", "print-all"])
    p.add_argument("--config")
    p.add_argument("--port", type=int)
    p.add_argument("--no-browser", action="store_true")
    args = p.parse_args()

    cfg = config.load(args.config)
    engine = Engine(cfg)

    if args.command == "scan":
        for o in engine.scan():
            print(f"{o.key:32} #{o.match_code or '----'}  {o.status:24} "
                  f"{', '.join(f'{i.qty}x {i.name}' for i in o.items)}")
        for name, err in engine.source_errors.items():
            print(f"!! {name}: {err['code']} {err['message']} {err.get('detail', '')}")
    elif args.command == "print-all":
        engine.scan()
        job = engine.wait(engine.start_print_all(), timeout=3600)
        for r in job.results:
            print(("OK  " if r.ok else "ERR ") + f"{r.key}: {r.message}")
    else:
        from .web import create_app

        app_cfg = cfg.get("app", {})
        port = args.port or int(app_cfg.get("port", 8765))
        host = app_cfg.get("host", "0.0.0.0")  # 0.0.0.0: reachable from phones/iPads on the home Wi-Fi
        url = f"http://localhost:{port}"
        keep_awake()
        if not args.no_browser and not os.environ.get("PACKSTATION_RESTARTED"):
            webbrowser.open(url)
        print(f"Pack Station running at {url}  (Ctrl+C to stop)")
        import time
        from werkzeug.serving import make_server

        app = create_app(engine)
        # A copy started by Flask's run() passes its listening socket on through restarts
        # (WERKZEUG_SERVER_FD). Close that leftover so this copy can bind the port itself.
        fd = os.environ.pop("WERKZEUG_SERVER_FD", None)
        if fd and fd.isdigit():
            try:
                os.close(int(fd))
            except OSError:
                pass
        for attempt in range(20):  # after a restart the old copy may need a moment to let go of the port
            try:
                srv = make_server(host, port, app, threaded=True)
                break
            except OSError:
                if attempt == 19:
                    raise
                time.sleep(1)
        app.config["SERVER"] = srv
        srv.serve_forever()


if __name__ == "__main__":
    main()
