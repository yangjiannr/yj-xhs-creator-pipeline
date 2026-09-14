# -*- coding: utf-8 -*-
"""Xiaohongshu video downloader.

Primary: open note_url page, extract CDN masterUrl from __INITIAL_STATE__,
  html-unescape (&amp; → &), then HTTP download with Referer.
Secondary: MediaCrawler contents_videos.jsonl video_url (if present).
Fallback: videodl RednoteVideoClient (often 403 due to &amp; in URL — keep as last resort).

Shares logs/pipeline.lock; existing mp4s skipped.
"""
from __future__ import annotations

import csv
import html
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
STATE_RE = re.compile(
    r"window\.__INITIAL_STATE__\s*=\s*(.*?)</script>",
    re.DOTALL | re.IGNORECASE,
)


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


def acquire_lock(logs: Path) -> Path:
    lock = logs / "pipeline.lock"
    if lock.exists():
        try:
            old_pid = int(lock.read_text(encoding="utf-8").strip().splitlines()[0])
            if pid_alive(old_pid):
                raise SystemExit(f"[{now()}] lock exists (alive pid={old_pid})")
            print(f"[{now()}] stale lock (dead pid {old_pid}), removing")
        except SystemExit:
            raise
        except Exception:
            pass
        lock.unlink(missing_ok=True)
    lock.write_text(f"{os.getpid()}\n{now()}\n", encoding="utf-8")
    return lock


def normalize_cdn_url(url: str) -> str:
    if not url:
        return ""
    url = html.unescape(url.strip())
    url = url.replace("\\u002F", "/").replace("\\/", "/")
    if url.startswith("http://sns-video"):
        url = "https://" + url[len("http://") :]
    return url


def load_jsonl_urls(path: Path) -> dict[str, str]:
    """note_id -> first CDN video_url."""
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
            url = normalize_cdn_url(raw.split(",")[0] if raw else "")
            if nid and url and nid not in out:
                out[nid] = url
    return out


def _walk_find_urls(obj, keys: set[str], out: list[str]) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in keys and isinstance(v, str) and v.startswith("http"):
                out.append(v)
            elif k in keys and isinstance(v, list):
                for item in v:
                    if isinstance(item, str) and item.startswith("http"):
                        out.append(item)
            else:
                _walk_find_urls(v, keys, out)
    elif isinstance(obj, list):
        for item in obj:
            _walk_find_urls(item, keys, out)


def parse_note_cdn_url(note_url: str, timeout: int = 60) -> tuple[str, str]:
    """Fetch note page and extract a playable CDN mp4 URL. Returns (url, err)."""
    try:
        r = requests.get(
            note_url,
            headers={"User-Agent": UA, "Referer": REFERER},
            timeout=timeout,
            allow_redirects=True,
        )
        if r.status_code != 200:
            return "", f"page http {r.status_code}"
        m = STATE_RE.search(r.text)
        if not m:
            return "", "no __INITIAL_STATE__"
        raw = m.group(1).strip().rstrip(";")
        # XHS sometimes uses undefined in JSON
        raw = raw.replace(":undefined", ":null")
        try:
            state = json.loads(raw)
        except Exception:
            try:
                import json_repair  # type: ignore

                state = json_repair.loads(raw)
            except Exception as e:
                return "", f"json parse {e}"

        found: list[str] = []
        _walk_find_urls(state, {"masterUrl", "master_url", "backupUrls", "backup_urls"}, found)
        # Prefer mp4 stream urls
        ranked = []
        for u in found:
            nu = normalize_cdn_url(u if isinstance(u, str) else str(u))
            if not nu:
                continue
            score = 0
            if ".mp4" in nu:
                score += 2
            if "sns-video" in nu:
                score += 1
            ranked.append((score, nu))
        ranked.sort(key=lambda x: x[0], reverse=True)
        if ranked:
            return ranked[0][1], ""
        return "", "no masterUrl in state"
    except Exception as e:
        return "", f"parse exc {e}"


def find_downloaded_file(work_dir: Path):
    if not work_dir.exists():
        return None
    cands = []
    for p in work_dir.rglob("*"):
        if p.is_file() and p.suffix.lower() in {".mp4", ".webm", ".mkv", ".mov"} and p.stat().st_size > 100_000:
            cands.append(p)
    if not cands:
        return None

    def score(p: Path) -> tuple:
        stem = p.stem
        hashlike = len(stem) == 32 and all(c in "0123456789abcdef" for c in stem.lower())
        return (0 if hashlike else 1, p.stat().st_size, p.stat().st_mtime)

    cands.sort(key=score, reverse=True)
    return cands[0]


