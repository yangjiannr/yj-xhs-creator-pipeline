# -*- coding: utf-8 -*-
"""Retry failed Xiaohongshu downloads from logs/download_failed.log."""
from __future__ import annotations

import argparse
import csv
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
)
REFERER = "https://www.xiaohongshu.com/"


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def append_log(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(f"[{now()}] {line}\n")


def load_config(creator_root: Path) -> dict:
    cfg = {}
    cand = creator_root / "pipeline_config.json"
    if cand.exists():
        try:
            cfg.update(json.loads(cand.read_text(encoding="utf-8")))
        except Exception:
            pass
    return cfg


def pid_alive(pid: int) -> bool:
    import ctypes

    handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
    if handle:
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    return False


def find_downloaded_file(work_dir: Path):
    if not work_dir.exists():
        return None
    cands = [
        p
        for p in work_dir.rglob("*")
        if p.is_file() and p.suffix.lower() in {".mp4", ".webm", ".mkv", ".mov"} and p.stat().st_size > 100_000
    ]
    if not cands:
        return None

    def score(p: Path) -> tuple:
        stem = p.stem
        hashlike = len(stem) == 32 and all(c in "0123456789abcdef" for c in stem.lower())
        return (0 if hashlike else 1, p.stat().st_size, p.stat().st_mtime)

    cands.sort(key=score, reverse=True)
    return cands[0]


def load_jsonl_urls(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            o = json.loads(line)
            nid = str(o.get("note_id") or "").strip()
            raw = str(o.get("video_url") or "").strip()
            url = raw.split(",")[0].strip() if raw else ""
            if nid and url and nid not in out:
                out[nid] = url
    return out


def parse_failed(log: Path) -> list[tuple[str, str]]:
    """Return unique (filename, note_url) from FAIL lines (latest wins)."""
    if not log.exists():
        return []
    latest: dict[str, str] = {}
    for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
        if "\tFAIL\t" not in f"\t{line}" and not re.search(r"\]\s*FAIL\t", line):
            if "FAIL\t" not in line:
                continue
        parts = line.split("\t")
        # [ts] FAIL \t filename \t url \t msg
        try:
            idx = next(i for i, p in enumerate(parts) if p.strip() == "FAIL" or p.strip().endswith("FAIL"))
            filename = parts[idx + 1].strip()
            url = parts[idx + 2].strip()
            if filename and url:
                latest[filename] = url
        except (StopIteration, IndexError):
            continue
    return list(latest.items())


def download_direct(url: str, dest: Path, work_dir: Path) -> tuple[bool, str]:
    tmp = work_dir / "direct.mp4"
    work_dir.mkdir(parents=True, exist_ok=True)
    if url.startswith("http://sns-video"):
        url = "https://" + url[len("http://") :]
    try:
        with requests.get(url, headers={"User-Agent": UA, "Referer": REFERER}, timeout=180, stream=True) as r:
            if r.status_code != 200:
                return False, f"direct http {r.status_code}"
            tmp.unlink(missing_ok=True)
            size = 0
            with tmp.open("wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 16):
                    if chunk:
                        f.write(chunk)
                        size += len(chunk)
            if size <= 100_000:
                tmp.unlink(missing_ok=True)
                return False, f"direct too small {size}"
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                dest.unlink()
            shutil.move(str(tmp), str(dest))
            return True, f"direct size={size}"
    except Exception as e:
        tmp.unlink(missing_ok=True)
        return False, f"direct exc {e}"


def download_videodl(videodl, videodl_cwd, client, work, url, dest) -> tuple[bool, str]:
    job_dir = work / f"dl_{dest.stem}"
    if job_dir.exists():
        shutil.rmtree(job_dir, ignore_errors=True)
    job_dir.mkdir(parents=True, exist_ok=True)
    cobj = {client: {"work_dir": str(job_dir).replace("\\", "/")}}
    cmd = [str(videodl), "-i", url, "-g", "-a", client, "-c", json.dumps(cobj, ensure_ascii=False)]
    try:
        p = subprocess.run(
            cmd, cwd=str(videodl_cwd), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=1800,
        )
        out = ((p.stdout or "") + "\n" + (p.stderr or "")).strip()
        found = find_downloaded_file(job_dir)
        if found and found.stat().st_size > 100_000:
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                dest.unlink()
            shutil.move(str(found), str(dest))
            shutil.rmtree(job_dir, ignore_errors=True)
            return True, f"videodl/{client} size={dest.stat().st_size}"
        shutil.rmtree(job_dir, ignore_errors=True)
        return False, f"videodl rc={p.returncode} detail={out[-600:]}"
    except Exception as e:
        shutil.rmtree(job_dir, ignore_errors=True)
        return False, f"videodl exc {e}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--creator-root", required=True)
    ap.add_argument("--max-passes", type=int, default=4)
    ap.add_argument("--sleep-min", type=float, default=0)
    ap.add_argument("--sleep-max", type=float, default=0)
    args = ap.parse_args()

    root = Path(args.creator_root)
    cfg = load_config(root)
    ffmpeg_bin = cfg.get("ffmpeg_bin") or ""
    if ffmpeg_bin and ffmpeg_bin not in os.environ.get("PATH", ""):
        os.environ["PATH"] = ffmpeg_bin + os.pathsep + os.environ.get("PATH", "")

    videos = root / "videos"
    logs = root / "logs"
    work = root / "work"
    meta = root / "meta"
    videodl = Path(cfg.get("videodl_exe", ""))
    videodl_cwd = Path(cfg.get("videodl_cwd", ""))
    client = cfg.get("download_client") or "RednoteVideoClient"
    sleep_min = args.sleep_min or float(cfg.get("sleep_min_sec", 5))
    sleep_max = args.sleep_max or float(cfg.get("sleep_max_sec", 12))

    lock = logs / "pipeline.lock"
    if lock.exists():
        try:
            old = int(lock.read_text(encoding="utf-8").strip().splitlines()[0])
            if pid_alive(old):
                print(f"[{now()}] lock alive pid={old}")
                return 2
        except Exception:
            pass
        lock.unlink(missing_ok=True)
    lock.write_text(f"{os.getpid()}\n{now()}\n", encoding="utf-8")

    # note_id lookup from queue
    id_by_name: dict[str, str] = {}
    queue_csv = meta / "download_queue.csv"
    if queue_csv.exists():
        with queue_csv.open("r", encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                fn = (row.get("filename") or "").strip()
                nid = (row.get("note_id") or "").strip()
                if fn and nid:
                    id_by_name[fn] = nid
    url_map = load_jsonl_urls(meta / "contents_videos.jsonl")

    try:
        for pass_i in range(1, args.max_passes + 1):
            failed = parse_failed(logs / "download_failed.log")
            pending = [(fn, url) for fn, url in failed if not ((videos / fn).exists() and (videos / fn).stat().st_size > 100_000)]
            if not pending:
                print(f"[{now()}] pass {pass_i}: nothing to retry")
                break
            print(f"[{now()}] pass {pass_i}/{args.max_passes} pending={len(pending)}")
            improved = 0
            for fn, url in pending:
                dest = videos / fn
                nid = id_by_name.get(fn, "")
                durl = url_map.get(nid, "")
                ok, msg = False, ""
                if durl:
                    ok, msg = download_direct(durl, dest, work / f"dl_{Path(fn).stem}")
                if not ok and videodl.exists():
                    ok2, msg2 = download_videodl(videodl, videodl_cwd, client, work, url, dest)
                    if ok2:
                        ok, msg = True, msg2
                    else:
                        msg = f"{msg} | {msg2}"
                if ok:
                    improved += 1
                    append_log(logs / "download_success.log", f"OK\t{fn}\t{url}\tretry {msg}")
                else:
                    append_log(logs / "download_failed.log", f"FAIL\t{fn}\t{url}\tretry {msg}")
                time.sleep(random.uniform(sleep_min, sleep_max))
            print(f"[{now()}] pass {pass_i} recovered={improved}")
            if improved == 0:
                break
    finally:
        lock.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
