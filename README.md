---
title: Caption Studio
emoji: 🎬
colorFrom: blue
colorTo: purple
sdk: docker
app_port: 7860
pinned: false
---

# Caption Studio

Caption Studio is a web app for transcribing video speech, editing caption text and appearance, and exporting a captioned video. It runs locally by default. An access-controlled hosted preview can be deployed with Docker.

## Requirements

- Python 3.10 or newer (Python 3.12 recommended on Windows)
- FFmpeg with `ffmpeg` and `ffprobe` available on `PATH`
- The Python packages in `requirements.txt`

## Run on Windows

Install FFmpeg (for example, `winget install --id Gyan.FFmpeg -e`), then open a new PowerShell window and run:

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
python .\caption_studio.py
```

Open http://127.0.0.1:8765 in a browser on the same computer. Stop the server with Ctrl+C.

## Run on macOS or Linux

Install Python and FFmpeg using your operating system's package manager, then run:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python caption_studio.py
```

Open http://127.0.0.1:8765 in a browser on the same computer. Stop the server with Ctrl+C.

## Speech model

The first transcription downloads the default `large-v3-turbo` model from Hugging Face. Model files are cached locally. Transcription speed depends on your computer; the app uses CPU inference by default.

## Self-test

```sh
python windows_selftest.py
```

The self-test checks FFmpeg, dependencies, caption rendering, transcription, and the local web server. Its first run downloads a small test model.

## Deploy a no-login prototype on Hugging Face Spaces

This repository's existing web app includes both the UI and its API. Deploy that single app as a Docker Space; the UI remains the current Caption Studio UI and does not need a separate GitHub Pages copy.

1. Create a **Docker Space** on Hugging Face and connect or push this repository to it. Hugging Face currently requires a paid plan to create/run Docker Spaces, even on CPU Basic hardware; this is not a $0 hosting path for a new Space.
2. In the Space settings, set `CAPTION_STUDIO_ALLOW_ANONYMOUS=1` and `CAPTION_STUDIO_ALLOWED_HOSTS` to the exact Space hostname (for example, `owner-caption-studio.hf.space`). The app binds to the Space-provided `PORT` (7860).
3. Keep the hosted limits in the defaults or set the environment variables below. The first transcription downloads `large-v3-turbo`; the existing worker uses faster-whisper, CTranslate2, CPU `int8`, beam size 5, and word timestamps.
4. Upload a short, non-sensitive video to verify transcription, editing, and export. CPU Basic storage is ephemeral, and sleeping/restarts can interrupt jobs or clear files and model cache.

For anonymous mode, use these limits as a starting point:

| Key | Value |
|-----|-------|
| `CAPTION_STUDIO_ALLOW_ANONYMOUS` | `1` |
| `CAPTION_STUDIO_ALLOWED_HOSTS` | Exact `*.hf.space` hostname |
| `CAPTION_STUDIO_MAX_UPLOAD_BYTES` | `104857600` |
| `CAPTION_STUDIO_MAX_VIDEO_SECONDS` | `600` |
| `CAPTION_STUDIO_MAX_ACTIVE_JOBS` | `1` |
| `CAPTION_STUDIO_MAX_JOBS_PER_DAY` | `5` |
| `CAPTION_STUDIO_RETENTION_HOURS` | `24` |

The app exposes `/healthz` for health checks. The model is downloaded at runtime and cached under `/var/data/huggingface`; avoid baking it into the image so every source change does not require rebuilding a multi-gigabyte image.

## Deploy a no-login prototype on Render

This public prototype does not require user accounts or a password. It processes one heavy job at a time and is intended for short, non-sensitive clips. The `large-v3-turbo` CPU model can be slow, and hosted restarts can lose queued or completed work in memory.

1. Sign in to Render and connect the GitHub account that owns `PRAMAKANTHREDDY/captionStuduio`.
2. Select **New → Web Service**, choose the repository and `main` branch, and let Render use the repository's `Dockerfile`.
3. Choose a paid compute plan with several GB of RAM. Start with at least 4 GB. CPU transcription is slower than GPU transcription.
4. Add a persistent disk mounted at `/var/data` if you need model and session data to survive restarts. Uploads and exports are still managed by the app's retention setting.
5. Add these environment variables in the Render service settings:

	| Key | Value |
	|-----|-------|
	| `CAPTION_STUDIO_ALLOWED_HOSTS` | The exact Render hostname, e.g. `your-service.onrender.com`; add custom domains if used |
	| `CAPTION_STUDIO_ALLOW_ANONYMOUS` | `1` |
	| `CAPTION_STUDIO_MAX_UPLOAD_BYTES` | `104857600` |
	| `CAPTION_STUDIO_MAX_VIDEO_SECONDS` | `600` |
	| `CAPTION_STUDIO_MAX_ACTIVE_JOBS` | `1` |
	| `CAPTION_STUDIO_MAX_JOBS_PER_DAY` | `5` |
	| `CAPTION_STUDIO_RETENTION_HOURS` | `24` |

	The Docker image sets `CAPTION_STUDIO_HOST=0.0.0.0`, `CAPTION_STUDIO_UPLOAD_DIR=/var/data/uploads`, and `HF_HOME=/var/data/huggingface`. Render supplies `PORT`. Do not set username/password variables in anonymous mode.

	In Render's health-check settings, set the path to `/healthz`.

6. Deploy. The first transcription downloads the Whisper model and may take several minutes. Test with a short, non-sensitive video.
7. Check logs and disk use. Confirm upload, transcription, editing, export, and cleanup before sharing the URL.

This prototype keeps the same editing UI and export features. It has no individual accounts and uses a shared daily limit. Do not use it for confidential videos. A production launch needs a durable job queue, managed object storage, individual quotas, and abuse protection.

## Privacy and network access

Without hosted configuration, the server binds to `127.0.0.1` and can only be opened on the same computer. For anonymous public mode, set `CAPTION_STUDIO_ALLOW_ANONYMOUS=1` and allowlist the public hostname. Hosted mode should only be used behind the platform's HTTPS proxy. This repository does not include user recordings, model caches, or local environment files.

For current platform constraints, see [Render free services](https://render.com/docs/free), [Render web services](https://render.com/docs/web-services), and [persistent disks](https://render.com/docs/disks).
