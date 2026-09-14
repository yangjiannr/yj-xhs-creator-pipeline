# -*- coding: utf-8 -*-
"""Build meta/download_queue.csv from MediaCrawler Xiaohongshu jsonl.

Only keeps notes with type=video. Fields:
  note_id, publish_date, filename, note_url
Also writes contents.jsonl (all) + contents_videos.jsonl (video-only with video_url).
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
from datetime import datetime
from pathlib import Path


def local_date_from_ts(ts) -> str:
    try:
        ts_i = int(ts)
        if ts_i > 10_000_000_000:
            ts_i //= 1000
        return datetime.fromtimestamp(ts_i).strftime("%Y-%m-%d")
    except Exception:
        return datetime.now().strftime("%Y-%m-%d")


def clean_title(title: str) -> str:
    def _sanitize(t: str) -> str:
        t = re.sub(r'[\\/:*?"<>|\r\n\t]', " ", t)
        t = re.sub(r"\s+", " ", t).strip(" .")
        return t[:30].strip(" .-_")

    if not title:
        return ""
    body = re.sub(r"#[\w\u4e00-\u9fa5]+", "", title)
    body = _sanitize(body)
    if body:
        return body
    tags = re.findall(r"#[\w\u4e00-\u9fa5]+", title)
    if tags:
        return _sanitize(" ".join(tag.lstrip("#") for tag in tags))
    return ""


def pick_title(o: dict) -> str:
    title = str(o.get("title") or "").strip()
    if title:
        return title
    desc = str(o.get("desc") or "").strip()
    if desc:
        return desc.splitlines()[0][:80]
    tags = str(o.get("tag_list") or "").strip()
    if tags:
        return " ".join(f"#{t}" for t in tags.split(",") if t.strip())
    return ""


def first_video_url(raw: str) -> str:
    if not raw:
        return ""
    return raw.split(",")[0].strip()


def ensure_note_url(o: dict, note_id: str) -> str:
    url = (o.get("note_url") or "").strip()
    if url:
        return url
    token = (o.get("xsec_token") or "").strip()
    if token:
        return (
            f"https://www.xiaohongshu.com/explore/{note_id}"
            f"?xsec_token={token}&xsec_source=pc_search"
        )
    return f"https://www.xiaohongshu.com/explore/{note_id}"


def load_jsonl(path: Path) -> list[dict]:
    items = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            items.append(json.loads(line))
    return items


def build_rows(items: list[dict], videos_only: bool = True) -> tuple[list[dict], list[dict]]:
    """Return (queue_rows, video_items_deduped)."""
    rows = []
    videos = []
    seen = set()
    for o in items:
        note_id = str(o.get("note_id") or "").strip()
        if not note_id or note_id in seen:
            continue
        ntype = str(o.get("type") or "").strip().lower()
        if videos_only and ntype and ntype != "video":
            continue
        # Some crawls omit type but still have video_url
        vurl = first_video_url(str(o.get("video_url") or ""))
        if videos_only and ntype != "video" and not vurl:
            continue
        seen.add(note_id)
        publish_date = local_date_from_ts(o.get("time") or o.get("last_update_time") or o.get("create_time"))
        title = clean_title(pick_title(o))
        if title:
            filename = f"{publish_date}_{title}_{note_id}.mp4"
        else:
            filename = f"{publish_date}_{note_id}.mp4"
        note_url = ensure_note_url(o, note_id)
        rows.append(
            {
                "note_id": note_id,
                "publish_date": publish_date,
                "filename": filename,
                "note_url": note_url,
            }
        )
        vo = dict(o)
        vo["note_id"] = note_id
        vo["note_url"] = note_url
        vo["video_url"] = vurl or str(o.get("video_url") or "")
        vo["type"] = ntype or ("video" if vurl else ntype)
        videos.append(vo)
    return rows, videos


def write_outputs(meta: Path, all_items: list[dict], rows: list[dict], videos: list[dict]) -> None:
    meta.mkdir(parents=True, exist_ok=True)

    contents = meta / "contents.jsonl"
    with contents.open("w", encoding="utf-8") as f:
        for o in all_items:
            f.write(json.dumps(o, ensure_ascii=False) + "\n")

    contents_videos = meta / "contents_videos.jsonl"
    with contents_videos.open("w", encoding="utf-8") as f:
        for o in videos:
            f.write(json.dumps(o, ensure_ascii=False) + "\n")

    queue_csv = meta / "download_queue.csv"
    with queue_csv.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["note_id", "publish_date", "filename", "note_url"])
        w.writeheader()
        w.writerows(rows)

    urls_txt = meta / "video_urls.txt"
    urls_txt.write_text("\n".join(r["note_url"] for r in rows) + ("\n" if rows else ""), encoding="utf-8")

    urls_csv = meta / "video_urls.csv"
    with urls_csv.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["note_id", "publish_date", "note_url", "filename"])
        w.writeheader()
        for r in rows:
            w.writerow(r)

    print(f"all_notes={len(all_items)} videos={len(rows)} -> {queue_csv}")


def find_latest_mc_jsonl(mediacrawler_dir: Path) -> Path | None:
    base = mediacrawler_dir / "data" / "xhs" / "jsonl"
    if not base.exists():
        return None
    cands = sorted(base.glob("creator_contents_*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    return cands[0] if cands else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--creator-root", required=True)
    ap.add_argument("--jsonl", default="", help="MediaCrawler contents jsonl")
    ap.add_argument("--from-mediacrawler", default="", help="MediaCrawler repo dir; use latest xhs creator jsonl")
    ap.add_argument("--include-non-video", action="store_true", help="keep image notes too (not recommended)")
    args = ap.parse_args()

    root = Path(args.creator_root)
    meta = root / "meta"
    meta.mkdir(parents=True, exist_ok=True)

    src: Path | None = None
    if args.jsonl:
        src = Path(args.jsonl)
    elif args.from_mediacrawler:
        src = find_latest_mc_jsonl(Path(args.from_mediacrawler))
        if not src:
            raise SystemExit(f"no creator_contents_*.jsonl under {args.from_mediacrawler}/data/xhs/jsonl")
        print(f"using MediaCrawler jsonl: {src}")
    else:
        cand = meta / "contents.jsonl"
        if cand.exists():
            src = cand
        else:
            raise SystemExit("Provide --jsonl or --from-mediacrawler, or place contents.jsonl under meta/")

    if not src.exists():
        raise SystemExit(f"jsonl not found: {src}")

    # If source is outside meta, keep a raw copy under meta/crawl_raw.jsonl
    if src.resolve() != (meta / "contents.jsonl").resolve():
        shutil.copy2(src, meta / "crawl_raw.jsonl")

    all_items = load_jsonl(src)
    rows, videos = build_rows(all_items, videos_only=not args.include_non_video)
    write_outputs(meta, all_items, rows, videos)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
