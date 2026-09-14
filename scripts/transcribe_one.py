# -*- coding: utf-8 -*-
"""Transcribe one video to .txt + .srt.

Primary: FunASR (if installed)
Fallback: faster-whisper medium
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path


def ms_to_srt(ms: int) -> str:
    if ms < 0:
        ms = 0
    h = ms // 3600000
    m = (ms % 3600000) // 60000
    s = (ms % 60000) // 1000
    milli = ms % 1000
    return f"{h:02d}:{m:02d}:{s:02d},{milli:03d}"


def write_outputs(segments: list[dict], txt_path: Path, srt_path: Path) -> None:
    texts = []
    srt_lines = []
    for i, seg in enumerate(segments, 1):
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        texts.append(text)
        start_ms = int(seg.get("start_ms", 0))
        end_ms = int(seg.get("end_ms", start_ms + 1000))
        if end_ms <= start_ms:
            end_ms = start_ms + 500
        srt_lines.append(str(i))
        srt_lines.append(f"{ms_to_srt(start_ms)} --> {ms_to_srt(end_ms)}")
        srt_lines.append(text)
        srt_lines.append("")
    txt_path.write_text("\n".join(texts).strip() + ("\n" if texts else ""), encoding="utf-8")
    srt_path.write_text("\n".join(srt_lines), encoding="utf-8")


def extract_wav(video: Path, wav: Path) -> None:
    cmd = [
        "ffmpeg", "-y", "-i", str(video),
        "-vn", "-ac", "1", "-ar", "16000", "-f", "wav", str(wav),
    ]
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode != 0 or not wav.exists() or wav.stat().st_size < 1000:
        raise RuntimeError(f"ffmpeg extract failed: {p.stderr[-800:]}")


def _segments_from_char_timestamps(text: str, timestamps: list) -> list[dict]:
    """Build sentence segments from FunASR char-level timestamps."""
    ts_i = 0
    char_ts: list[tuple[str, int | None, int | None]] = []
    for ch in text:
        if ch.isspace():
            char_ts.append((ch, None, None))
            continue
        if ts_i < len(timestamps):
            pair = timestamps[ts_i]
            s, e = int(pair[0]), int(pair[1])
            char_ts.append((ch, s, e))
            ts_i += 1
        else:
            char_ts.append((ch, None, None))

    segments: list[dict] = []
    buf: list[str] = []
    start_ms: int | None = None
    end_ms: int | None = None
    ends = set("。！？；!?.;\n")
    for ch, s, e in char_ts:
        if s is not None:
            if start_ms is None:
                start_ms = s
            end_ms = e
        buf.append(ch)
        if ch in ends:
            t = "".join(buf).strip()
            if t and start_ms is not None:
                segments.append({
                    "text": t,
                    "start_ms": start_ms,
                    "end_ms": end_ms if end_ms is not None else start_ms + 500,
                })
            buf, start_ms, end_ms = [], None, None
    t = "".join(buf).strip()
    if t and start_ms is not None:
        segments.append({
            "text": t,
            "start_ms": start_ms,
            "end_ms": end_ms if end_ms is not None else start_ms + 500,
        })
    return segments


def try_funasr(wav: Path) -> list[dict] | None:
    try:
        from funasr import AutoModel  # type: ignore
    except Exception:
        return None

    # Prefer Chinese models; fall through on failure
    candidates = [
        "paraformer-zh",
        "iic/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-pytorch",
        "SenseVoiceSmall",
    ]
    last_err = None
    for model_name in candidates:
        try:
            model = AutoModel(
                model=model_name,
                vad_model="fsmn-vad",
                punc_model="ct-punc",
                disable_update=True,
            )
            # sentence_timestamp=True → sentence_info with start/end (ms)
            res = model.generate(
                input=str(wav),
                batch_size_s=300,
                sentence_timestamp=True,
            )
            if not res:
                continue
            item = res[0] if isinstance(res, list) else res
            segments: list[dict] = []
            sentence_info = None
            timestamps = None
            if isinstance(item, dict):
                sentence_info = item.get("sentence_info")
                timestamps = item.get("timestamp")
                text = (item.get("text") or "").strip()
            else:
                text = str(item).strip()

            if sentence_info:
                for sent in sentence_info:
                    t = (sent.get("text") or "").strip()
                    if not t:
                        continue
                    segments.append({
                        "text": t,
                        "start_ms": int(sent.get("start", 0)),
                        "end_ms": int(sent.get("end", 0)),
                    })
            elif text and timestamps:
                # Fallback: split by punctuation using char-level timestamps
                segments = _segments_from_char_timestamps(text, timestamps)
            elif text:
                segments.append({"text": text, "start_ms": 0, "end_ms": 1000})

            if segments:
                return segments
        except Exception as e:
            last_err = e
            continue
    if last_err:
        print(f"[funasr] failed: {last_err}", file=sys.stderr)
    return None


def _looks_like_background_music(texts: list[str]) -> bool:
    """Heuristic: background-music credits / repeated watermarks instead of real speech.

    Conservative on purpose — this only runs when VAD found no speech, so false
    positives would wrongly suppress a real (short) transcript:
    - explicit music-credit markers (詞曲/作曲/作词/演唱/歌手/词：/曲：), or
    - every line byte-identical (e.g. 詞曲 李宗盛 x5, or a hallucinated watermark).
    """
    cleaned = [t.strip() for t in texts if t.strip()]
    if not cleaned:
        return True
    joined = "".join(cleaned)
    markers = ("詞曲", "词曲", "作曲", "作词", "演唱", "歌手", "词：", "曲：", "词:", "曲:")
    if any(m in joined for m in markers):
        return True
    unique = set(cleaned)
    return len(unique) == 1


def try_whisper(wav: Path, model_size: str = "medium") -> tuple[list[dict] | None, str]:
    """Returns (segments, status). status: 'ok' | 'no_speech' | error message.

    - vad_filter=True first (speech only). If empty, retry vad_filter=False.
    - If content only appears without VAD and looks like background music credits
      (e.g. 词曲/演唱 markers, or 1-2 short repeated lines), treat as no-speech
      instead of emitting garbage subtitles.
    """
    try:
        from faster_whisper import WhisperModel
    except Exception as e:
        return None, f"import error: {e}"
    try:
        model = WhisperModel(model_size, device="cpu", compute_type="int8")
        for vad in (True, False):
            segments_iter, info = model.transcribe(
                str(wav),
                language="zh",
                vad_filter=vad,
                beam_size=5,
            )
            segments = []
            for seg in segments_iter:
                text = (seg.text or "").strip()
                if not text:
                    continue
                segments.append({
                    "text": text,
                    "start_ms": int(seg.start * 1000),
                    "end_ms": int(seg.end * 1000),
                })
            if segments:
                if not vad and _looks_like_background_music([s["text"] for s in segments]):
                    return None, "no_speech"
                return segments, "ok"
        return None, "no_speech"
    except Exception as e:
        return None, f"error: {e}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", required=True)
    ap.add_argument("--txt", required=True)
    ap.add_argument("--srt", required=True)
    ap.add_argument("--engine", default="auto", choices=["auto", "funasr", "whisper"])
    ap.add_argument("--whisper-model", default="medium")
    ap.add_argument("--work-dir", default="")
    args = ap.parse_args()

    video = Path(args.video)
    txt_path = Path(args.txt)
    srt_path = Path(args.srt)
    if not video.exists():
        print(json.dumps({"ok": False, "error": f"video not found: {video}"}, ensure_ascii=False))
        return 2

    txt_path.parent.mkdir(parents=True, exist_ok=True)
    srt_path.parent.mkdir(parents=True, exist_ok=True)

    work = Path(args.work_dir) if args.work_dir else Path(tempfile.mkdtemp(prefix="asr_"))
    work.mkdir(parents=True, exist_ok=True)
    wav = work / f"{video.stem}.wav"

    engine_used = None
    try:
        extract_wav(video, wav)
        segments = None
        if args.engine in ("auto", "funasr"):
            segments = try_funasr(wav)
            if segments:
                engine_used = "funasr"
        if segments is None:
            if args.engine == "funasr":
                raise RuntimeError("FunASR unavailable")
            segments, wstatus = try_whisper(wav, args.whisper_model)
            if segments:
                engine_used = f"whisper:{args.whisper_model}"
            elif wstatus == "no_speech":
                print(json.dumps({
                    "ok": False,
                    "no_speech": True,
                    "engine": f"whisper:{args.whisper_model}",
                    "error": "no speech detected (background music or silent)",
                }, ensure_ascii=False))
                return 3
            else:
                raise RuntimeError(f"whisper failed: {wstatus}")
        write_outputs(segments, txt_path, srt_path)
        print(json.dumps({
            "ok": True,
            "engine": engine_used,
            "segments": len(segments),
            "txt": str(txt_path),
            "srt": str(srt_path),
        }, ensure_ascii=False))
        return 0
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e), "engine": engine_used}, ensure_ascii=False))
        return 1
    finally:
        try:
            if wav.exists():
                wav.unlink()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
