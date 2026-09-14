# -*- coding: utf-8 -*-
"""Ordered ASR worker with backpressure.

Rules (2026-09 user config for creator pipelines):
1. Start transcribing only after >5 successful downloads exist.
2. Order by download-completion time (mp4 mtime ascending) — NOT file size.
3. Pause while only the last 2 downloaded files remain untranscribed (wait for
   more downloads); when the downloader is no longer running, finish the
   remainder and exit.

Runs lock-free against the downloader: uses its own logs/asr.lock (one ASR at a
time), never touches pipeline.lock, only reads videos/ and writes
transcripts/ + logs. Compatible with download_direct.py / run_pipeline.py.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path


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


def downloader_running(root: Path) -> bool:
    lock = root / "logs" / "pipeline.lock"
    if not lock.exists():
        return False
    try:
        return pid_alive(int(lock.read_text(encoding="utf-8").strip().splitlines()[0]))
    except Exception:
        return False


def acquire_asr_lock(root: Path) -> Path:
    lock = root / "logs" / "asr.lock"
    if lock.exists():
        try:
            old_pid = int(lock.read_text(encoding="utf-8").strip().splitlines()[0])
            if pid_alive(old_pid):
                raise SystemExit(f"ASR already running, pid={old_pid}")
            print(f"[{now()}] stale asr.lock (dead pid {old_pid}), removing")
        except SystemExit:
            raise
        except Exception:
            pass
        lock.unlink(missing_ok=True)
    lock.write_text(f"{os.getpid()}\n{now()}\n", encoding="utf-8")
    return lock


def transcript_done(v: Path, transcripts: Path, failed_set: set[str]) -> bool:
    txt = transcripts / f"{v.stem}.txt"
    srt = transcripts / f"{v.stem}.srt"
    if txt.exists() and srt.exists() and txt.stat().st_size > 0 and srt.stat().st_size > 0:
        return True
    return v.name in failed_set


def load_failed_set(root: Path) -> set[str]:
    out = set()
    f = root / "logs" / "transcript_failed.log"
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.startswith("[") and "\t" in line:
                parts = line.split("\t")
                if len(parts) >= 3:
                    out.add(parts[1].strip())
    return out


def transcribe_one(asr_python: str, transcribe_py: Path, video: Path, work: Path, cfg: dict) -> tuple[bool, str]:
    txt = video.parent.parent / "transcripts" / f"{video.stem}.txt"
    srt = video.parent.parent / "transcripts" / f"{video.stem}.srt"
    cmd = [
        asr_python,
        str(transcribe_py),
        "--video", str(video),
        "--txt", str(txt),
        "--srt", str(srt),
        "--engine", cfg.get("asr_engine") or "auto",
        "--whisper-model", cfg.get("whisper_model") or "medium",
        "--work-dir", str(work / "asr"),
    ]
    env = os.environ.copy()
    if cfg.get("hf_endpoint"):
        env["HF_ENDPOINT"] = cfg["hf_endpoint"]
    if cfg.get("hf_hub_disable_xet"):
        env["HF_HUB_DISABLE_XET"] = "1"
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=7200, env=env)
        out = ((p.stdout or "") + "\n" + (p.stderr or "")).strip()
        for line in reversed(out.splitlines()):
            line = line.strip()
            if line.startswith("{") and line.endswith("}"):
                try:
                    data = json.loads(line)
                    if data.get("ok"):
                        return True, f"engine={data.get('engine')} segments={data.get('segments')}"
                    if data.get("no_speech"):
                        return False, f"no_speech ({data.get('error') or 'background music or silent'})"
                    return False, data.get("error") or out[-800:]
                except Exception:
                    pass
        if p.returncode == 0:
            return True, "ok"
        return False, f"rc={p.returncode} detail={out[-800:]}"
    except Exception as e:
        return False, str(e)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--creator-root", required=True)
    ap.add_argument("--poll", type=int, default=15, help="wait seconds while paused/gated")
    ap.add_argument("--min-downloaded", type=int, default=5, help="start only after this many downloads exist")
    ap.add_argument("--pause-remaining", type=int, default=2, help="pause while only this many untranscribed remain")
    args = ap.parse_args()

    root = Path(args.creator_root)
    cfg = load_config(root)
    ffmpeg_bin = cfg.get("ffmpeg_bin") or ""
    if ffmpeg_bin and ffmpeg_bin not in os.environ.get("PATH", ""):
        os.environ["PATH"] = ffmpeg_bin + os.pathsep + os.environ.get("PATH", "")

    videos = root / "videos"
    transcripts = root / "transcripts"
    logs = root / "logs"
    work = root / "work"
    scripts = root / "scripts"
    for d in (videos, transcripts, logs, work, scripts):
        d.mkdir(parents=True, exist_ok=True)

    asr_python = cfg.get("asr_python") or ""
    if not asr_python:
        local_venv = scripts / "funasr_venv" / "Scripts" / "python.exe"
        asr_python = str(local_venv if local_venv.exists() else sys.executable)

    transcribe_py = scripts / "transcribe_one.py"
    if not transcribe_py.exists():
        bundled = Path(__file__).with_name("transcribe_one.py")
        if bundled.exists():
            shutil.copy2(bundled, transcribe_py)

    lock = acquire_asr_lock(root)
    failed_set = load_failed_set(root)
    print(f"[{now()}] asr_ordered start: min_downloaded={args.min_downloaded} pause_remaining={args.pause_remaining} poll={args.poll}s engine={cfg.get('asr_engine') or 'auto'}/{cfg.get('whisper_model') or 'medium'}")
    append_log(logs / "pipeline.log", f"ASR_ORDERED_START min_downloaded={args.min_downloaded} pause_remaining={args.pause_remaining}")

    total_done = 0
    last_activity = time.time()
    while True:
        downloaded = [v for v in videos.glob("*.mp4") if v.stat().st_size > 100_000]
        n_dl = len(downloaded)
        if n_dl <= args.min_downloaded:
            if time.time() - last_activity > 120:
                print(f"[{now()}] WAIT start-gate: downloaded={n_dl} <= {args.min_downloaded}")
                last_activity = time.time()
            time.sleep(args.poll)
            continue

        pending = [v for v in downloaded if not transcript_done(v, transcripts, failed_set)]
        pending.sort(key=lambda p: p.stat().st_mtime)  # download-completion order

        if len(pending) <= args.pause_remaining:
            if downloader_running(root):
                if time.time() - last_activity > 120:
                    print(f"[{now()}] PAUSE remaining_untranscribed={len(pending)} (waiting for more downloads)")
                    append_log(logs / "pipeline.log", f"ASR_ORDERED_PAUSE pending={len(pending)} downloader_running=True")
                    last_activity = time.time()
                time.sleep(args.poll)
                continue
            if len(pending) == 0:
                print(f"[{now()}] all transcribed, exiting")
                append_log(logs / "pipeline.log", f"ASR_ORDERED_END transcribed={total_done}")
                break
            # downloader finished/died → finish the last few, then exit
            v = pending[0]
        else:
            v = pending[0]  # oldest download first

        size_mb = round(v.stat().st_size / 1e6, 1)
        # Windows consoles may be GBK; emoji/special chars in filenames can crash print()
        safe_name = v.name.encode("ascii", "backslashreplace").decode("ascii")
        print(f"[{now()}] ASR {safe_name} size_mb={size_mb} pending={len(pending)}")
        append_log(logs / "pipeline.log", f"ASR_START\t{v.name}\tsize_mb={size_mb}")
        ok, msg = transcribe_one(asr_python, transcribe_py, v, work, cfg)
        if ok:
            total_done += 1
            append_log(logs / "transcript_success.log", f"OK\t{v.name}\t{msg}\tsize_mb={size_mb}")
        else:
            failed_set.add(v.name)
            append_log(logs / "transcript_failed.log", f"FAIL\t{v.name}\t{msg}\tsize_mb={size_mb}")
        last_activity = time.time()

    lock.unlink(missing_ok=True)
    print(f"[{now()}] asr_ordered finished, transcribed={total_done}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
