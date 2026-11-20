"""Entry point.

  python -m tracker            run the dashboard + passive capture
  python -m tracker chrome     launch Chrome with a dedicated profile and a local DevTools port
  python -m tracker demo       run the dashboard on fake data (no Chrome, separate demo.db)
"""
from __future__ import annotations

import argparse
import logging
import os
import platform
import random
import shutil
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit

import uvicorn

from .config import Config
from .redact import RedactingFilter


def setup_logging(cfg: Config) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    handler.addFilter(RedactingFilter(cfg.secrets()))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(cfg.log_level)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers[:] = []
        lg.propagate = True
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)


def find_chrome(cfg: Config) -> str | None:
    if cfg.chrome_path and Path(cfg.chrome_path).exists():
        return cfg.chrome_path
    system = platform.system()
    candidates: list[str] = []
    if system == "Windows":
        for base in (os.environ.get("PROGRAMFILES", r"C:\Program Files"),
                     os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
                     os.environ.get("LOCALAPPDATA", "")):
            if base:
                candidates.append(str(Path(base) / "Google/Chrome/Application/chrome.exe"))
    elif system == "Darwin":
        candidates.append("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    for c in candidates:
        if Path(c).exists():
            return c
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome"):
        if p := shutil.which(name):
            return p
    return None


def cmd_chrome(cfg: Config) -> int:
    chrome = find_chrome(cfg)
    if not chrome:
        print("Chrome not found. Set CHROME_PATH in .env.", file=sys.stderr)
        return 1
    port = urlsplit(cfg.cdp_url).port or 9222
    profile = cfg.chrome_profile_dir.resolve()
    profile.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(profile, 0o700)
    except OSError:
        pass
    args = [chrome, f"--remote-debugging-port={port}", "--remote-debugging-address=127.0.0.1",
            f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check", cfg.site_link]
    print(f"Launching Chrome (dedicated profile: {profile}, DevTools on 127.0.0.1:{port}).")
    print("Log in by hand in that window. Browse the appointment page as you normally would.")
    subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # noqa: S603
    return 0


def seed_demo(cfg: Config) -> None:
    from .db import DB, utcnow

    db = DB(cfg.db_path)
    if db.history(days=365):
        db.close()
        return
    rng = random.Random(7)
    now = utcnow()
    for loc_i, loc in enumerate(cfg.locations):
        for t in ("OFC", "INTERVIEW"):
            base = 25 + loc_i * 9 + (0 if t == "OFC" else 30)
            for step in range(0, 14 * 4):
                at = now - timedelta(hours=6 * (14 * 4 - step))
                drift = max(2, base - step // 3 + rng.randint(-3, 3))
                if rng.random() < 0.12:
                    db.insert_snapshot(at, loc, t, [], "NO SLOTS")
                    continue
                start = (at + timedelta(days=drift)).date()
                dates = sorted({(start + timedelta(days=rng.randint(0, 120))).isoformat() for _ in range(rng.randint(4, 30))}
                               | {start.isoformat()})
                db.insert_snapshot(at, loc, t, dates, "OK")
            first = (now + timedelta(days=max(2, base - 18))).date().isoformat()
            times = [{"time": f"{h:02d}:{m:02d}", "count": rng.randint(1, 6)} for h in range(8, 12) for m in (0, 30)
                     if rng.random() < 0.6]
            db.insert_time_slots(now, loc, t, first, times)
    db.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m tracker")
    ap.add_argument("command", nargs="?", default="run", choices=["run", "chrome", "demo"])
    ap.add_argument("--env", default=".env")
    a = ap.parse_args(argv)
    cfg = Config.load(a.env)
    setup_logging(cfg)

    if a.command == "chrome":
        return cmd_chrome(cfg)

    from .app import create_app

    if a.command == "demo":
        cfg.db_path = Path("data/demo.db")
        cfg.discovery_mode = False
        seed_demo(cfg)
        app = create_app(cfg, start_watcher=False)
    else:
        app = create_app(cfg)
    if cfg.host not in ("127.0.0.1", "localhost", "::1"):
        logging.getLogger("tracker").warning("Dashboard bound to %s, not localhost. Anyone on that network can read it.", cfg.host)
    print(f"Dashboard: http://{cfg.host}:{cfg.port}")
    uvicorn.run(app, host=cfg.host, port=cfg.port, log_config=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
