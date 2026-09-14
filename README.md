# yj-xhs-creator-pipeline

小红书博主「主页抓取 → 仅视频队列 → CDN 下载 → FunASR/Whisper 转写」可续跑流水线。

```text
主页 URL (+ xsec_token)
        │
        ▼
MediaCrawler --platform xhs --type creator
        │
        ▼
init_queue_xhs.py  (type=video → download_queue.csv)
        │
        ▼
download_xhs.py  (解析笔记页 masterUrl + html.unescape → CDN)
        │
        ▼
asr_ordered.py / transcribe_one.py  → transcripts/*.txt + *.srt
```

Sibling skill for Douyin: `yj-douyin-creator-pipeline`（目录与 ASR 约定相同，下载通道不同）。

## Quick start

```text
# 1) 部署 scripts + pipeline_config.json 到 F:/downloads/<博主名>/
# 2) 抓列表（需扫码）
python scripts/crawl_xhs_creator.py --creator-root <path>
# 3) 试下 3 条
python scripts/download_xhs.py --creator-root <path> --limit 3 --no-fallback
# 4) 转写预览后全量并行
python scripts/download_xhs.py --creator-root <path> --no-fallback
python scripts/asr_ordered.py --creator-root <path>
```

详见 `SKILL.md` 与 `references/pitfalls.md`。
