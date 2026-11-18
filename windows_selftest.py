#!/usr/bin/env python3
"""
Caption Studio self-test. Run it on the Windows PC (it also runs on a Mac):

    .venv\\Scripts\\python windows_selftest.py          (Windows)
    .venv/bin/python windows_selftest.py               (Mac)

It checks every part of the captions pipeline and prints PASS / FAIL for each.
First run downloads the small "tiny" speech model (~75 MB). Nothing is uploaded.
It leaves no files behind (works in a temp folder and removes its test session).
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
results = []


def check(name, fn):
    try:
        detail = fn()
        results.append((True, name, detail or ""))
        print(f"PASS  {name}" + (f"  ({detail})" if detail else ""))
    except Exception as e:
        results.append((False, name, f"{type(e).__name__}: {e}"))
        print(f"FAIL  {name}\n      {type(e).__name__}: {e}")
        traceback.print_exc(limit=3)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


PORT = free_port()
os.environ["CAPTION_STUDIO_PORT"] = str(PORT)  # must be set before caption_studio is imported
tmp = tempfile.mkdtemp(prefix="caption_selftest_")
video = os.path.join(tmp, "in.mp4")
CAPS = [(0.2, 1.6, "Hello from the self test"), (1.8, 3.4, "money rocket growth"),
        (3.5, 4.8, "हिंदी 日本語 العربية")]
tr = cs = None


def t_platform():
    return f"{sys.platform}, Python {sys.version.split()[0]}"


def t_tools():
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            raise RuntimeError(f"{tool} not found on PATH (install ffmpeg, then open a new terminal)")
    return shutil.which("ffmpeg")


def t_imports():
    global tr, cs
    import PIL, fontTools, faster_whisper  # noqa: F401
    import transcribe, caption_studio, caption_effects  # noqa: F401
    tr, cs = transcribe, caption_studio
    return f"Pillow {PIL.__version__}, WINDOWS={tr.WINDOWS}"


def t_make_video():
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "testsrc=size=720x1280:rate=30:duration=5", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=5", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-shortest", video], check=True)
    return f"{os.path.getsize(video)} bytes"


def t_style_fonts():
    bad = []
    for k, st in tr.STYLES.items():
        faces = [st["font"]] + ([st["alt_font"]] if "alt_font" in st else []) + \
                [f for f, _, _ in st.get("variants", [])]
        for f in faces:
            if not os.path.exists(f[0]):
                bad.append(f"{k}: {f[0]}")
    if bad:
        raise RuntimeError("missing font files: " + "; ".join(bad))
    return f"{len(tr.STYLES)} styles, all font files exist"


def t_style_fonts_real():
    """Each style must load its OWN font, not Pillow's tiny built-in fallback."""
    from PIL import ImageFont
    for k, st in tr.STYLES.items():
        ImageFont.truetype(st["font"][0], 40, index=st["font"][1])
    return "all load with Pillow"


def t_render_all():
    d = os.path.join(tmp, "png")
    os.makedirs(d)
    for k in tr.STYLES:
        tr.render_caption("Hello world test", 720, os.path.join(d, k + ".png"), style=k)
    return f"{len(tr.STYLES)} captions drawn"


