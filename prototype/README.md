# Caption Studio Prototype

This is a no-login CPU prototype. It uses the existing transcription settings:

- Model: `large-v3-turbo`
- Engine: `faster-whisper` / CTranslate2
- Device: CPU
- Compute type: `int8`
- Beam size: `5`
- Word timestamps: enabled

## Limits

- Maximum upload: 100 MB
- Maximum video: 10 minutes
- Maximum one active job
- Maximum 5 jobs per day
- Files are removed after 30 minutes
- Jobs are lost if the server restarts or sleeps

## Local run

```powershell
python -m venv .venv-prototype
.venv-prototype\Scripts\python -m pip install -r prototype\requirements.txt
.venv-prototype\Scripts\python -m uvicorn prototype.app:app --reload --port 7860
```

Open `http://127.0.0.1:7860`.

## Hugging Face Space

1. Create a new Hugging Face Docker Space.
2. Upload the contents of `prototype/` to the Space repository.
3. Set the Space port to `7860`.
4. Open the Space URL.

Do not upload videos or private credentials to the Hugging Face or GitHub repositories.

## Optional environment variables

- `MAX_UPLOAD_BYTES`: upload limit in bytes
- `MAX_VIDEO_SECONDS`: maximum video duration
- `MAX_JOBS_PER_DAY`: shared daily job limit
- `RETENTION_SECONDS`: temporary file lifetime
- `CORS_ORIGINS`: comma-separated browser origins

This prototype uses a temporary in-memory queue. A restart or cold start can lose waiting jobs.
