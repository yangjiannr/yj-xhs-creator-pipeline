# Folder layout

```text
<root>/<博主名>/
├── meta/
│   ├── crawl_raw.jsonl           # optional copy of MediaCrawler export
│   ├── contents.jsonl            # all notes
│   ├── contents_videos.jsonl     # type=video only
│   ├── cdn_url_cache.jsonl       # resolved masterUrl after parse
│   ├── download_queue.csv        # note_id,publish_date,filename,note_url
│   ├── video_urls.txt
│   └── video_urls.csv
├── videos/
│   └── yyyy-MM-dd_<title>_<note_id>.mp4
├── transcripts/
│   ├── yyyy-MM-dd_<title>_<note_id>.txt
│   └── yyyy-MM-dd_<title>_<note_id>.srt
├── logs/
│   ├── pipeline.log
│   ├── pipeline.lock             # downloader PID
│   ├── asr.lock                  # asr_ordered PID
│   ├── download_success.log
│   ├── download_failed.log
│   ├── transcript_success.log
│   └── transcript_failed.log
├── scripts/
│   ├── crawl_xhs_creator.py
│   ├── init_queue_xhs.py
│   ├── download_xhs.py
│   ├── retry_failed_xhs.py
│   ├── asr_ordered.py
│   └── transcribe_one.py
├── work/
└── pipeline_config.json
```

## Filename rules

- Title: strip `#话题`；空标题用话题词；清洗 Windows 非法字符；截断 30 字
- Skip download if mp4 >100KB；skip ASR if txt+srt non-empty
- SRT must have real sentence timestamps（not one fake 0→1s cue）
