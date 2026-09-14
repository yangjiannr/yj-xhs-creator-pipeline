# -*- coding: utf-8 -*-
"""Launch MediaCrawler for one Xiaohongshu creator, then init the download queue.

Does not download videos. Requires MediaCrawler venv + XHS login (QR / browser).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def load_config(creator_root: Path) -> dict:
    cfg = {}
    cand = creator_root / "pipeline_config.json"
    if cand.exists():
        cfg.update(json.loads(cand.read_text(encoding="utf-8")))
    return cfg


def find_python(mc_dir: Path) -> Path:
    for rel in (
        ".venv/Scripts/python.exe",
        "venv/Scripts/python.exe",
        ".venv/bin/python",
        "venv/bin/python",
    ):
        p = mc_dir / rel
        if p.exists():
            return p
    return Path(sys.executable)


def find_latest_jsonl(mc_dir: Path) -> Path | None:
    base = mc_dir / "data" / "xhs" / "jsonl"
    if not base.exists():
        return None
    cands = sorted(base.glob("creator_contents_*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    return cands[0] if cands else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--creator-root", required=True)
    ap.add_argument("--creator-url", default="", help="override pipeline_config.xhs_creator_url")
    ap.add_argument("--skip-crawl", action="store_true", help="only init queue from latest MediaCrawler jsonl")
    ap.add_argument("--dry-run", action="store_true", help="print crawl command and exit")
    args = ap.parse_args()

    root = Path(args.creator_root)
    cfg = load_config(root)
    mc_dir = Path(cfg.get("mediacrawler_dir") or "")
    creator_url = (args.creator_url or cfg.get("xhs_creator_url") or "").strip()
    if not mc_dir.exists():
        raise SystemExit(f"mediacrawler_dir missing: {mc_dir}")
    if not creator_url and not args.skip_crawl:
        raise SystemExit("set xhs_creator_url in pipeline_config.json or pass --creator-url")

    py = find_python(mc_dir)
    init_py = root / "scripts" / "init_queue_xhs.py"
    if not init_py.exists():
        raise SystemExit(f"missing {init_py}")

    if not args.skip_crawl:
        cmd = [
            str(py),
            "main.py",
            "--platform",
            "xhs",
            "--type",
            "creator",
            "--creator_id",
            creator_url,
            "--save_data_option",
            "jsonl",
            "--get_comment",
            "false",
        ]
        print(f"[{now()}] cwd={mc_dir}")
        print(f"[{now()}] {' '.join(cmd)}")
        if args.dry_run:
            return 0
        p = subprocess.run(cmd, cwd=str(mc_dir))
        if p.returncode != 0:
            return p.returncode

    latest = find_latest_jsonl(mc_dir)
    if not latest:
        raise SystemExit(f"no jsonl under {mc_dir / 'data' / 'xhs' / 'jsonl'}")
    print(f"[{now()}] init queue from {latest}")
    return subprocess.call(
        [sys.executable, str(init_py), "--creator-root", str(root), "--jsonl", str(latest)]
    )


if __name__ == "__main__":
    raise SystemExit(main())
