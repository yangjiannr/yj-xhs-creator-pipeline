# Xiaohongshu pitfalls (from real runs)

## Creator URL

Must include browser query params:

```text
https://www.xiaohongshu.com/user/profile/<id>?xsec_token=...&xsec_source=pc_search
```

Without `xsec_token`, crawl / page parse often fails. Tokens expire — re-copy from browser when needed.

## Empty `video_url` from MediaCrawler

`store.xhs.get_video_url_arr` frequently yields empty strings even for `type=video`.  
Do **not** treat empty CDN field as “not a video”. Queue by `type=video` + `note_url`.

## CDN 403 = unescaped `&amp;`

Parsed `masterUrl` may contain HTML entity `&amp;`.  
Requesting literally with `&amp;` → **403 Forbidden**.  
Fix: `html.unescape(url)` before GET. Verified: same URL returns 200 after unescape.

videodl `RednoteVideoClient` hits the same bug on download; prefer skill’s parse+direct path (`--no-fallback`).

## Parallel full run

| Process | Lock | Role |
|---------|------|------|
| `download_xhs.py` | `pipeline.lock` | network |
| `asr_ordered.py` | `asr.lock` | CPU |

ASR waits until downloaded count **> 5**, then mtime order; pauses when ≤2 untranscribed remain while downloader alive.

## Windows console crash (emoji titles)

Note titles may include ❗️ etc. Printing raw `Path.name` under GBK raises `UnicodeEncodeError` and **kills** `asr_ordered`.  
Always print ascii-safe names; logs can keep original UTF-8.

## Resume

- Re-run same commands; existing mp4/txt/srt skipped
- Stale locks: if PID dead, delete `pipeline.lock` / `asr.lock`
- After ASR crash:  
  `python scripts/asr_ordered.py --creator-root <path> --min-downloaded 0 --pause-remaining 0`
- Failed downloads: `retry_failed_xhs.py`（仍失败则换 token / 重抓）

## Scale reference (2026-09 大王育儿真心话)

- ~204 notes → ~202 video queue
- Download ~25–30s/video with parse+CDN → ~1.5h for 200
- ASR ~2min/video FunASR → overnight for full set
- Bottleneck is ASR, not download