def download_direct(url: str, dest: Path, work_dir: Path, timeout: int = 300) -> tuple[bool, str]:
    url = normalize_cdn_url(url)
    if not url:
        return False, "empty url"
    tmp = work_dir / "direct.mp4"
    work_dir.mkdir(parents=True, exist_ok=True)
    try:
        with requests.get(
            url,
            headers={"User-Agent": UA, "Referer": REFERER},
            timeout=timeout,
            stream=True,
            allow_redirects=True,
        ) as r:
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
            cmd,
            cwd=str(videodl_cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=1800,
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


def cache_cdn_url(meta: Path, note_id: str, cdn: str) -> None:
    """Append/update a resolved CDN url into meta/cdn_url_cache.jsonl for resume."""
    path = meta / "cdn_url_cache.jsonl"
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"note_id": note_id, "video_url": cdn}, ensure_ascii=False) + "\n")


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--creator-root", required=True)
    ap.add_argument("--sleep-min", type=float, default=0)
    ap.add_argument("--sleep-max", type=float, default=0)
    ap.add_argument("--no-fallback", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="only process first N pending rows")
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
    for d in (videos, logs, work, meta):
        d.mkdir(parents=True, exist_ok=True)

    videodl = Path(cfg.get("videodl_exe", ""))
    videodl_cwd = Path(cfg.get("videodl_cwd", ""))
    client = cfg.get("download_client") or "RednoteVideoClient"
    sleep_min = args.sleep_min or float(cfg.get("sleep_min_sec", 5))
    sleep_max = args.sleep_max or float(cfg.get("sleep_max_sec", 12))

    lock = acquire_lock(logs)

    queue_csv = meta / "download_queue.csv"
    if not queue_csv.exists():
        print(f"[{now()}] queue not found: {queue_csv}")
        lock.unlink(missing_ok=True)
        return 2

    rows = []
    with queue_csv.open("r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            note_url = (row.get("note_url") or row.get("aweme_url") or "").strip()
            filename = (row.get("filename") or "").strip()
            if note_url and filename:
                rows.append(row)

    url_map = load_jsonl_urls(meta / "contents_videos.jsonl")
    if not url_map:
        url_map = load_jsonl_urls(meta / "contents.jsonl")
    url_map.update(load_jsonl_urls(meta / "cdn_url_cache.jsonl"))

    print(
        f"[{now()}] queue={len(rows)} cached_cdn={len(url_map)} "
        f"client={client} fallback={'off' if args.no_fallback else 'on'} sleep={sleep_min}-{sleep_max}s"
    )
    append_log(
        logs / "pipeline.log",
        f"XHS_DL_START queue={len(rows)} cached_cdn={len(url_map)} client={client}",
    )

    done = skipped = failed = 0
    parsed_ok = 0
    fallback_used = 0
    attempted = 0
    try:
        for i, row in enumerate(rows, 1):
            filename = row["filename"]
            note_id = (row.get("note_id") or row.get("aweme_id") or "").strip()
            note_url = (row.get("note_url") or row.get("aweme_url") or "").strip()
            dest = videos / filename
            if dest.exists() and dest.stat().st_size > 100_000:
                skipped += 1
                append_log(logs / "download_success.log", f"SKIP\t{filename}\t{note_url}\talready exists")
                continue
            attempted += 1
            if args.limit and attempted > args.limit:
                break

            msgs: list[str] = []
            ok = False
            msg = ""

            # 1) cached / jsonl CDN
            durl = url_map.get(note_id, "")
            if durl:
                ok, msg = download_direct(durl, dest, work / f"dl_{Path(filename).stem}")
                msgs.append(msg)
                if ok:
                    msg = f"cached {msg}"

            # 2) parse note page for CDN (fixes videodl &amp; 403)
            if not ok:
                cdn, perr = parse_note_cdn_url(note_url)
                if cdn:
                    parsed_ok += 1
                    cache_cdn_url(meta, note_id, cdn)
                    url_map[note_id] = cdn
                    ok, msg = download_direct(cdn, dest, work / f"dl_{Path(filename).stem}")
                    msgs.append(f"parse+{msg}")
                    if ok:
                        msg = f"parse {msg}"
                else:
                    msgs.append(f"parse fail {perr}")

            # 3) videodl fallback
            if not ok and not args.no_fallback:
                if videodl.exists():
                    fallback_used += 1
                    ok2, msg2 = download_videodl(videodl, videodl_cwd, client, work, note_url, dest)
                    msgs.append(msg2)
                    if ok2:
                        ok, msg = True, msg2
                    else:
                        msg = " | ".join(msgs)
                else:
                    msg = " | ".join(msgs + ["videodl missing"])

            if not ok and not msg:
                msg = " | ".join(msgs) or "unknown"

            if ok:
                done += 1
                append_log(logs / "download_success.log", f"OK\t{filename}\t{note_url}\t{msg}")
            else:
                failed += 1
                append_log(logs / "download_failed.log", f"FAIL\t{filename}\t{note_url}\t{msg}")

            if i < len(rows) and (args.limit == 0 or attempted < args.limit):
                time.sleep(random.uniform(sleep_min, sleep_max))
            if attempted % 10 == 0 or i == len(rows) or (args.limit and attempted >= args.limit):
                print(
                    f"[{now()}] {i}/{len(rows)} ok={done} skip={skipped} fail={failed} "
                    f"parsed={parsed_ok} fallback={fallback_used}"
                )
    finally:
        append_log(
            logs / "pipeline.log",
            f"XHS_DL_END download_ok={done} skip={skipped} fail={failed} parsed={parsed_ok} fallback={fallback_used}",
        )
        lock.unlink(missing_ok=True)

    print(
        f"[{now()}] finished ok={done} skip={skipped} fail={failed} "
        f"parsed={parsed_ok} fallback={fallback_used}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
