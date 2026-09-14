# ASR notes (FunASR + Whisper)

## Engine choice

| Content | Prefer |
|---------|--------|
| Chinese 口播 / 教育讲解 | **FunASR** (`paraformer-zh` + vad + punc) |
| English or heavy EN↔ZH mix | **Whisper** (`medium`; `small` only for quick smoke tests) |
| FunASR missing / torch broken | Whisper fallback via `asr_engine=auto` |

### Whisper / HF downloads (mirror + xet)

- HF 直连经常超时 → `HF_ENDPOINT=https://hf-mirror.com`
- HF 新 xet 存储（`cas-server.xethub.hf.co`）会 401 → 必须 `HF_HUB_DISABLE_XET=1`
- run_pipeline.py 已按 `hf_endpoint` / `hf_hub_disable_xet` 配置自动设置这两个环境变量；
  手动跑 transcribe_one.py 时需自行 `export HF_ENDPOINT=... HF_HUB_DISABLE_XET=1`

### No-speech detection (背景音乐/无口播视频)

- FunASR 对纯音乐音频可能报 `None argument after ** must be a mapping`（vad 处理 bug）
- whisper 带 `vad_filter=True` 时纯音乐输出空
- transcribe_one.py 兜底链：vad 空 → 无 vad 复检 → 内容仅出现在无 vad 且疑似音乐（词曲/演唱署名或 1–2 句短重复）→ 标记 `no_speech`
- 输出 `{"ok": false, "no_speech": true}`（退出码 3）；run_pipeline 记入 `transcript_failed.log` 为 `FAIL ... no_speech`，属预期

## FunASR timestamps (critical)

Without `sentence_timestamp=True`, `generate()` often returns:

- `text`: full transcript
- `timestamp`: char-level `[start_ms, end_ms]` list
- **no** `sentence_info`

Old bug: code only read `sentence_info`, fell back to one cue `0 → 1000ms` for the whole file →「没有时间轴」.

Correct call:

```python
res = model.generate(
    input=wav_path,
    batch_size_s=300,
    sentence_timestamp=True,  # → sentence_info with start/end
)
```

Fallback if still no `sentence_info`: split on `。！？；!?.;` using char-level `timestamp`.

Sanity check: a ~4 min video should produce dozens–hundreds of SRT cues, not 1.

## Short-first queue

Long files (e.g. 200MB+) can occupy the single ASR worker for a long time. Always:

1. Seed existing videos into a **PriorityQueue** by size
2. Bucket files ≥ `long_video_bytes` (default 80MB) after all shorts
3. Log `ASR_SEEDED` and each `ASR_START … size_mb=`

## Preview before bulk

1. Download a handful of videos (or use existing)
2. `--mode asr` or `transcribe_one.py --engine funasr` on 1–3 shortest
3. Open `transcripts/*.txt` and `*.srt` for the user
4. Only then start full `all` / continue `download`

## Model download

- FunASR hub: ModelScope (`ms`)
- Whisper / faster-whisper: Hugging Face (TUNA `HF_ENDPOINT` helps here)
- Cache once under `~/.cache/modelscope` (and HF cache); later runs skip download
