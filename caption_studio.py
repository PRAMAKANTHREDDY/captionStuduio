#!/usr/bin/env python3
"""
Caption Studio - local web page to caption a video.

  1. Upload a video from this Mac
  2. Audio is extracted and transcribed into captions (locally, nothing uploaded online)
  3. Adjust caption size / position / colors with a live preview
  4. Export and download the captioned video

Run:
    .venv/bin/python caption_studio.py        # then open http://127.0.0.1:8765

Security: listens on 127.0.0.1 only (not reachable from other machines), rejects
requests whose Host/Origin is not this local address, and stores uploads under
server-generated folder names.
"""

import datetime as dt
import json
import os
import sys
import re
import secrets
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import caption_effects as fx
import transcribe as tr

WINDOWS = sys.platform == "win32"
HOST, PORT = "127.0.0.1", int(os.environ.get("CAPTION_STUDIO_PORT", "8765"))
ROOT = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(ROOT, "recordings", "uploads")
LANGUAGES = {  # Whisper language codes offered in the page
    "en": "English", "hi": "Hindi", "te": "Telugu", "ta": "Tamil", "bn": "Bengali", "mr": "Marathi",
    "ur": "Urdu", "es": "Spanish", "fr": "French", "de": "German", "it": "Italian", "pt": "Portuguese",
    "nl": "Dutch", "sv": "Swedish", "pl": "Polish", "tr": "Turkish", "ru": "Russian", "uk": "Ukrainian",
    "ar": "Arabic", "he": "Hebrew", "fa": "Persian", "id": "Indonesian", "vi": "Vietnamese", "th": "Thai",
    "zh": "Chinese", "ja": "Japanese", "ko": "Korean",
}
ALLOWED_EXT = {".mov", ".mp4", ".m4v"}
MAX_UPLOAD = 4 * 1024 ** 3  # 4 GB
ID_RE = re.compile(r"^[0-9]{8}_[0-9]{6}_[0-9a-f]{8}$")
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}

jobs = {}  # job id -> {"state": queued|running|done|error, "log": [...], ...}
jobs_lock = threading.Lock()
work_lock = threading.Lock()
kw_lock = threading.Lock()  # one transcription/export at a time (CPU heavy)


def session_path(sid):
    if not sid or not ID_RE.match(sid):
        return None
    p = os.path.join(UPLOAD_DIR, sid)
    return p if os.path.isdir(p) else None


def session_video(sess):
    for ext in ALLOWED_EXT:
        p = os.path.join(sess, "video" + ext)
        if os.path.exists(p):
            return p
    return None


def run_job(job_id, fn):
    def log(msg):
        with jobs_lock:
            jobs[job_id]["log"].append(str(msg))

    def worker():
        with work_lock:
            with jobs_lock:
                jobs[job_id]["state"] = "running"
            try:
                result = fn(log)
                with jobs_lock:
                    jobs[job_id].update(state="done", result=result)
            except Exception as e:
                traceback.print_exc()
                with jobs_lock:
                    jobs[job_id].update(state="error", error=str(e))

    with jobs_lock:
        jobs[job_id] = {"state": "queued", "log": []}
    threading.Thread(target=worker, daemon=True).start()


_font_bytes = {}


def font_bytes(font_id):
    """The font file for one catalogue face, as a standalone TTF/OTF the browser can load."""
    fonts = tr.list_fonts()
    if not 0 <= font_id < len(fonts):
        return None
    if font_id not in _font_bytes:
        f = fonts[font_id]
        if f["path"].lower().endswith(".ttc"):
            import io
            from fontTools.ttLib import TTCollection
            buf = io.BytesIO()
            TTCollection(f["path"]).fonts[f["index"]].save(buf)
            _font_bytes[font_id] = buf.getvalue()
        else:
            with open(f["path"], "rb") as fh:
                _font_bytes[font_id] = fh.read()
    return _font_bytes[font_id]


class Handler(BaseHTTPRequestHandler):
    server_version = "CaptionStudio"

    # ---------- helpers ----------
    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _err(self, code, msg):
        self._json({"error": msg}, code)

    def _local_request_ok(self):
        """Block DNS-rebinding and cross-site requests from other web pages."""
        if self.headers.get("Host", "") not in ALLOWED_HOSTS:
            return False
        origin = self.headers.get("Origin")
        if origin is not None and urlparse(origin).netloc not in ALLOWED_HOSTS:
            return False
        return True

    def _read_json(self):
        n = int(self.headers.get("Content-Length", "0"))
        if n > 1_000_000:
            raise ValueError("request too large")
        return json.loads(self.rfile.read(n) or b"{}")

    def _send_file(self, path, mime, download_name=None):
        size = os.path.getsize(path)
        start, end = 0, size - 1
        rng = self.headers.get("Range")
        m = re.match(r"bytes=(\d*)-(\d*)$", rng or "")
        if m:  # video players need byte ranges to seek
            if m.group(1):
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else size - 1
            else:
                start = max(0, size - int(m.group(2)))
            end = min(end, size - 1)
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if download_name:
            self.send_header("Content-Disposition", f'attachment; filename="{download_name}"')
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = f.read(min(1024 * 1024, left))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    return
                left -= len(chunk)

    # ---------- routes ----------
    def do_GET(self):
        if not self._local_request_ok():
            return self._err(403, "forbidden")
        u = urlparse(self.path)
        q = parse_qs(u.query)
        sid = (q.get("id") or [""])[0]

        if u.path == "/":
            body = PAGE.replace("@@LANG_OPTIONS@@", "".join(
                f'<option value="{c}"{" selected" if c == "en" else ""}>{n}</option>'
                for c, n in LANGUAGES.items())).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return self.wfile.write(body)

        if u.path == "/api/fonts":
            fonts = tr.list_fonts(refresh=(q.get("refresh") or [""])[0] == "1")
            return self._json([{"id": f["id"], "family": f["family"], "style": f["style"]} for f in fonts])

        if u.path == "/api/fontfile":
            try:
                fid = int((q.get("id") or [""])[0])
            except ValueError:
                return self._err(400, "bad font id")
            data = font_bytes(fid)
            if data is None:
                return self._err(404, "no such font")
            self.send_response(200)
            self.send_header("Content-Type", "font/ttf")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "max-age=86400")
            self.end_headers()
            return self.wfile.write(data)

        if u.path == "/api/sfx":
            return self._json(tr.SFX)

        if u.path == "/effects.js":  # live preview of the letter/word effects
            with open(os.path.join(ROOT, "effects.js"), "rb") as fh:
                body = fh.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/javascript; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            return self.wfile.write(body)

        if u.path == "/api/anim_meta":  # groups, which animations work per word, and their curves
            return self._json({**fx.anim_meta(), "table": fx.all_tables()})

        if u.path == "/api/animations":
            return self._json(tr.ANIMATIONS)

        if u.path == "/api/styles":
            keys = ("name", "css", "upper", "text", "box", "opacity", "radius",
                    "stroke", "stroke_color", "shadows", "gradient",
                    "premium", "mode", "hl", "hl_text", "scale", "anim", "anim_dur", "tail",
                    "alt_css", "alt_color", "main_mult", "alt_mult", "cursor",
                    "align", "leading", "small_mult", "big_mult", "title", "ultra", "words_per", "emoji",
                    "extrude", "liquid", "palette")
            out = {}
            for k, v in tr.STYLES.items():
                out[k] = {f: v[f] for f in keys if f in v}
                if "variants" in v:  # match-cut looks: browser needs only the CSS + colour
                    out[k]["variants"] = [{"css": css, "color": col} for _, css, col in v["variants"]]
            return self._json(out)

        if u.path == "/api/job":
            jid = (q.get("job") or [""])[0]
            with jobs_lock:
                job = jobs.get(jid)
                return self._json(job) if job else self._err(404, "no such job")

        sess = session_path(sid)
        if u.path in ("/media/video", "/media/captioned", "/api/captions") and not sess:
            return self._err(404, "no such session")

        if u.path == "/api/captions":
            srt = os.path.join(sess, "subtitles.srt")
            caps = tr.parse_srt(srt) if os.path.exists(srt) else []
            kws = tr.load_keywords(sess, caps) if caps else []
            ems = tr.load_emojis(sess, caps, kws) if caps else []
            wss = tr.load_word_styles(sess, caps) if caps else []
            wsd = tr._ws_load(sess)
            return self._json({"captions": [
                {"start": a, "end": b, "text": t, "check": tr.low_confidence_words(sess, a, b),
                 "times": tr.word_times(sess, a, b, t), "kw": kws[i], "emoji": ems[i],
                 "ws": wss[i], "wsLocal": (wsd["caption"].get(str(i), {}).get("words", {})
                                           if wsd["caption"].get(str(i), {}).get("text") == t else {})}
                for i, (a, b, t) in enumerate(caps)], "globalWords": wsd["global"]})

        if u.path == "/media/video":
            v = session_video(sess)
            return self._send_file(v, "video/mp4") if v else self._err(404, "no video")

        if u.path == "/media/captioned":
            v = session_video(sess)
            out = os.path.join(sess, "video_captioned" + os.path.splitext(v)[1]) if v else ""
            if not os.path.exists(out):
                return self._err(404, "not exported yet")
            name = "captioned_" + os.path.splitext(open(os.path.join(sess, "original_name.txt"), encoding="utf-8").read().strip() or "video")[0] + os.path.splitext(v)[1]
            name = re.sub(r'[^A-Za-z0-9._ -]', "_", name)
            return self._send_file(out, "video/mp4", download_name=name)

        return self._err(404, "not found")

    def do_POST(self):
        if not self._local_request_ok():
            return self._err(403, "forbidden")
        u = urlparse(self.path)

        if u.path == "/api/upload":
            return self._upload()

        if u.path == "/api/captions":
            try:
                data = self._read_json()
                idx = int(data.get("index"))
                text = " ".join(str(data.get("text", "")).split())  # single line
            except Exception:
                return self._err(400, "bad request")
            sess = session_path(data.get("id"))
            if not sess:
                return self._err(404, "no such session")
            if not text or len(text) > 500:
                return self._err(400, "caption must be 1-500 characters")
            srt = os.path.join(sess, "subtitles.srt")
            with work_lock:
                caps = tr.parse_srt(srt)
                if not 0 <= idx < len(caps):
                    return self._err(400, "bad caption index")
                caps[idx] = (caps[idx][0], caps[idx][1], text)
                tr.write_srt(caps, srt)
            # edited text: keywords for this caption are re-detected unless still valid
            return self._json({"ok": True, "kw": tr.load_keywords(sess, caps)[idx]})

        if u.path == "/api/wordstyle":
            try:
                data = self._read_json()
                idx = int(data.get("index"))
                words = [int(w) for w in data.get("words", [])]
                scope = data.get("scope", "caption")
                props = data.get("props")
            except Exception:
                return self._err(400, "bad request")
            sess = session_path(data.get("id"))
            if not sess:
                return self._err(404, "no such session")
            caps = tr.parse_srt(os.path.join(sess, "subtitles.srt"))
            if not 0 <= idx < len(caps):
                return self._err(400, "bad caption index")
            n = len(caps[idx][2].split())
            if not words or any(not 0 <= w < n for w in words) or scope not in ("caption", "global"):
                return self._err(400, "pick words from this caption")
            clean = None
            if props is not None:
                if not isinstance(props, dict):
                    return self._err(400, "bad settings")
                clean = {}
                for k in ("color", "glowColor", "marker", "underline", "pill"):
                    v = props.get(k)
                    if v:
                        if not COLOR_RE.match(str(v)):
                            return self._err(400, f"bad colour for {k}")
                        clean[k] = v
                try:
                    if props.get("size") not in (None, "", 1, 1.0):
                        sz = float(props["size"])
                        if not 0.5 <= sz <= 2.5:
                            return self._err(400, "size must be 50-250%")
                        clean["size"] = sz
                    if props.get("glow"):
                        gl = float(props["glow"])
                        if not 0 <= gl <= 1:
                            return self._err(400, "glow must be 0-100%")
                        if gl > 0:
                            clean["glow"] = gl
                    if props.get("fontId") not in (None, ""):
                        fid = int(props["fontId"])
                        if tr.font_by_id(fid) is None:
                            return self._err(400, "unknown font")
                        clean["fontId"] = fid
                except (TypeError, ValueError):
                    return self._err(400, "bad number")
                m = props.get("motion")
                if m and m != "none":
                    if m not in tr.WORD_MOTIONS and m not in tr.ANIMATIONS:
                        return self._err(400, "unknown motion")
                    clean["motion"] = m
                sty = props.get("style")
                if sty:
                    if sty not in tr.STYLES or "~" in sty:
                        return self._err(400, "unknown style")
                    clean["style"] = sty
                try:
                    if props.get("pillOpacity") is not None and clean.get("pill"):
                        po = float(props["pillOpacity"])
                        if not 0 <= po <= 1:
                            return self._err(400, "opacity must be 0-100%")
                        clean["pillOpacity"] = po
                    if props.get("motionDur") is not None and clean.get("motion"):
                        md = float(props["motionDur"])
                        if not 0.1 <= md <= 2:
                            return self._err(400, "animation length out of range")
                        clean["motionDur"] = md
                except (TypeError, ValueError):
                    return self._err(400, "bad number")
                if "glow" not in clean:
                    clean.pop("glowColor", None)
                if not clean:
                    clean = None  # nothing set = same as clearing
            with kw_lock:
                tr.save_word_style(sess, idx, caps[idx][2], words, scope, clean)
            return self._json({"ok": True})

        if u.path == "/api/emoji":
            try:
                data = self._read_json()
                idx = int(data.get("index"))
                emoji = data.get("emoji")
                if emoji is not None:
                    emoji = str(emoji).strip()
            except Exception:
                return self._err(400, "bad request")
            sess = session_path(data.get("id"))
            if not sess:
                return self._err(404, "no such session")
            if emoji is not None and (len(emoji) > 16 or any(ch.isascii() and ch.isalnum() for ch in emoji)):
                return self._err(400, "type an emoji (or leave empty for auto)")
            caps = tr.parse_srt(os.path.join(sess, "subtitles.srt"))
            if not 0 <= idx < len(caps):
                return self._err(400, "bad caption index")
            with kw_lock:
                tr.save_emoji(sess, idx, caps[idx][2], emoji)
            return self._json({"ok": True, "emoji": tr.load_emojis(sess, caps)[idx]})

        if u.path == "/api/keywords":
            try:
                data = self._read_json()
                idx = int(data.get("index"))
                kw = data.get("kw")
                if kw is not None:
                    kw = [int(i) for i in kw]
            except Exception:
                return self._err(400, "bad request")
            sess = session_path(data.get("id"))
            if not sess:
                return self._err(404, "no such session")
            caps = tr.parse_srt(os.path.join(sess, "subtitles.srt"))
            if not 0 <= idx < len(caps):
                return self._err(400, "bad caption index")
            n = len(caps[idx][2].split())
            if kw is not None and (not kw or len(kw) > 6 or any(not 0 <= i < n for i in kw)):
                return self._err(400, "pick 1-6 words from this caption")
            with kw_lock:
                tr.save_keywords(sess, idx, caps[idx][2], kw)
            return self._json({"ok": True, "kw": tr.load_keywords(sess, caps)[idx]})

        if u.path == "/api/export":
            try:
                data = self._read_json()
            except Exception:
                return self._err(400, "bad json")
            sess = session_path(data.get("id"))
            if not sess:
                return self._err(404, "no such session")
            try:
                size = float(data.get("size", 1 / 16))
                y = float(data.get("y", 0.62))
                x = float(data.get("x", 0.5))
                opacity = float(data.get("opacity", 0.75))
            except (TypeError, ValueError):
                return self._err(400, "bad numbers")
            style = data.get("style", "classic")
            if style not in tr.STYLES:
                return self._err(400, "unknown style")
            text_color = data.get("textColor", "#ffffff")
            box_color = data.get("boxColor", "#000000")
            font = None
            if data.get("fontId") is not None:
                try:
                    font = tr.font_by_id(int(data["fontId"]))
                except (TypeError, ValueError):
                    font = None
                if font is None:
                    return self._err(400, "unknown font")
            emoji_on = bool(data.get("emoji", False))
            sfx = data.get("sfx", "none")
            if sfx not in tr.SFX:
                return self._err(400, "unknown sound effect")
            try:
                words_per = int(data.get("wordsPer", 0))
            except (TypeError, ValueError):
                return self._err(400, "bad words per caption")
            if not 0 <= words_per <= 3:
                return self._err(400, "words per caption must be 0-3")
            anim = data.get("anim", "none")
            if anim not in tr.ANIMATIONS:
                return self._err(400, "unknown animation")
            try:
                feather = float(data.get("feather", 0))
            except (TypeError, ValueError):
                return self._err(400, "bad edge fade")
            if not 0 <= feather <= 1:
                return self._err(400, "edge fade must be 0-100%")
            try:
                glow = float(data.get("glow", 0))
            except (TypeError, ValueError):
                return self._err(400, "bad glow")
            glow_color = data.get("glowColor", "#ffffff")
            if not 0 <= glow <= 1 or not COLOR_RE.match(str(glow_color)):
                return self._err(400, "glow must be 0-100% with a colour like #ff00aa")
            anim_scope = data.get("animScope", "line")
            if anim_scope not in ("line", "word"):
                return self._err(400, "animation scope must be line or word")
            try:
                anim_dur = float(data.get("animDur", 0.3))
            except (TypeError, ValueError):
                return self._err(400, "bad animation duration")
            if not 0.05 <= anim_dur <= 2:
                return self._err(400, "animation duration out of range")
            hl = data.get("hlColor") or None
            if hl is not None and not COLOR_RE.match(str(hl)):
                return self._err(400, "bad highlight color")
            if not (0.01 <= size <= 0.2 and 0 <= y <= 1 and 0 <= x <= 1 and 0 <= opacity <= 1
                    and COLOR_RE.match(text_color) and COLOR_RE.match(box_color)):
                return self._err(400, "value out of range")

            video = session_video(sess)
            out = os.path.join(sess, "video_captioned" + os.path.splitext(video)[1])

            def export(log):
                caps = tr.parse_srt(os.path.join(sess, "subtitles.srt"))
                log(f"drawing {len(caps)} captions onto the video ...")
                tr.burn_captions(video, caps, out, size, y, opacity, text_color, box_color, x, style, hl,
                                 font, anim, anim_dur, words_per, sfx, emoji_on, anim_scope, feather,
                                 glow, glow_color)
                log("done")
                return {"download": f"/media/captioned?id={os.path.basename(sess)}"}

            jid = secrets.token_hex(8)
            run_job(jid, export)
            return self._json({"job": jid})

        return self._err(404, "not found")

    def _upload(self):
        name = os.path.basename(self.headers.get("X-Filename", "video.mov"))
        use_vocab = self.headers.get("X-Use-Vocab") == "1"
        language = self.headers.get("X-Language", "en")
        if language == "auto":
            language = None
        elif language not in LANGUAGES:
            return self._err(400, "unsupported language")
        translate = self.headers.get("X-Translate") == "1"
        ext = os.path.splitext(name)[1].lower()
        if ext not in ALLOWED_EXT:
            return self._err(400, f"only {', '.join(sorted(ALLOWED_EXT))} files are supported")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return self._err(400, "bad length")
        if length <= 0 or length > MAX_UPLOAD:
            return self._err(400, "file is empty or larger than 4 GB")

        sid = dt.datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + secrets.token_hex(4)
        sess = os.path.join(UPLOAD_DIR, sid)
        os.makedirs(sess)
        video = os.path.join(sess, "video" + ext)
        with open(video, "wb") as f:
            left = length
            while left > 0:
                chunk = self.rfile.read(min(1024 * 1024, left))
                if not chunk:
                    break
                f.write(chunk)
                left -= len(chunk)
        if left:
            return self._err(400, "upload interrupted")
        with open(os.path.join(sess, "original_name.txt"), "w", encoding="utf-8") as f:
            f.write(name)

        def job(log):
            tr.transcribe_session(sess, video, None, log, language=language, use_vocab=use_vocab,
                                  translate=translate)
            return {"id": sid}

        jid = secrets.token_hex(8)
        run_job(jid, job)
        return self._json({"job": jid, "id": sid})

    def log_message(self, fmt, *args):  # keep the terminal quiet except errors
        if args and str(args[1]).startswith(("4", "5")):
            super().log_message(fmt, *args)


