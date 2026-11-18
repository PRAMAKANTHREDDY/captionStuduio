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

## Deploy an access-controlled preview on Render

This is a **paid, single-worker preview**, not a free or production multi-user service. Render's free web service has 512 MB RAM, sleeps when idle, and loses local files on restart; that is unsuitable for the Whisper model and uploaded videos. Check Render's current pricing before enabling a paid service.

1. Sign in to Render and connect the GitHub account that owns `PRAMAKANTHREDDY/captionStuduio`.
2. Select **New → Web Service**, choose that repository and the `main` branch, and let Render use the repository's `Dockerfile`.
3. Choose a **paid** compute plan with several GB of RAM. Start with at least 4 GB and benchmark a short video; CPU transcription may be slow. Set one instance and keep the app's single-job processing model.
4. Add a persistent disk mounted at `/var/data`. Model files and uploads are stored there. A disk has an additional charge and needs enough space for the downloaded model plus temporary input/output videos.
5. Add these environment variables in the Render service settings before the first successful deploy:

	| Key | Value |
	|-----|-------|
	| `CAPTION_STUDIO_ALLOWED_HOSTS` | The exact Render hostname, e.g. `your-service.onrender.com`; add a custom domain as a comma-separated value if used |
	| `CAPTION_STUDIO_USERNAME` | A username for the shared preview login |
	| `CAPTION_STUDIO_PASSWORD` | A long, unique password stored as a secret |
	| `CAPTION_STUDIO_MAX_UPLOAD_BYTES` | `104857600` (100 MB default) |
	| `CAPTION_STUDIO_RETENTION_HOURS` | `24` (hosted session folders are automatically deleted after this period) |

	The Docker image sets `CAPTION_STUDIO_HOST=0.0.0.0`, `CAPTION_STUDIO_UPLOAD_DIR=/var/data/uploads`, and `HF_HOME=/var/data/huggingface`. Render supplies `PORT`. Never commit the username or password.

	In Render's health-check settings, set the path to `/healthz`.

6. Deploy. The first transcription downloads the Whisper model and may take several minutes. Open the Render HTTPS URL, enter the shared login, and test with a short, non-sensitive video.
7. Check logs and disk use. Confirm transcription, editing, export, and 24-hour cleanup before sharing the URL.

This preview uses one shared Basic Auth login, keeps job state in memory, and processes one heavy job at a time. Do not use it for confidential videos or invite the general public yet. A public launch needs individual user accounts, quotas, a durable job queue, and managed object storage. Those pieces can be added later without changing the transcription model.

## Privacy and network access

Without hosted configuration, the server binds to `127.0.0.1` and can only be opened on the same computer. Binding to a public interface requires an allowlisted hostname and both username and password; the app refuses to start if these are missing. Hosted mode should only be used behind the platform's HTTPS proxy. This repository does not include user recordings, model caches, or local environment files.

For current platform constraints, see [Render free services](https://render.com/docs/free), [Render web services](https://render.com/docs/web-services), and [persistent disks](https://render.com/docs/disks).
