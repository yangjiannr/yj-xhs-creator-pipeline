---
name: yj-xhs-creator-pipeline
description: >-
  End-to-end Xiaohongshu (小红书) creator pipeline: MediaCrawler homepage crawl →
  video-only queue → parse-note CDN download → FunASR/Whisper transcription to
  .txt+.srt under F:/downloads/<博主名>/. Use when the user asks to 抓小红书博主、
  批量下载小红书视频、xiaohongshu.com/user/profile、xsec_token、Rednote、
  口播转写/字幕、或提到 yj-xhs-creator-pipeline；accepts creator URL or existing queue.
---

# yj-xhs-creator-pipeline

把「小红书博主主页 → 视频笔记列表 → 下载 → 转写」做成可续跑流水线。

| 模式 | 用户给什么 | 做什么 |
|------|------------|--------|
| `creator` | 主页 URL（**必须含** `xsec_token`）+ 博主名 | MediaCrawler xhs → 建队列 → 下载 → 转写 |
| `queue` | 已有 `download_queue.csv` / jsonl | 跳过抓取，直接下载 → 转写 |

与抖音流水线（`yj-douyin-creator-pipeline`）目录约定、ASR 相同；抓取字段与下载通道不同，**不要**复用抖音的 `download_direct.py` / `KedouVideoClient`。

## Always do first

1. Read `references/defaults.md` and `references/folder-layout.md`. For ASR: `references/asr.md`. For pitfalls: `references/pitfalls.md`.
2. Resolve root:
   - If skill `.last_root` exists, ask: 「上次根目录是 `<path>`，还用这个吗？」
   - Else / user refuses → take absolute path; write `.last_root`.
3. Confirm creator folder name → `<root>/<博主名>/`.
4. Confirm mode: `creator` or `queue`.
5. Do **not** start bulk download until user confirms（开始抓取 / 试下 N 条 / 开始全量）.
6. Before full run, offer **quality preview**: download 1–3 → FunASR → user reviews `.txt`/`.srt`.

## Defaults (this machine)

Copy `config.example.json` → `<creator_root>/pipeline_config.json`.

| Key | Typical value |
|-----|----------------|
| `root_downloads` | `F:/downloads` |
| `mediacrawler_dir` | `D:/300GitHub/MediaCrawler` |
| `videodl_exe` / `videodl_cwd` | videodl venv（仅兜底；主通道自解析 CDN） |
| `ffmpeg_bin` | 本机可用的 FFmpeg `bin`（抽音轨必需；可用 WinGet 安装路径） |
| `asr_python` | FunASR venv 的 `python.exe`；可复用其他博主目录下的 `scripts/funasr_venv` |
| `download_client` | `RednoteVideoClient`（仅兜底） |
| `sleep_min_sec` / `sleep_max_sec` | `5` / `12` |
| `asr_engine` | `auto`（FunASR → Whisper `medium`） |
| `xhs_creator_url` | 带 `xsec_token` 的主页 URL |

## Environment (first machine)

1. Python **3.9–3.12**（MediaCrawler 不支持 3.13+）
2. MediaCrawler + Playwright Chromium；小红书需扫码登录
3. `requests`（下载脚本）；可选 videodl / `json_repair`（解析兜底）
4. FFmpeg on PATH via `ffmpeg_bin`
5. FunASR（首选）或 faster-whisper（兜底）；Whisper 用 `hf_endpoint` + `hf_hub_disable_xet`

## Folder contract

```text
<root>/<博主名>/
  meta/          # contents.jsonl, contents_videos.jsonl, download_queue.csv, cdn_url_cache.jsonl
  videos/        # yyyy-MM-dd_<title>_<note_id>.mp4
  transcripts/   # same stem .txt + .srt
  logs/          # pipeline.lock, asr.lock, *_success/failed.log
  scripts/       # copied from this skill
  work/
  pipeline_config.json
```

Queue columns: `note_id,publish_date,filename,note_url`（仅 `type=video`）。

## Workflow A — creator homepage

1. Deploy scripts into `<creator_root>/scripts/`（缺啥拷啥；ASR 逻辑变更时务必覆盖 `transcribe_one.py` / `asr_ordered.py`）.
2. Crawl（CLI，不永久改 MediaCrawler 的 `PLATFORM=dy`）:
   ```text
   python scripts/crawl_xhs_creator.py --creator-root <path>
   ```
   等价：`main.py --platform xhs --type creator --creator_id <url> --save_data_option jsonl --get_comment false`，再 `init_queue_xhs.py`。
3. Smoke download:
   ```text
   python scripts/download_xhs.py --creator-root <path> --limit 3 --no-fallback
   ```
4. ASR preview on those files（`transcribe_one.py` 或短跑 `asr_ordered.py --min-downloaded 0`）.
5. Full parallel run:
   ```text
   # terminal 1
   python scripts/download_xhs.py --creator-root <path> --no-fallback
   # terminal 2
   python scripts/asr_ordered.py --creator-root <path>
   ```
6. Failures: `retry_failed_xhs.py`；ASR 中断后可 `--min-downloaded 0 --pause-remaining 0` 收尾。

## Workflow B — existing queue

1. `init_queue_xhs.py --creator-root <path> [--jsonl ...] [--from-mediacrawler ...]`
2. Same download + ASR as above.

## Download rules (critical)

MediaCrawler 的 `video_url` **经常为空**。`download_xhs.py` 主通道：

1. 打开 `note_url`，解析 `__INITIAL_STATE__` 里的 `masterUrl`
2. **`html.unescape`**（`&amp;` → `&`）——不解义会 **CDN 403**
3. HTTP 下载：`Referer: https://www.xiaohongshu.com/` + 浏览器 UA
4. 缓存到 `meta/cdn_url_cache.jsonl`
5. 可选兜底：videodl `RednoteVideoClient`（同样受 `&amp;` 影响，常失败；默认可 `--no-fallback`）

其他：

- Serial only；间隔随机 sleep
- Skip if mp4 exists >100KB
- `logs/pipeline.lock`（PID；死锁可清）
- 图文笔记不进队列

## Transcription rules

复用抖音同一套：

- `asr_ordered.py`：下载 >5 才开始；按 mp4 **mtime** 顺序；末尾背压；自持 `asr.lock`
- FunASR + `sentence_timestamp=True` → 真实 SRT；Whisper 兜底
- Skip if txt+srt 非空
- **Windows 控制台**：文件名可能含 emoji（如 ❗️）；`asr_ordered` 打印须 ascii-safe，否则 GBK `UnicodeEncodeError` 会中断整条流水线

## Progress reporting

Report: downloaded/queue、transcript pairs、recent FAIL、lock PID alive、当前 ASR `size_mb=`。  
不要宣称完成，直到下载进程退出且未转写队列清空（或用户叫停）。

## Safety

个人学习用途；尊重平台 ToS 与频率限制；主页 URL 的 `xsec_token` 会过期，失效后重新从浏览器复制再抓。