def t_font_list():
    fonts = tr.list_fonts()
    if not fonts:
        raise RuntimeError("no fonts found in " + ", ".join(tr.FONT_DIRS))
    for fid in (0, len(fonts) // 2, len(fonts) - 1):
        if not cs.font_bytes(fid):
            raise RuntimeError(f"font file for id {fid} could not be read")
    return f"{len(fonts)} fonts in the picker"


def t_emoji():
    img = tr._emoji_img("🚀", 80)
    raw = img.convert("RGBA").tobytes()
    px = [tuple(raw[i:i + 4]) for i in range(0, len(raw), 4) if raw[i + 3] > 200]
    coloured = sum(1 for r, g, b, a in px if max(r, g, b) - min(r, g, b) > 40)
    if not px or coloured < len(px) * 0.1:
        raise RuntimeError("emoji drew but not in colour")
    return f"{tr.EMOJI_FONT}"


def t_srt_unicode():
    p = os.path.join(tmp, "t.srt")
    tr.write_srt(CAPS, p)
    back = tr.parse_srt(p)
    if back[-1][2] != CAPS[-1][2]:
        raise RuntimeError(f"text changed: {back[-1][2]!r}")
    return "Hindi / Japanese / Arabic round-trip OK"


def export(name, **kw):
    out = os.path.join(tmp, f"out_{name}.mp4")
    tr.burn_captions(video, CAPS, out, **kw)
    if not os.path.exists(out) or os.path.getsize(out) < 1000:
        raise RuntimeError("no output video")
    return f"{os.path.getsize(out)} bytes"


def t_whisper():
    sess = os.path.join(tmp, "sess")
    os.makedirs(sess)
    shutil.copy(video, os.path.join(sess, "video.mp4"))
    tr.transcribe_session(sess, os.path.join(sess, "video.mp4"), "tiny", log=lambda m: None)
    for f in ("audio.wav", "words.json", "transcript.txt"):
        if not os.path.exists(os.path.join(sess, f)):
            raise RuntimeError(f"{f} not written")
    return "model ran, files written (test audio is a tone, so no words is expected)"


def t_server():
    from http.server import ThreadingHTTPServer
    if cs.WINDOWS:
        ThreadingHTTPServer.allow_reuse_address = False  # what caption_studio.main() does on Windows
    httpd = ThreadingHTTPServer((cs.HOST, cs.PORT), cs.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{PORT}"
    sid = time.strftime("%Y%m%d_%H%M%S") + "_5e1f7e57"
    sess = os.path.join(cs.UPLOAD_DIR, sid)
    try:
        # a second server on the same port must be refused (Windows would otherwise allow it)
        try:
            ThreadingHTTPServer((cs.HOST, cs.PORT), cs.Handler).server_close()
            raise RuntimeError("a second server could bind the same port")
        except OSError:
            pass
        page = urllib.request.urlopen(base + "/", timeout=10).read()
        styles = json.loads(urllib.request.urlopen(base + "/api/styles", timeout=10).read())
        if b"<html" not in page.lower() or "classic" not in styles:
            raise RuntimeError("page or styles missing")
        # a session like an upload creates, then export through the API like the page does
        os.makedirs(sess)
        shutil.copy(video, os.path.join(sess, "video.mp4"))
        tr.write_srt(CAPS, os.path.join(sess, "subtitles.srt"))
        with open(os.path.join(sess, "original_name.txt"), "w", encoding="utf-8") as f:
            f.write("vidéo test.mp4")
        caps = json.loads(urllib.request.urlopen(f"{base}/api/captions?id={sid}", timeout=10).read())
        req = urllib.request.Request(base + "/api/export", method="POST",
                                     data=json.dumps({"id": sid, "style": "hormozi", "emoji": True,
                                                      "sfx": "pop", "anim": "pop"}).encode(),
                                     headers={"Content-Type": "application/json"})
        jid = json.loads(urllib.request.urlopen(req, timeout=10).read())["job"]
        for _ in range(600):
            job = json.loads(urllib.request.urlopen(f"{base}/api/job?job={jid}", timeout=10).read())
            if job["state"] in ("done", "error"):
                break
            time.sleep(0.5)
        if job["state"] != "done":
            raise RuntimeError(f"export job: {job.get('state')} {job.get('error', '')}")
        r = urllib.request.urlopen(base + job["result"]["download"], timeout=30)
        size = len(r.read())
        if size < 1000:
            raise RuntimeError("downloaded video is empty")
        return f"page, styles, captions ({len(str(caps))} chars), export + download {size} bytes"
    finally:
        httpd.shutdown()
        httpd.server_close()
        shutil.rmtree(sess, ignore_errors=True)


try:
    check("platform", t_platform)
    check("ffmpeg / ffprobe on PATH", t_tools)
    check("Python packages + project modules import", t_imports)
    if tr:
        check("make 5 s test video", t_make_video)
        check("every style's font file exists", t_style_fonts)
        check("every style's font loads", t_style_fonts_real)
        check("draw a caption in every style", t_render_all)
        check("font picker list + font download", t_font_list)
        check("colour emoji", t_emoji)
        check("subtitles with non-English text", t_srt_unicode)
        if os.path.exists(video):
            check("export: plain style", lambda: export("classic", style="classic"))
            check("export: highlight style", lambda: export("hormozi", style="hormozi"))
            check("export: kinetic style (two fonts)", lambda: export("poster", style="poster"))
            check("export: match cut (6 fonts)", lambda: export("matchcut", style="matchcut"))
            check("export: entrance animation", lambda: export("anim", style="classic", anim="pop"))
            check("export: letter effect", lambda: export("typew", style="classic", anim="typewriter"))
            check("export: 2 words at a time", lambda: export("wp", style="classic", words_per=2))
            check("export: glow + edge fade", lambda: export("glow", style="neon", glow=0.6, feather=0.3))
            check("export: user-picked font", lambda: export("font", style="classic", font=tr.font_by_id(0)))
            check("export: emoji", lambda: export("emoji", style="classic", emoji=True))
            check("export: sound effect", lambda: export("sfx", style="classic", sfx="pop"))
            check("speech-to-text (tiny model)", t_whisper)
            check("web server: page, API, export, download", t_server)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

failed = [r for r in results if not r[0]]
print("\n" + "=" * 60)
print(f"{len(results) - len(failed)} passed, {len(failed)} failed")
for _, name, detail in failed:
    print(f"  FAILED: {name}: {detail}")
print("ALL GOOD - Caption Studio works on this machine." if not failed else
      "Send this whole output back so the failures can be fixed.")
sys.exit(1 if failed else 0)
