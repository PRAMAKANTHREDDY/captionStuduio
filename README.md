# Caption Studio

Caption Studio is a local web app for transcribing video speech, editing caption text and appearance, and exporting a captioned video. Video processing runs on the computer that starts the app; uploaded files are not sent to a hosted service.

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

## Privacy and network access

The server binds to `127.0.0.1` and is intended for use on the same computer. Do not expose it directly to the public internet. This repository does not include user recordings, model caches, or local environment files.
