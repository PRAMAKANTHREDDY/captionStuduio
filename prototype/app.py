import json
import os
import shutil
import subprocess
import tempfile
import threading
import queue
import time
import uuid
from datetime import date

from fastapi import FastAPI, File, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", str(100 * 1024 * 1024)))
MAX_VIDEO_SECONDS = int(os.getenv("MAX_VIDEO_SECONDS", "600"))
MAX_JOBS_PER_DAY = int(os.getenv("MAX_JOBS_PER_DAY", "5"))
RETENTION_SECONDS = int(os.getenv("RETENTION_SECONDS", "1800"))

MODEL = None
JOBS = {}
JOBS_LOCK = threading.Lock()
WORK_QUEUE = queue.Queue()
WORKER_STARTED = False

app = FastAPI(title="Caption Studio Prototype")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in os.getenv("CORS_ORIGINS", "*").split(",") if origin.strip()],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


def get_model():
    global MODEL
    if MODEL is None:
        from faster_whisper import WhisperModel
        MODEL = WhisperModel("large-v3-turbo", device="cpu", compute_type="int8")
    return MODEL


def srt_time(seconds):
    milliseconds = int(round(seconds * 1000))
    hours, milliseconds = divmod(milliseconds, 3600000)
    minutes, milliseconds = divmod(milliseconds, 60000)
    secs, milliseconds = divmod(milliseconds, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{milliseconds:03d}"


def probe_duration(path):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode:
        raise ValueError("Could not read video duration")
    return float(result.stdout.strip())


def jobs_today():
    today = date.today().isoformat()
    with JOBS_LOCK:
        return sum(job.get("created") == today for job in JOBS.values())


def worker():
    while True:
        job_id = WORK_QUEUE.get()
        with JOBS_LOCK:
            job = JOBS[job_id]
            job["state"] = "running"
        work_dir = job["work_dir"]
        video = job["video"]
        audio = os.path.join(work_dir, "audio.wav")
        srt_path = os.path.join(work_dir, "captions.srt")
        try:
            subprocess.run(
                ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", video,
                 "-vn", "-ac", "1", "-ar", "16000", audio],
                check=True, stdin=subprocess.DEVNULL, timeout=600,
            )
            with JOBS_LOCK:
                job["message"] = "Transcribing with large-v3-turbo"
            segments, _ = get_model().transcribe(
                audio,
                beam_size=5,
                word_timestamps=True,
                language=None,
                condition_on_previous_text=False,
                vad_filter=True,
                vad_parameters={"min_silence_duration_ms": 300, "speech_pad_ms": 400},
            )
            segments = list(segments)
            lines = []
            serialized = []
            for index, segment in enumerate(segments, 1):
                text = segment.text.strip()
                lines.extend([str(index), f"{srt_time(segment.start)} --> {srt_time(segment.end)}", text, ""])
                serialized.append({
                    "start": segment.start,
                    "end": segment.end,
                    "text": text,
                    "words": [
                        {"word": word.word, "start": word.start, "end": word.end,
                         "probability": word.probability}
                        for word in (segment.words or [])
                    ],
                })
            with open(srt_path, "w", encoding="utf-8") as handle:
                handle.write("\n".join(lines).strip() + "\n")
            with JOBS_LOCK:
                job.update(state="done", message="Ready", segments=serialized,
                           srt_name=os.path.basename(srt_path))
        except Exception as exc:
            with JOBS_LOCK:
                job.update(state="error", message=str(exc))
        finally:
            WORK_QUEUE.task_done()


@app.on_event("startup")
async def startup():
    global WORKER_STARTED
    if not WORKER_STARTED:
        threading.Thread(target=worker, daemon=True).start()
        WORKER_STARTED = True


def cleanup_loop():
    while True:
        time.sleep(300)
        cutoff = time.time() - RETENTION_SECONDS
        with JOBS_LOCK:
            expired = [
                (job_id, job) for job_id, job in JOBS.items()
                if job.get("finished_at", job.get("created_at", time.time())) < cutoff
            ]
        for job_id, job in expired:
            shutil.rmtree(job["work_dir"], ignore_errors=True)
            with JOBS_LOCK:
                JOBS.pop(job_id, None)


@app.on_event("startup")
async def cleanup_startup():
    threading.Thread(target=cleanup_loop, daemon=True).start()


@app.post("/api/jobs")
async def create_job(file: UploadFile = File(...)):
    if jobs_today() >= MAX_JOBS_PER_DAY:
        return JSONResponse({"error": "Daily processing limit reached"}, status_code=429)

    name = os.path.basename(file.filename or "video.mp4").replace("\\", "_")
    extension = os.path.splitext(name)[1].lower()
    if extension not in {".mp4", ".mov", ".m4v"}:
        return JSONResponse({"error": "Upload an MP4, MOV, or M4V file"}, status_code=400)

    work_dir = tempfile.mkdtemp(prefix="caption-job-")
    video = os.path.join(work_dir, "input" + extension)
    size = 0
    with open(video, "wb") as handle:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES:
                handle.close()
                shutil.rmtree(work_dir, ignore_errors=True)
                return JSONResponse({"error": "Upload exceeds 100 MB"}, status_code=413)
            handle.write(chunk)

    try:
        duration = probe_duration(video)
    except (OSError, ValueError, subprocess.SubprocessError):
        shutil.rmtree(work_dir, ignore_errors=True)
        return JSONResponse({"error": "Could not read video duration"}, status_code=400)
    if duration <= 0 or duration > MAX_VIDEO_SECONDS:
        shutil.rmtree(work_dir, ignore_errors=True)
        return JSONResponse({"error": "Video must be no longer than 10 minutes"}, status_code=413)

    job_id = uuid.uuid4().hex
    now = time.time()
    with JOBS_LOCK:
        running = sum(job["state"] in {"queued", "running"} for job in JOBS.values())
        if running:
            shutil.rmtree(work_dir, ignore_errors=True)
            return JSONResponse({"error": "A job is processing; try again later"}, status_code=429)
        JOBS[job_id] = {
            "state": "queued",
            "message": "Queued",
            "created": date.today().isoformat(),
            "created_at": now,
            "finished_at": now,
            "work_dir": work_dir,
            "video": video,
        }
    WORK_QUEUE.put(job_id)
    return {"id": job_id}


@app.get("/api/jobs/{job_id}")
async def job_status(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return JSONResponse({"error": "Job not found"}, status_code=404)
        return {
            "state": job["state"],
            "message": job["message"],
            "segments": job.get("segments", []),
        }


@app.get("/api/jobs/{job_id}/download")
async def download_srt(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return JSONResponse({"error": "Job not found"}, status_code=404)
        if job["state"] != "done":
            return JSONResponse({"error": "Job is not ready"}, status_code=409)
        path = os.path.join(job["work_dir"], "captions.srt")
        job["finished_at"] = time.time()
    return FileResponse(path, media_type="application/x-subrip", filename="captions.srt")


ROOT = os.path.dirname(os.path.abspath(__file__))
app.mount("/", StaticFiles(directory=os.path.join(ROOT, "static"), html=True), name="static")