PAGE = r"""<!doctype html>
<html><head><meta charset="utf-8"><title>Caption Studio</title>
<style>
  :root {
    color-scheme: dark;
    --bg: #0a0b12; --card: rgba(22, 24, 36, 0.72); --card2: rgba(30, 33, 50, 0.7);
    --line: rgba(255, 255, 255, 0.08); --text: #eceefe; --muted: #9aa0bd; --dim: #6b7090;
    --accent: #8b5cf6; --accent2: #ec4899; --accent3: #22d3ee; --ok: #34d399; --warn: #fbbf24;
  }
  [hidden] { display: none !important; }
  * { box-sizing: border-box; }
  html, body { height: 100%; }
  body { margin: 0; font: 14px/1.45 -apple-system, BlinkMacSystemFont, "SF Pro Text", sans-serif;
         color: var(--text); background: var(--bg); overflow-x: hidden; }
  /* soft animated colour glows behind everything */
  body::before, body::after { content: ""; position: fixed; z-index: -1; width: 60vmax; height: 60vmax;
         border-radius: 50%; filter: blur(90px); opacity: .45; pointer-events: none; }
  body::before { background: radial-gradient(circle, #6d28d9 0%, transparent 65%); top: -20vmax; left: -15vmax;
         animation: drift1 22s ease-in-out infinite alternate; }
  body::after { background: radial-gradient(circle, #db2777 0%, transparent 65%); bottom: -25vmax; right: -15vmax;
         animation: drift2 26s ease-in-out infinite alternate; }
  @keyframes drift1 { to { transform: translate(18vmax, 12vmax) scale(1.15); } }
  @keyframes drift2 { to { transform: translate(-16vmax, -10vmax) scale(1.1); } }
  .grid-bg { position: fixed; inset: 0; z-index: -1; pointer-events: none;
         background-image: linear-gradient(var(--line) 1px, transparent 1px), linear-gradient(90deg, var(--line) 1px, transparent 1px);
         background-size: 44px 44px; mask-image: radial-gradient(ellipse at center, #000 30%, transparent 75%); opacity: .5; }

  /* header */
  header { position: sticky; top: 0; z-index: 10; display: flex; align-items: center; gap: 14px;
           padding: 12px 22px; background: rgba(10, 11, 18, 0.6); backdrop-filter: blur(16px);
           border-bottom: 1px solid var(--line); }
  .logo { width: 36px; height: 36px; border-radius: 10px; display: grid; place-items: center; font-weight: 800;
          font-size: 15px; background: linear-gradient(135deg, var(--accent), var(--accent2)); box-shadow: 0 6px 20px rgba(139,92,246,.45); }
  .brand b { display: block; font-size: 16px; letter-spacing: .2px; }
  .brand span { color: var(--muted); font-size: 12px; }
  .steps { margin-left: auto; display: flex; gap: 6px; }
  .step { padding: 6px 12px; border-radius: 999px; font-size: 12px; color: var(--dim); border: 1px solid var(--line); }
  .step i { font-style: normal; display: inline-grid; place-items: center; width: 18px; height: 18px; margin-right: 6px;
            border-radius: 50%; background: rgba(255,255,255,.06); font-size: 11px; }
  .step.on { color: var(--text); border-color: rgba(139,92,246,.5); background: rgba(139,92,246,.15); }
  .step.on i { background: linear-gradient(135deg, var(--accent), var(--accent2)); }
  .step.done i { background: var(--ok); color: #062; }

  .card { background: var(--card); border: 1px solid var(--line); border-radius: 18px;
          backdrop-filter: blur(18px); box-shadow: 0 20px 60px rgba(0,0,0,.35); }

  /* ---------- upload screen ---------- */
  #uploadView { max-width: 860px; margin: 48px auto; padding: 0 20px; }
  .hero { text-align: center; margin-bottom: 26px; }
  .hero h1 { font-size: 40px; line-height: 1.1; margin: 0 0 10px; letter-spacing: -.5px; }
  .hero h1 em { font-style: normal; background: linear-gradient(90deg, var(--accent3), var(--accent), var(--accent2));
                -webkit-background-clip: text; background-clip: text; color: transparent; }
  .hero p { color: var(--muted); margin: 0; font-size: 15px; }
  .upcard { padding: 22px; }
  #drop { border: 2px dashed rgba(139,92,246,.45); border-radius: 14px; padding: 46px 20px; text-align: center;
          cursor: pointer; transition: .2s; background: rgba(139,92,246,.05); }
  #drop:hover, #drop.over { border-color: var(--accent2); background: rgba(236,72,153,.08); transform: translateY(-2px); }
  .upicon { width: 64px; height: 64px; margin: 0 auto 14px; border-radius: 18px; display: grid; place-items: center;
            background: linear-gradient(135deg, rgba(139,92,246,.3), rgba(236,72,153,.3)); }
  .dropTitle { font-size: 18px; font-weight: 600; margin-bottom: 4px; }
  .dropSub { color: var(--muted); }
  .chips { margin-top: 14px; display: flex; gap: 6px; justify-content: center; }
  .chip { font-size: 11px; padding: 3px 9px; border-radius: 999px; background: rgba(255,255,255,.06); color: var(--muted);
          border: 1px solid var(--line); }
  .fakebtn { display: inline-block; margin-top: 16px; padding: 9px 18px; border-radius: 10px; font-weight: 600;
             background: linear-gradient(135deg, var(--accent), var(--accent2)); box-shadow: 0 8px 24px rgba(139,92,246,.35); }
  .opt { display: flex; align-items: center; gap: 12px; margin-top: 16px; padding: 12px 14px; border-radius: 12px;
         background: rgba(255,255,255,.03); border: 1px solid var(--line); cursor: pointer; }
  .opt small { display: block; color: var(--dim); }
  .switch { position: relative; width: 40px; height: 22px; flex: none; }
  .switch input { opacity: 0; width: 0; height: 0; }
  .switch span { position: absolute; inset: 0; border-radius: 999px; background: #2b2f45; transition: .2s; }
  .switch span::after { content: ""; position: absolute; left: 3px; top: 3px; width: 16px; height: 16px; border-radius: 50%;
                        background: #fff; transition: .2s; }
  .switch input:checked + span { background: linear-gradient(135deg, var(--accent), var(--accent2)); }
  .switch input:checked + span::after { transform: translateX(18px); }
  .progress { height: 6px; border-radius: 999px; background: rgba(255,255,255,.06); overflow: hidden; margin-top: 16px; }
  .progress i { display: block; height: 100%; width: 0; border-radius: inherit;
                background: linear-gradient(90deg, var(--accent3), var(--accent), var(--accent2)); transition: width .3s; }
  .progress.busy i { width: 35% !important; animation: slide 1.2s ease-in-out infinite; }
  @keyframes slide { from { transform: translateX(-100%); } to { transform: translateX(300%); } }
  #upStatus { margin-top: 10px; color: var(--muted); white-space: pre-wrap; font-size: 12px; text-align: center;
              max-height: 180px; overflow: auto; }
  .features { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-top: 18px; }
  .feat { padding: 14px; border-radius: 14px; background: var(--card2); border: 1px solid var(--line); }
  .feat b { display: block; margin: 6px 0 2px; font-size: 13px; }
  .feat span { color: var(--dim); font-size: 12px; }
  .feat .ic { font-size: 18px; }

  /* ---------- editor ---------- */
  main#editView { display: grid; grid-template-columns: var(--pw, auto) 14px minmax(420px, 1fr); gap: 4px; padding: 20px 22px;
                  height: calc(100vh - 61px); }
  /* drag handle between preview and controls */
  #splitter { cursor: col-resize; display: grid; place-items: center; border-radius: 8px; outline: none; touch-action: none; }
  #splitter::before { content: ""; width: 4px; height: 56px; border-radius: 4px; background: rgba(255,255,255,.18); transition: .15s; }
  #splitter:hover::before, #splitter:focus-visible::before, #splitter.drag::before { background: var(--accent); height: 90px; }
  body.resizing, body.resizing * { cursor: col-resize !important; user-select: none !important; }
  /* resizing changes only the preview WIDTH: the video never grows taller than the window */
  main.resized .viewer { min-width: 0; }
  main.resized .stageWrap { overflow: hidden; }
  main.resized #stage video { max-width: 100%; max-height: calc(100vh - 175px); }
  .viewer { display: flex; flex-direction: column; padding: 14px; min-height: 0; }
  .viewer .bar { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 10px; }
  .viewer .hint { max-width: 260px; text-align: right; }
  .viewer .bar b { font-size: 14px; }
  .hint { color: var(--dim); font-size: 12px; }
  .stageWrap { flex: 1; min-height: 0; display: grid; place-items: center; border-radius: 12px;
               background: repeating-conic-gradient(rgba(255,255,255,.03) 0% 25%, transparent 0% 50%) 50% / 22px 22px; }
  #stage { position: relative; display: inline-block; background: #000; line-height: 0; user-select: none;
           border-radius: 10px; overflow: hidden; box-shadow: 0 16px 50px rgba(0,0,0,.55); }
  #stage video { max-height: calc(100vh - 175px); max-width: min(62vw, 100%); display: block; }
  .stageWrap { padding: 10px; }
  #cap { position: absolute; display: block; cursor: move; line-height: 1.25; }
  #capBox { display: block; text-align: center; white-space: normal; }
  /* edge fade: the background lives in ::before, masked with two linear ramps (same maths as _box()) */
  #capBox.feather { position: relative; isolation: isolate; }
  #capBox.feather::before { content: ""; position: absolute; inset: 0; z-index: -1; border-radius: inherit;
    background: var(--boxbg);
    -webkit-mask: linear-gradient(to right, transparent, #000 var(--fe), #000 calc(100% - var(--fe)), transparent),
                  linear-gradient(to bottom, transparent, #000 var(--fe), #000 calc(100% - var(--fe)), transparent);
    -webkit-mask-composite: source-in; mask-composite: intersect; }
  #capText { display: block; }
  #capText .w { display: inline; }
  #cap.hidden { visibility: hidden; }
  /* on-video resize: frame, corner handles and a mini toolbar (shown on hover / when selected) */
  #cap .capFrame { position: absolute; inset: -4px; border: 1.5px dashed rgba(139,92,246,.9); border-radius: 6px;
                   pointer-events: none; display: none; }
  #cap .rh { position: absolute; width: 12px; height: 12px; background: #fff; border: 2px solid var(--accent);
             border-radius: 3px; display: none; z-index: 4; box-shadow: 0 1px 4px rgba(0,0,0,.5); }
  #cap .rh.nw { left: -10px; top: -10px; cursor: nwse-resize; }  #cap .rh.se { right: -10px; bottom: -10px; cursor: nwse-resize; }
  #cap .rh.ne { right: -10px; top: -10px; cursor: nesw-resize; }  #cap .rh.sw { left: -10px; bottom: -10px; cursor: nesw-resize; }
  #cap .capTools { position: absolute; left: 50%; bottom: calc(100% + 12px); transform: translateX(-50%); display: none;
                   align-items: center; gap: 2px; padding: 3px; border-radius: 10px; z-index: 5; white-space: nowrap;
                   background: rgba(18,19,30,.92); border: 1px solid rgba(255,255,255,.15); box-shadow: 0 6px 18px rgba(0,0,0,.5);
                   font: 600 12px/1 -apple-system, sans-serif; color: #fff; cursor: default; }
  #cap .capTools.below { bottom: auto; top: calc(100% + 12px); }
  #cap .capTools button { width: 28px; height: 26px; border: 0; border-radius: 7px; background: rgba(255,255,255,.08);
                          color: #fff; cursor: pointer; font: 700 12px -apple-system, sans-serif; padding: 0; }
  #cap .capTools button:hover { background: var(--accent); }
  #cap .capTools .val { min-width: 46px; text-align: center; color: #cfd2ff; }
  #cap:hover .capFrame, #cap.sel .capFrame, #cap:hover .rh, #cap.sel .rh { display: block; }
  #cap:hover .capTools, #cap.sel .capTools { display: flex; }
  body.resizingCap, body.resizingCap * { user-select: none !important; }
  /* on-video text editor */
  #capEdit { position: absolute; z-index: 8; display: flex; flex-direction: column; gap: 6px; padding: 8px; border-radius: 12px;
             background: rgba(18,19,30,.96); border: 1px solid rgba(139,92,246,.7); box-shadow: 0 10px 30px rgba(0,0,0,.6); }
  #capEdit textarea { width: 100%; min-height: 44px; resize: vertical; border-radius: 8px; border: 1px solid var(--line);
                      background: rgba(0,0,0,.4); color: #fff; font: 600 15px/1.35 -apple-system, sans-serif; padding: 8px; outline: none; }
  #capEdit textarea:focus { border-color: var(--accent); }
  #capEdit .row { display: flex; gap: 8px; justify-content: space-between; align-items: center; flex-wrap: wrap; }
  #capEdit .hint { color: var(--dim); font: 11px -apple-system, sans-serif; }
  #capEdit .btns { display: flex; gap: 6px; flex: none; margin-left: auto; }
  #capEdit button { border: 0; border-radius: 8px; padding: 6px 12px; cursor: pointer; font: 600 12px -apple-system, sans-serif; }
  #capEdit .save { background: linear-gradient(135deg, var(--accent), var(--accent2)); color: #fff; }
  #capEdit .cancel { background: rgba(255,255,255,.08); color: var(--muted); }

  .panel { display: flex; flex-direction: column; min-height: 0; overflow: hidden; }
  .tabs { display: flex; gap: 2px; padding: 10px 8px 0; border-bottom: 1px solid var(--line); flex-wrap: nowrap;
          overflow-x: auto; scrollbar-width: none; }
  .tabs::-webkit-scrollbar { display: none; }
  .tab { appearance: none; border: 0; background: none; color: var(--muted); padding: 10px 12px; font-size: 13px;
         white-space: nowrap; flex: 1 1 auto;
         border-radius: 10px 10px 0 0; cursor: pointer; width: auto; margin: 0; border-bottom: 2px solid transparent; }
  .tab:hover { color: var(--text); background: rgba(255,255,255,.04); }
  .tab.on { color: var(--text); border-bottom-color: var(--accent); background: rgba(139,92,246,.12); }
  .tab .count { font-size: 10px; padding: 1px 6px; border-radius: 999px; background: rgba(255,255,255,.1); margin-left: 4px; }
  .panes { flex: 1; overflow: auto; padding: 16px; }
  .pane { display: none; }
  .pane.on { display: block; animation: fade .2s; }
  @keyframes fade { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; transform: none; } }
  .pane h4 { margin: 0 0 4px; font-size: 14px; }
  .pane .sub { color: var(--dim); font-size: 12px; margin-bottom: 12px; }
  .group { padding: 12px; border-radius: 12px; background: rgba(255,255,255,.03); border: 1px solid var(--line); margin-bottom: 12px; }
  label { display: flex; justify-content: space-between; margin: 0 0 6px; color: var(--muted); font-size: 12px; }
  label span { color: var(--text); font-variant-numeric: tabular-nums; }
  .group label + input[type=range] { margin-bottom: 12px; }
  input[type=range] { width: 100%; accent-color: var(--accent); }
  input[type=search], #font { width: 100%; background: rgba(0,0,0,.35); color: var(--text); border: 1px solid var(--line);
                              border-radius: 10px; padding: 8px 10px; font-size: 13px; outline: none; }
  input[type=search]:focus, #font:focus { border-color: rgba(139,92,246,.6); }
  #font { margin-top: 8px; padding: 4px; height: min(52vh, 420px); }
  #font option { padding: 5px 8px; border-radius: 6px; }
  #fontNow { color: var(--accent3); font-size: 12px; margin-top: 8px; }
  .colors { display: grid; grid-template-columns: repeat(auto-fill, minmax(170px, 1fr)); gap: 10px; }
  .swatch { display: flex; align-items: center; gap: 10px; padding: 10px; border-radius: 12px;
            background: rgba(255,255,255,.03); border: 1px solid var(--line); }
  .swatch label { margin: 0; }
  input[type=color] { appearance: none; -webkit-appearance: none; width: 36px; height: 36px; border: 0; padding: 0;
                      border-radius: 10px; background: none; cursor: pointer; flex: none; }
  input[type=color]::-webkit-color-swatch-wrapper { padding: 0; }
  input[type=color]::-webkit-color-swatch { border: 2px solid rgba(255,255,255,.2); border-radius: 10px; }

  /* style gallery */
  #styles { display: grid; grid-template-columns: repeat(auto-fill, minmax(165px, 1fr)); gap: 10px; }
  .sect { grid-column: 1 / -1; font-size: 11px; font-weight: 700; letter-spacing: .8px; text-transform: uppercase;
          color: var(--muted); margin: 8px 0 2px; }
  .sect.premium { color: var(--warn); }
  .tile { position: relative; height: 62px; display: flex; align-items: center; justify-content: center; cursor: pointer;
          overflow: hidden; padding: 0 4px; border-radius: 12px; border: 2px solid transparent; transition: .15s;
          background: linear-gradient(135deg, #3a4160, #151826); }
  .tile:hover { transform: translateY(-2px); box-shadow: 0 8px 20px rgba(0,0,0,.35); }
  .tile.on { border-color: var(--accent); box-shadow: 0 0 0 3px rgba(139,92,246,.25); }
  .tile .b { display: inline-block; line-height: 1.25; white-space: nowrap; }
  .tile .star { position: absolute; top: 3px; right: 6px; font-size: 10px; color: var(--warn); }

  /* motion (entrance animations) — keyframes mirror anim_params() in transcribe.py */
  #anims { display: grid; grid-template-columns: repeat(auto-fill, minmax(120px, 1fr)); gap: 10px; }
  .mtile { height: 64px; border-radius: 12px; border: 2px solid transparent; cursor: pointer; display: flex;
           flex-direction: column; align-items: center; justify-content: center; gap: 4px; overflow: hidden;
           background: linear-gradient(135deg, #3a4160, #151826); transition: .15s; font-size: 12px; color: var(--muted); }
  .mtile:hover { transform: translateY(-2px); color: var(--text); }
  .mtile.on { border-color: var(--accent); box-shadow: 0 0 0 3px rgba(139,92,246,.25); color: var(--text); }
  .mtile .demo { font-weight: 800; font-size: 16px; color: #fff; padding: 1px 8px; border-radius: 6px;
                 background: rgba(0,0,0,.45); }
  .mtile:hover .demo { animation-duration: 1.1s !important; animation-iteration-count: infinite !important; }
  #replay { margin-top: 10px; width: 100%; padding: 9px; border-radius: 10px; cursor: pointer; color: var(--text);
            background: rgba(139,92,246,.15); border: 1px solid rgba(139,92,246,.4); }
  @keyframes cs-fade { from { opacity: 0; } }
  @keyframes cs-pop { 0% { opacity: 0; transform: scale(.5); } 33% { opacity: 1; } 70% { transform: scale(1.08); } 100% { transform: scale(1); } }
  @keyframes cs-zoom { from { opacity: 0; transform: scale(1.5); } }
  @keyframes cs-slide_up { from { opacity: 0; transform: translateY(80%); } }
  @keyframes cs-slide_down { from { opacity: 0; transform: translateY(-80%); } }
  @keyframes cs-slide_left { from { opacity: 0; transform: translateX(3em); } }
  @keyframes cs-bounce { 0% { opacity: 0; transform: translateY(-120%); } 25% { opacity: 1; } 36% { transform: translateY(0); }
                         54% { transform: translateY(-28%); } 72% { transform: translateY(0); } 84% { transform: translateY(-7%); }
                         94%, 100% { transform: translateY(0); } }
  @keyframes cs-shake { 0%, 100% { transform: none; } 12% { transform: translateX(-.35em); } 25% { transform: translateX(.3em); }
                        37% { transform: translateX(-.25em); } 50% { transform: translateX(.18em); } 62% { transform: translateX(-.12em); }
                        75% { transform: translateX(.06em); } }
  @keyframes cs-blur { from { opacity: 0; filter: blur(8px); } }
  @keyframes cs-glitch { 0% { opacity: 0; transform: translate(.3em,-.1em); filter: drop-shadow(-.12em 0 #ff004c) drop-shadow(.12em 0 #00f0ff); }
                         20% { opacity: 1; transform: translate(-.25em,.08em); } 40% { transform: translate(.18em,-.05em); filter: drop-shadow(-.08em 0 #ff004c) drop-shadow(.08em 0 #00f0ff); }
                         60% { transform: translate(-.1em,.03em); } 80% { transform: translate(.05em,0); filter: drop-shadow(-.03em 0 #ff004c) drop-shadow(.03em 0 #00f0ff); }
                         100% { transform: none; filter: none; } }
  @keyframes cs-rise { from { opacity: 0; transform: translateY(35%); } }
  @keyframes cs-wipe { from { clip-path: inset(0 100% 0 0); } to { clip-path: inset(0 0 0 0); } }

  .agroup { grid-column: 1 / -1; margin: 8px 0 -2px; font-size: 11px; font-weight: 700; letter-spacing: .06em;
            text-transform: uppercase; color: var(--dim); }
  .seg { display: flex; gap: 4px; padding: 3px; border-radius: 10px; background: rgba(0,0,0,.3); }
  .seg button { flex: 1; padding: 7px 8px; border: 0; border-radius: 8px; background: transparent; color: var(--muted);
                font: inherit; font-size: 12px; cursor: pointer; }
  .seg button.on { background: rgba(139,92,246,.35); color: var(--text); }
  .seg button:disabled { opacity: .4; cursor: default; }
  #cap.masking { overflow: hidden; }
  /* per-word effects (effects.js) */
  .fxpill { position: relative; display: inline-block; isolation: isolate; }
  .fxpill::before { content: ""; position: absolute; z-index: -1; left: -.12em; right: -.12em; top: calc(50% - .34em);
                    height: .76em; border-radius: .14em; background: var(--pill); opacity: .92; }
  .fxband { display: inline-block; clip-path: inset(-.1em); }
  /* trend features */
  #capText .cursor { display: inline-block; width: .08em; height: .9em; vertical-align: -.1em; margin: 0 .03em; }
  #capBox.tail, .tile .b.tail { position: relative; }
  #capBox.tail::after, .tile .b.tail::after { content: ""; position: absolute; right: -.3em; bottom: -.25em; width: .85em; height: .85em;
           background: var(--boxc); clip-path: polygon(0 0, 100% 100%, 0 60%); }
  #safe { position: absolute; left: 0; top: 0; pointer-events: none; }
  /* fullscreen = the whole preview (video + captions), video centred and scaled to fit */
  #fsBtn { margin-left: auto; padding: 6px 10px; font-size: 12px; }
  #fsExit { display: none; }
  #stage:fullscreen { width: 100vw; height: 100vh; display: flex; align-items: center; justify-content: center;
                      background: #000; border-radius: 0; box-shadow: none; }
  #stage:fullscreen #fsExit { display: block; position: absolute; top: 16px; right: 16px; z-index: 5; padding: 8px 12px;
                      border-radius: 10px; border: 1px solid rgba(255,255,255,.25); background: rgba(0,0,0,.55); color: #fff;
                      font: 600 13px -apple-system, sans-serif; cursor: pointer; line-height: 1.2; }
  #stage:-webkit-full-screen { width: 100vw; height: 100vh; display: flex; align-items: center; justify-content: center; background: #000; }
  #safe .sz { position: absolute; background: rgba(255,59,48,.16); border: 1px dashed rgba(255,99,88,.75); }
  #safe .sz span { position: absolute; left: 6px; top: 4px; font: 600 11px -apple-system, sans-serif; color: #ffb4ae; line-height: 1.2; }
  #safe .good { position: absolute; border: 2px dashed rgba(52,211,153,.8); background: rgba(52,211,153,.07); }
  #safe .good span { position: absolute; left: 6px; top: 4px; font: 600 11px -apple-system, sans-serif; color: #8ef0c6; line-height: 1.2; }
  select.sel { width: 100%; background: rgba(0,0,0,.35); color: var(--text); border: 1px solid var(--line); border-radius: 10px;
               padding: 8px 10px; font-size: 13px; }
  .row2 { display: flex; gap: 8px; align-items: center; }
  .btn2 { padding: 8px 10px; border-radius: 10px; cursor: pointer; color: var(--text); white-space: nowrap;
          background: rgba(139,92,246,.15); border: 1px solid rgba(139,92,246,.4); }

  /* captions list */
  #list { font-size: 12px; }
  .item { padding: 8px; border-radius: 10px; margin-bottom: 6px; border: 1px solid transparent; background: rgba(255,255,255,.03); }
  .item.on { border-color: rgba(139,92,246,.5); background: rgba(139,92,246,.12); }
  .item .t { color: var(--accent3); cursor: pointer; font-variant-numeric: tabular-nums; font-size: 11px; }
  .item input { width: 100%; background: rgba(0,0,0,.35); color: var(--text); border: 1px solid var(--line);
                border-radius: 8px; padding: 6px 8px; font-size: 12px; margin-top: 4px; outline: none; }
  .item input:focus { border-color: rgba(139,92,246,.6); }
  .item input.saved { border-color: var(--ok); }
  .item .warn { color: var(--warn); font-size: 11px; margin-top: 4px; }
  .kwrow { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 6px; align-items: center; }
  .kwrow .lbl { color: var(--dim); font-size: 11px; margin-right: 2px; }
  .kwrow .chip { cursor: pointer; padding: 2px 8px; border-radius: 999px; font-size: 11px; user-select: none;
                 background: rgba(255,255,255,.05); border: 1px solid var(--line); color: var(--muted); }
  .kwrow .chip:hover { color: var(--text); border-color: rgba(255,255,255,.25); }
  #capEmoji { position: absolute; z-index: 3; text-align: center; line-height: 1; pointer-events: none; }
  .kwrow input.emo { width: 3.2em; text-align: center; background: rgba(0,0,0,.35); color: var(--text); border: 1px solid var(--line);
                     border-radius: 8px; padding: 1px 4px; font-size: 14px; margin: 0; }
  .sect.ultra { color: #ff9cf2; }
  .tile.ultraTile { background: linear-gradient(135deg, #4b2a63, #18122a); }
  .kwrow .chip.kw { background: rgba(242,255,58,.18); border-color: rgba(242,255,58,.6); color: #f6ff8a; font-weight: 700; }
  .kwrow .auto { cursor: pointer; font-size: 11px; color: var(--accent3); margin-left: 4px; background: none; border: 0; padding: 2px 4px; }

  /* export footer */
  .footer { padding: 12px 16px; border-top: 1px solid var(--line); background: rgba(10,11,18,.4);
            display: grid; grid-template-columns: 1fr auto; gap: 8px 10px; align-items: center; }
  .footer #status { grid-column: 1 / -1; margin-top: 0; }
  .footer #status:empty { display: none; }
  button { font: inherit; }
  #export { width: 100%; border: 0; border-radius: 12px; padding: 12px; font-size: 15px; font-weight: 700; color: #fff;
            cursor: pointer; background: linear-gradient(135deg, var(--accent), var(--accent2));
            box-shadow: 0 10px 28px rgba(139,92,246,.4); transition: .15s; }
  #export:hover { transform: translateY(-1px); filter: brightness(1.08); }
  #export:disabled { background: #3a3d52; box-shadow: none; cursor: default; transform: none; }
  #status { margin-top: 8px; color: var(--muted); white-space: pre-wrap; font-size: 12px; max-height: 90px; overflow: auto; }
  #status a { color: var(--accent3); font-weight: 600; }
  #another { width: auto; margin: 0; padding: 12px 16px; border-radius: 12px; cursor: pointer; color: var(--muted); white-space: nowrap;
             background: transparent; border: 1px solid var(--line); }
  #another:hover { color: var(--text); border-color: rgba(255,255,255,.2); }
  a { color: var(--accent3); }
  ::-webkit-scrollbar { width: 10px; height: 10px; }
  ::-webkit-scrollbar-thumb { background: rgba(255,255,255,.12); border-radius: 10px; border: 2px solid transparent; background-clip: padding-box; }

  @media (max-width: 1000px) {
    main#editView { grid-template-columns: 1fr; height: auto; }
    #splitter { display: none; }
    .features { grid-template-columns: 1fr 1fr; }
  }
  /* resume dialog */
  #resume { position: fixed; inset: 0; z-index: 100; display: grid; place-items: center; background: rgba(5,6,12,.6);
            backdrop-filter: blur(6px); }
  #resume .dlg { width: min(440px, 92vw); padding: 22px; text-align: left; }
  #resume h3 { margin: 0 0 6px; font-size: 17px; }
  #resume p { margin: 0 0 16px; color: var(--muted); font-size: 13px; }
  #resume .acts { display: flex; gap: 10px; justify-content: flex-end; }
  #resume button { padding: 10px 16px; border-radius: 10px; cursor: pointer; font-weight: 600; }
  #resumeYes { border: 0; color: #fff; background: linear-gradient(135deg, var(--accent), var(--accent2)); }
  #resumeNo { background: transparent; color: var(--muted); border: 1px solid var(--line); }

  /* ---------- per-word styles ---------- */
  #wordBar { margin: 10px 10px 0; padding: 10px 12px; border-radius: 12px; background: rgba(255,213,74,.12);
             border: 1px solid rgba(255,213,74,.55); font-size: 12px; }
  #wordBar .wbTitle b { color: #ffd54a; }
  #wordBar .wbHint { color: var(--dim); }
  #wordBar .wbRow { display: flex; align-items: center; gap: 12px; margin-top: 6px; flex-wrap: wrap; }
  #wordBar label { display: flex; align-items: center; gap: 5px; margin: 0; color: var(--text); font-size: 12px; }
  #wordBar button { border: 0; border-radius: 8px; padding: 6px 12px; cursor: pointer; font-weight: 600; font-size: 12px; }
  #wbReset { background: rgba(255,255,255,.08); color: var(--muted); }
  #wbDone { background: linear-gradient(135deg, var(--accent), var(--accent2)); color: #fff; }
  body.wordmode .capOnly { opacity: .35; pointer-events: none; }
  #capText .w.wsel { outline: 2px dashed #ffd54a; outline-offset: 3px; border-radius: 4px; }
  .kwrow .chip.wst { box-shadow: inset 0 -2px 0 #ff9cf2; }
  #wordPanel { position: fixed; z-index: 60; width: 320px; padding: 14px; border-radius: 14px; font-size: 12px;
               background: rgba(18,19,30,.97); border: 1px solid rgba(139,92,246,.6); box-shadow: 0 16px 40px rgba(0,0,0,.6); }
  #wordPanel h5 { margin: 0 0 2px; font-size: 14px; }
  #wordPanel .wsub { color: var(--dim); margin-bottom: 10px; }
  #wordPanel .wrow { display: grid; grid-template-columns: 92px 1fr; align-items: center; gap: 8px; margin: 7px 0; }
  #wordPanel .wrow > span { color: var(--muted); }
  #wordPanel .wctl { display: flex; align-items: center; gap: 6px; }
  #wordPanel input[type=color] { width: 30px; height: 26px; }
  #wordPanel select { width: 100%; background: rgba(0,0,0,.35); color: var(--text); border: 1px solid var(--line); border-radius: 8px; padding: 4px 6px; }
  #wordPanel .wscope { display: flex; flex-direction: column; gap: 4px; margin: 10px 0 4px; color: var(--text); }
  #wordPanel .wbtns { display: flex; gap: 6px; justify-content: flex-end; margin-top: 10px; }
  #wordPanel .wbtns button { border: 0; border-radius: 8px; padding: 7px 12px; cursor: pointer; font-weight: 600; }
  #wordPanel .wApply { background: linear-gradient(135deg, var(--accent), var(--accent2)); color: #fff; }
  #wordPanel .wClear, #wordPanel .wClose { background: rgba(255,255,255,.08); color: var(--muted); }
  #wordPanel .wval { min-width: 38px; text-align: right; color: var(--text); font-variant-numeric: tabular-nums; }
</style></head>
<body>
<div class="grid-bg"></div>
<header>
  <div class="logo">CC</div>
  <div class="brand"><b>Caption Studio</b><span>Auto captions · styles · export. Runs entirely on this Mac.</span></div>
  <div class="steps">
    <div class="step on" id="step1"><i>1</i>Upload</div>
    <div class="step" id="step2"><i>2</i>Style</div>
    <div class="step" id="step3"><i>3</i>Export</div>
  </div>
</header>

<div id="uploadView">
  <div class="hero">
    <h1>Turn speech into <em>stunning captions</em></h1>
    <p>Upload a video. It's transcribed locally, then you style, position and export.</p>
  </div>
  <div class="card upcard">
    <div id="drop">
      <div class="upicon">
        <svg width="30" height="30" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 16V4"/><path d="m6 10 6-6 6 6"/><path d="M4 20h16"/></svg>
      </div>
      <div class="dropTitle">Drop a video here</div>
      <div class="dropSub">or click to choose a file</div>
      <div class="fakebtn">Choose video</div>
      <div class="chips"><span class="chip">.mov</span><span class="chip">.mp4</span><span class="chip">.m4v</span><span class="chip">up to 4 GB</span></div>
      <input id="file" type="file" accept=".mov,.mp4,.m4v,video/*" hidden>
    </div>
    <div class="opt">
      <span style="flex:1;color:var(--text);font-size:13px">Spoken language
        <small>Auto-detect works for most videos</small></span>
      <select id="lang" class="sel" style="width:170px" aria-label="Spoken language">
        <option value="auto">Auto-detect</option>@@LANG_OPTIONS@@
      </select>
    </div>
    <label class="opt" for="translate">
      <span class="switch"><input id="translate" type="checkbox"><span></span></span>
      <span style="flex:1;color:var(--text);font-size:13px">Translate captions to English
        <small>Speak any language, get English subtitles</small></span>
    </label>
    <label class="opt" id="vocabRow" for="useVocab">
      <span class="switch"><input id="useVocab" type="checkbox"><span></span></span>
      <span style="flex:1;color:var(--text);font-size:13px">Use app vocabulary
        <small>Clearer for fast speech, but may hide mispronunciations (edit vocabulary.txt)</small></span>
    </label>
    <div class="progress" id="upProg" hidden><i id="upBar"></i></div>
    <div id="upStatus"></div>
  </div>
  <div class="features">
    <div class="feat"><div class="ic">🎙️</div><b>Accurate speech-to-text</b><span>large-v3-turbo, word timings</span></div>
    <div class="feat"><div class="ic">✨</div><b>25 caption styles</b><span>incl. Premium YouTube &amp; meme looks</span></div>
    <div class="feat"><div class="ic">🔤</div><b>Any font</b><span>every font on this Mac, incl. Office</span></div>
    <div class="feat"><div class="ic">🔒</div><b>Private</b><span>nothing leaves your computer</span></div>
  </div>
</div>

<main id="editView" hidden>
  <section class="card viewer">
    <div class="bar"><b>Preview</b><span class="hint">Drag the caption to move it · click a caption time to jump</span>
      <button id="fsBtn" type="button" class="btn2" title="Fullscreen with captions (F)">⛶ Fullscreen</button></div>
    <div class="stageWrap">
      <div id="stage">
        <video id="vid" controls playsinline controlslist="nofullscreen" disablepictureinpicture></video>
        <button id="fsExit" type="button" title="Exit fullscreen (Esc)">✕ Exit fullscreen</button>
        <div id="cap" class="hidden"><div id="capBox"><div id="capText"></div></div></div>
        <div id="capEmoji" hidden></div>
        <div id="safe" hidden>
          <div class="sz" style="left:0;right:0;top:0;height:8%"><span>Top bar / header</span></div>
          <div class="sz" style="right:0;top:38%;width:15%;height:44%"><span>Like · comment · share</span></div>
          <div class="sz" style="left:0;right:0;bottom:0;height:17%"><span>Username · description · music</span></div>
          <div class="good" style="left:6%;right:17%;top:48%;height:30%"><span>Safe: centre / lower-middle third</span></div>
        </div>
      </div>
    </div>
  </section>

  <div id="splitter" role="separator" aria-orientation="vertical" tabindex="0"
       title="Drag to resize the preview · double-click to reset"></div>

  <aside class="card panel">
    <div id="wordBar" hidden>
      <div class="wbTitle">✏️ Editing <b id="wbWords"></b> <span class="wbHint">· the tabs below now change only these words</span></div>
      <div class="wbRow">
        <label><input type="radio" name="wbScope" value="caption" checked> Only here</label>
        <label><input type="radio" name="wbScope" value="global"> <span id="wbGlobal">Every time it appears</span></label>
        <span style="flex:1"></span>
        <button type="button" id="wbReset">Reset word</button>
        <button type="button" id="wbDone">Done</button>
      </div>
    </div>
    <nav class="tabs">
      <button class="tab on" data-tab="style">✨ Style</button>
      <button class="tab" data-tab="font">🔤 Font</button>
      <button class="tab" data-tab="layout">📐 Layout</button>
      <button class="tab" data-tab="colors">🎨 Colors</button>
      <button class="tab" data-tab="motion">🎬 Motion</button>
      <button class="tab" data-tab="captions">💬 Captions <span class="count" id="capCount">0</span></button>
    </nav>
    <div class="panes">
      <div class="pane on" data-pane="style">
        <h4>Caption style</h4>
        <div class="sub">Basic looks, ★ Premium animated styles and ★★ Ultra Premium effects.</div>
        <label class="opt" for="emojiToggle" style="margin:0 0 12px;justify-content:flex-start">
          <span class="switch"><input id="emojiToggle" type="checkbox"><span></span></span>
          <span style="color:var(--text);font-size:13px">Auto emoji above the key word
            <small>Works with every style. Change or remove per caption in the Captions tab.</small></span>
        </label>
        <div id="styles"></div>
      </div>

      <div class="pane" data-pane="font">
        <h4>Font</h4>
        <div class="sub">Works with every style. Effects and animation are kept.</div>
        <input id="fontSearch" type="search" placeholder="Search fonts, e.g. Calibri, Times New Roman">
        <select id="font" size="12"></select>
        <div id="fontNow">Using the style's own font</div>
      </div>

      <div class="pane" data-pane="layout">
        <h4>Size &amp; position</h4>
        <div class="sub">Tip: you can also drag the caption on the video.</div>
        <div class="group">
          <label>Size <span id="sizeVal"></span></label>
          <input id="size" type="range" min="2" max="12" step="0.25" value="6.25">
          <label>Horizontal position <span id="xVal"></span></label>
          <input class="capOnly" id="x" type="range" min="0" max="100" step="0.5" value="50">
          <label>Vertical position <span id="yVal"></span></label>
          <input class="capOnly" id="y" type="range" min="0" max="95" step="0.5" value="62">
        </div>
        <div class="group">
          <label>Words per caption</label>
          <select id="wordsPer" class="sel capOnly">
            <option value="0">Auto (as transcribed)</option>
            <option value="1">1 word at a time</option>
            <option value="2">2 words at a time</option>
            <option value="3">3 words at a time</option>
          </select>
        </div>
        <div class="group">
          <label class="opt" for="safeToggle" style="margin:0;padding:0;border:0;background:none;justify-content:flex-start">
            <span class="switch"><input id="safeToggle" type="checkbox"><span></span></span>
            <span style="color:var(--text);font-size:13px">Show safe zones <small style="color:var(--dim)">TikTok · Reels · Shorts</small></span>
          </label>
          <button id="snapSafe" class="btn2" style="width:100%;margin-top:10px">⤓ Snap caption to safe zone</button>
        </div>
      </div>

      <div class="pane" data-pane="colors">
        <h4>Colors</h4>
        <div class="sub">Picking a style sets these; adjust freely afterwards.</div>
        <div class="colors">
          <div class="swatch"><input id="textColor" type="color" value="#ffffff"><label>Text</label></div>
          <div class="swatch"><input id="boxColor" type="color" value="#000000"><label>Background</label></div>
          <div class="swatch" id="hlRow" hidden><input id="hlColor" type="color" value="#ffe600"><label>Spoken word</label></div>
        </div>
        <div class="group" style="margin-top:12px">
          <label>Background opacity <span id="opVal"></span></label>
          <input id="opacity" type="range" min="0" max="100" step="1" value="75">
          <label style="margin-top:12px">Edge fade (soft edges on all four sides) <span id="featherVal"></span></label>
          <input class="capOnly" id="feather" type="range" min="0" max="100" step="1" value="0">
        </div>
        <div class="group">
          <label>Text glow <span id="glowVal"></span></label>
          <div class="row2"><input id="glow" type="range" min="0" max="100" step="1" value="0" style="flex:1">
            <input id="glowColor" type="color" value="#ffffff" title="Glow colour"></div>
        </div>
      </div>

      <div class="pane" data-pane="motion">
        <h4>Animation</h4>
        <div class="sub">How each caption appears. Works with every style and font. Hover a tile to preview it.</div>
        <div id="anims"></div>
        <div class="group" id="scopeRow" style="margin-top:12px">
          <label>Apply to</label>
          <div class="seg" id="animScope" role="group" aria-label="Apply animation to">
            <button type="button" data-v="line" class="on">Whole line</button>
            <button type="button" data-v="word">Each word</button>
          </div>
          <div class="sub" id="scopeNote" style="margin:6px 0 0"></div>
        </div>
        <div class="group" style="margin-top:12px">
          <label>Animation length <span id="animDurVal"></span></label>
          <input id="animDur" type="range" min="0.1" max="1" step="0.05" value="0.3">
        </div>
        <button id="replay">▶ Replay on preview</button>
        <div class="group" style="margin-top:12px">
          <label>Sound effect on each entrance</label>
          <div class="row2"><select id="sfx" class="sel capOnly"></select><button id="sfxTest" class="btn2">▶ Test</button></div>
          <div class="sub" style="margin:6px 0 0">Plays per caption, per word for word-synced styles, per letter for Typewriter + Click. Mixed into the exported audio.</div>
        </div>
      </div>

      <div class="pane" data-pane="captions">
        <h4>Captions</h4>
        <div class="sub">Click a time to jump there. Edit text to fix mistakes; ⚠ marks unclear words.</div>
        <div id="list"></div>
      </div>
    </div>
    <div class="footer">
      <button id="export">⬇ Export captioned video</button>
      <button id="another">↺ Caption another video</button>
      <div id="status"></div>
    </div>
  </aside>
</main>
<div id="wordPanel" hidden>
  <h5 id="wpTitle">Style word</h5>
  <div class="wsub">Changes show on the video right away and are saved automatically. Shift-click more words to add them.</div>
  <div class="wrow"><span>Colour</span><div class="wctl"><input type="checkbox" id="wpColorOn"><input type="color" id="wpColor" value="#ffe600"></div></div>
  <div class="wrow"><span>Size</span><div class="wctl"><input type="range" id="wpSize" min="50" max="250" step="5" value="100" style="flex:1"><span class="wval" id="wpSizeVal">100%</span></div></div>
  <div class="wrow"><span>Font</span><select id="wpFont"><option value="">Caption's font</option></select></div>
  <div class="wrow"><span>Motion</span><select id="wpMotion">
    <option value="none">None</option><option value="pop">Pop (when spoken)</option><option value="shake">Shake</option>
    <option value="bounce">Bounce</option><option value="wiggle">Wiggle</option><option value="pulse">Pulse (all the time)</option></select></div>
  <div class="wrow"><span>Glow</span><div class="wctl"><input type="range" id="wpGlow" min="0" max="100" step="1" value="0" style="flex:1"><span class="wval" id="wpGlowVal">0%</span><input type="color" id="wpGlowColor" value="#00e5ff"></div></div>
  <div class="wrow"><span>Highlighter</span><div class="wctl"><input type="checkbox" id="wpMarkerOn"><input type="color" id="wpMarker" value="#ffe600"></div></div>
  <div class="wrow"><span>Underline</span><div class="wctl"><input type="checkbox" id="wpUnderOn"><input type="color" id="wpUnder" value="#00e5ff"></div></div>
  <div class="wrow"><span>Background pill</span><div class="wctl"><input type="checkbox" id="wpPillOn"><input type="color" id="wpPill" value="#ff2b6d"></div></div>
  <div class="wscope">
    <label style="display:flex;gap:6px;margin:0;color:var(--text)"><input type="radio" name="wpScope" value="caption" checked> Only here (this caption)</label>
    <label style="display:flex;gap:6px;margin:0;color:var(--text)"><input type="radio" name="wpScope" value="global"> <span id="wpGlobalLbl">Every time this word appears</span></label>
  </div>
  <div class="wbtns"><button type="button" class="wClear" id="wpClear">Reset word</button><button type="button" class="wClose" id="wpClose">Close</button><button type="button" class="wApply" id="wpApply">Done</button></div>
</div>
<div id="resume" hidden>
  <div class="card dlg">
    <h3>Continue where you left off?</h3>
    <p id="resumeInfo">Your last video and caption settings are still saved on this Mac.</p>
    <div class="acts"><button id="resumeNo" type="button">Start new</button><button id="resumeYes" type="button">Resume</button></div>
  </div>
</div>
<script>
const $ = id => document.getElementById(id);
let sid = null, captions = [];

async function pollJob(job, onLog) {
  while (true) {
    const r = await fetch('/api/job?job=' + job);
    const j = await r.json();
    if (j.log) onLog(j.log.join('\n'));
    if (j.state === 'done') return j.result;
    if (j.state === 'error') throw new Error(j.error);
    await new Promise(res => setTimeout(res, 800));
  }
}

// ---------- upload ----------
const drop = $('drop'), file = $('file');
drop.onclick = () => file.click();
drop.ondragover = e => { e.preventDefault(); drop.classList.add('over'); };
drop.ondragleave = () => drop.classList.remove('over');
drop.ondrop = e => { e.preventDefault(); drop.classList.remove('over'); if (e.dataTransfer.files[0]) upload(e.dataTransfer.files[0]); };
file.onchange = () => file.files[0] && upload(file.files[0]);

function upload(f) {
  const st = $('upStatus');
  st.textContent = 'Uploading ' + f.name + ' ...';
  const xhr = new XMLHttpRequest();
  xhr.open('POST', '/api/upload');
  xhr.setRequestHeader('X-Filename', f.name);
  xhr.setRequestHeader('X-Use-Vocab', $('useVocab').checked ? '1' : '0');
  xhr.setRequestHeader('X-Language', $('lang').value);
  xhr.setRequestHeader('X-Translate', $('translate').checked ? '1' : '0');
  xhr.upload.onprogress = e => { if (e.lengthComputable) st.textContent = `Uploading ${f.name} ... ${Math.round(100 * e.loaded / e.total)}%`; };
  xhr.onload = async () => {
    const res = JSON.parse(xhr.responseText || '{}');
    if (xhr.status !== 200) { st.textContent = 'Error: ' + (res.error || xhr.status); return; }
    try {
      st.textContent = 'Creating captions ...';
      await pollJob(res.job, log => st.textContent = 'Creating captions ...\n' + log);
      openEditor(res.id);
    } catch (e) { st.textContent = 'Error: ' + e.message; }
  };
  xhr.onerror = () => st.textContent = 'Upload failed.';
  xhr.send(f);
}

// vocabulary.txt is English only
$('lang').onchange = $('translate').onchange = () => {
  const en = $('lang').value === 'en' && !$('translate').checked;
  $('vocabRow').style.opacity = en ? 1 : 0.4;
  $('useVocab').disabled = !en;
  if (!en) $('useVocab').checked = false;
};

// ---------- editor ----------
async function openEditor(id) {
  sid = id;
  const r = await fetch('/api/captions?id=' + id);
  const _cj = await r.json(); captions = _cj.captions; window.GLOBAL_WORDS = _cj.globalWords || {};
  capsVersion++;
  $('vid').src = '/media/video?id=' + id;
  $('uploadView').hidden = true; $('editView').hidden = false;
  $('list').innerHTML = captions.length ? '' : '<i>No speech detected.</i>';
  captions.forEach((c, i) => {
    const d = document.createElement('div');
    d.className = 'item'; d.id = 'li' + i;
    const t = document.createElement('span');
    t.className = 't'; t.textContent = c.start.toFixed(1) + 's';
    t.onclick = () => { $('vid').currentTime = c.start + 0.01; };
    const inp = document.createElement('input');
    inp.value = c.text;
    inp.onfocus = () => { $('vid').pause(); $('vid').currentTime = c.start + 0.01; };
    inp.oninput = () => { captions[i].text = inp.value; capsVersion++; inp.classList.remove('saved'); };
    inp.onchange = async () => {
      const r = await fetch('/api/captions', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                               body: JSON.stringify({ id: sid, index: i, text: inp.value }) });
      if (r.ok) {
        inp.classList.add('saved');
        const j = await r.json(); if (j.kw) { captions[i].kw = j.kw; capsVersion++; renderKey = ''; d._drawKw && d._drawKw(); }
      } else alert('Could not save: ' + ((await r.json()).error || r.status));
    };
    d.append(t, inp);
    const kr = document.createElement('div'); kr.className = 'kwrow';
    const drawKw = () => {
      kr.textContent = '';
      const lb = document.createElement('span'); lb.className = 'lbl'; lb.textContent = 'Key words:'; kr.appendChild(lb);
      captions[i].text.split(/\s+/).filter(Boolean).forEach((w, wi) => {
        const ch = document.createElement('span');
        ch.className = 'chip' + ((captions[i].kw || []).includes(wi) ? ' kw' : ''); ch.textContent = w;
        ch.title = 'Click to mark / unmark as a key word';
        ch.onclick = () => {
          let cur = new Set(captions[i].kw || []);
          cur.has(wi) ? cur.delete(wi) : cur.add(wi);
          if (!cur.size) return;  // keep at least one key word
          if (STYLES[curStyle] && STYLES[curStyle].mode === 'keyword') {
            // Keyword Hero shows ONE big line: key words are a continuous run of words
            const lo = Math.min(...cur), hi = Math.max(...cur);
            cur = new Set(Array.from({ length: hi - lo + 1 }, (_, k) => lo + k));
            if (cur.size > 6) return alert('Pick up to 6 words in a row for Keyword Hero.');
          }
          saveKw([...cur]);
        };
        kr.appendChild(ch);
      });
      const el = document.createElement('span'); el.className = 'lbl'; el.textContent = ' Emoji:'; el.style.marginLeft = '8px';
      const ei = document.createElement('input'); ei.className = 'emo'; ei.value = captions[i].emoji || ''; ei.placeholder = '—';
      ei.title = 'Emoji shown above the key word. Clear it to go back to automatic.';
      ei.onchange = () => saveEmoji(ei.value.trim() === '' ? null : ei.value.trim());
      const en = document.createElement('button'); en.className = 'auto'; en.type = 'button'; en.textContent = '✕';
      en.title = 'No emoji for this caption'; en.onclick = () => saveEmoji('');
      kr.append(el, ei, en);
      const au = document.createElement('button'); au.className = 'auto'; au.type = 'button'; au.textContent = '↺ Auto';
      au.title = 'Go back to the automatically detected key word'; au.onclick = () => saveKw(null);
      kr.appendChild(au);
    };
    const saveEmoji = async emoji => {
      const r = await fetch('/api/emoji', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                            body: JSON.stringify({ id: sid, index: i, emoji }) });
      const j = await r.json();
      if (!r.ok) { alert(j.error || r.status); drawKw(); return; }
      captions[i].emoji = j.emoji; emojiKey = ''; drawKw();
    };
    const saveKw = async kw => {
      const r = await fetch('/api/keywords', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                               body: JSON.stringify({ id: sid, index: i, kw }) });
      const j = await r.json();
      if (!r.ok) { alert(j.error || r.status); return; }
      captions[i].kw = j.kw; capsVersion++; renderKey = ''; drawKw();
    };
    d._drawKw = drawKw;
    drawKw();
    d.append(kr);
    const wr = document.createElement('div'); wr.className = 'kwrow';
    const drawWs = () => {
      wr.textContent = '';
      const lb = document.createElement('span'); lb.className = 'lbl'; lb.textContent = '🎨 Style a word:'; wr.appendChild(lb);
      captions[i].text.split(/\s+/).filter(Boolean).forEach((w, wi) => {
        const ch = document.createElement('span');
        ch.className = 'chip' + ((captions[i].ws || {})[wi] ? ' wst' : ''); ch.textContent = w;
        ch.title = 'Click to style this word (colour, size, font, motion ...)';
        ch.onclick = ev => openWordPanel(i, [wi], ev.clientX, ev.clientY);
        wr.appendChild(ch);
      });
    };
    d._drawWs = drawWs; drawWs(); d.append(wr);
    if (c.check && c.check.length) {
      const w = document.createElement('div');
      w.className = 'warn';
      w.textContent = 'Check (unclear): ' + c.check.join(', ');
      d.appendChild(w);
    }
    $('list').appendChild(d);
  });
  applyStyle();
}

function style() {
  return {
    size: $('size').value / 100,
    y: $('y').value / 100,
    x: $('x').value / 100,
    opacity: $('opacity').value / 100,
    feather: $('feather').value / 100,
    glow: $('glow').value / 100,
    glowColor: $('glowColor').value,
    textColor: $('textColor').value,
    boxColor: $('boxColor').value,
    style: curStyle,
    hlColor: hlTouched ? $('hlColor').value : null,
    fontId: fontId,
    anim: curAnim,
    animScope: animScope,
    wordsPer: parseInt($('wordsPer').value) || 0,
    sfx: $('sfx').value || 'none',
    emoji: $('emojiToggle').checked,
    animDur: parseFloat($('animDur').value),
  };
}

function rgba(hex, a) {
  const n = parseInt(hex.slice(1), 16);
  return `rgba(${n >> 16},${(n >> 8) & 255},${n & 255},${a})`;
}

// Apply a style's look (font, box, outline, shadows/glow, gradient) to a box + text element.
// All effect sizes are fractions of the font size, exactly like the export renderer.
function look(box, txt, st, fs, textColor, boxColor, opacity, fontFace) {
  const sw = Math.round((st.stroke || 0) * fs);
  const pad = fs / 2;
  box.style.padding = `${pad}px ${pad + sw}px`;
  box.style.borderRadius = ((st.radius ?? 0.5) * fs) + 'px';
  box.style.background = opacity > 0 ? rgba(boxColor, opacity) : 'transparent';
  box.classList.toggle('tail', !!st.tail && opacity > 0);
  box.style.textAlign = st.align || 'center';
  txt.style.lineHeight = st.leading || 1.25;
  box.style.setProperty('--boxc', rgba(boxColor, opacity));
  txt.style.fontSize = fs + 'px';
  txt.style.fontFamily = fontFace ? `'${fontFace}'` : st.css.family + ', sans-serif';
  txt.style.fontWeight = fontFace ? 'normal' : st.css.weight;
  txt.style.fontStyle = !fontFace && st.css.italic ? 'italic' : 'normal';
  txt.style.fontStretch = fontFace ? 'normal' : (st.css.stretch || 'normal');
  txt.style.textTransform = st.upper ? 'uppercase' : 'none';
  // outline: ring of hard text-shadows
  const ring = [];
  if (sw) for (let k = 0; k < 16; k++) {
    const a = k * Math.PI / 8;
    ring.push(`${(Math.cos(a) * sw).toFixed(2)}px ${(Math.sin(a) * sw).toFixed(2)}px 0 ${st.stroke_color}`);
  }
  if (st.extrude) {  // 3D depth: stacked hard shadows stepping back diagonally, darkening
    const ex = st.extrude, steps = Math.max(2, Math.round(fs * ex.depth));
    const c0 = parseInt(ex.color.slice(1), 16), c1 = parseInt((ex.dark || ex.color).slice(1), 16);
    for (let j = 1; j <= steps; j++) {
      const f = j / steps, ch = sh => Math.round(((c0 >> sh) & 255) + ((((c1 >> sh) & 255) - ((c0 >> sh) & 255)) * f));
      ring.push(`${(ex.dx * j).toFixed(1)}px ${(ex.dy * j).toFixed(1)}px 0 rgb(${ch(16)},${ch(8)},${ch(0)})`);
    }
  }
  const clipFill = st.gradient || st.liquid || st.glowText;
  txt.style.textShadow = clipFill ? 'none' : (ring.join(',') || 'none');
  // shadows and glows (gradient text gets its outline as tight drop-shadows instead)
  const outline = clipFill && sw ? [0, 1, 2, 3].map(k => {
    const a = k * Math.PI / 2; return `drop-shadow(${(Math.cos(a) * sw).toFixed(1)}px ${(Math.sin(a) * sw).toFixed(1)}px 0 ${st.stroke_color})`; }) : [];
  txt.style.filter = outline.concat((st.shadows || []).map(([dx, dy, b, c, al]) =>
    `drop-shadow(${dx * fs}px ${dy * fs}px ${b * fs}px ${rgba(c, al)})`)).join(' ') || 'none';
  if (st.glowText) {  // letters glow from inside (export: apply_text_glow in transcribe.py)
    const { g, color } = st.glowText, lh = st.leading || 1.25;
    const base = textColor.length === 7 ? textColor : '#ffffff';
    const edge = mixHex(base, color, 0.75 * g);                    // same 0.75*g tint as the export
    const core = mixHex(base, toHex(mixHex(color, '#ffffff', 0.7)), 0.75 * g);
    txt.style.background = `linear-gradient(to bottom, ${edge} 22%, ${core} 50%, ${edge} 78%)`;
    txt.style.backgroundSize = `100% ${lh}em`; txt.style.backgroundRepeat = 'repeat-y';
    txt.style.webkitBackgroundClip = 'text'; txt.style.backgroundClip = 'text';
    txt.style.color = 'transparent';
  } else if (st.liquid) {  // colours flowing through the letters
    const cols = st.liquid.concat(st.liquid[0]).join(', '), ph = parseFloat(txt.dataset.phase || 0);
    txt.style.background = `linear-gradient(90deg, ${cols})`;
    txt.style.backgroundSize = '200% 100%'; txt.style.backgroundPosition = `${(ph * 200) % 200}% 0`;
    txt.style.webkitBackgroundClip = 'text'; txt.style.backgroundClip = 'text';
    txt.style.color = 'transparent';
  } else if (st.gradient) {
    const sh = txt.dataset.shine;
    txt.style.background = (sh !== undefined && sh !== ''
      ? `linear-gradient(105deg, transparent ${-15 + 115 * sh}%, rgba(255,255,255,.97) ${-9 + 115 * sh}%, transparent ${-3 + 115 * sh}%), ` : '')
      + `linear-gradient(${textColor}, ${st.gradient})`;
    txt.style.webkitBackgroundClip = 'text'; txt.style.backgroundClip = 'text';
    txt.style.color = 'transparent';
  } else {
    txt.style.background = 'none';
    txt.style.webkitBackgroundClip = 'border-box'; txt.style.backgroundClip = 'border-box';
    txt.style.color = textColor;
  }
}

// Preview mirrors the export: font = video width * size, box top at y * height, centre at x * width.
function applyStyle() {
  if (!STYLES) return;
  const s = style(), v = $('vid'), cap = $('cap');
  const w = v.clientWidth || 1, fs = w * s.size * (STYLES[s.style].scale || 1);
  look($('capBox'), $('capText'), withGlow(STYLES[s.style]), fs, s.textColor, s.boxColor, s.opacity, fontFace);
  $('capBox').style.maxWidth = (0.9 * w) + 'px';
  cap.style.top = (v.offsetTop + s.y * v.clientHeight) + 'px';
  const sz = $('safe');
  sz.style.left = v.offsetLeft + 'px'; sz.style.top = v.offsetTop + 'px';
  sz.style.width = v.clientWidth + 'px'; sz.style.height = v.clientHeight + 'px';
  // horizontal centre at x, kept inside the frame (same rule as the export)
  const cw = cap.offsetWidth;
  cap.style.left = (v.offsetLeft + Math.max(0, Math.min(w - cw, s.x * w - cw / 2))) + 'px';
  $('xVal').textContent = $('x').value + '%';
  $('sizeVal').textContent = $('size').value + '%';
  $('yVal').textContent = $('y').value + '%';
  $('opVal').textContent = $('opacity').value + '%';
  $('featherVal').textContent = $('feather').value + '%';
  $('glowVal').textContent = $('glow').value + '%';
  applyFeather();
}

// ---------- style gallery ----------
let STYLES = null, curStyle = 'classic', hlTouched = false, renderKey = '';
$('hlColor').oninput = () => { hlTouched = true; renderKey = ''; };
$('textColor').addEventListener('input', () => { renderKey = ''; });

// same rule as the server: use word timings if they still match the (possibly edited) text,
// otherwise spread the caption's time across its words by length
function wordTimes(c) {
  const words = c.text.split(/\s+/).filter(Boolean);
  if (c.times && c.times.length === words.length) return c.times;
  const total = words.reduce((n, w) => n + w.length, 0) || 1;
  let t = c.start; const out = [];
  for (const w of words) { const d = (c.end - c.start) * w.length / total; out.push([t, t + d]); t += d; }
  return out;
}
function hlFor(st, i) {
  if (hlTouched) return $('hlColor').value;
  return Array.isArray(st.hl) ? st.hl[i % st.hl.length] : (st.hl || '#ffe600');
}
// draw one animation state in the preview (mirrors render_words in transcribe.py)
function renderAnimated(text, wi, st) { renderInto($('capText'), text, wi, st); }
function renderInto(el, text, wi, st) {
  el.textContent = '';
  let words = text.split(/\s+/).filter(Boolean), ids = words.map((_, i) => i);
  if (st.mode === 'one_word') { words = [words[wi]]; ids = [wi]; }
  words.forEach((w, k) => {
    const i = ids[k], sp = document.createElement('span');
    sp.className = 'w'; sp.textContent = w;
    if ((st.mode === 'highlight' && i === wi) || (st.mode === 'fill' && i <= wi)) sp.style.color = hlFor(st, i);
    if (st.mode === 'box' && i === wi) {
      const c = hlFor(st, i);
      sp.style.background = c; sp.style.boxShadow = `0 0 0 0.18em ${c}`;
      sp.style.borderRadius = '0.2em'; sp.style.color = st.hl_text || '';
    }
    if (st.mode === 'reveal' && i > wi) sp.style.visibility = 'hidden';
    el.appendChild(sp);
    if (k < words.length - 1) el.appendChild(document.createTextNode(' '));
  });
}

function pickStyle(id) {
  curStyle = id;
  const st = STYLES[id];
  hlTouched = false; renderKey = '';
  $('hlRow').hidden = !st.mode;
  if (st.mode) $('hlColor').value = Array.isArray(st.hl) ? st.hl[0] : (st.hl || '#ffe600');
  $('textColor').value = st.text;
  if (st.words_per) $('wordsPer').value = String(st.words_per);
  if (st.emoji !== undefined) $('emojiToggle').checked = !!st.emoji;
  if (st.anim && typeof pickAnim === 'function') {
    $('animDur').value = st.anim_dur || 0.3; $('animDur').oninput(); pickAnim(st.anim);
  }
  $('boxColor').value = st.box;
  $('opacity').value = Math.round(st.opacity * 100);
  document.querySelectorAll('.tile').forEach(t => t.classList.toggle('on', t.dataset.id === id));
  if (typeof refreshScope === 'function') refreshScope();
  applyStyle();
}
fetch('/api/styles').then(r => r.json()).then(styles => {
  STYLES = styles;
  const section = (label, cls) => {
    const h = document.createElement('div'); h.className = 'sect ' + cls; h.textContent = label;
    $('styles').appendChild(h);
  };
  section('Basic', '');
  let premiumShown = false, ultraShown = false;
  for (const [id, st] of Object.entries(styles)) {
    if (st.ultra && !ultraShown) { section('★★ Ultra Premium', 'ultra'); ultraShown = true; }
    else if (st.premium && !st.ultra && !premiumShown) { section('★ Premium: YouTube & meme styles', 'premium'); premiumShown = true; }
    const tile = document.createElement('div');
    tile.className = 'tile'; tile.dataset.id = id;
    tile.title = st.name + (st.mode ? ' (animated: ' + st.mode.replace('_', ' ') + ')' : '');
    const b = document.createElement('span'); b.className = 'b';
    const t = document.createElement('span');
    b.appendChild(t); tile.appendChild(b);
    if (KINETIC.has(st.mode)) {  // kinetic demo: a frame from the middle of the effect
      const label = st.mode === 'matchcut' ? 'Match' : st.mode === 'typewriter' ? 'Typewriter'
                  : st.mode === 'keyword' ? 'the Keyword hero' : st.mode === 'keyword_inline' ? 'Key word' : st.name;
      const c = { start: 0, end: 2, text: label, times: label.split(' ').map((_, k, a) => [k * 2 / a.length, (k + 1) * 2 / a.length]) };
      renderKineticInto(t, kineticState(c, st.mode === 'typewriter' ? 1.6 : 1.99, st).toks, st);
    } else if (st.mode) {  // show the animation idea on the tile (2nd word "spoken")
      const label = st.mode === 'one_word' ? st.name.split(' ')[0] : st.name;
      const two = label.split(' ').length > 1 ? label : label + ' style';
      renderInto(t, two, 1, st);
    } else t.textContent = st.name;
    look(b, t, st, 15, st.text, st.box, st.opacity);
    if (st.premium) { const s = document.createElement('span'); s.className = 'star'; s.textContent = st.ultra ? '★★' : '★'; tile.appendChild(s); }
    if (st.ultra) tile.classList.add('ultraTile');
    tile.onclick = () => pickStyle(id);
    $('styles').appendChild(tile);
  }
  pickStyle('classic');
});

['size', 'x', 'y', 'opacity', 'feather', 'glow', 'glowColor', 'textColor', 'boxColor'].forEach(id => $(id).oninput = applyStyle);
window.onresize = applyStyle;
$('vid').onloadedmetadata = applyStyle;

// show the caption for the current time (show the first one while paused at 0 so styling is visible)
function tick() {
  const v = $('vid'), t = v.currentTime, caps = dispCaps();
  let idx = caps.findIndex(c => t >= c.start && t <= c.end);
  if (idx < 0 && v.paused && caps.length && t < caps[0].start) idx = 0;
  const cap = $('cap');
  if (idx >= 0) {
    const c = caps[idx], st = STYLES && STYLES[curStyle];
    let key, sk = String(idx);
    if (typeof fxMode === 'function' && fxMode(st)) {
      const k = fxFrame(c, t, st, idx);
      sk = idx + ':' + k.wi;
    } else if (st && c.ws && Object.keys(c.ws).length) {
      const k = tokensForAny(c, t, st), wk = applyWordProps(k.toks, c, t);
      key = [idx, 'ws', JSON.stringify(k.key), JSON.stringify(wk), JSON.stringify(c.ws), c.text, curStyle, $('hlColor').value, $('textColor').value, fontFace].join('|');
      if (key !== renderKey) { renderKineticInto($('capText'), k.toks, st); renderKey = key; applyStyle(); markWordSel(c); }
      if (String(idx) !== animKey) { animKey = String(idx); playAnim(); }
      sk = idx + ':' + k.wi;
    } else if (st && KINETIC.has(st.mode)) {
      const k = kineticState(c, t, st);
      key = [idx, JSON.stringify(k.key), c.text, curStyle, $('hlColor').value, $('textColor').value, fontFace].join('|');
      if (key !== renderKey) { renderKineticInto($('capText'), k.toks, st); renderKey = key; applyStyle(); }
      if (String(idx) !== animKey) { animKey = String(idx); playAnim(); }
      sk = st.mode === 'typewriter' && curSfx === 'click' ? idx + ':' + k.chars : idx + ':' + k.wi;
    } else if (st && st.mode) {
      const times = wordTimes(c);
      let wi = 0;
      for (let k = 1; k < times.length; k++) if (t >= times[k][0]) wi = k;
      key = [idx, wi, c.text, curStyle, $('hlColor').value, $('textColor').value].join('|');
      if (key !== renderKey) { renderAnimated(c.text, wi, st); renderKey = key; applyStyle(); }
      const ak = st.mode === 'one_word' ? idx + ':' + wi : String(idx);
      if (ak !== animKey) { animKey = ak; playAnim(); }
      sk = idx + ':' + wi;
    } else {
      key = [idx, c.text, curStyle].join('|');
      if (key !== renderKey) { delete $('capText').dataset.shine; renderPlainWords($('capText'), c.text); renderKey = key; applyStyle(); markWordSel(c); }
      if (String(idx) !== animKey) { animKey = String(idx); playAnim(); }
    }
    if (sk !== sfxKey) { if (!v.paused && sfxKey !== '') playSfx(curSfx); sfxKey = sk; }
    cap.classList.remove('hidden');
    hlSrc = c.src;
  }
  else { cap.classList.add('hidden'); animKey = ''; sfxKey = ''; hlSrc = -1; }
  document.querySelectorAll('#list .item').forEach((d, i) => d.classList.toggle('on', i === hlSrc));
  updateEmoji(t);
  requestAnimationFrame(tick);
}
requestAnimationFrame(tick);

// drag the caption anywhere to position it
$('cap').onpointerdown = e => {
  const v = $('vid'), cap = $('cap');
  cap.setPointerCapture(e.pointerId);
  const startX = e.clientX, startY = e.clientY;
  const startXVal = parseFloat($('x').value), startYVal = parseFloat($('y').value);
  cap.onpointermove = m => {
    $('x').value = Math.max(0, Math.min(100, startXVal + (m.clientX - startX) / v.clientWidth * 100));
    $('y').value = Math.max(0, Math.min(95, startYVal + (m.clientY - startY) / v.clientHeight * 100));
    applyStyle();
  };
  cap.onpointerup = () => { cap.onpointermove = null; };
};

$('export').onclick = async () => {
  const btn = $('export'), st = $('status');
  btn.disabled = true; st.textContent = 'Exporting ...';
  try {
    const r = await fetch('/api/export', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                           body: JSON.stringify({ id: sid, ...style() }) });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || r.status);
    const res = await pollJob(j.job, log => st.textContent = log);
    st.innerHTML = 'Done. <a href="' + res.download + '">Download captioned video</a>';
    window.location.href = res.download;
  } catch (e) { st.textContent = 'Error: ' + e.message; }
  btn.disabled = false;
};

$('another').onclick = () => { $('vid').pause(); $('editView').hidden = true; $('uploadView').hidden = false; $('upStatus').textContent = ''; file.value = ''; };

// ---------- font picker ----------
let FONTS = [], fontId = null, fontFace = null;
function fillFonts() {
  const q = $('fontSearch').value.trim().toLowerCase(), sel = $('font');
  sel.textContent = '';
  const def = new Option("Style's own font", '');
  sel.add(def);
  let n = 0;
  for (const f of FONTS) {
    const label = f.family + (f.style && f.style !== 'Regular' ? ' (' + f.style + ')' : '');
    if (q && !label.toLowerCase().includes(q)) continue;
    const o = new Option(label, f.id);
    sel.add(o);
    if (++n >= 400) break;  // keep the list snappy; search narrows it
  }
  sel.value = fontId === null ? '' : String(fontId);
}
async function pickFont(id) {
  if (id === '' || id === null) {
    fontId = null; fontFace = null;
    $('fontNow').textContent = "Using the style's own font";
  } else {
    id = Number(id);
    const f = FONTS.find(x => x.id === id);
    $('fontNow').textContent = 'Loading ' + f.family + ' ...';
    const name = 'csfont' + id;
    if (![...document.fonts].some(ff => ff.family === name)) {
      const face = new FontFace(name, `url(/api/fontfile?id=${id})`);
      await face.load();
      document.fonts.add(face);
    }
    fontId = id; fontFace = name;
    $('fontNow').textContent = 'Font: ' + f.family + (f.style !== 'Regular' ? ' ' + f.style : '');
  }
  renderKey = '';
  applyStyle();
}
$('fontSearch').oninput = fillFonts;
$('font').onchange = () => pickFont($('font').value).catch(e => $('fontNow').textContent = 'Could not load font: ' + e.message);
fetch('/api/fonts').then(r => r.json()).then(list => { FONTS = list; fillFonts(); });

// ---------- UI chrome (tabs, steps, upload progress) — presentation only ----------
document.querySelectorAll('.tab').forEach(t => t.onclick = () => {
  document.querySelectorAll('.tab').forEach(x => x.classList.toggle('on', x === t));
  document.querySelectorAll('.pane').forEach(p => p.classList.toggle('on', p.dataset.pane === t.dataset.tab));
});
function setStep(n) {
  [1, 2, 3].forEach(i => { const s = $('step' + i); s.classList.toggle('on', i === n); s.classList.toggle('done', i < n); });
}
new MutationObserver(() => { setStep($('editView').hidden ? 1 : 2); requestAnimationFrame(applyStyle); })
  .observe($('editView'), { attributes: true, attributeFilter: ['hidden'] });
new MutationObserver(() => { $('capCount').textContent = document.querySelectorAll('#list .item').length; })
  .observe($('list'), { childList: true });
new MutationObserver(() => {
  const txt = $('upStatus').textContent, prog = $('upProg'), bar = $('upBar');
  const m = txt.match(/Uploading .* (\d+)%/);
  prog.hidden = !txt || txt.startsWith('Error');
  prog.classList.toggle('busy', !m && txt.startsWith('Creating'));
  if (m) bar.style.width = m[1] + '%';
}).observe($('upStatus'), { childList: true, characterData: true, subtree: true });
new MutationObserver(() => {
  const t = $('status').textContent;
  setStep(t.startsWith('Exporting') || t.startsWith('drawing') || t.startsWith('Done') ? 3 : 2);
}).observe($('status'), { childList: true, characterData: true, subtree: true });

// ---------- motion (entrance animations) ----------
let curAnim = 'none', animKey = '', animScope = 'line';
// animation tiles, playAnim/pickAnim and the letter/word effects live in effects.js

// ---------- short-form trend features ----------
const KINETIC = new Set(['pop', 'focus', 'poster', 'typewriter', 'matchcut', 'keyword', 'keyword_inline']);
const KEYWORD = new Set(['keyword', 'keyword_inline']);
['hormozi2', 'shine', 'marker', 'sticker', 'extrude', 'underline', 'neon_flicker', 'liquid', 'chaos'].forEach(m => KINETIC.add(m));
const CHAOS_TILT = [-8, 6, -5, 9, -7, 4, -9, 7];
const NEON_FLICKER = [[0.05, 0.1], [0.10, 1], [0.16, 0.15], [0.26, 1], [0.31, 0.2], [0.42, 0.9], [0.47, 0.3], [1e9, 1]];
function easeBounce(t) { const n = 7.5625, d = 2.75;
  if (t < 1 / d) return n * t * t; if (t < 2 / d) { t -= 1.5 / d; return n * t * t + .75; }
  if (t < 2.5 / d) { t -= 2.25 / d; return n * t * t + .9375; } t -= 2.625 / d; return n * t * t + .984375; }
const STICKER_TILT = [-4, 3, -2, 4, -3, 2, -5, 3];
let capsVersion = 0, _disp = null, _dispKey = '', hlSrc = -1, sfxKey = '', curSfx = 'none';

// captions as shown: optionally re-chunked into N words (same rule as split_captions in Python)
function dispCaps() {
  const kwStyle = STYLES && STYLES[curStyle] && KEYWORD.has(STYLES[curStyle].mode);
  const n = kwStyle ? 0 : (parseInt($('wordsPer').value) || 0), k = n + ':' + capsVersion + ':' + captions.length;
  if (k === _dispKey && _disp) return _disp;
  const out = [];
  captions.forEach((c, i) => {
    const words = c.text.split(/\s+/).filter(Boolean), times = wordTimes(c);
    if (!n) { out.push({ ...c, times, src: i, off: 0 }); return; }
    for (let g = 0; g < words.length; g += n) {
      const s = g === 0 ? c.start : times[g][0];
      const e = g + n < words.length ? times[g + n][0] : c.end;
      const ws = {}; Object.entries(c.ws || {}).forEach(([k, v]) => { k = +k; if (k >= g && k < g + n) ws[k - g] = v; });
      if (e - s > 0.01) out.push({ start: s, end: e, text: words.slice(g, g + n).join(' '), times: times.slice(g, g + n), src: i, off: g, ws, kw: c.kw });
    }
  });
  _disp = out; _dispKey = k;
  return out;
}

// mirror of kinetic_tokens() in transcribe.py
function kineticState(c, t, st) {
  const words = c.text.split(/\s+/).filter(Boolean), times = wordTimes(c), n = words.length;
  let wi = 0;
  for (let k = 1; k < n; k++) if (t >= times[k][0]) wi = k;
  const toks = [];
  if (st.mode === 'extrude') { words.forEach(w => toks.push({ text: w })); return { key: ['static'], toks, wi }; }
  if (st.mode === 'underline') {
    let kw = (c.kw || []).filter(i => i >= 0 && i < n);
    if (!kw.length) { let b = 0; words.forEach((w, i) => { if (w.length > words[b].length) b = i; }); kw = [b]; }
    const prog = [];
    words.forEach((w, k) => { const tk = { text: w };
      if (kw.includes(k)) { const p = Math.min(1, Math.max(0, (t - (k ? times[k][0] : c.start)) / 0.3));
        prog.push(Math.round(p * 100)); tk.underline = { color: hlFor(st, k), p }; }
      toks.push(tk); });
    return { key: [kw, prog], toks, wi };
  }
  if (st.mode === 'neon_flicker') {
    const dt = t - c.start, a = NEON_FLICKER.find(([u]) => dt < u)[1];
    words.forEach(w => toks.push({ text: w, alpha: a }));
    return { key: [a], toks, wi };
  }
  if (st.mode === 'liquid') {
    const ph = Math.round(((t * 0.6) % 1) * 40) / 40;
    words.forEach(w => toks.push({ text: w })); toks[0].fx_phase = ph;
    return { key: [ph], toks, wi };
  }
  if (st.mode === 'chaos') {
    const ws = wi ? times[wi][0] : c.start, q = Math.min(1, Math.max(0, (t - ws) / 0.35)), pal = st.palette || [];
    words.forEach((w, k) => { const tk = { text: w, visible: k <= wi, rot: CHAOS_TILT[k % CHAOS_TILT.length],
      color: (k % pal.length) ? pal[k % pal.length] : null };
      if (k === wi) tk.dy = -1.1 * (1 - easeBounce(q));
      toks.push(tk); });
    return { key: [wi, Math.round(q * 100)], toks, wi };
  }
  if (st.mode === 'hormozi2') {
    const ws = wi ? times[wi][0] : c.start, q = Math.min(1, Math.max(0, (t - ws) / 0.2));
    const sc = q < 0.55 ? 0.85 + 0.27 * (q / 0.55) : 1.12 - 0.12 * ((q - 0.55) / 0.45);
    words.forEach((w, k) => { const on = k === wi;
      toks.push({ text: w, color: on ? hlFor(st, k) : null, scale: on ? sc : 1, rot: on ? (k % 2 ? -3 : 3) : 0 }); });
    return { key: [wi, Math.round(q * 100)], toks, wi };
  }
  if (st.mode === 'shine') {
    const p = (t - c.start - 0.15) / 0.7;
    words.forEach(w => toks.push({ text: w }));
    if (p >= 0 && p <= 1) toks[0].fx_shine = Math.round(p * 1000) / 1000;
    return { key: [p >= 0 && p <= 1 ? Math.round(p * 1000) : (p < 0 ? 'pre' : 'done')], toks, wi };
  }
  if (st.mode === 'marker') {
    let kw = (c.kw || []).filter(i => i >= 0 && i < n);
    if (!kw.length) { let b = 0; words.forEach((w, i) => { if (w.length > words[b].length) b = i; }); kw = [b]; }
    const prog = [];
    words.forEach((w, k) => {
      if (kw.includes(k)) {
        const p = Math.min(1, Math.max(0, (t - (k ? times[k][0] : c.start)) / 0.35)); prog.push(Math.round(p * 100));
        toks.push({ text: w, color: p > 0.5 ? (st.hl_text || '#111') : null, marker: { color: hlFor(st, k), p } });
      } else toks.push({ text: w });
    });
    return { key: [kw, prog], toks, wi };
  }
  if (st.mode === 'sticker') {
    const ws = wi ? times[wi][0] : c.start, q = Math.min(1, Math.max(0, (t - ws) / 0.22));
    const sc = q < 0.6 ? 0.3 + 0.82 * (q / 0.6) : 1.12 - 0.12 * ((q - 0.6) / 0.4);
    words.forEach((w, k) => toks.push({ text: w, visible: k <= wi, sticker: hlFor(st, k),
                                        rot: STICKER_TILT[k % STICKER_TILT.length], scale: k === wi ? sc : 1 }));
    return { key: [wi, Math.round(q * 100)], toks, wi };
  }
  if (KEYWORD.has(st.mode)) {
    let kw = (c.kw || []).filter(i => i >= 0 && i < n);
    if (!kw.length) { let b = 0; words.forEach((w, i) => { if (w.length > words[b].length) b = i; }); kw = [b]; }
    const lo = Math.min(...kw), hi = Math.max(...kw), ks = lo ? times[lo][0] : c.start, q = (t - ks) / 0.22;
    let sc = 1, phase = q < 0 ? 'pre' : 'done';
    if (q >= 0 && q < 1) { sc = q < 0.5 ? 1 + 0.08 * (q / 0.5) : 1.08 - 0.08 * ((q - 0.5) / 0.5); phase = Math.round(q * 100); }
    const title = w => st.title ? w.charAt(0).toUpperCase() + w.slice(1) : w, big = hlFor(st, 0);
    if (st.mode === 'keyword') {
      let line = 0;
      if (lo > 0) { words.slice(0, lo).forEach(w => toks.push({ text: title(w), line, mult: st.small_mult || 0.42 })); line++; }
      words.slice(lo, hi + 1).forEach(w => toks.push({ text: title(w), line, mult: st.big_mult || 1.7, color: big, scale: sc }));
      line++;
      if (hi + 1 < n) words.slice(hi + 1).forEach(w => toks.push({ text: title(w), line, mult: st.small_mult || 0.42 }));
    } else {
      words.forEach((w, i) => { const k = kw.includes(i);
        toks.push({ text: w, mult: k ? (st.big_mult || 1.3) : 1, color: k ? big : null, scale: k ? sc : 1 }); });
    }
    return { key: [kw, phase], toks, wi };
  }
  if (st.mode === 'pop') {
    const ws = wi ? times[wi][0] : c.start, q = Math.min(1, Math.max(0, (t - ws) / 0.18));
    const sc = q < 0.6 ? 0.8 + 0.25 * (q / 0.6) : 1.05 - 0.05 * ((q - 0.6) / 0.4);
    words.forEach((w, k) => toks.push({ text: w, visible: k <= wi, color: k === wi ? hlFor(st, k) : null, scale: k === wi ? sc : 1 }));
    return { key: [wi, Math.round(q * 100)], toks, wi };
  }
  if (st.mode === 'focus') {
    words.forEach((w, k) => toks.push({ text: w, blur: k <= wi ? 0 : 0.07, alpha: k <= wi ? 1 : 0.4 }));
    return { key: [wi], toks, wi };
  }
  if (st.mode === 'poster') {
    words.forEach((w, k) => { const alt = Math.floor(k / 2) % 2 === 1;
      toks.push({ text: w, line: Math.floor(k / 2), visible: k <= wi, css: alt ? st.alt_css : null,
                  mult: alt ? (st.alt_mult || 0.85) : (st.main_mult || 1.2), upper: !alt && st.upper, lower: alt,
                  color: alt ? (st.alt_color || '#ffd54a') : null }); });
    return { key: [wi], toks, wi };
  }
  if (st.mode === 'typewriter') {
    const rev = words.map((w, k) => { const [s, e] = times[k];
      return t >= e ? w.length : t >= s ? Math.max(1, Math.floor(w.length * (t - s) / Math.max(0.01, e - s)) + 1) : 0; });
    let last = 0; rev.forEach((r, k) => { if (r) last = k; });
    const typing = rev[last] < words[last].length || (last + 1 < n && t >= times[last][1]);
    const cur = typing || Math.floor((t - c.start) * 2.5) % 2 === 0;
    words.forEach((w, k) => toks.push({ text: w, reveal: rev[k], cursor: k === last && cur, cursorColor: st.cursor }));
    return { key: [rev, cur], toks, wi, chars: rev.reduce((a, b) => a + b, 0) };
  }
  if (st.mode === 'matchcut') {
    const ws = wi ? times[wi][0] : c.start, flips = Math.floor((t - ws) / 0.09), vs = st.variants;
    const vi = flips < 4 ? (wi + flips) % vs.length : wi % vs.length;
    toks.push({ text: words[wi], css: vs[vi].css, color: vs[vi].color, keepFont: true });
    return { key: [wi, vi], toks, wi };
  }
}

function renderKineticInto(el, toks, st) {
  el.textContent = '';
  if (toks.length && toks[0].fx_shine !== undefined) el.dataset.shine = toks[0].fx_shine; else delete el.dataset.shine;
  if (toks.length && toks[0].fx_phase !== undefined) el.dataset.phase = toks[0].fx_phase; else delete el.dataset.phase;
  let line = 0;
  toks.forEach((tk, k) => {
    if (tk.line !== undefined && tk.line !== line) { el.appendChild(document.createElement('br')); line = tk.line; }
    else if (k > 0) el.appendChild(document.createTextNode(' '));
    const sp = document.createElement('span');
    sp.className = 'w';
    let text = tk.text;
    if (tk.upper) text = text.toUpperCase();
    if (tk.css && (tk.keepFont || !fontFace || tk.css !== st.css)) {
      sp.style.fontFamily = tk.css.family + ', sans-serif'; sp.style.fontWeight = tk.css.weight;
      sp.style.fontStyle = tk.css.italic ? 'italic' : 'normal'; sp.style.fontStretch = tk.css.stretch || 'normal';
    }
    if (tk.lower) sp.style.textTransform = 'none';
    if (tk.mult) sp.style.fontSize = tk.mult + 'em';
    if (tk.color) sp.style.color = tk.color;
    if (tk.visible === false) sp.style.visibility = 'hidden';
    if (tk.blur) sp.style.filter = `blur(${tk.blur}em)`;
    if (tk.alpha !== undefined && tk.alpha < 1) sp.style.opacity = tk.alpha;
    if ((tk.scale && tk.scale !== 1) || tk.rot || tk.sticker || tk.dy || tk.dxo) {
      sp.style.display = 'inline-block';
      sp.style.transform = `translate(${(tk.dxo || 0)}em, ${(tk.dy || 0)}em) rotate(${tk.rot || 0}deg) scale(${tk.scale || 1})`;
    }
    if (tk.underline) {  // underline drawing itself under the word
      sp.style.backgroundImage = `linear-gradient(${tk.underline.color}, ${tk.underline.color})`;
      sp.style.backgroundRepeat = 'no-repeat'; sp.style.backgroundPosition = '0 96%';
      sp.style.backgroundSize = `${Math.round(tk.underline.p * 100)}% .11em`; sp.style.paddingBottom = '.06em';
    }
    if (tk.marker) {  // highlighter painting left -> right behind the word
      sp.style.backgroundImage = `linear-gradient(${tk.marker.color}, ${tk.marker.color})`;
      sp.style.backgroundRepeat = 'no-repeat'; sp.style.backgroundPosition = '0 60%';
      sp.style.backgroundSize = `${Math.round(tk.marker.p * 100)}% 76%`;
      sp.style.padding = '0 .12em'; sp.style.margin = '0 -.12em'; sp.style.borderRadius = '.14em';
    }
    if (tk.wst) {
      const fs = parseFloat($('capText').style.fontSize) || 24, w = tk.wst, swp = Math.round((w.stroke || 0) * fs);
      const ring = []; if (swp) for (let q = 0; q < 16; q++) { const an = q * Math.PI / 8;
        ring.push(`${(Math.cos(an) * swp).toFixed(1)}px ${(Math.sin(an) * swp).toFixed(1)}px 0 ${w.stroke_color || '#000'}`); }
      sp.style.textShadow = ring.join(',') || 'none';
      sp.style.filter = (w.shadows || []).map(([dx, dy, b, cc, al]) => `drop-shadow(${dx}em ${dy}em ${b}em ${rgba(cc, al)})`).join(' ');
      if (!tk.upper) sp.style.textTransform = 'none';
    }
    if (tk.glowSh) sp.style.filter = ((sp.style.filter || '') + ' ' + tk.glowSh.map(([dx, dy, b, c, al]) =>
      `drop-shadow(0 0 ${b}em ${rgba(c, al)})`).join(' ')).trim();
    if (tk.pill) { sp.style.background = tk.pill; sp.style.boxShadow = `0 0 0 .18em ${tk.pill}`; sp.style.borderRadius = '.2em'; }
    if (tk.fontFam) { sp.style.fontFamily = `'${tk.fontFam}'`; sp.style.fontWeight = 'normal'; sp.style.fontStyle = 'normal'; sp.style.fontStretch = 'normal'; }
    if (tk.sticker) {  // each word on its own sticker
      sp.style.background = tk.sticker; sp.style.color = tk.color || '';
      sp.style.padding = '.14em .28em'; sp.style.margin = '.1em .3em'; sp.style.borderRadius = '.22em';
      sp.style.boxShadow = '.03em .08em .1em rgba(0,0,0,.55)';
    }
    if (tk.reveal !== undefined) {
      const a = document.createElement('span'); a.textContent = text.slice(0, tk.reveal);
      const b = document.createElement('span'); b.textContent = text.slice(tk.reveal); b.style.visibility = 'hidden';
      sp.append(a);
      if (tk.cursor) { const cu = document.createElement('span'); cu.className = 'cursor'; cu.style.background = tk.cursorColor || '#fff'; sp.append(cu); }
      sp.append(b);
    } else sp.textContent = text;
    el.appendChild(sp);
  });
}

// ---------- sound effects (WebAudio preview; export synthesises the same sounds) ----------
let actx = null;
function playSfx(kind) {
  if (!kind || kind === 'none') return;
  actx = actx || new (window.AudioContext || window.webkitAudioContext)();
  const t0 = actx.currentTime, g = actx.createGain();
  g.connect(actx.destination);
  if (kind === 'pop') {
    const o = actx.createOscillator(); o.frequency.setValueAtTime(900, t0); o.frequency.exponentialRampToValueAtTime(300, t0 + 0.07);
    g.gain.setValueAtTime(0.35, t0); g.gain.exponentialRampToValueAtTime(0.001, t0 + 0.08);
    o.connect(g); o.start(t0); o.stop(t0 + 0.09);
  } else {
    const len = kind === 'whoosh' ? 0.28 : 0.025, buf = actx.createBuffer(1, actx.sampleRate * len, actx.sampleRate);
    const d = buf.getChannelData(0); for (let i = 0; i < d.length; i++) d[i] = Math.random() * 2 - 1;
    const src = actx.createBufferSource(); src.buffer = buf;
    const f = actx.createBiquadFilter();
    if (kind === 'whoosh') { f.type = 'bandpass'; f.frequency.setValueAtTime(300, t0); f.frequency.exponentialRampToValueAtTime(3000, t0 + len);
      g.gain.setValueAtTime(0.001, t0); g.gain.linearRampToValueAtTime(0.5, t0 + len / 2); g.gain.linearRampToValueAtTime(0.001, t0 + len); }
    else { f.type = 'highpass'; f.frequency.value = 1500; g.gain.setValueAtTime(0.4, t0); g.gain.exponentialRampToValueAtTime(0.001, t0 + len); }
    src.connect(f); f.connect(g); src.start(t0);
  }
}
$('sfx').onchange = () => { curSfx = $('sfx').value; playSfx(curSfx); };
$('sfxTest').onclick = () => playSfx($('sfx').value);
fetch('/api/sfx').then(r => r.json()).then(list => {
  for (const [id, name] of Object.entries(list)) $('sfx').add(new Option(name, id));
});

// ---------- words per caption + safe zones ----------
$('wordsPer').onchange = () => { renderKey = ''; animKey = ''; };
$('safeToggle').onchange = () => { $('safe').hidden = !$('safeToggle').checked; };
$('snapSafe').onclick = () => { $('x').value = 45; $('y').value = 58; $('safeToggle').checked = true; $('safe').hidden = false; applyStyle(); };

// ---------- resizable preview (drag the handle; double-click to reset) ----------
(function () {
  const main = $('editView'), sp = $('splitter'), MIN = 260, PANEL_MIN = 420;
  function maxW() { return main.clientWidth - PANEL_MIN - 14 - 8 - 44; }
  function setW(w, save) {
    if (w === null) {
      main.style.removeProperty('--pw'); main.classList.remove('resized');
      if (save) localStorage.removeItem('cs_preview_w');
    } else {
      if (main.clientWidth) w = Math.min(maxW(), w);  // editor hidden: can't measure yet, keep as saved
      w = Math.round(Math.max(MIN, w));
      main.style.setProperty('--pw', w + 'px'); main.classList.add('resized');
      if (save) localStorage.setItem('cs_preview_w', w);
    }
    requestAnimationFrame(applyStyle);
  }
  sp.onpointerdown = e => {
    e.preventDefault();
    sp.setPointerCapture(e.pointerId); sp.classList.add('drag'); document.body.classList.add('resizing');
    const left = document.querySelector('.viewer').getBoundingClientRect().left;
    sp.onpointermove = m => setW(m.clientX - left - 2, false);
    sp.onpointerup = () => {
      sp.onpointermove = null; sp.classList.remove('drag'); document.body.classList.remove('resizing');
      setW(document.querySelector('.viewer').getBoundingClientRect().width, true);
    };
  };
  sp.ondblclick = () => setW(null, true);
  sp.onkeydown = e => {
    if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
    e.preventDefault();
    const cur = document.querySelector('.viewer').getBoundingClientRect().width;
    setW(cur + (e.key === 'ArrowRight' ? 30 : -30), true);
  };
  const saved = parseInt(localStorage.getItem('cs_preview_w'));
  if (saved) setW(saved, false);
  window.addEventListener('resize', () => { if (main.classList.contains('resized')) setW(parseInt(localStorage.getItem('cs_preview_w')) || null, false); });
})();

// ---------- fullscreen with captions ----------
(function () {
  const stage = $('stage'), v = $('vid');
  const fsEl = () => document.fullscreenElement || document.webkitFullscreenElement;
  function enter() {
    const req = stage.requestFullscreen || stage.webkitRequestFullscreen;
    if (req) Promise.resolve(req.call(stage)).catch(e => console.warn('fullscreen refused', e));
  }
  function exit() {
    if (!fsEl()) return;
    Promise.resolve((document.exitFullscreen || document.webkitExitFullscreen).call(document)).catch(() => {});
  }
  function toggle() { fsEl() ? exit() : enter(); }
  function fit() {  // scale the video to fill the screen while in fullscreen
    if (fsEl() === stage && v.videoWidth) {
      const ar = v.videoWidth / v.videoHeight, W = innerWidth, H = innerHeight;
      const w = Math.min(W, H * ar);
      v.style.width = w + 'px'; v.style.height = (w / ar) + 'px'; v.style.maxWidth = 'none'; v.style.maxHeight = 'none';
    } else {
      v.style.width = v.style.height = v.style.maxWidth = v.style.maxHeight = '';
    }
    renderKey = '';
    requestAnimationFrame(applyStyle);
  }
  $('fsBtn').onclick = toggle;
  $('fsExit').onclick = exit;
  v.addEventListener('dblclick', e => { e.preventDefault(); toggle(); });
  document.addEventListener('keydown', e => {
    if ((e.key === 'f' || e.key === 'F') && !/INPUT|SELECT|TEXTAREA/.test(document.activeElement.tagName) && !$('editView').hidden) toggle();
  });
  ['fullscreenchange', 'webkitfullscreenchange'].forEach(ev => document.addEventListener(ev, () => {
    // a browser's own video fullscreen (e.g. Safari's button) would hide the captions: switch to ours
    if (fsEl() === v) { exit(); setTimeout(enter, 50); return; }
    fit();
  }));
  window.addEventListener('resize', () => { if (fsEl() === stage) fit(); });
})();

// ---------- don't lose work on refresh: leave warning + resume ----------
(function () {
  const KEY = 'cs_session_v1';
  const busy = () => {
    const up = $('upStatus').textContent;
    return /^(Uploading|Creating)/.test(up) || $('export').disabled;
  };
  // browser's own "Leave/Reload site?" dialog (wording is fixed by the browser)
  window.addEventListener('beforeunload', e => {
    if ((!$('editView').hidden && sid) || busy()) { e.preventDefault(); e.returnValue = ''; }
  });

  function save() {
    if ($('editView').hidden || !sid) return;
    const st = style();
    localStorage.setItem(KEY, JSON.stringify({
      sid, name: $('fontNow').textContent, savedAt: Date.now(), style: st.style, fontId: st.fontId, anim: st.anim, feather: $('feather').value, glow: $('glow').value, glowColor: $('glowColor').value, animScope: st.animScope,
      animDur: st.animDur, size: $('size').value, x: $('x').value, y: $('y').value, opacity: $('opacity').value,
      textColor: st.textColor, boxColor: st.boxColor, hlColor: $('hlColor').value, hlTouched,
      wordsPer: $('wordsPer').value, sfx: $('sfx').value, time: $('vid').currentTime || 0, emojiOn: $('emojiToggle').checked,
      tab: (document.querySelector('.tab.on') || {}).dataset?.tab || 'style'}));
  }
  setInterval(save, 1000);
  window.addEventListener('pagehide', save);
  $('another').addEventListener('click', () => localStorage.removeItem(KEY));  // starting a new video on purpose

  const until = (cond, ms = 8000) => new Promise(res => {
    const t0 = Date.now(), iv = setInterval(() => { if (cond() || Date.now() - t0 > ms) { clearInterval(iv); res(); } }, 50);
  });

  async function restore(stt) {
    await until(() => STYLES && FONTS.length && document.querySelectorAll('.mtile').length && $('sfx').options.length);
    await openEditor(stt.sid);
    if (STYLES[stt.style]) pickStyle(stt.style);
    // settings after pickStyle (it resets colours/animation to the style's defaults)
    for (const k of ['size', 'x', 'y', 'opacity', 'feather', 'glow', 'glowColor', 'textColor', 'boxColor', 'hlColor', 'wordsPer', 'sfx']) if (stt[k] != null) $(k).value = stt[k];
    hlTouched = !!stt.hlTouched; curSfx = $('sfx').value;
    if (stt.emojiOn !== undefined) $('emojiToggle').checked = !!stt.emojiOn;
    $('animDur').value = stt.animDur || 0.3; $('animDur').oninput();
    pickAnim(stt.anim || 'none');
    setScope(stt.animScope || 'line');
    if (stt.fontId != null) await pickFont(stt.fontId).catch(() => {});
    const tab = document.querySelector(`.tab[data-tab="${stt.tab}"]`); if (tab) tab.click();
    $('vid').addEventListener('loadedmetadata', () => { $('vid').currentTime = stt.time || 0; }, { once: true });
    renderKey = ''; animKey = ''; applyStyle();
  }

  let saved = null;
  try { saved = JSON.parse(localStorage.getItem(KEY) || 'null'); } catch (e) {}
  if (saved && saved.sid) {
    fetch('/api/captions?id=' + encodeURIComponent(saved.sid)).then(r => {
      if (!r.ok) { localStorage.removeItem(KEY); return; }  // video no longer on disk
      const m = Math.round((Date.now() - (saved.savedAt || Date.now())) / 60000);
      const ago = m < 1 ? 'just now' : m < 60 ? `${m} min ago` : m < 1440 ? `${Math.round(m / 60)} h ago` : `${Math.round(m / 1440)} days ago`;
      const styleName = (STYLES && STYLES[saved.style] && STYLES[saved.style].name) || saved.style;
      $('resumeInfo').textContent = `Your last video (style: ${styleName}, saved ${ago}) and its caption settings are still on this Mac.`;
      $('resume').hidden = false;
      $('resumeYes').onclick = () => { $('resume').hidden = true; restore(saved); };
      $('resumeNo').onclick = () => { $('resume').hidden = true; localStorage.removeItem(KEY); };
    }).catch(() => {});
  }
})();

// ---------- auto emoji (preview; export draws the same in a second pass) ----------
let emojiKey = '';
function updateEmoji(t) {
  const el = $('capEmoji'), v = $('vid');
  const i = captions.findIndex(c => t >= c.start && t <= c.end);
  const c = i >= 0 ? captions[i] : null;
  if (!$('emojiToggle').checked || !c || !c.emoji) { el.hidden = true; emojiKey = ''; return; }
  const times = wordTimes(c), k = (c.kw && c.kw.length) ? c.kw[0] : 0, t0 = k ? times[k][0] : c.start;
  if (t < t0) { el.hidden = true; emojiKey = ''; return; }
  const w = v.clientWidth, h = v.clientHeight, s = style();
  const px = Math.min(w * 0.3, w * s.size * 2.2);
  const cx = Math.max(px / 2, Math.min(w - px / 2, w * s.x));
  el.textContent = c.emoji;
  el.style.width = px + 'px'; el.style.height = px + 'px'; el.style.fontSize = (px * 0.86) + 'px';
  el.style.left = (v.offsetLeft + cx - px / 2) + 'px';
  el.style.top = (v.offsetTop + h * s.y - px * 0.08 - px) + 'px';
  el.hidden = false;
  if (emojiKey !== String(i)) {  // pop in
    emojiKey = String(i);
    el.style.animation = 'none'; void el.offsetWidth; el.style.animation = 'cs-pop .25s linear both';
  }
}
$('emojiToggle').onchange = () => { emojiKey = ''; };

// ---------- resize the caption directly on the video (stays in sync with Layout ▸ Size) ----------
(function () {
  const cap = $('cap'), size = $('size');
  const MIN = parseFloat(size.min), MAX = parseFloat(size.max), STEP = parseFloat(size.step) || 0.25;
  const frame = document.createElement('div'); frame.className = 'capFrame';
  const tools = document.createElement('div'); tools.className = 'capTools';
  tools.innerHTML = '<button type="button" data-d="-1" title="Smaller">A−</button><span class="val"></span>' +
                    '<button type="button" data-d="1" title="Bigger">A+</button>' +
                    '<button type="button" class="editBtn" title="Edit text (or double-click the caption)">✎</button>';
  cap.append(frame, tools);
  ['nw', 'ne', 'sw', 'se'].forEach(k => { const h = document.createElement('div'); h.className = 'rh ' + k; h.title = 'Drag to resize'; cap.appendChild(h); });

  function setSize(v) {
    v = Math.round(Math.max(MIN, Math.min(MAX, v)) / STEP) * STEP;
    size.value = v; size.dispatchEvent(new Event('input'));  // same path as moving the Layout slider
    applyStyle(); refresh();
  }
  function refresh() {
    tools.querySelector('.val').textContent = parseFloat(size.value).toFixed(2).replace(/\.?0+$/, '') + '%';
    tools.classList.toggle('below', cap.offsetTop < 48);  // no room above: show the toolbar under the caption
  }
  tools.addEventListener('pointerdown', e => e.stopPropagation());  // toolbar never starts a move
  tools.querySelectorAll('button[data-d]').forEach(b => b.onclick = () => setSize(parseFloat(size.value) + STEP * 2 * b.dataset.d));
  tools.querySelector('.editBtn').onclick = () => window.openCapEdit && window.openCapEdit();

  cap.querySelectorAll('.rh').forEach(h => h.addEventListener('pointerdown', e => {
    e.stopPropagation(); e.preventDefault();
    h.setPointerCapture(e.pointerId); document.body.classList.add('resizingCap'); cap.classList.add('sel');
    const r = cap.getBoundingClientRect(), cx = r.left + r.width / 2, cy = r.top + r.height / 2;
    const d0 = Math.max(8, Math.hypot(e.clientX - cx, e.clientY - cy)), s0 = parseFloat(size.value);
    h.onpointermove = m => setSize(s0 * Math.hypot(m.clientX - cx, m.clientY - cy) / d0);
    h.onpointerup = () => { h.onpointermove = null; document.body.classList.remove('resizingCap'); };
  }));

  // trackpad pinch (or ctrl + scroll) over the caption
  cap.addEventListener('wheel', e => {
    if (!e.ctrlKey) return;
    e.preventDefault();
    setSize(parseFloat(size.value) * Math.exp(-e.deltaY * 0.004));
  }, { passive: false });

  // click the caption to keep the handles visible; click anywhere else to hide them
  cap.addEventListener('pointerdown', () => { cap.classList.add('sel'); refresh(); });
  document.addEventListener('pointerdown', e => { if (!cap.contains(e.target)) cap.classList.remove('sel'); });
  size.addEventListener('input', refresh);
  setInterval(refresh, 500);
  refresh();
})();

// ---------- edit caption text directly on the video ----------
(function () {
  const stage = $('stage'), cap = $('cap'), v = $('vid');
  let box = null, wasPlaying = false;
  function close(restore) {
    if (!box) return;
    box.remove(); box = null;
    if (restore && wasPlaying) v.play();
  }
  window.openCapEdit = function () {
    if (cap.classList.contains('hidden')) return;
    const t = v.currentTime;
    let i = hlSrc >= 0 ? hlSrc : captions.findIndex(c => t >= c.start && t <= c.end);
    if (i < 0 || !captions[i]) return;
    close(false);
    wasPlaying = !v.paused; v.pause();
    box = document.createElement('div'); box.id = 'capEdit';
    box.innerHTML = '<textarea spellcheck="true"></textarea><div class="row"><span class="hint">Enter = save · Esc = cancel</span>' +
                    '<span class="btns"><button type="button" class="cancel">Cancel</button><button type="button" class="save">Save</button></span></div>';
    const ta = box.querySelector('textarea');
    ta.value = captions[i].text;
    // place it over the caption, inside the video
    const sr = stage.getBoundingClientRect(), cr = cap.getBoundingClientRect(), vr = v.getBoundingClientRect();
    const w = Math.min(sr.width - 8, Math.max(240, cr.width + 24));
    let left = cr.left + cr.width / 2 - w / 2; left = Math.max(sr.left + 4, Math.min(sr.right - 4 - w, left));
    box.style.left = (left - sr.left) + 'px'; box.style.width = w + 'px';
    box.style.top = Math.max(vr.top + 8, Math.min(vr.bottom - 130, cr.top - 10)) - sr.top + 'px';
    box.addEventListener('pointerdown', e => e.stopPropagation());
    box.addEventListener('dblclick', e => e.stopPropagation());
    stage.appendChild(box);
    ta.focus(); ta.select();
    const save = () => {
      const txt = ta.value.replace(/\s+/g, ' ').trim();
      if (!txt) { ta.focus(); return; }
      if (txt !== captions[i].text) {
        // same save path as the Captions tab (updates list, key words, emoji, preview and export)
        const inp = document.querySelector(`#li${i} input:not(.emo)`);
        if (inp) { inp.value = txt; inp.dispatchEvent(new Event('input')); inp.dispatchEvent(new Event('change')); }
        renderKey = '';
      }
      close(true);
    };
    box.querySelector('.save').onclick = save;
    box.querySelector('.cancel').onclick = () => close(true);
    ta.addEventListener('keydown', e => {
      e.stopPropagation();  // typing never triggers app shortcuts (e.g. F = fullscreen)
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); save(); }
      if (e.key === 'Escape') { e.preventDefault(); close(true); }
    });
  };
  cap.addEventListener('dblclick', e => { e.preventDefault(); window.openCapEdit(); });
  // leaving the editor / switching video closes the edit box
  new MutationObserver(() => { if ($('editView').hidden) close(false); }).observe($('editView'), { attributes: true });
})();

// ---------- edge fade on the caption background (preview) ----------
function applyFeather() {
  const box = $('capBox'), fe = parseFloat($('feather').value) / 100, op = parseFloat($('opacity').value) / 100;
  const on = fe > 0 && op > 0;
  box.classList.toggle('feather', on);
  if (!on) return;
  box.style.setProperty('--boxbg', box.style.background || rgba($('boxColor').value, op));
  box.style.background = 'transparent';
  box.style.setProperty('--fe', Math.max(1, fe * Math.min(box.offsetWidth, box.offsetHeight) / 2) + 'px');
}

// ---------- text glow (preview; export uses glow_shadows() in transcribe.py) ----------
function glowShadows(strength, color) {  // same layers as glow_shadows() in transcribe.py
  const g = Math.max(0, Math.min(1, strength)), a = Math.pow(g, 0.85), k = 0.6 + 0.8 * g;
  return [[0, 0, 0.03 * k, color, a], [0, 0, 0.07 * k, color, 0.9 * a], [0, 0, 0.15 * k, color, 0.7 * a], [0, 0, 0.32 * k, color, 0.4 * a]];
}
function isDark(hex) { const n = parseInt(hex.slice(1), 16); return (0.299 * (n >> 16) + 0.587 * ((n >> 8) & 255) + 0.114 * (n & 255)) / 255 <= 0.3; }
function withGlow(st) {
  const g = parseFloat($('glow').value) / 100;
  if (!st || !(g > 0)) return st;
  const c = $('glowColor').value;
  const strong = g >= 0.4;  // only a strong glow replaces the style's dark outline / shadow
  return Object.assign({}, st, {
    shadows: (st.shadows || []).filter(x => !(strong && isDark(x[3]))).concat(glowShadows(g, c)),
    stroke: strong ? 0 : st.stroke, glowText: { g, color: c } });
}
function toHex(rgb) { return '#' + rgb.match(/\d+/g).map(n => (+n).toString(16).padStart(2, '0')).join(''); }
function mixHex(a, b, t) {  // a -> b by t (0..1)
  const A = parseInt(a.slice(1), 16), B = parseInt(b.slice(1), 16);
  const c = sh => Math.round(((A >> sh) & 255) + ((((B >> sh) & 255) - ((A >> sh) & 255)) * t));
  return `rgb(${c(16)},${c(8)},${c(0)})`;
}

// ---------- per-word styles (preview mirrors tokens_for_any + apply_word_props in transcribe.py) ----------
function renderPlainWords(el, text) {
  el.textContent = '';
  text.split(/\s+/).filter(Boolean).forEach((w, k) => {
    if (k) el.appendChild(document.createTextNode(' '));
    const sp = document.createElement('span'); sp.className = 'w'; sp.textContent = w; el.appendChild(sp);
  });
}
function tokensForAny(c, t, st) {
  if (KINETIC.has(st.mode)) return kineticState(c, t, st);
  const words = c.text.split(/\s+/).filter(Boolean), times = wordTimes(c), n = words.length;
  let wi = 0; for (let k = 1; k < n; k++) if (t >= times[k][0]) wi = k;
  if (st.mode === 'one_word') return { key: ['ow', wi], toks: [{ text: words[wi], k: wi }], wi };
  const toks = words.map((w, k) => {
    const tk = { text: w, k };
    if (st.mode === 'highlight' && k === wi) tk.color = hlFor(st, k);
    else if (st.mode === 'fill' && k <= wi) tk.color = hlFor(st, k);
    else if (st.mode === 'reveal') tk.visible = k <= wi;
    else if (st.mode === 'box' && k === wi) { tk.pill = hlFor(st, k); tk.color = st.hl_text || null; }
    return tk;
  });
  return { key: [st.mode || 'static', wi], toks, wi };
}
const _wsFontLoading = {};
function wsFont(id) {  // load a font for a single word (same file the export uses)
  const name = 'csfont' + id;
  if (![...document.fonts].some(f => f.family === name) && !_wsFontLoading[id]) {
    _wsFontLoading[id] = new FontFace(name, `url(/api/fontfile?id=${id})`).load()
      .then(f => { document.fonts.add(f); renderKey = ''; }).catch(() => {});
  }
  return name;
}
function applyWordProps(toks, c, t) {
  const props = c.ws || {}, words = c.text.split(/\s+/).filter(Boolean), times = wordTimes(c), n = words.length;
  let wi = 0; for (let k = 1; k < n; k++) if (t >= times[k][0]) wi = k;
  const idx = toks.every(tk => tk.k !== undefined) ? toks.map(tk => tk.k) : toks.length === n ? toks.map((_, k) => k)
            : toks.length === 1 ? [wi] : toks.map(() => null);
  const key = [];
  toks.forEach((tk, j) => {
    const k = idx[j], p = k == null ? null : props[k];
    if (!p) return;
    const wst = p.style && STYLES[p.style];
    if (wst) {  // this word uses another caption style
      tk.css = wst.css; tk.keepFont = true; tk.wst = wst;
      if (wst.upper) tk.upper = true; else tk.lower = true;
      if (!p.color) tk.color = wst.text;
      if ((wst.opacity || 0) > 0 && !p.pill) tk.pill = rgba(wst.box || '#000000', wst.opacity);
    }
    if (p.color) tk.color = p.color;
    if (p.size) tk.mult = (tk.mult || 1) * p.size;
    if (p.fontId != null) tk.fontFam = wsFont(p.fontId);
    if (p.glow) tk.glowSh = glowShadows(p.glow, p.glowColor || '#ffffff');
    if (p.marker) { tk.marker = { color: p.marker, p: 1 }; if (!p.color) tk.color = tk.color || '#111111'; }
    if (p.underline) tk.underline = { color: p.underline, p: 1 };
    if (p.pill) tk.pill = rgba(p.pill, p.pillOpacity ?? 1);
    const m = p.motion, ws = k ? times[k][0] : c.start, dt = t - ws;
    if (m && WM_ANIMS.has(m)) {  // any Motion-tab animation, played when the word is spoken
      const d = p.motionDur || 0.35;
      if (dt >= 0 && dt < d) {
        const a = wmAnim(m, dt / d);
        tk.alpha = (tk.alpha ?? 1) * a.alpha; tk.scale = (tk.scale || 1) * a.scale;
        tk.dxo = (tk.dxo || 0) + a.dx * 0.8; tk.dy = (tk.dy || 0) + a.dy * 0.8;
        if (a.blur) tk.blur = a.blur * 0.4;
        key.push([k, 'a', Math.round(dt / d * 100)]);
      } else if (dt < 0 && WM_HIDE_FIRST.has(m)) { tk.alpha = 0; key.push([k, 'a', 'pre']); }
      return;
    }
    if (m === 'pop' && dt >= 0 && dt < 0.25) { const q = dt / 0.25;
      tk.scale = (tk.scale || 1) * (q < 0.6 ? 0.6 + 0.5 * q / 0.6 : 1.1 - 0.1 * (q - 0.6) / 0.4); key.push([k, 'p', Math.round(q * 100)]); }
    else if (m === 'shake' && dt >= 0 && dt < 0.4) { tk.dxo = 0.12 * Math.sin(dt * 60) * (1 - dt / 0.4); key.push([k, 's', Math.round(dt * 100)]); }
    else if (m === 'bounce' && dt >= 0 && dt < 0.45) { tk.dy = (tk.dy || 0) - 0.8 * (1 - easeBounce(dt / 0.45)); key.push([k, 'b', Math.round(dt * 100)]); }
    else if (m === 'wiggle' && dt >= 0 && dt < 0.5) { tk.rot = (tk.rot || 0) + 10 * Math.sin(dt * 40) * (1 - dt / 0.5); key.push([k, 'w', Math.round(dt * 100)]); }
    else if (m === 'pulse') { const ph = Math.round(((t - c.start) * 1.5) % 1 * 100) / 100;
      tk.scale = (tk.scale || 1) * (1 + 0.08 * Math.sin(2 * Math.PI * ph)); key.push([k, 'u', ph]); }
  });
  return key;
}

// selection: click a word on the video (shift-click to add more), or a chip in the Captions tab
let wordSel = null;  // { src, idx: Set }
function markWordSel(c) {
  document.querySelectorAll('#capText .w').forEach((sp, j) => {
    const words = $('capText').querySelectorAll('.w').length;
    const k = (words === 1 && c && c.text.split(/\s+/).filter(Boolean).length > 1) ? null : j;
    sp.classList.toggle('wsel', !!(wordSel && c && wordSel.src === c.src && k != null && wordSel.idx.has((c.off || 0) + k)));
  });
}
(function () {
  const cap = $('cap'); let down = null;
  cap.addEventListener('pointerdown', e => { down = [e.clientX, e.clientY]; });
  cap.addEventListener('click', e => {
    if (!down || Math.hypot(e.clientX - down[0], e.clientY - down[1]) > 4) return;  // it was a drag
    // the caption captures the pointer for dragging, so find the word under the click by position
    const sp = [...$('capText').querySelectorAll('.w')].find(el => {
      const r = el.getBoundingClientRect();
      return e.clientX >= r.left - 2 && e.clientX <= r.right + 2 && e.clientY >= r.top - 2 && e.clientY <= r.bottom + 2;
    });
    if (!sp) return;
    const caps = dispCaps(), t = $('vid').currentTime;
    const c = caps.find(c => t >= c.start && t <= c.end) || (caps.length && t < caps[0].start ? caps[0] : null);
    if (!c) return;
    const spans = [...$('capText').querySelectorAll('.w')], j = spans.indexOf(sp);
    const n = c.text.split(/\s+/).filter(Boolean).length;
    let k = j;
    if (spans.length === 1 && n > 1) {  // one-word styles show only the spoken word
      const times = wordTimes(c); k = 0; for (let q = 1; q < n; q++) if (t >= times[q][0]) k = q;
    }
    const wi = (c.off || 0) + k;
    if (e.shiftKey && wordSel && wordSel.src === c.src) wordSel.idx.add(wi);
    else wordSel = { src: c.src, idx: new Set([wi]) };
    openWordPanel(c.src, [...wordSel.idx], e.clientX, e.clientY);
  });
})();

function openWordPanel(src, idxs, x, y) {
  const c = captions[src]; if (!c) return;
  wordSel = { src, idx: new Set(idxs) };
  const words = c.text.split(/\s+/).filter(Boolean), pick = [...wordSel.idx].sort((a, b) => a - b);
  const p = (c.ws || {})[pick[0]] || {};
  $('wpTitle').textContent = 'Style: ' + pick.map(i => '“' + words[i] + '”').join(' ');
  $('wpGlobalLbl').textContent = pick.length === 1 ? `Every time “${words[pick[0]].replace(/[^\p{L}\p{N}']/gu, '')}” appears` : 'Every time these words appear';
  const set = (on, inp, val, def) => { $(on).checked = !!val; $(inp).value = val || def; };
  set('wpColorOn', 'wpColor', p.color, '#ffe600');
  $('wpSize').value = Math.round((p.size || 1) * 100); $('wpSizeVal').textContent = $('wpSize').value + '%';
  $('wpMotion').value = p.motion || 'none';
  $('wpGlow').value = Math.round((p.glow || 0) * 100); $('wpGlowVal').textContent = $('wpGlow').value + '%'; $('wpGlowColor').value = p.glowColor || '#00e5ff';
  set('wpMarkerOn', 'wpMarker', p.marker, '#ffe600'); set('wpUnderOn', 'wpUnder', p.underline, '#00e5ff'); set('wpPillOn', 'wpPill', p.pill, '#ff2b6d');
  const sel = $('wpFont');
  if (sel.options.length < 2 && typeof FONTS !== 'undefined')
    FONTS.forEach(f => sel.add(new Option(f.family + (f.style && f.style !== 'Regular' ? ' (' + f.style + ')' : ''), f.id)));
  sel.value = p.fontId != null ? String(p.fontId) : '';
  const glob = pick.every(i => (window.GLOBAL_WORDS || {})[words[i].toLowerCase().replace(/[^\p{L}\p{N}']/gu, '')]) && !pick.some(i => (c.wsLocal || {})[i]);
  document.querySelector(`input[name=wpScope][value=${glob ? 'global' : 'caption'}]`).checked = true;
  const pn = $('wordPanel'); pn.hidden = false;
  const w = pn.offsetWidth, h = pn.offsetHeight;
  // open beside the video so it never covers the caption (shift-click more words while it is open)
  const vr = $('vid').getBoundingClientRect();
  let left = vr.right + 12;
  if (left + w > innerWidth - 8) left = Math.max(8, vr.left - w - 12);
  pn.style.left = left + 'px';
  pn.style.top = Math.max(8, Math.min(innerHeight - h - 8, vr.top)) + 'px';
  $('vid').pause(); renderKey = '';
}
async function saveWordStyle(props) {
  if (!wordSel) return;
  const scope = document.querySelector('input[name=wpScope]:checked').value;
  const body = { id: sid, index: wordSel.src, words: [...wordSel.idx], scope, props };
  let r = await fetch('/api/wordstyle', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  if (!r.ok) { alert((await r.json()).error || r.status); return; }
  if (props === null && scope === 'caption') {  // resetting also removes an "every time" style of the same word(s)
    await fetch('/api/wordstyle', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ...body, scope: 'global' }) });
  }
  await refreshWordStyles();
}
async function refreshWordStyles() {
  const j = await (await fetch('/api/captions?id=' + sid)).json();
  window.GLOBAL_WORDS = j.globalWords || {};
  j.captions.forEach((c, i) => { if (captions[i]) { captions[i].ws = c.ws; captions[i].wsLocal = c.wsLocal; } });
  capsVersion++; renderKey = '';
  document.querySelectorAll('#list .item').forEach(d => d._drawWs && d._drawWs());
}
$('wpSize').oninput = () => { $('wpSizeVal').textContent = $('wpSize').value + '%'; };
$('wpGlow').oninput = () => { $('wpGlowVal').textContent = $('wpGlow').value + '%'; };
function wordPanelProps() {
  const props = {};
  if ($('wpColorOn').checked) props.color = $('wpColor').value;
  if (+$('wpSize').value !== 100) props.size = $('wpSize').value / 100;
  if ($('wpFont').value !== '') props.fontId = +$('wpFont').value;
  if ($('wpMotion').value !== 'none') props.motion = $('wpMotion').value;
  if (+$('wpGlow').value > 0) { props.glow = $('wpGlow').value / 100; props.glowColor = $('wpGlowColor').value; }
  if ($('wpMarkerOn').checked) props.marker = $('wpMarker').value;
  if ($('wpUnderOn').checked) props.underline = $('wpUnder').value;
  if ($('wpPillOn').checked) props.pill = $('wpPill').value;
  return Object.keys(props).length ? props : null;
}
// LIVE: every change shows on the video at once and is saved automatically
let _wpTimer = null;
function wordPanelChanged() {
  if (!wordSel) return;
  const props = wordPanelProps(), c = captions[wordSel.src];
  if (c) {  // instant preview
    c.ws = c.ws || {};
    [...wordSel.idx].forEach(i => { if (props) c.ws[i] = props; else delete c.ws[i]; });
    capsVersion++; renderKey = '';
  }
  clearTimeout(_wpTimer);
  _wpTimer = setTimeout(() => saveWordStyle(props), 250);  // then save
}
// picking a colour switches that option on
[['wpColor', 'wpColorOn'], ['wpMarker', 'wpMarkerOn'], ['wpUnder', 'wpUnderOn'], ['wpPill', 'wpPillOn']].forEach(([c, on]) =>
  $(c).addEventListener('input', () => { $(on).checked = true; }));
['wpColorOn', 'wpColor', 'wpSize', 'wpFont', 'wpMotion', 'wpGlow', 'wpGlowColor', 'wpMarkerOn', 'wpMarker',
 'wpUnderOn', 'wpUnder', 'wpPillOn', 'wpPill'].forEach(id => {
  $(id).addEventListener('input', wordPanelChanged);
  $(id).addEventListener('change', wordPanelChanged);
});
document.querySelectorAll('input[name=wpScope]').forEach(r => r.addEventListener('change', wordPanelChanged));
$('wpApply').onclick = () => { clearTimeout(_wpTimer); saveWordStyle(wordPanelProps()); $('wpClose').click(); };
$('wpClear').onclick = () => saveWordStyle(null);
$('wpClose').onclick = () => { $('wordPanel').hidden = true; wordSel = null; renderKey = ''; };
$('wordPanel').addEventListener('pointerdown', e => e.stopPropagation());
document.addEventListener('keydown', e => { if (e.key === 'Escape' && !$('wordPanel').hidden) $('wpClose').click(); });

// ---------- word mode: the existing tabs edit only the selected word(s) ----------
let wordMode = null;  // { src, idx: Set, cap: {...caption control values} }
const WM_IDS = ['size', 'textColor', 'boxColor', 'opacity', 'glow', 'glowColor', 'animDur'];
function wmCaptionValues() {
  const v = {}; WM_IDS.forEach(id => v[id] = $(id).value);
  return Object.assign(v, { style: curStyle, fontId, anim: typeof curAnim !== 'undefined' ? curAnim : 'none' });
}
function wmProps() { const c = wordMode && captions[wordMode.src]; return (c && c.ws && c.ws[[...wordMode.idx][0]]) || {}; }
function wmShowControls() {  // put the word's values (or the caption's) into the existing controls
  const p = wmProps(), cv = wordMode.cap;
  $('size').value = p.size ? (+cv.size * p.size).toFixed(2) : cv.size;
  $('textColor').value = p.color || (p.style && STYLES[p.style] ? STYLES[p.style].text : cv.textColor);
  $('boxColor').value = p.pill || cv.boxColor;
  $('opacity').value = p.pill ? Math.round((p.pillOpacity ?? 1) * 100) : 0;
  $('glow').value = Math.round((p.glow || 0) * 100); $('glowColor').value = p.glowColor || cv.glowColor;
  $('animDur').value = p.motionDur || cv.animDur;
  document.querySelectorAll('.tile').forEach(t => t.classList.toggle('on', t.dataset.id === (p.style || '')));
  document.querySelectorAll('.mtile').forEach(t => t.classList.toggle('on', t.dataset.id === (p.motion || 'none')));
  const fsel = $('font'); if (fsel) fsel.value = p.fontId != null ? String(p.fontId) : '';
  $('fontNow').textContent = p.fontId != null ? 'Word font: ' + ((FONTS.find(f => f.id === p.fontId) || {}).family || '') : "Word uses the caption's font";
  ['sizeVal', 'opVal', 'glowVal'].forEach(id => { if ($(id)) $(id).textContent = ($(id.replace('Val', '').replace('op', 'opacity')) || {}).value + '%'; });
}
let _wmTimer = null;
function wmSet(changes) {  // merge changes into the selected words' settings, preview at once, save shortly after
  if (!wordMode) return;
  const c = captions[wordMode.src]; c.ws = c.ws || {};
  const p = Object.assign({}, wmProps(), changes);
  Object.keys(p).forEach(k => { if (p[k] === null || p[k] === undefined || p[k] === '') delete p[k]; });
  [...wordMode.idx].forEach(i => { if (Object.keys(p).length) c.ws[i] = p; else delete c.ws[i]; });
  capsVersion++; renderKey = '';
  wordSel = { src: wordMode.src, idx: new Set(wordMode.idx) };
  clearTimeout(_wmTimer);
  _wmTimer = setTimeout(() => {
    document.querySelector(`input[name=wpScope][value=${document.querySelector('input[name=wbScope]:checked').value}]`).checked = true;
    saveWordStyle(Object.keys(p).length ? p : null);
  }, 250);
}
function enterWordMode(src, idxs) {
  const c = captions[src]; if (!c) return;
  if (!wordMode) wordMode = { cap: wmCaptionValues() };
  wordMode.src = src; wordMode.idx = new Set(idxs);
  wordSel = { src, idx: new Set(idxs) };
  const words = c.text.split(/\s+/).filter(Boolean), pick = [...wordMode.idx].sort((a, b) => a - b);
  $('wbWords').textContent = pick.map(i => '“' + words[i] + '”').join(' ');
  $('wbGlobal').textContent = pick.length === 1 ? `Every “${words[pick[0]].replace(/[^\p{L}\p{N}']/gu, '')}”` : 'Every time these appear';
  const glob = pick.every(i => (window.GLOBAL_WORDS || {})[words[i].toLowerCase().replace(/[^\p{L}\p{N}']/gu, '')]) && !pick.some(i => (c.wsLocal || {})[i]);
  document.querySelector(`input[name=wbScope][value=${glob ? 'global' : 'caption'}]`).checked = true;
  $('wordBar').hidden = false; document.body.classList.add('wordmode');
  $('vid').pause(); wmShowControls(); renderKey = '';
}
function exitWordMode() {
  if (!wordMode) return;
  const cv = wordMode.cap; wordMode = null; wordSel = null;
  WM_IDS.forEach(id => $(id).value = cv[id]);
  document.querySelectorAll('.tile').forEach(t => t.classList.toggle('on', t.dataset.id === cv.style));
  document.querySelectorAll('.mtile').forEach(t => t.classList.toggle('on', t.dataset.id === cv.anim));
  if ($('font')) $('font').value = cv.fontId === null ? '' : String(cv.fontId);
  $('fontNow').textContent = cv.fontId === null ? "Using the style's own font" : 'Font: ' + ((FONTS.find(f => f.id === cv.fontId) || {}).family || '');
  $('wordBar').hidden = true; document.body.classList.remove('wordmode');
  $('wordPanel').hidden = true; renderKey = ''; applyStyle();
}
// while editing words the caption keeps its own values (the controls show the word's)
const _styleCaption = style;
style = function () {
  const o = _styleCaption();
  if (!wordMode) return o;
  const cv = wordMode.cap;
  return Object.assign(o, { size: cv.size / 100, textColor: cv.textColor, boxColor: cv.boxColor, opacity: cv.opacity / 100,
    glow: cv.glow / 100, glowColor: cv.glowColor, animDur: parseFloat(cv.animDur), style: cv.style, fontId: cv.fontId, anim: cv.anim });
};
// existing controls -> word settings
const _wmMap = {
  size: () => ({ size: Math.max(0.5, Math.min(2.5, +$('size').value / +wordMode.cap.size)) }),
  textColor: () => ({ color: $('textColor').value }),
  boxColor: () => (+$('opacity').value > 0 ? { pill: $('boxColor').value, pillOpacity: $('opacity').value / 100 } : {}),
  opacity: () => (+$('opacity').value > 0 ? { pill: $('boxColor').value, pillOpacity: $('opacity').value / 100 } : { pill: null, pillOpacity: null }),
  glow: () => (+$('glow').value > 0 ? { glow: $('glow').value / 100, glowColor: $('glowColor').value } : { glow: null, glowColor: null }),
  glowColor: () => (+$('glow').value > 0 ? { glow: $('glow').value / 100, glowColor: $('glowColor').value } : {}),
  animDur: () => ({ motionDur: parseFloat($('animDur').value) }),
};
Object.keys(_wmMap).forEach(id => $(id).addEventListener('input', () => { if (wordMode) wmSet(_wmMap[id]()); }));
const _pickStyleCaption = pickStyle;
pickStyle = function (id) {
  if (!wordMode) return _pickStyleCaption(id);
  wmSet({ style: id === wordMode.cap.style ? null : id, color: null });
  wmShowControls();
};
const _pickFontCaption = pickFont;
pickFont = async function (id) {
  if (!wordMode) return _pickFontCaption(id);
  wmSet({ fontId: id === '' || id === null ? null : Number(id) });
  wmShowControls();
};
function wmWrapAnim() {  // pickAnim lives in effects.js (loaded after this script)
  if (typeof pickAnim !== 'function' || pickAnim._wm) return;
  const orig = pickAnim;
  pickAnim = function (id, fromUser) {
    if (!wordMode) return orig(id, fromUser);
    wmSet({ motion: id === 'none' ? null : id, motionDur: parseFloat($('animDur').value) });
    wmShowControls();
  };
  pickAnim._wm = true;
}
setTimeout(wmWrapAnim, 0); window.addEventListener('load', wmWrapAnim);
$('wbDone').onclick = exitWordMode;
$('wbReset').onclick = () => { if (!wordMode) return; wmSet({}); const c = captions[wordMode.src];
  [...wordMode.idx].forEach(i => delete c.ws[i]); clearTimeout(_wmTimer);
  document.querySelector('input[name=wpScope][value=caption]').checked = true; saveWordStyle(null); wmShowControls(); };
document.querySelectorAll('input[name=wbScope]').forEach(r => r.addEventListener('change', () => { if (wordMode) wmSet({}); }));
// clicking a word (or a word chip) now enters word mode instead of opening a separate panel
openWordPanel = function (src, idxs) { enterWordMode(src, idxs); };
document.addEventListener('keydown', e => { if (e.key === 'Escape' && wordMode) exitWordMode(); });

// same curves as anim_params() in transcribe.py (used for per-word Motion-tab animations)
const WM_ANIMS = new Set(['fade', 'pop', 'zoom', 'slide_up', 'slide_down', 'slide_left', 'bounce', 'shake', 'blur', 'wipe', 'glitch', 'rise']);
const WM_HIDE_FIRST = new Set(['fade', 'pop', 'zoom', 'blur', 'rise', 'slide_up', 'slide_down', 'slide_left', 'glitch', 'wipe']);
function wmAnim(anim, p) {
  const a = { alpha: 1, scale: 1, dx: 0, dy: 0, blur: 0 }, e = 1 - Math.pow(1 - p, 3);
  const back = t => { const s = 1.70158; t -= 1; return t * t * ((s + 1) * t + s) + 1; };
  if (anim === 'fade') a.alpha = e;
  else if (anim === 'pop') { a.alpha = Math.min(1, p * 3); a.scale = 0.5 + 0.5 * back(p); }
  else if (anim === 'zoom') { a.alpha = e; a.scale = 1.5 - 0.5 * e; }
  else if (anim === 'slide_up') { a.alpha = e; a.dy = 0.8 * (1 - e); }
  else if (anim === 'slide_down') { a.alpha = e; a.dy = -0.8 * (1 - e); }
  else if (anim === 'slide_left') { a.alpha = e; a.dx = 1.5 * (1 - e); }
  else if (anim === 'bounce') { a.alpha = Math.min(1, p * 4); a.dy = -1.2 * (1 - easeBounce(p)); }
  else if (anim === 'shake') a.dx = 0.12 * Math.sin(p * Math.PI * 8) * (1 - p);
  else if (anim === 'blur') { a.alpha = e; a.blur = 0.25 * (1 - e); }
  else if (anim === 'wipe') a.alpha = e;
  else if (anim === 'glitch') { const k = 1 - p; a.dx = 0.12 * k * Math.sin(p * 97); a.dy = 0.05 * k * Math.sin(p * 61); a.alpha = Math.min(1, p * 5); }
  else if (anim === 'rise') { a.alpha = e; a.dy = 0.35 * (1 - e); }
  return a;
}
</script>
<script src="/effects.js"></script>
</body></html>
"""


