# Defaults & configuration

## Config resolution

1. `<creator_root>/pipeline_config.json`
2. Skill `config.example.json`
3. Skill `.last_root`（一行绝对路径，下载根目录）

## Required / important keys

| Key | Meaning |
|-----|---------|
| `root_downloads` | 博主文件夹父目录 |
| `mediacrawler_dir` | MediaCrawler 仓库 |
| `xhs_creator_url` | 带 `xsec_token` 的主页 URL |
| `xhs_creator_name` | 文件夹显示名 |
| `videodl_exe` / `videodl_cwd` | 兜底用；主通道不依赖 |
| `download_client` | `RednoteVideoClient` |
| `asr_python` | FunASR/faster-whisper 解释器；空则探测 `scripts/funasr_venv` |
| `ffmpeg_bin` | 注入 PATH（ASR 抽 wav） |
| `hf_endpoint` / `hf_hub_disable_xet` | Whisper 镜像与禁用 xet |
| `sleep_min_sec` / `sleep_max_sec` | 下载间隔，建议 5–12 |
| `asr_engine` / `whisper_model` | `auto` / `medium` |

## Asking for root

1. Read `.last_root` if present
2. Ask reuse
3. Write chosen root back

Creator path is always `<root>/<博主名>/`.

## Tooling notes

- MediaCrawler：用 CLI `--platform xhs`，避免改坏抖音默认 `PLATFORM=dy`
- ASR 可复用其他博主目录已装好的 `funasr_venv`（在 config 里写绝对 `asr_python`）
- FFmpeg：若 `D:/300GitHub/ffmpeg/bin` 不存在，改用本机实际路径（如 WinGet `Gyan.FFmpeg`）