def _use_project_python():
    """Re-launch with the project's .venv Python (it has the speech-to-text, image and font tools)."""
    if WINDOWS:
        venv_py = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
    else:
        venv_py = os.path.join(ROOT, ".venv", "bin", "python")
    if os.path.exists(venv_py) and os.path.realpath(sys.prefix) != os.path.realpath(os.path.join(ROOT, ".venv")):
        print("Using the project's Python (.venv) ...")
        if WINDOWS:  # os.execv doesn't replace the process on Windows; run it and pass on the exit code
            import subprocess
            try:
                sys.exit(subprocess.call([venv_py, os.path.abspath(__file__)] + sys.argv[1:]))
            except KeyboardInterrupt:
                sys.exit(0)
        os.execv(venv_py, [venv_py, os.path.abspath(__file__)] + sys.argv[1:])


def main():
    import errno
    import urllib.request
    import webbrowser
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    url = f"http://{HOST}:{PORT}"
    if WINDOWS:
        # Windows console may not be UTF-8: never crash on printing a non-English caption
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(errors="replace")
            except Exception:
                pass
        # On Windows SO_REUSEADDR lets a second server bind a port that is already in use,
        # so turn it off there to get the normal "port in use" error.
        ThreadingHTTPServer.allow_reuse_address = False
    try:
        httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError as e:
        if e.errno != errno.EADDRINUSE and getattr(e, "winerror", None) != 10048:
            raise
        try:  # is it Caption Studio that already has the port?
            urllib.request.urlopen(url + "/api/sfx", timeout=2).read()
        except Exception:
            if WINDOWS:
                sys.exit(f"Port {PORT} is used by another program. Close it, or run with "
                         f"set CAPTION_STUDIO_PORT=8766 && .venv\\Scripts\\python caption_studio.py")
            sys.exit(f"Port {PORT} is used by another program. Close it, or run with "
                     f"CAPTION_STUDIO_PORT=8766 .venv/bin/python caption_studio.py")
        print(f"Caption Studio is already running at {url}. Opening it in your browser.")
        print("(To restart it: close its window or press Ctrl+C in it, then run this again.)" if WINDOWS
              else "(To restart it: pkill -f caption_studio.py, then run this again.)")
        webbrowser.open(url)
        return
    print(f"Caption Studio running at {url}  (this {'PC' if WINDOWS else 'Mac'} only; Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    _use_project_python()
    main()
