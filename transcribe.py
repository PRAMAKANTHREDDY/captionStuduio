#!/usr/bin/env python3
"""
Transcribe the audio of a recording session to text (runs locally, nothing uploaded).

Writes into the session folder:
  audio.wav            extracted audio
  transcript.txt       text with timestamps
  subtitles.srt        captions file (open alongside the video in VLC/IINA, etc.)
  video_captioned.*    copy of the video with the captions drawn on (always visible)

Usage:
    .venv/bin/python transcribe.py                      # latest session
    .venv/bin/python transcribe.py recordings/<session> # a specific session
    .venv/bin/python transcribe.py --model small        # more accurate, slower
"""

import argparse
import glob
import json
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
REC_DIR = os.path.join(ROOT, "recordings")


def latest_session():
    dirs = [d for d in glob.glob(os.path.join(REC_DIR, "*")) if os.path.isdir(d)]
    if not dirs:
        sys.exit("ERROR: no sessions found in recordings/")
    return max(dirs, key=os.path.getmtime)


def find_video(sess):
    for name in ("video.mov", "video.mp4"):
        p = os.path.join(sess, name)
        if os.path.exists(p):
            return p
    sys.exit(f"ERROR: no video.mov / video.mp4 in {sess}")


def ts(sec):
    m, s = divmod(sec, 60)
    return f"{int(m):02d}:{s:05.2f}"


def srt_time(sec):
    ms = int(round(sec * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _caption_text(words, language=None):
    """Join timestamp tokens into readable text across space and non-space scripts."""
    no_word_spaces = {"zh", "ja", "th", "lo", "km", "my"}
    lang = (language or "").lower().split("-")[0]
    closing_punctuation = ".,! ?;:%)]}".replace(" ", "") + "\u0964\u0965\u060c\u061b\u061f\u3002\uff01\uff1f\u3001\u300d\u300f\u3011\u300b\u3009\uff09\u201d\u2019"
    text = ""
    for word in words:
        token = word.word
        if not text:
            text = token.lstrip()
        elif token[:1].isspace() or lang in no_word_spaces:
            text += token
        elif token[:1] in closing_punctuation:
            text += token
        elif text[-1:] in "([{\u2018\u201c":
            text += token
        else:
            text += " " + token
    return text.strip()


def _reconcile_segment_words(segment, language=None):
    """Keep full segment text even when the API word timestamp list is incomplete."""
    from types import SimpleNamespace
    no_word_spaces = {"zh", "ja", "th", "lo", "km", "my"}
    lang = (language or "").lower().split("-")[0]
    if lang in no_word_spaces:
        return
    text_words = (segment.text or "").split()
    if not text_words:
        return
    timed_words = segment.words or []
    if len(text_words) == len(timed_words):
        for timed, text in zip(timed_words, text_words):
            timed.word = text
        return
    duration = max(0.0, segment.end - segment.start)
    count = len(text_words)
    segment.words = [SimpleNamespace(
        start=segment.start + duration * i / count,
        end=segment.start + duration * (i + 1) / count,
        word=text, probability=1.0) for i, text in enumerate(text_words)]


def caption_chunks(segments, max_words=7, max_secs=3.5, language=None):
    """Split speech into short captions using word timestamps."""
    chunks = []
    for seg in segments:
        cur = []
        for w in seg.words or []:
            if cur and (len(cur) >= max_words or w.end - cur[0].start > max_secs):
                chunks.append(cur)
                cur = []
            cur.append(w)
            if w.word.strip().endswith((".", ",", "?", "!")) and len(cur) >= 3:
                chunks.append(cur)
                cur = []
        if cur:
            chunks.append(cur)
    return [(c[0].start, c[-1].end, _caption_text(c, language)) for c in chunks]


def write_srt(captions, path):
    with open(path, "w", encoding="utf-8") as f:
        for i, (start, end, text) in enumerate(captions, 1):
            f.write(f"{i}\n{srt_time(start)} --> {srt_time(end)}\n{text}\n\n")


def parse_srt(path):
    """Read captions back from an .srt file -> [(start, end, text)]."""
    def secs(t):
        h, m, rest = t.strip().split(":")
        s, ms = rest.split(",")
        return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000
    caps = []
    for block in open(path, encoding="utf-8").read().strip().split("\n\n"):
        lines = block.strip().splitlines()
        if len(lines) >= 3 and "-->" in lines[1]:
            a, b = lines[1].split("-->")
            caps.append((secs(a), secs(b), " ".join(lines[2:])))
    return caps


def steady_fps(video):
    """Frame rate to normalise to. Screen recordings only store frames when the screen
    changes, so without this a caption shown during a still moment would never be drawn."""
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                          "-show_entries", "stream=r_frame_rate", "-of", "csv=p=0", video],
                         capture_output=True, text=True).stdout.strip()
    try:
        n, d = out.split("/")
        fps = float(n) / float(d)
    except (ValueError, ZeroDivisionError):
        fps = 30.0
    return int(min(60, max(24, round(fps))))


def video_size(video):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                          "-show_entries", "stream=width,height", "-of", "csv=p=0", video],
                         capture_output=True, text=True).stdout.strip()
    w, h = out.split(",")[:2]
    return int(w), int(h)


def hex_rgb(color):
    c = color.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


F = "/System/Library/Fonts/"
FS = "/System/Library/Fonts/Supplemental/"

# Caption style presets. Sizes (stroke, shadow offset/blur, radius) are fractions of the font size.
# "css" lets the web preview use the very same installed font face.
# shadows: (dx, dy, blur, color, alpha); blur 0 = hard shadow, dx=dy=0 with blur = glow.
STYLES = {
    "classic": dict(name="Classic", font=(F + "SFNS.ttf", 0),
                    css=dict(family="-apple-system, 'SF Pro Text'", weight=400),
                    text="#ffffff", box="#000000", opacity=0.75, radius=0.5),
    "netflix": dict(name="Streaming", font=(F + "Helvetica.ttc", 1),
                    css=dict(family="Helvetica", weight=700),
                    text="#ffffff", box="#000000", opacity=0.0, radius=0.2, stroke=0.05,
                    stroke_color="#000000", shadows=[(0, 0.06, 0.15, "#000000", 0.9)]),
    "youtube": dict(name="YouTube CC", font=(FS + "Arial.ttf", 0),
                    css=dict(family="Arial", weight=400),
                    text="#ffffff", box="#080808", opacity=0.8, radius=0.0),
    "impact": dict(name="Meme Impact", font=(FS + "Impact.ttf", 0),
                   css=dict(family="Impact", weight=400), upper=True,
                   text="#ffffff", box="#000000", opacity=0.0, radius=0.2, stroke=0.09,
                   stroke_color="#000000"),
    "pop": dict(name="Social Pop", font=(FS + "Arial Black.ttf", 0),
                css=dict(family="'Arial Black'", weight=900), upper=True,
                text="#ffe600", box="#000000", opacity=0.0, radius=0.2, stroke=0.1,
                stroke_color="#000000", shadows=[(0.06, 0.08, 0, "#000000", 1.0)]),
    "neon": dict(name="Neon Glow", font=(F + "Avenir Next.ttc", 2),
                 css=dict(family="'Avenir Next'", weight=600),
                 text="#e6fdff", box="#000000", opacity=0.0, radius=0.2,
                 shadows=[(0, 0, 0.35, "#00e5ff", 1.0), (0, 0, 0.12, "#00e5ff", 1.0)]),
    "sunset": dict(name="Sunset Gradient", font=(FS + "Futura.ttc", 2),
                   css=dict(family="Futura", weight=700), upper=True,
                   text="#ff5f6d", gradient="#ffc371", box="#000000", opacity=0.0, radius=0.2,
                   shadows=[(0, 0.06, 0.12, "#000000", 0.8)]),
    "terminal": dict(name="Terminal", font=(F + "Menlo.ttc", 1),
                     css=dict(family="Menlo", weight=700),
                     text="#33ff66", box="#000000", opacity=0.88, radius=0.1,
                     shadows=[(0, 0, 0.18, "#33ff66", 0.8)]),
    "typewriter": dict(name="Typewriter", font=(FS + "AmericanTypewriter.ttc", 0),
                       css=dict(family="'American Typewriter'", weight=400),
                       text="#1a1a1a", box="#f5ecd7", opacity=0.96, radius=0.12),
    "highlighter": dict(name="Highlighter", font=(F + "Avenir Next.ttc", 8),
                        css=dict(family="'Avenir Next'", weight=800),
                        text="#111111", box="#ffeb3b", opacity=1.0, radius=0.25),
    "chalk": dict(name="Chalkboard", font=(FS + "Chalkduster.ttf", 0),
                  css=dict(family="Chalkduster", weight=400),
                  text="#f4f4f0", box="#2f4f3e", opacity=0.96, radius=0.2),
    "handwritten": dict(name="Handwritten", font=(F + "MarkerFelt.ttc", 0),
                        css=dict(family="'Marker Felt'", weight=400),
                        text="#ffffff", box="#000000", opacity=0.0, radius=0.2,
                        shadows=[(0, 0.05, 0.18, "#000000", 0.95)]),
    "cinema": dict(name="Cinematic Serif", font=(FS + "Georgia Italic.ttf", 0),
                   css=dict(family="Georgia", weight=400, italic=True),
                   text="#fdf6e3", box="#000000", opacity=0.0, radius=0.2,
                   shadows=[(0, 0.04, 0.2, "#000000", 1.0)]),
}


# ---------- Premium styles (famous YouTube / Shorts / meme looks) ----------
# Extra keys:  premium=True
#   mode: None (static) | "highlight" (spoken word changes colour) | "box" (pill behind spoken word)
#         | "one_word" (only the spoken word) | "reveal" (words appear one by one)
#         | "fill" (karaoke: spoken words stay coloured)
#   hl: highlight colour (or list of colours, cycled per word); hl_text: text colour on the pill
#   scale: font size multiplier
STYLES.update({
    "hormozi": dict(name="Hormozi", premium=True, mode="highlight", hl="#ffe600",
                    font=(FS + "Futura.ttc", 4), css=dict(family="Futura", weight=800, stretch="condensed"),
                    upper=True, text="#ffffff", box="#000000", opacity=0.0, radius=0.2, stroke=0.09,
                    stroke_color="#000000", shadows=[(0, 0.06, 0.1, "#000000", 0.9)]),
    "mrbeast": dict(name="MrBeast", premium=True, mode="highlight",
                    hl=["#ffe600", "#2bff00", "#ff2e2e", "#00d5ff"],
                    font=(FS + "Arial Rounded Bold.ttf", 0), css=dict(family="'Arial Rounded MT Bold'", weight=700),
                    upper=True, text="#ffffff", box="#000000", opacity=0.0, radius=0.2, stroke=0.13,
                    stroke_color="#000000", shadows=[(0.05, 0.09, 0, "#000000", 1.0)], scale=1.15),
    "submagic": dict(name="Viral Box", premium=True, mode="box", hl="#22c55e", hl_text="#ffffff",
                     font=(FS + "Futura.ttc", 4), css=dict(family="Futura", weight=800, stretch="condensed"),
                     upper=True, text="#ffffff", box="#000000", opacity=0.0, radius=0.2, stroke=0.07,
                     stroke_color="#000000", shadows=[(0, 0.05, 0.1, "#000000", 0.8)]),
    "abdaal": dict(name="Ali Abdaal", premium=True, mode="box", hl="#7c5cff", hl_text="#ffffff",
                   font=(F + "Avenir Next.ttc", 2), css=dict(family="'Avenir Next'", weight=600),
                   text="#ffffff", box="#000000", opacity=0.0, radius=0.2,
                   shadows=[(0, 0.04, 0.15, "#000000", 0.9)]),
    "brainrot": dict(name="Brainrot 1-Word", premium=True, mode="one_word",
                     font=(FS + "Impact.ttf", 0), css=dict(family="Impact", weight=400),
                     upper=True, text="#ffffff", box="#000000", opacity=0.0, radius=0.2, stroke=0.1,
                     stroke_color="#000000", shadows=[(0, 0.07, 0.1, "#000000", 0.9)], scale=1.8),
    "karaoke": dict(name="Karaoke", premium=True, mode="fill", hl="#ff3ea5",
                    font=(F + "Avenir Next.ttc", 8), css=dict(family="'Avenir Next'", weight=800),
                    text="#ffffff", box="#000000", opacity=0.0, radius=0.2, stroke=0.07,
                    stroke_color="#1a0010", shadows=[(0, 0.05, 0.12, "#000000", 0.8)]),
    "wordpop": dict(name="Word Reveal", premium=True, mode="reveal",
                    font=(F + "Helvetica.ttc", 1), css=dict(family="Helvetica", weight=700),
                    text="#ffffff", box="#000000", opacity=0.0, radius=0.2, stroke=0.07,
                    stroke_color="#000000", shadows=[(0, 0.05, 0.12, "#000000", 0.9)]),
    "tiktok": dict(name="TikTok Text", premium=True, font=(F + "Avenir Next.ttc", 2),
                   css=dict(family="'Avenir Next'", weight=600),
                   text="#111111", box="#ffffff", opacity=1.0, radius=0.35),
    "vhs": dict(name="Retro VHS", premium=True, font=(FS + "Futura.ttc", 2),
                css=dict(family="Futura", weight=700), upper=True,
                text="#ffffff", box="#000000", opacity=0.0, radius=0.2,
                shadows=[(-0.06, 0, 0, "#ff004c", 0.9), (0.06, 0, 0, "#00f0ff", 0.9)]),
    "fire": dict(name="Fire", premium=True, font=(FS + "Impact.ttf", 0),
                 css=dict(family="Impact", weight=400), upper=True,
                 text="#fff176", gradient="#ff3d00", box="#000000", opacity=0.0, radius=0.2,
                 stroke=0.07, stroke_color="#3a0a00", shadows=[(0, 0, 0.35, "#ff6d00", 0.9)]),
    "comic": dict(name="Comic Sans Meme", premium=True, font=(FS + "Comic Sans MS Bold.ttf", 0),
                  css=dict(family="'Comic Sans MS'", weight=700),
                  text="#ffffff", box="#000000", opacity=0.0, radius=0.2, stroke=0.08,
                  stroke_color="#000000"),
    "anime": dict(name="Anime Fansub", premium=True, font=(FS + "Trebuchet MS Bold.ttf", 0),
                  css=dict(family="'Trebuchet MS'", weight=700),
                  text="#ffe94d", box="#000000", opacity=0.0, radius=0.2, stroke=0.08,
                  stroke_color="#000000", shadows=[(0.03, 0.05, 0, "#000000", 1.0)]),
})
ANIMATED_MODES = {"highlight", "box", "one_word", "reveal", "fill"}

# ---------- Trend styles (short-form video caption trends) ----------
# Extra keys: anim / anim_dur = suggested entrance animation picked with the style,
#   tail = chat-bubble tail, alt_* = poster-stack second font, variants = match-cut looks.
STYLES.update({
    "pop_scale": dict(name="Word Pop", premium=True, mode="pop", hl="#ffe600",
                      font=(F + "Avenir Next.ttc", 8), css=dict(family="'Avenir Next'", weight=800),
                      upper=True, text="#ffffff", box="#000000", opacity=0.0, radius=0.2, stroke=0.08,
                      stroke_color="#000000", shadows=[(0, 0.06, 0.12, "#000000", 0.9)]),
    "focus": dict(name="Focus Blur", premium=True, mode="focus",
                  font=(F + "Avenir Next.ttc", 2), css=dict(family="'Avenir Next'", weight=600),
                  text="#ffffff", box="#000000", opacity=0.0, radius=0.2,
                  shadows=[(0, 0.05, 0.15, "#000000", 0.9)]),
    "poster": dict(name="Poster Stack", premium=True, mode="poster",
                   font=(FS + "Futura.ttc", 4), css=dict(family="Futura", weight=800, stretch="condensed"),
                   upper=True, text="#ffffff", box="#000000", opacity=0.0, radius=0.2,
                   alt_font=(FS + "Georgia Italic.ttf", 0), alt_css=dict(family="Georgia", weight=400, italic=True),
                   alt_color="#ffd54a", main_mult=1.2, alt_mult=0.85,
                   shadows=[(0, 0.05, 0.12, "#000000", 0.9)]),
    "typecursor": dict(name="Typewriter Cursor", premium=True, mode="typewriter",
                       font=(FS + "Courier New Bold.ttf", 0), css=dict(family="'Courier New'", weight=700),
                       text="#ffffff", box="#000000", opacity=0.6, radius=0.15, cursor="#ffffff"),
    "matchcut": dict(name="Match Cut", premium=True, mode="matchcut", scale=1.6,
                     font=(FS + "Impact.ttf", 0), css=dict(family="Impact", weight=400), upper=True,
                     text="#ffffff", box="#000000", opacity=0.0, radius=0.2, stroke=0.06,
                     stroke_color="#000000",
                     variants=[  # (font, css, colour) cycled on each new word
                         ((FS + "Impact.ttf", 0), dict(family="Impact", weight=400), "#ffffff"),
                         ((FS + "Georgia Italic.ttf", 0), dict(family="Georgia", weight=400, italic=True), "#ffe600"),
                         ((FS + "Futura.ttc", 4), dict(family="Futura", weight=800, stretch="condensed"), "#ff3ea5"),
                         ((FS + "Courier New Bold.ttf", 0), dict(family="'Courier New'", weight=700), "#00e5ff"),
                         ((F + "MarkerFelt.ttc", 1), dict(family="'Marker Felt'", weight=700), "#7cff6b"),
                         ((FS + "Bodoni 72.ttc", 2), dict(family="'Bodoni 72'", weight=700), "#ffffff"),
                     ]),
    "bubble": dict(name="Chat Bubble", premium=True, font=(F + "SFNS.ttf", 0),
                   css=dict(family="-apple-system, 'SF Pro Text'", weight=400),
                   text="#ffffff", box="#0a84ff", opacity=1.0, radius=0.9, tail=True,
                   anim="pop", anim_dur=0.35),
    "glitch": dict(name="Glitch", premium=True, font=(FS + "Futura.ttc", 2),
                   css=dict(family="Futura", weight=700), upper=True,
                   text="#ffffff", box="#000000", opacity=0.0, radius=0.2,
                   shadows=[(-0.05, 0, 0, "#ff004c", 0.85), (0.05, 0, 0, "#00f0ff", 0.85),
                            (0, 0, 0.3, "#ffffff", 0.35)],
                   anim="glitch", anim_dur=0.45),
    "editorial": dict(name="Editorial", premium=True, font=(FS + "Bodoni 72.ttc", 0),
                      css=dict(family="'Bodoni 72'", weight=400),
                      text="#ffffff", box="#000000", opacity=0.0, radius=0.2,
                      shadows=[(0, 0.03, 0.2, "#000000", 0.8)], anim="rise", anim_dur=0.8),
})
KINETIC_MODES = {"pop", "focus", "poster", "typewriter", "matchcut"}

# ---------- Keyword styles: important word(s) auto-detected, user-editable ----------
STYLES.update({
    "keyword": dict(name="Keyword Hero", premium=True, mode="keyword", font=(F + "Avenir Next.ttc", 0),
                    css=dict(family="'Avenir Next'", weight=700), text="#f2ff3a", hl="#f2ff3a",
                    box="#000000", opacity=0.0, radius=0.2, align="right", leading=1.0,
                    small_mult=0.42, big_mult=1.7, title=True,
                    shadows=[(0, 0, 0.35, "#f2ff3a", 0.5), (0, 0.04, 0.12, "#000000", 0.6)],
                    anim="fade", anim_dur=0.35),
    "keyword_inline": dict(name="Keyword Highlight", premium=True, mode="keyword_inline",
                           font=(F + "Avenir Next.ttc", 2), css=dict(family="'Avenir Next'", weight=600),
                           text="#ffffff", hl="#ffe600", box="#000000", opacity=0.0, radius=0.2,
                           big_mult=1.3, stroke=0.06, stroke_color="#000000",
                           shadows=[(0, 0.05, 0.12, "#000000", 0.9)]),
})
KEYWORD_MODES = {"keyword", "keyword_inline"}
KINETIC_MODES = KINETIC_MODES | KEYWORD_MODES

# ---------- ★★ Ultra Premium styles ----------
STYLES.update({
    "hormozi2": dict(name="Hormozi 2.0", premium=True, ultra=True, mode="hormozi2", words_per=3, emoji=True,
                     font=(FS + "Futura.ttc", 4), css=dict(family="Futura", weight=800, stretch="condensed"),
                     upper=True, text="#ffffff", hl=["#ffe600", "#39ff14"], box="#000000", opacity=0.0, radius=0.2,
                     stroke=0.1, stroke_color="#000000", scale=1.15,
                     shadows=[(0.04, 0.08, 0, "#000000", 1.0)]),
    "shine": dict(name="Shine Sweep", premium=True, ultra=True, mode="shine",
                  font=(FS + "Futura.ttc", 2), css=dict(family="Futura", weight=700), upper=True,
                  text="#ffe082", gradient="#ff8f00", box="#000000", opacity=0.0, radius=0.2,
                  shadows=[(0, 0.05, 0.1, "#3a2200", 0.9), (0, 0, 0.3, "#ffb300", 0.35)], anim="zoom", anim_dur=0.3),
    "marker": dict(name="Marker Draw", premium=True, ultra=True, mode="marker", emoji=False,
                   font=(F + "Avenir Next.ttc", 0), css=dict(family="'Avenir Next'", weight=700),
                   text="#ffffff", hl="#ffe600", hl_text="#111111", box="#000000", opacity=0.0, radius=0.2,
                   shadows=[(0, 0.05, 0.14, "#000000", 0.9)]),
    "sticker": dict(name="Sticker Pop", premium=True, ultra=True, mode="sticker",
                    font=(FS + "Arial Rounded Bold.ttf", 0), css=dict(family="'Arial Rounded MT Bold'", weight=700),
                    text="#111111", hl="#ffffff", box="#000000", opacity=0.0, radius=0.2,
                    shadows=[(0.03, 0.08, 0.1, "#000000", 0.55)]),
})
KINETIC_MODES = KINETIC_MODES | {"hormozi2", "shine", "marker", "sticker"}
NEEDS_KW = KEYWORD_MODES | {"marker"}
STICKER_TILT = [-4, 3, -2, 4, -3, 2, -5, 3]

# ---------- ★★ Ultra Premium, part 2 ----------
STYLES.update({
    "extrude": dict(name="3D Extruded", premium=True, ultra=True, mode="extrude",
                    font=(FS + "Arial Black.ttf", 0), css=dict(family="'Arial Black'", weight=900), upper=True,
                    text="#ffe600", box="#000000", opacity=0.0, radius=0.2, stroke=0.04, stroke_color="#3a1300",
                    extrude=dict(depth=0.16, dx=0.55, dy=1.0, color="#e65100", dark="#5d1a00"),
                    shadows=[(0.12, 0.26, 0.14, "#000000", 0.55)], anim="pop", anim_dur=0.3),
    "underline": dict(name="Underline Draw", premium=True, ultra=True, mode="underline",
                      font=(F + "Avenir Next.ttc", 0), css=dict(family="'Avenir Next'", weight=700),
                      text="#ffffff", hl="#00e5ff", box="#000000", opacity=0.0, radius=0.2,
                      shadows=[(0, 0.05, 0.14, "#000000", 0.9)]),
    "neon_flicker": dict(name="Neon Flicker", premium=True, ultra=True, mode="neon_flicker",
                         font=(F + "Avenir Next.ttc", 5), css=dict(family="'Avenir Next'", weight=500),
                         text="#fff0fb", box="#000000", opacity=0.0, radius=0.2,
                         shadows=[(0, 0, 0.55, "#ff2bd6", 1.0), (0, 0, 0.2, "#ff2bd6", 1.0), (0, 0, 0.06, "#ffffff", 0.7)]),
    "liquid": dict(name="Liquid Gradient", premium=True, ultra=True, mode="liquid",
                   font=(FS + "Futura.ttc", 2), css=dict(family="Futura", weight=700), upper=True,
                   text="#ff6ec7", liquid=["#ff6ec7", "#7873f5", "#4ade80", "#facc15"], box="#000000", opacity=0.0,
                   radius=0.2, stroke=0.035, stroke_color="#14001f", shadows=[(0, 0.05, 0.12, "#000000", 0.8)]),
    "chaos": dict(name="Chaos Bounce", premium=True, ultra=True, mode="chaos",
                  font=(FS + "Arial Black.ttf", 0), css=dict(family="'Arial Black'", weight=900), upper=True,
                  text="#ffffff", palette=["#ffffff", "#ffe600", "#00e5ff", "#ff3ea5"], box="#000000", opacity=0.0,
                  radius=0.2, stroke=0.09, stroke_color="#000000", shadows=[(0.04, 0.08, 0, "#000000", 1.0)]),
})
KINETIC_MODES = KINETIC_MODES | {"extrude", "underline", "neon_flicker", "liquid", "chaos"}
NEEDS_KW = NEEDS_KW | {"underline"}
CHAOS_TILT = [-8, 6, -5, 9, -7, 4, -9, 7]
NEON_FLICKER = [(0.05, 0.1), (0.10, 1.0), (0.16, 0.15), (0.26, 1.0), (0.31, 0.2), (0.42, 0.9), (0.47, 0.3), (1e9, 1.0)]


# ---------- Font catalogue (every font installed on this Mac, incl. Microsoft Office) ----------
FONT_DIRS = [
    "/System/Library/Fonts", "/System/Library/Fonts/Supplemental", "/Library/Fonts",
    os.path.expanduser("~/Library/Fonts"),
    "/Applications/Microsoft Word.app/Contents/Resources/DFonts",
    "/Applications/Microsoft PowerPoint.app/Contents/Resources/DFonts",
    "/Applications/Microsoft Excel.app/Contents/Resources/DFonts",
    "/Applications/Microsoft Outlook.app/Contents/Resources/DFonts",
]
# ---------- Windows: same styles, using the closest font that ships with Windows ----------
WINDOWS = sys.platform == "win32"
LINUX = sys.platform.startswith("linux")
WIN_FONT_DIR = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
LINUX_FONT_DIRS = [
    "/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/truetype/liberation",
    "/usr/share/fonts/truetype/noto", "/usr/local/share/fonts", os.path.expanduser("~/.fonts"),
]
if WINDOWS:
    FONT_DIRS.append(WIN_FONT_DIR)
    if os.environ.get("LOCALAPPDATA"):  # fonts installed "for this user only"
        FONT_DIRS.append(os.path.join(os.environ["LOCALAPPDATA"], "Microsoft", "Windows", "Fonts"))
# (macOS font file, index) -> Windows font files to try, in order (first installed one wins)
_WIN_FONTS = {
    ("SFNS.ttf", 0): ["segoeui.ttf"],
    ("Helvetica.ttc", 1): ["arialbd.ttf"],                       # Helvetica Bold
    ("Arial.ttf", 0): ["arial.ttf"],
    ("Arial Black.ttf", 0): ["ariblk.ttf"],
    ("Arial Rounded Bold.ttf", 0): ["ARLRDBD.TTF", "arialbd.ttf"],
    ("Impact.ttf", 0): ["impact.ttf"],
    ("Avenir Next.ttc", 0): ["segoeuib.ttf"],                    # Bold
    ("Avenir Next.ttc", 2): ["seguisb.ttf", "segoeuib.ttf"],     # Demi Bold
    ("Avenir Next.ttc", 5): ["segoeui.ttf"],                     # Medium
    ("Avenir Next.ttc", 8): ["seguibl.ttf", "segoeuib.ttf"],     # Heavy
    ("Futura.ttc", 2): ["GOTHICB.TTF", "segoeuib.ttf"],          # Bold
    ("Futura.ttc", 4): ["seguibl.ttf", "impact.ttf"],            # Condensed ExtraBold
    ("Menlo.ttc", 1): ["consolab.ttf", "courbd.ttf"],            # Bold
    ("AmericanTypewriter.ttc", 0): ["cour.ttf"],
    ("Chalkduster.ttf", 0): ["segoepr.ttf", "comic.ttf"],
    ("MarkerFelt.ttc", 0): ["Inkfree.ttf", "comic.ttf"],
    ("MarkerFelt.ttc", 1): ["Inkfree.ttf", "comicbd.ttf"],
    ("Georgia Italic.ttf", 0): ["georgiai.ttf"],
    ("Courier New Bold.ttf", 0): ["courbd.ttf"],
    ("Comic Sans MS Bold.ttf", 0): ["comicbd.ttf"],
    ("Trebuchet MS Bold.ttf", 0): ["trebucbd.ttf"],
    ("Bodoni 72.ttc", 0): ["BOD_R.TTF", "georgia.ttf"],          # Book
    ("Bodoni 72.ttc", 2): ["BOD_B.TTF", "georgiab.ttf"],         # Bold
}
def _windows_face(face, css):
    """Swap a style's macOS font for its Windows stand-in, and add that font's family to the CSS
    so the browser preview draws with the same font. Fonts that exist are left alone."""
    if os.path.exists(face[0]):
        return face, css
    names = _WIN_FONTS.get((os.path.basename(face[0]), face[1]), []) + ["arial.ttf"]
    new = next(((p, 0) for p in (os.path.join(WIN_FONT_DIR, n) for n in names) if os.path.exists(p)), None)
    if new is None:
        return face, css
    try:
        from PIL import ImageFont
        family = ImageFont.truetype(new[0], 12, index=new[1]).getname()[0]
    except Exception:
        return new, css
    return new, dict(css, family=f"{css['family']}, '{family}'")
if WINDOWS:
    for _st in STYLES.values():
        _st["font"], _st["css"] = _windows_face(_st["font"], _st["css"])
        if "alt_font" in _st:
            _st["alt_font"], _st["alt_css"] = _windows_face(_st["alt_font"], _st["alt_css"])
        if "variants" in _st:
            _st["variants"] = [_windows_face(f, c) + (col,) for f, c, col in _st["variants"]]
if LINUX:
    FONT_DIRS.extend(LINUX_FONT_DIRS)
    _LINUX_FONTS = {
        ("SFNS.ttf", 0): ["dejavu/DejaVuSans.ttf"],
        ("Helvetica.ttc", 1): ["liberation/LiberationSans-Bold.ttf", "dejavu/DejaVuSans-Bold.ttf"],
        ("Arial.ttf", 0): ["liberation/LiberationSans-Regular.ttf", "dejavu/DejaVuSans.ttf"],
        ("Arial Black.ttf", 0): ["dejavu/DejaVuSans-Bold.ttf"],
        ("Arial Rounded Bold.ttf", 0): ["dejavu/DejaVuSans-Bold.ttf"],
        ("Impact.ttf", 0): ["dejavu/DejaVuSansCondensed-Bold.ttf", "dejavu/DejaVuSans-Bold.ttf"],
        ("Avenir Next.ttc", 0): ["dejavu/DejaVuSans-Bold.ttf"],
        ("Avenir Next.ttc", 2): ["dejavu/DejaVuSans.ttf"],
        ("Avenir Next.ttc", 5): ["dejavu/DejaVuSans.ttf"],
        ("Avenir Next.ttc", 8): ["dejavu/DejaVuSans-Bold.ttf"],
        ("Futura.ttc", 2): ["dejavu/DejaVuSans-Bold.ttf"],
        ("Futura.ttc", 4): ["dejavu/DejaVuSansCondensed-Bold.ttf", "dejavu/DejaVuSans-Bold.ttf"],
        ("Menlo.ttc", 1): ["dejavu/DejaVuSansMono-Bold.ttf"],
        ("AmericanTypewriter.ttc", 0): ["dejavu/DejaVuSerif.ttf"],
        ("Chalkduster.ttf", 0): ["dejavu/DejaVuSans-Bold.ttf"],
        ("MarkerFelt.ttc", 0): ["dejavu/DejaVuSans.ttf"],
        ("MarkerFelt.ttc", 1): ["dejavu/DejaVuSans-Bold.ttf"],
        ("Georgia Italic.ttf", 0): ["liberation/LiberationSerif-Italic.ttf", "dejavu/DejaVuSerif-Italic.ttf"],
        ("Courier New Bold.ttf", 0): ["liberation/LiberationMono-Bold.ttf", "dejavu/DejaVuSansMono-Bold.ttf"],
        ("Comic Sans MS Bold.ttf", 0): ["dejavu/DejaVuSans-Bold.ttf"],
        ("Trebuchet MS Bold.ttf", 0): ["liberation/LiberationSans-Bold.ttf", "dejavu/DejaVuSans-Bold.ttf"],
        ("Bodoni 72.ttc", 0): ["liberation/LiberationSerif-Regular.ttf", "dejavu/DejaVuSerif.ttf"],
        ("Bodoni 72.ttc", 2): ["liberation/LiberationSerif-Bold.ttf", "dejavu/DejaVuSerif-Bold.ttf"],
    }

    def _linux_face(face, css):
        if os.path.exists(face[0]):
            return face, css
        names = _LINUX_FONTS.get((os.path.basename(face[0]), face[1]), [])
        path = next((os.path.join("/usr/share/fonts/truetype", name)
                     for name in names if os.path.exists(os.path.join("/usr/share/fonts/truetype", name))), None)
        if path is None:
            return face, css
        try:
            from PIL import ImageFont
            family = ImageFont.truetype(path, 12).getname()[0]
        except Exception:
            return (path, 0), css
        return (path, 0), dict(css, family=f"'{family}'")

    for _st in STYLES.values():
        _st["font"], _st["css"] = _linux_face(_st["font"], _st["css"])
        if "alt_font" in _st:
            _st["alt_font"], _st["alt_css"] = _linux_face(_st["alt_font"], _st["alt_css"])
        if "variants" in _st:
            _st["variants"] = [_linux_face(f, c) + (col,) for f, c, col in _st["variants"]]
_SKIP_FONTS = ("emoji", "lastresort", "braille", "keyboard", "symbol", "wingding", "webding",
               "dingbat", "bodoni ornaments", "marlett")
_font_cache = None


def list_fonts(refresh=False):
    """[{id, family, style, path, index}] sorted by family. Duplicates (same font in several
    Office apps) are listed once."""
    global _font_cache
    if _font_cache is not None and not refresh:
        return _font_cache
    from PIL import ImageFont
    seen, fonts = set(), []
    for d in FONT_DIRS:
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if fn.startswith(".") or not fn.lower().endswith((".ttf", ".otf", ".ttc")):
                continue
            path = os.path.join(d, fn)
            for i in range(32):
                try:
                    f = ImageFont.truetype(path, 24, index=i)
                    family, style = f.getname()
                    f.getbbox("Ab")  # must actually render
                except Exception:
                    break
                family, style = (family or "").strip(), (style or "Regular").strip()
                key = (family.lower(), style.lower())
                if (not family or family.startswith(".") or key in seen
                        or any(s in family.lower() for s in _SKIP_FONTS)):
                    continue
                seen.add(key)
                fonts.append(dict(family=family, style=style, path=path, index=i))
                if not fn.lower().endswith(".ttc"):
                    break
    fonts.sort(key=lambda f: (f["family"].lower(), f["style"] != "Regular", f["style"].lower()))
    for n, f in enumerate(fonts):
        f["id"] = n
    _font_cache = fonts
    return fonts


def font_by_id(font_id):
    """(path, index) for a catalogue id, or None for the style's own font."""
    if font_id is None:
        return None
    fonts = list_fonts()
    return (fonts[font_id]["path"], fonts[font_id]["index"]) if 0 <= font_id < len(fonts) else None



def style_extent(st, size):
    """Extra margin around the box so outlines/shadows/glows aren't clipped."""
    sw = round(st.get("stroke", 0) * size)
    sh = max([abs(dx) * size + abs(dy) * size + 3 * b * size
              for dx, dy, b, _, _ in st.get("shadows", [])] or [0])
    return int(sw + sh) + 2 + (size // 2 if st.get("tail") else 0)


def _box(W, H, rect, radius, color, opacity, feather=0.0):
    """Caption background as its own layer. feather (0-1) fades it out towards all four edges:
    alpha ramps linearly over feather * half the box's shorter side (same as the preview's CSS mask)."""
    from PIL import Image, ImageChops, ImageDraw
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(layer).rounded_rectangle(rect, radius=radius, fill=hex_rgb(color) + (int(255 * opacity),))
    if feather <= 0:
        return layer
    x0, y0, x1, y1 = [int(round(v)) for v in rect]
    bw, bh = max(1, x1 - x0 + 1), max(1, y1 - y0 + 1)
    f = max(1.0, feather * min(bw, bh) / 2)
    row = Image.new("L", (bw, 1)); col = Image.new("L", (1, bh))
    for i in range(bw):
        row.putpixel((i, 0), int(255 * min(1.0, (i + 0.5) / f, (bw - i - 0.5) / f)))
    for j in range(bh):
        col.putpixel((0, j), int(255 * min(1.0, (j + 0.5) / f, (bh - j - 0.5) / f)))
    ramp = ImageChops.multiply(row.resize((bw, bh)), col.resize((bw, bh)))
    mask = Image.new("L", (W, H), 0)
    mask.paste(ramp, (x0, y0))
    layer.putalpha(ImageChops.multiply(layer.getchannel("A"), mask))
    return layer


def apply_text_glow(img, mask, size, strength, color):
    """Make the LETTERS glow (lit from inside, like a neon tube) - nothing is drawn outside them.
    mask = where the letters are. The letters are tinted with the glow colour and get a white-hot
    core along the middle of each stroke."""
    from PIL import Image, ImageChops, ImageFilter
    g = max(0.0, min(1.0, float(strength)))
    if g <= 0:
        return img
    W, H = img.size
    tint = Image.new("RGBA", (W, H), hex_rgb(color) + (0,))
    tint.putalpha(mask.point(lambda v: int(v * 0.75 * g)))  # 0 at 0%, strong at 100%
    img = Image.alpha_composite(img, tint)
    core = ImageChops.multiply(mask.filter(ImageFilter.GaussianBlur(max(1.0, size * 0.05))), mask)
    core = core.point(lambda v: 0 if v < 110 else int((v - 110) * 255 / 145 * 0.7 * g))
    hot = Image.new("RGBA", (W, H), (255, 255, 255, 0))
    hot.putalpha(core)
    return Image.alpha_composite(img, hot)


def render_caption(text, width, path, size_frac=1 / 16, opacity=0.75,
                   text_color="#ffffff", box_color="#000000", style="classic", font=None):
    """Draw one caption (box + effects) as a transparent PNG. Returns the margin used."""
    from PIL import Image, ImageDraw, ImageFilter, ImageFont
    st = STYLES.get(style, STYLES["classic"])
    size = max(12, int(width * size_frac * st.get("scale", 1)))
    face = font or st["font"]  # font = (path, index) override chosen by the user
    try:
        font = ImageFont.truetype(face[0], size, index=face[1])
    except OSError:
        font = ImageFont.load_default(size)
    if st.get("upper"):
        text = text.upper()
    sw = round(st.get("stroke", 0) * size)
    pad = size // 2
    line_h = size * 1.25  # same as the preview's CSS line-height

    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    tw = lambda s: probe.textlength(s, font=font) + 2 * sw
    max_w = width * 0.9
    lines, cur = [], ""
    for word in text.split():  # wrap to fit the video width
        trial = f"{cur} {word}".strip()
        if cur and tw(trial) > max_w:
            lines.append(cur)
            cur = word
        else:
            cur = trial
    lines.append(cur)

    box_w = int(max(tw(l) for l in lines)) + 2 * pad
    box_h = int(line_h * len(lines)) + 2 * pad
    e = style_extent(st, size)
    W, H = box_w + 2 * e, box_h + 2 * e
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))

    if opacity > 0:
        img = Image.alpha_composite(img, _box(W, H, [e, e, e + box_w - 1, e + box_h - 1],
                                              int(st.get("radius", 0.5) * size), box_color, opacity, st.get("feather", 0)))
        if st.get("tail") and not st.get("feather"):  # chat-bubble tail at the bottom-right corner
            x1, y1 = e + box_w - 1, e + box_h - 1
            ImageDraw.Draw(img).polygon(
                [(x1 - pad * 1.6, y1 - pad * 0.9), (x1 + pad * 0.45, y1 + pad * 0.35), (x1 - pad * 0.2, y1 - pad * 1.4)],
                fill=hex_rgb(box_color) + (int(255 * opacity),))

    def draw_lines(draw, fill, stroke=0, dx=0, dy=0):
        for i, l in enumerate(lines):
            lw = probe.textlength(l, font=font)
            x = e + (box_w - lw) / 2 + dx
            y = e + pad + i * line_h + line_h / 2 + dy
            draw.text((x, y), l, font=font, fill=fill, anchor="lm",
                      stroke_width=stroke, stroke_fill=fill)

    for dx, dy, blur, color, alpha in st.get("shadows", []):  # shadows and glows
        mask = Image.new("L", (W, H), 0)
        draw_lines(ImageDraw.Draw(mask), 255, sw, dx * size, dy * size)
        if blur:
            mask = mask.filter(ImageFilter.GaussianBlur(blur * size / 2))
        layer = Image.new("RGBA", (W, H), hex_rgb(color) + (0,))
        layer.putalpha(mask.point(lambda v: int(v * alpha)))
        img = Image.alpha_composite(img, layer)

    d = ImageDraw.Draw(img)
    if sw:  # outline
        draw_lines(d, hex_rgb(st.get("stroke_color", "#000000")) + (255,), sw)
    if st.get("gradient"):  # vertical gradient fill
        mask = Image.new("L", (W, H), 0)
        draw_lines(ImageDraw.Draw(mask), 255)
        top, bot = hex_rgb(text_color), hex_rgb(st["gradient"])
        y0, y1 = e + pad, e + pad + line_h * len(lines)
        col = Image.new("RGBA", (1, H))
        for y in range(H):
            t = min(1, max(0, (y - y0) / max(1, y1 - y0)))
            col.putpixel((0, y), tuple(int(top[k] + (bot[k] - top[k]) * t) for k in range(3)) + (255,))
        grad = col.resize((W, H))
        layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        layer.paste(grad, (0, 0), mask)
        img = Image.alpha_composite(img, layer)
    else:
        draw_lines(d, hex_rgb(text_color) + (255,))
    if st.get("glow"):  # letters lit from inside
        m = Image.new("L", (W, H), 0)
        draw_lines(ImageDraw.Draw(m), 255)
        img = apply_text_glow(img, m, size, *st["glow"])
    img.save(path)
    return e


def _burn_core(video, captions, out, size_frac=1 / 16, y_frac=0.62, opacity=0.75,
               text_color="#ffffff", box_color="#000000", x_frac=0.5, style="classic", hl=None,
               font=None, anim="none", anim_dur=0.3):
    """Re-encode the video with every caption drawn on at its time.
    font: optional (path, index) to use instead of the style's own font.
    anim: entrance animation (see ANIMATIONS); "none" keeps the original export paths."""
    if anim and anim != "none":
        return burn_motion(video, captions, out, size_frac, y_frac, opacity, text_color,
                           box_color, x_frac, style, hl, font, anim, anim_dur)
    if STYLES.get(style, {}).get("mode") in ANIMATED_MODES:
        return burn_animated(video, captions, out, size_frac, y_frac, opacity, text_color,
                             box_color, x_frac, style, hl, font)
    import tempfile
    width, height = video_size(video)
    tmp = tempfile.mkdtemp(prefix="captions_")
    inputs, filters, last = ["-i", video], [f"[0:v]fps={steady_fps(video)}[base]"], "base"
    for i, (start, end, text) in enumerate(captions):
        png = os.path.join(tmp, f"c{i}.png")
        e = render_caption(text, width, png, size_frac, opacity, text_color, box_color, style, font)
        inputs += ["-i", png]
        tag = f"v{i}"
        # x_frac = horizontal centre of the caption box (kept inside the frame),
        # y_frac = top of the box, both fractions of the video size; e = effects margin
        bw = f"(overlay_w-{2 * e})"
        x_expr = f"'clip(main_w*{x_frac:.4f}-{bw}/2,0,main_w-{bw})-{e}'"
        filters.append(f"[{last}][{i + 1}:v]overlay=x={x_expr}:y=main_h*{y_frac:.4f}-{e}:"
                       f"enable='between(t,{start:.3f},{end:.3f})'[{tag}]")
        last = tag
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-y"] + inputs
    if len(filters) > 1:
        cmd += ["-filter_complex", ";".join(filters), "-map", f"[{last}]"]
    else:
        cmd += ["-map", "0:v"]
    cmd += ["-map", "0:a?", "-c:v", "libx264", "-crf", "20", "-preset", "veryfast",
            "-pix_fmt", "yuv420p", "-c:a", "copy", out]
    try:
        subprocess.run(cmd, check=True, stdin=subprocess.DEVNULL)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def word_times(sess, start, end, text):
    """Start/end time of each word of a caption. Uses words.json when the words line up,
    otherwise spreads the caption's time across its words by length (works after edits too)."""
    words = text.split()
    if not words:
        return []
    p = os.path.join(sess, "words.json") if sess else ""
    if p and os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            ws = [w for w in json.load(f).get("words", [])
                  if w["start"] >= start - 0.05 and w["end"] <= end + 0.05]
        if len(ws) == len(words):
            return [(w["start"], w["end"]) for w in ws]
    total = sum(len(w) for w in words)
    t, out = start, []
    for w in words:
        d = (end - start) * len(w) / total
        out.append((t, t + d))
        t += d
    return out


def hl_color(st, i, override=None):
    if override:
        return override
    hl = st.get("hl", "#ffe600")
    return hl[i % len(hl)] if isinstance(hl, list) else hl


def render_words(words, active, width, size_frac, opacity, text_color, box_color, style, hl=None,
                 font=None):
    """Draw one animation state of an animated caption. Returns (image, margin)."""
    from PIL import Image, ImageDraw, ImageFilter, ImageFont
    st = STYLES[style]
    mode = st.get("mode")
    size = max(12, int(width * size_frac * st.get("scale", 1)))
    face = font or st["font"]
    font = ImageFont.truetype(face[0], size, index=face[1])
    if st.get("upper"):
        words = [w.upper() for w in words]
    idx = list(range(len(words)))
    if mode == "one_word":
        words, idx = [words[active]], [active]
    sw = round(st.get("stroke", 0) * size)
    pad = size // 2
    line_h = size * 1.25
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    space = probe.textlength(" ", font=font)
    wl = [probe.textlength(w, font=font) for w in words]

    lines, cur, cur_w = [], [], 0  # wrap: lists of word positions
    for k, w in enumerate(wl):
        add = w if not cur else cur_w + space + w
        if cur and add + 2 * sw > width * 0.9:
            lines.append(cur)
            cur, cur_w = [k], w
        else:
            cur.append(k)
            cur_w = add
    lines.append(cur)
    line_w = [sum(wl[k] for k in ln) + space * (len(ln) - 1) for ln in lines]

    box_w = int(max(line_w) + 2 * sw) + 2 * pad
    box_h = int(line_h * len(lines)) + 2 * pad
    e = style_extent(st, size) + int(size * 0.2)
    W, H = box_w + 2 * e, box_h + 2 * e
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    if opacity > 0:
        img = Image.alpha_composite(img, _box(W, H, [e, e, e + box_w - 1, e + box_h - 1],
                                              int(st.get("radius", 0.5) * size), box_color, opacity, st.get("feather", 0)))

    pos = {}  # word position -> (x, y_center)
    for li, ln in enumerate(lines):
        x = e + (box_w - line_w[li]) / 2
        for k in ln:
            pos[k] = (x, e + pad + li * line_h + line_h / 2)
            x += wl[k] + space

    def visible(k):
        return mode != "reveal" or idx[k] <= active

    def draw_words(draw, fill_fn, stroke=0, dx=0, dy=0):
        for k, w in enumerate(words):
            if visible(k):
                x, y = pos[k]
                f = fill_fn(k)
                draw.text((x + dx, y + dy), w, font=font, fill=f, anchor="lm",
                          stroke_width=stroke, stroke_fill=f)

    for dx, dy, blur, color, alpha in st.get("shadows", []):
        mask = Image.new("L", (W, H), 0)
        draw_words(ImageDraw.Draw(mask), lambda k: 255, sw, dx * size, dy * size)
        if blur:
            mask = mask.filter(ImageFilter.GaussianBlur(blur * size / 2))
        layer = Image.new("RGBA", (W, H), hex_rgb(color) + (0,))
        layer.putalpha(mask.point(lambda v: int(v * alpha)))
        img = Image.alpha_composite(img, layer)

    d = ImageDraw.Draw(img)
    if mode == "box":  # pill behind the spoken word
        k = idx.index(active)
        x, y = pos[k]
        p = size * 0.18
        d.rounded_rectangle([x - p, y - size * 0.62, x + wl[k] + p, y + size * 0.62],
                            radius=int(size * 0.22), fill=hex_rgb(hl_color(st, active, hl)) + (255,))
    if sw:
        sc = hex_rgb(st.get("stroke_color", "#000000")) + (255,)
        draw_words(d, lambda k: sc, sw)

    def fill(k):
        i = idx[k]
        if mode == "highlight" and i == active:
            return hex_rgb(hl_color(st, i, hl)) + (255,)
        if mode == "fill" and i <= active:
            return hex_rgb(hl_color(st, i, hl)) + (255,)
        if mode == "box" and i == active:
            return hex_rgb(st.get("hl_text", text_color)) + (255,)
        return hex_rgb(text_color) + (255,)
    draw_words(d, fill)
    if st.get("glow"):  # letters lit from inside
        m = Image.new("L", (W, H), 0)
        draw_words(ImageDraw.Draw(m), lambda k: 255)
        img = apply_text_glow(img, m, size, *st["glow"])
    return img, e


def burn_animated(video, captions, out, size_frac, y_frac, opacity, text_color, box_color,
                  x_frac, style, hl=None, font=None):
    """Word-by-word captions: every animation state becomes a full-frame transparent PNG,
    played as ONE overlay stream (ffmpeg concat), so long videos stay fast."""
    import tempfile
    from PIL import Image
    width, height = video_size(video)
    dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                "-of", "csv=p=0", video], capture_output=True, text=True).stdout.strip() or 0)
    sess = os.path.dirname(os.path.abspath(video))
    tmp = tempfile.mkdtemp(prefix="captions_")
    try:
        blank = os.path.join(tmp, "blank.png")
        Image.new("RGBA", (width, height), (0, 0, 0, 0)).save(blank, compress_level=1)
        events, n = [], 0
        for cs, ce, text in captions:
            words = text.split()
            times = word_times(sess, cs, ce, text)
            for wi in range(len(words)):
                s = cs if wi == 0 else times[wi][0]
                e_ = times[wi + 1][0] if wi + 1 < len(words) else ce
                if e_ - s < 0.02:
                    continue
                img, m = render_words(words, wi, width, size_frac, opacity, text_color, box_color, style, hl,
                                      font)
                bw = img.width - 2 * m
                x = int(max(0, min(width - bw, width * x_frac - bw / 2)) - m)
                y = int(height * y_frac - m)
                frame = Image.new("RGBA", (width, height), (0, 0, 0, 0))
                # paste with clipping at the frame edges
                cx0, cy0 = max(0, -x), max(0, -y)
                crop = img.crop((cx0, cy0, min(img.width, width - x), min(img.height, height - y)))
                frame.alpha_composite(crop, (x + cx0, y + cy0))
                p = os.path.join(tmp, f"s{n}.png")
                n += 1
                frame.save(p, compress_level=1)
                events.append((s, e_, p))

        lst, t = [], 0.0
        for s, e_, p in sorted(events):
            s = max(s, t)
            if e_ <= s:
                continue
            if s - t > 0.001:
                lst.append((blank, s - t))
            lst.append((p, e_ - s))
            t = e_
        lst.append((blank, max(0.1, dur - t + 1)))
        listfile = os.path.join(tmp, "list.txt")
        with open(listfile, "w", encoding="utf-8") as f:
            for p, d in lst:
                f.write(f"file '{p}'\nduration {d:.3f}\n")
            f.write(f"file '{lst[-1][0]}'\n")  # concat needs the last file repeated

        cmd = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", video,
               "-f", "concat", "-safe", "0", "-i", listfile,
               "-filter_complex", f"[0:v]fps={steady_fps(video)}[b];[1:v]format=rgba[c];[b][c]overlay=0:0:eof_action=pass[v]",
               "-map", "[v]", "-map", "0:a?", "-c:v", "libx264", "-crf", "20", "-preset", "veryfast",
               "-pix_fmt", "yuv420p", "-c:a", "copy", "-t", f"{dur:.3f}", out]
        subprocess.run(cmd, check=True, stdin=subprocess.DEVNULL)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------- Caption entrance animations (work with every style and font) ----------
ANIMATIONS = {
    "none": "None", "fade": "Fade", "pop": "Pop", "zoom": "Zoom", "slide_up": "Slide up",
    "slide_down": "Slide down", "slide_left": "Slide in", "bounce": "Bounce", "shake": "Shake",
    "blur": "Blur in", "wipe": "Wipe", "glitch": "Glitch", "rise": "Rise (editorial)",
    "spring": "Spring", "flip_x": "3D flip (up)", "flip_y": "3D flip (side)", "mask_rise": "Mask rise",
    # letter / word effects, drawn by caption_effects.py
    "typewriter": "Typewriter", "karaoke": "Karaoke", "word_pop": "Word pop", "wave": "Wave",
}


def _ease_out(t):
    return 1 - (1 - t) ** 3


def _ease_back(t, s=1.70158):  # overshoots a little, then settles (pop)
    t -= 1
    return t * t * ((s + 1) * t + s) + 1


def _ease_bounce(t):
    n, d = 7.5625, 2.75
    if t < 1 / d:
        return n * t * t
    if t < 2 / d:
        t -= 1.5 / d
        return n * t * t + 0.75
    if t < 2.5 / d:
        t -= 2.25 / d
        return n * t * t + 0.9375
    t -= 2.625 / d
    return n * t * t + 0.984375


def anim_params(anim, p):
    """Animation state at progress p (0..1): alpha, scale, dx/dy (fraction of caption height),
    blur (fraction of caption height) and wipe (visible fraction of width)."""
    a = dict(alpha=1.0, scale=1.0, dx=0.0, dy=0.0, blur=0.0, wipe=1.0)
    e = _ease_out(p)
    if anim == "fade":
        a["alpha"] = e
    elif anim == "pop":
        a["alpha"] = min(1, p * 3)
        a["scale"] = 0.5 + 0.5 * _ease_back(p)
    elif anim == "zoom":
        a["alpha"] = e
        a["scale"] = 1.5 - 0.5 * e
    elif anim == "slide_up":
        a["alpha"] = e
        a["dy"] = 0.8 * (1 - e)
    elif anim == "slide_down":
        a["alpha"] = e
        a["dy"] = -0.8 * (1 - e)
    elif anim == "slide_left":
        a["alpha"] = e
        a["dx"] = 1.5 * (1 - e)
    elif anim == "bounce":
        a["alpha"] = min(1, p * 4)
        a["dy"] = -1.2 * (1 - _ease_bounce(p))
    elif anim == "shake":
        import math
        a["dx"] = 0.12 * math.sin(p * math.pi * 8) * (1 - p)
    elif anim == "blur":
        a["alpha"] = e
        a["blur"] = 0.25 * (1 - e)
    elif anim == "wipe":
        a["wipe"] = e
    elif anim == "glitch":  # RGB split + jitter that settles
        import math
        k = 1 - p
        a["rgb"] = 0.09 * k
        a["dx"] = 0.12 * k * math.sin(p * 97.0)
        a["dy"] = 0.05 * k * math.sin(p * 61.0)
        a["alpha"] = min(1, p * 5)
    elif anim == "rise":  # slow, smooth editorial slide + fade
        a["alpha"] = e
        a["dy"] = 0.35 * (1 - e)
    elif anim == "spring":  # tight elastic recoil: starts small, overshoots, settles
        import math
        a["alpha"] = min(1, p * 6)
        a["scale"] = 1 - 0.6 * math.exp(-6 * p) * math.cos(11 * p)
    elif anim in ("flip_x", "flip_y"):  # 3D flip, drawn as a squash that opens to full size
        import math
        a["alpha"] = min(1, p * 4)
        a["sy" if anim == "flip_x" else "sx"] = max(0.02, math.cos(math.radians(90 * (1 - e))))
    elif anim == "mask_rise":  # slides up out of an invisible line (clipped to its own box)
        a["dy"] = 1.0 * (1 - e)
        a["mask"] = 1.0
    return a


def _transform(img, prm):
    """Apply one animation state to a caption image. Returns (image, shift_x, shift_y)."""
    from PIL import Image, ImageFilter
    w, h = img.size
    out = img
    if prm.get("rgb", 0) > 0:  # chromatic split: red copy left, cyan copy right, original on top
        off = max(1, int(prm["rgb"] * h))
        alpha = img.getchannel("A")
        red = Image.new("RGBA", (w, h), (255, 0, 76, 0)); red.putalpha(alpha.point(lambda v: int(v * .8)))
        cyan = Image.new("RGBA", (w, h), (0, 240, 255, 0)); cyan.putalpha(alpha.point(lambda v: int(v * .8)))
        base = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        base.alpha_composite(red.crop((off, 0, w, h)), (0, 0))
        base.alpha_composite(cyan.crop((0, 0, w - off, h)), (off, 0))
        out = Image.alpha_composite(base, img)
    if prm["blur"] > 0:
        out = out.filter(ImageFilter.GaussianBlur(prm["blur"] * h))
    if prm["wipe"] < 1:
        mask = Image.new("L", (w, h), 0)
        mask.paste(255, (0, 0, max(0, int(w * prm["wipe"])), h))
        a = out.getchannel("A")
        out = out.copy()
        out.putalpha(Image.composite(a, Image.new("L", (w, h), 0), mask))
    if prm["alpha"] < 1:
        out = out.copy()
        out.putalpha(out.getchannel("A").point(lambda v: int(v * max(0, prm["alpha"]))))
    sx = sy = 0
    s = max(0.05, prm["scale"])
    fx, fy = max(0.02, prm.get("sx", 1.0)), max(0.02, prm.get("sy", 1.0))
    if prm.get("mask"):  # slide inside the caption's own box; nothing shows outside it
        shifted = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        shifted.paste(out, (int(prm["dx"] * h), int(prm["dy"] * h)))
        out, prm = shifted, dict(prm, dx=0, dy=0)
    if abs(s - 1) > 1e-3 or abs(fx - 1) > 1e-3 or abs(fy - 1) > 1e-3:
        nw, nh = max(1, int(w * s * fx)), max(1, int(h * s * fy))
        out = out.resize((nw, nh), Image.LANCZOS)
        sx, sy = (w - nw) // 2, (h - nh) // 2  # keep centred
    return out, sx + int(prm["dx"] * h), sy + int(prm["dy"] * h)


def _place(frame_size, img, x, y):
    """Full-frame transparent image with img pasted at (x, y), clipped at the edges."""
    from PIL import Image
    W, H = frame_size
    frame = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    cx0, cy0 = max(0, -x), max(0, -y)
    cx1, cy1 = min(img.width, W - x), min(img.height, H - y)
    if cx1 > cx0 and cy1 > cy0:
        frame.alpha_composite(img.crop((cx0, cy0, cx1, cy1)), (x + cx0, y + cy0))
    return frame


def burn_motion(video, captions, out, size_frac, y_frac, opacity, text_color, box_color,
                x_frac, style, hl=None, font=None, anim="fade", anim_dur=0.3):
    """Captions with an entrance animation. Every state/frame becomes a full-frame PNG played
    as ONE overlay stream (ffmpeg concat). Used only when an animation is chosen."""
    import tempfile
    from PIL import Image
    width, height = video_size(video)
    fps = steady_fps(video)
    dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                "-of", "csv=p=0", video], capture_output=True, text=True).stdout.strip() or 0)
    sess = os.path.dirname(os.path.abspath(video))
    st = STYLES.get(style, STYLES["classic"])
    word_mode = st.get("mode") in ANIMATED_MODES
    tmp = tempfile.mkdtemp(prefix="captions_")
    try:
        blank = os.path.join(tmp, "blank.png")
        Image.new("RGBA", (width, height), (0, 0, 0, 0)).save(blank, compress_level=1)
        counter = [0]

        def save(frame):
            p = os.path.join(tmp, f"f{counter[0]}.png")
            counter[0] += 1
            frame.save(p, compress_level=1)
            return p

        def position(img, m):
            bw = img.width - 2 * m
            return int(max(0, min(width - bw, width * x_frac - bw / 2)) - m), int(height * y_frac - m)

        # states: (start, end, image, margin, animate_in)
        states = []
        for cs, ce, text in captions:
            if word_mode:
                words = text.split()
                times = word_times(sess, cs, ce, text)
                for wi in range(len(words)):
                    s = cs if wi == 0 else times[wi][0]
                    e_ = times[wi + 1][0] if wi + 1 < len(words) else ce
                    if e_ - s < 0.02:
                        continue
                    img, m = render_words(words, wi, width, size_frac, opacity, text_color,
                                          box_color, style, hl, font)
                    # one-word captions animate every word; others only when the caption appears
                    states.append((s, e_, img, m, wi == 0 or st.get("mode") == "one_word"))
            else:
                p = os.path.join(tmp, "cap.png")
                m = render_caption(text, width, p, size_frac, opacity, text_color, box_color, style, font)
                states.append((cs, ce, Image.open(p).copy(), m, True))

        events = []  # (start, end, png)
        step = 1.0 / fps
        for s, e_, img, m, animate in states:
            x, y = position(img, m)
            t = s
            if animate and anim != "none":
                n = max(1, int(round(min(anim_dur, e_ - s) * fps)))
                for k in range(n):
                    a_img, ox, oy = _transform(img, anim_params(anim, (k + 1) / (n + 1)))
                    events.append((t, min(e_, t + step), save(_place((width, height), a_img, x + ox, y + oy))))
                    t += step
                    if t >= e_:
                        break
            if t < e_:
                events.append((t, e_, save(_place((width, height), img, x, y))))

        lst, t = [], 0.0
        for s, e_, p in sorted(events):
            s = max(s, t)
            if e_ - s <= 0.0005:
                continue
            if s - t > 0.001:
                lst.append((blank, s - t))
            lst.append((p, e_ - s))
            t = e_
        lst.append((blank, max(0.1, dur - t + 1)))
        listfile = os.path.join(tmp, "list.txt")
        with open(listfile, "w", encoding="utf-8") as f:
            for p, d in lst:
                f.write(f"file '{p}'\nduration {d:.4f}\n")
            f.write(f"file '{lst[-1][0]}'\n")
        cmd = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", video,
               "-f", "concat", "-safe", "0", "-i", listfile,
               "-filter_complex", f"[0:v]fps={fps}[b];[1:v]format=rgba[c];[b][c]overlay=0:0:eof_action=pass[v]",
               "-map", "[v]", "-map", "0:a?", "-c:v", "libx264", "-crf", "20", "-preset", "veryfast",
               "-pix_fmt", "yuv420p", "-c:a", "copy", "-t", f"{dur:.3f}", out]
        subprocess.run(cmd, check=True, stdin=subprocess.DEVNULL)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------- Kinetic captions (frame-accurate, speech-synced), word splitting, sound effects ----------
_font_obj_cache = {}


def _font_obj(face, size):
    from PIL import ImageFont
    key = (face[0], face[1], size)
    if key not in _font_obj_cache:
        try:
            _font_obj_cache[key] = ImageFont.truetype(face[0], size, index=face[1])
        except OSError:
            _font_obj_cache[key] = ImageFont.load_default(size)
    return _font_obj_cache[key]


# ---------- keyword detection (text + how the word was spoken) and user overrides ----------
STOPWORDS = set("""a an the and or but if so of to in on at by for with from up down out over under into onto
about as is am are was were be been being do does did done have has had having i me my mine we us our you your
he him his she her it its they them their this that these those there here what which who whom whose when where
why how not no yes just very really also too than then can could would should will shall may might must
let lets let's i'm i've i'll i'd you're you've we're they're it's that's there's what's don't doesn't didn't
can't won't isn't aren't wasn't oh uh um ah like okay ok well yeah hey hi all any some more most much many
get got go going gonna want wanna one two""".split())


def _session_video(sess):
    for ext in (".mov", ".mp4", ".m4v"):
        p = os.path.join(sess, "video" + ext)
        if os.path.exists(p):
            return p
    return None


def _clean(w):
    return "".join(ch for ch in w.lower() if ch.isalnum() or ch == "'")


def detect_keywords(sess, captions):
    """For each caption, the index of its most important word: content words (not filler),
    longer words and numbers score higher, plus words spoken louder or stretched out."""
    import array
    import math
    import wave
    samples, rate, off = None, 16000, 0.0
    wav = os.path.join(sess, "audio.wav")
    if os.path.exists(wav):
        try:
            with wave.open(wav, "rb") as w:
                rate = w.getframerate()
                samples = array.array("h", w.readframes(w.getnframes()))
            v = _session_video(sess)
            off = audio_offset(v) if v else 0.0
        except Exception:
            samples = None
    feats = []  # (caption idx, word idx, word, rms, sec_per_char)
    for ci, (cs, ce, text) in enumerate(captions):
        words = text.split()
        times = word_times(sess, cs, ce, text)
        for wi, w in enumerate(words):
            s, e = times[wi]
            rms = 0.0
            if samples:
                a, b = max(0, int((s - off) * rate)), min(len(samples), int((e - off) * rate))
                if b > a:
                    step = max(1, (b - a) // 2000)  # sample up to ~2000 points per word
                    seg = samples[a:b:step]
                    rms = math.sqrt(sum(x * x for x in seg) / len(seg))
            feats.append((ci, wi, w, rms, (e - s) / max(1, len(_clean(w)))))

    def z(vals):
        if not vals:
            return []
        m = sum(vals) / len(vals)
        sd = math.sqrt(sum((v - m) ** 2 for v in vals) / len(vals)) or 1.0
        return [(v - m) / sd for v in vals]
    zr, zd = z([f[3] for f in feats]), z([f[4] for f in feats])
    best = {}
    for k, (ci, wi, w, _, _) in enumerate(feats):
        c = _clean(w)
        score = min(len(c), 10) * 0.35
        score += 3.0 if c and c not in STOPWORDS else -3.0
        score += 1.0 if any(ch.isdigit() for ch in c) else 0.0
        score += 0.9 * zr[k] + 0.5 * max(-1.5, min(2.0, zd[k]))
        if ci not in best or score > best[ci][0]:
            best[ci] = (score, wi)
    return [[best[ci][1]] if ci in best else [] for ci in range(len(captions))]


def load_keywords(sess, captions):
    """Keyword word-indices per caption: the user's choice when saved (and the caption text
    hasn't changed since), otherwise auto-detected."""
    auto = detect_keywords(sess, captions)
    p = os.path.join(sess, "keywords.json")
    over = {}
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                over = json.load(f)
        except Exception:
            over = {}
    out = []
    for ci, (_, _, text) in enumerate(captions):
        o = over.get(str(ci))
        n = len(text.split())
        if o and o.get("text") == text and all(0 <= i < n for i in o.get("kw", [])):
            out.append(sorted(set(o["kw"])))
        else:
            out.append(auto[ci])
    return out


def save_keywords(sess, index, text, kw):
    """Store the user's keyword choice for one caption (kw=None removes it = back to auto)."""
    p = os.path.join(sess, "keywords.json")
    over = {}
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                over = json.load(f)
        except Exception:
            over = {}
    if kw is None:
        over.pop(str(index), None)
    else:
        over[str(index)] = {"text": text, "kw": sorted(set(kw))}
    with open(p, "w", encoding="utf-8") as f:
        json.dump(over, f, indent=1)


# ---------- Auto emoji above the key word (works with every style) ----------
EMOJI_FONT = "/System/Library/Fonts/Apple Color Emoji.ttc"
if WINDOWS:
    EMOJI_FONT = os.path.join(WIN_FONT_DIR, "seguiemj.ttf")  # Segoe UI Emoji (colour, any size)
elif LINUX:
    EMOJI_FONT = "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf"
EMOJI_MAP = {}
for _e, _words in {
    "💰": "money cash dollar dollars rich pay paid price cost profit income salary million millions billion",
    "🚀": "rocket launch growth grow scale fast faster boost skyrocket",
    "🔥": "fire hot lit burn burning amazing insane",
    "😂": "laugh funny lol joke hilarious",
    "🧠": "brain think thinking smart mind mindset genius",
    "💡": "idea ideas learn learned learning lesson insight tip tips",
    "❤️": "love heart loved lovely",
    "⏰": "time minute minutes hour hours clock deadline today tonight",
    "🏆": "win winner winning success successful champion best award",
    "🎯": "goal goals target focus aim",
    "💼": "work job business career office company",
    "💪": "strong strength power gym workout muscle",
    "😢": "sad cry crying sorry",
    "🤯": "wow shock shocking crazy mind-blowing unbelievable",
    "👀": "look looking watch watching see seen eyes",
    "🎵": "music song songs sound",
    "🎬": "movie movies film films cinema flicks sequel original",
    "📺": "tv television show shows series episode",
    "📱": "phone app apps mobile iphone android",
    "💻": "computer laptop code coding software tech",
    "📚": "book books read reading study",
    "🍔": "food eat eating hungry burger",
    "☕": "coffee morning",
    "🚗": "car cars drive driving",
    "✈️": "plane fly flying travel trip fighter pilot",
    "🏠": "home house",
    "🌍": "world global earth everyone",
    "⭐": "star stars famous rating imdb",
    "🛑": "stop never",
    "✅": "yes right correct done check",
    "❌": "wrong bad mistake fail failed",
    "⚠️": "warning danger careful risk",
    "🤫": "secret secrets hidden",
    "🎧": "listen listening hear audio podcast",
    "🗣️": "talk talking speak say voice alexa conversation",
    "⚔️": "military war army battle",
    "🥊": "rivalry fight fighting compete competition",
    "⚽": "sport sports football soccer",
    "🎮": "game games gaming play",
    "🔘": "button press tap",
    "🔍": "search find discover explore",
    "👤": "personal personalized profile",
    "📜": "history past",
    "🔑": "key keys keyword keywords",
    "🔒": "lock security safe private privacy",
    "✨": "new magic fresh special",
    "🎉": "party celebrate celebration congrats",
    "😊": "happy smile",
    "😡": "angry mad",
    "😴": "tired sleep sleepy",
    "🤝": "friend friends deal partner partnership team",
    "📈": "increase up revenue sales",
    "📉": "drop loss down decrease",
    "📊": "data stats chart numbers",
    "⚡": "energy electric quick instant",
    "🎁": "gift free bonus",
    "🎓": "school college university graduate",
    "🛠️": "fix tool tools build solution",
    "🧩": "problem puzzle",
}.items():
    for _w in _words.split():
        EMOJI_MAP[_w] = _e


def emoji_for(word):
    w = _clean(word)
    for cand in (w, w[:-1] if w.endswith("s") else None, w[:-2] if w.endswith("ed") else None,
                 w[:-3] if w.endswith("ing") else None):
        if cand and cand in EMOJI_MAP:
            return EMOJI_MAP[cand]
    return ""


def load_emojis(sess, captions, kws=None):
    """Emoji per caption: the user's choice if saved (and text unchanged), else one matching the
    key word (or any word) of the caption; '' = none."""
    kws = kws if kws is not None else load_keywords(sess, captions)
    p = os.path.join(sess, "emoji.json")
    over = {}
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                over = json.load(f)
        except Exception:
            over = {}
    out = []
    for ci, (_, _, text) in enumerate(captions):
        o = over.get(str(ci))
        if o and o.get("text") == text:
            out.append(o.get("emoji", ""))
            continue
        words = text.split()
        e = ""
        for i in list(kws[ci]) + list(range(len(words))):
            e = emoji_for(words[i])
            if e:
                break
        out.append(e)
    return out


def save_emoji(sess, index, text, emoji):
    p = os.path.join(sess, "emoji.json")
    over = {}
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                over = json.load(f)
        except Exception:
            over = {}
    if emoji is None:
        over.pop(str(index), None)
    else:
        over[str(index)] = {"text": text, "emoji": emoji}
    with open(p, "w", encoding="utf-8") as f:
        json.dump(over, f, indent=1)


def _emoji_img(emoji, px):
    from PIL import Image, ImageDraw, ImageFont
    f = ImageFont.truetype(EMOJI_FONT, 160)  # Apple's colour emoji only exist at 160 px
    l, t_, r, b = f.getbbox(emoji)
    img = Image.new("RGBA", (max(1, r - l + 8), max(1, b - t_ + 8)), (0, 0, 0, 0))
    ImageDraw.Draw(img).text((4 - l, 4 - t_), emoji, font=f, embedded_color=True)
    s = px / max(img.width, img.height)
    return img.resize((max(1, int(img.width * s)), max(1, int(img.height * s))), Image.LANCZOS)


def add_emoji(video_in, out, captions, sess, size_frac, x_frac, y_frac):
    """Second pass: an emoji pops in above the caption when its key word is spoken."""
    import tempfile
    from PIL import Image
    width, height = video_size(video_in)
    fps = steady_fps(video_in)
    dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                "-of", "csv=p=0", video_in], capture_output=True, text=True).stdout.strip() or 0)
    kws = load_keywords(sess, captions)
    emojis = load_emojis(sess, captions, kws)
    px = int(min(width * 0.3, width * size_frac * 2.2))
    tmp = tempfile.mkdtemp(prefix="emoji_")
    try:
        blank = os.path.join(tmp, "blank.png")
        Image.new("RGBA", (width, height), (0, 0, 0, 0)).save(blank, compress_level=1)
        events, n = [], 0
        for ci, (cs, ce, text) in enumerate(captions):
            if not emojis[ci]:
                continue
            times = word_times(sess, cs, ce, text)
            k = kws[ci][0] if kws[ci] else 0
            t0 = times[k][0] if k else cs
            base = _emoji_img(emojis[ci], px)
            cx = max(px / 2, min(width - px / 2, width * x_frac))
            bottom = height * y_frac - px * 0.08
            frames = int(0.25 * fps)
            for j in range(frames + 1):  # pop in (overshoot), then hold until the caption ends
                q = j / max(1, frames)
                sc = _ease_back(q) if q < 1 else 1.0
                im = base.resize((max(1, int(base.width * sc)), max(1, int(base.height * sc))), Image.LANCZOS)
                frame = _place((width, height), im, int(cx - im.width / 2), int(bottom - base.height / 2 - im.height / 2))
                p = os.path.join(tmp, f"e{n}.png"); n += 1
                frame.save(p, compress_level=1)
                s = t0 + j / fps
                e = ce if j == frames else min(ce, t0 + (j + 1) / fps)
                if e > s:
                    events.append((s, e, p))
        if not events:
            shutil.copyfile(video_in, out)
            return
        lst, t = [], 0.0
        for s, e_, p in sorted(events):
            s = max(s, t)
            if e_ - s <= 0.0005:
                continue
            if s - t > 0.001:
                lst.append((blank, s - t))
            lst.append((p, e_ - s))
            t = e_
        lst.append((blank, max(0.1, dur - t + 1)))
        listfile = os.path.join(tmp, "list.txt")
        with open(listfile, "w", encoding="utf-8") as f:
            for p, d in lst:
                f.write(f"file '{p}'\nduration {d:.4f}\n")
            f.write(f"file '{lst[-1][0]}'\n")
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", video_in, "-f", "concat", "-safe", "0",
                        "-i", listfile, "-filter_complex",
                        f"[0:v]fps={fps}[b];[1:v]format=rgba[c];[b][c]overlay=0:0:eof_action=pass[v]",
                        "-map", "[v]", "-map", "0:a?", "-c:v", "libx264", "-crf", "20", "-preset", "veryfast",
                        "-pix_fmt", "yuv420p", "-c:a", "copy", "-t", f"{dur:.3f}", out],
                       check=True, stdin=subprocess.DEVNULL)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------- per-word styles: colour, size, font, glow, highlighter, underline, pill, motion ----------
WORD_MOTIONS = ("none", "pop", "shake", "bounce", "wiggle", "pulse")


def _ws_load(sess):
    p = os.path.join(sess, "word_styles.json")
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
            return {"caption": d.get("caption", {}), "global": d.get("global", {})}
        except Exception:
            pass
    return {"caption": {}, "global": {}}


def word_key(w):
    """Key for 'every time this word appears' (case and punctuation ignored)."""
    return _clean(w)


def load_word_styles(sess, captions):
    """Per caption: {word index: props}. 'Every time it appears' styles first, then this-caption
    styles on top (only while the caption text is unchanged)."""
    d = _ws_load(sess)
    out = []
    for ci, (_, _, text) in enumerate(captions):
        words = text.split()
        res = {}
        for wi, w in enumerate(words):
            g = d["global"].get(word_key(w))
            if g:
                res[wi] = dict(g)
        o = d["caption"].get(str(ci))
        if o and o.get("text") == text:
            for k, v in o.get("words", {}).items():
                i = int(k)
                if 0 <= i < len(words):
                    res[i] = {**res.get(i, {}), **v}
        out.append(res)
    return out


def save_word_style(sess, index, text, word_indices, scope, props):
    """props=None clears. scope 'caption' = only these words of this caption,
    'global' = every time each of these words appears in the video."""
    d = _ws_load(sess)
    words = text.split()
    if scope == "global":
        for wi in word_indices:
            k = word_key(words[wi])
            if not k:
                continue
            if props is None:
                d["global"].pop(k, None)
            else:
                d["global"][k] = props
    else:
        o = d["caption"].get(str(index))
        if not o or o.get("text") != text:
            o = {"text": text, "words": {}}
        for wi in word_indices:
            if props is None:
                o["words"].pop(str(wi), None)
            else:
                o["words"][str(wi)] = props
        if o["words"]:
            d["caption"][str(index)] = o
        else:
            d["caption"].pop(str(index), None)
    with open(os.path.join(sess, "word_styles.json"), "w", encoding="utf-8") as f:
        json.dump(d, f, indent=1)


def split_captions_map(captions, n, sess):
    """Like split_captions, but also returns where each piece came from: (s, e, text, src, offset)."""
    out = []
    for ci, (cs, ce, text) in enumerate(captions):
        words = text.split()
        if not n or n <= 0:
            out.append((cs, ce, text, ci, 0))
            continue
        times = word_times(sess, cs, ce, text)
        for g in range(0, len(words), n):
            s = cs if g == 0 else times[g][0]
            e = times[g + n][0] if g + n < len(words) else ce
            if e - s > 0.01:
                out.append((s, e, " ".join(words[g:g + n]), ci, g))
    return out


def tokens_for_any(st, words, times, t, cs, text_color, hl, font, kw=None):
    """Tokens for ANY style (so word styles can be applied to Basic and word-by-word styles too)."""
    mode = st.get("mode")
    if mode in KINETIC_MODES:
        return kinetic_tokens(st, words, times, t, cs, text_color, hl, font, kw)
    n = len(words)
    wi = 0
    for k in range(1, n):
        if t >= times[k][0]:
            wi = k
    face = font or st["font"]
    if mode in ANIMATED_MODES:
        if mode == "one_word":
            return ("one_word", wi), [dict(text=words[wi], face=face, color=text_color, k=wi)]
        toks = []
        for k, w in enumerate(words):
            tk = dict(text=w, face=face, color=text_color, k=k)
            if mode == "highlight" and k == wi:
                tk["color"] = hl_color(st, k, hl)
            elif mode == "fill" and k <= wi:
                tk["color"] = hl_color(st, k, hl)
            elif mode == "reveal":
                tk["visible"] = k <= wi
            elif mode == "box" and k == wi:
                tk["pill"] = hl_color(st, k, hl)
                tk["color"] = st.get("hl_text", text_color)
            toks.append(tk)
        return (mode, wi), toks
    return ("static",), [dict(text=w, face=face, color=text_color, k=k) for k, w in enumerate(words)]


def apply_word_props(toks, words, times, t, cs, props):
    """Apply per-word settings to tokens. Returns a key part (changes while a word's motion plays)."""
    import math
    if not props:
        return ()
    n = len(words)
    wi = 0
    for k in range(1, n):
        if t >= times[k][0]:
            wi = k
    if all("k" in tk for tk in toks):
        idx = [tk["k"] for tk in toks]
    elif len(toks) == n:
        idx = list(range(n))
    elif len(toks) == 1:
        idx = [wi]
    else:
        idx = [None] * len(toks)
    key = []
    for tk, k in zip(toks, idx):
        p = props.get(k) if k is not None else None
        if not p:
            continue
        if p.get("color"):
            tk["color"] = p["color"]; tk["solid"] = True
        if p.get("size"):
            tk["mult"] = tk.get("mult", 1.0) * float(p["size"])
        ws_st = STYLES.get(p.get("style")) if p.get("style") else None
        if ws_st:  # this word uses another caption style (font, colours, outline, shadows ...)
            tk["ost"] = ws_st
            tk["face"] = ws_st["font"]
            if not p.get("color"):
                tk["color"] = ws_st.get("text", tk.get("color")); tk["solid"] = False
            if ws_st.get("opacity", 0) > 0 and not p.get("pill"):
                tk["pill"] = ws_st.get("box", "#000000"); tk["pill_alpha"] = ws_st["opacity"]
        if p.get("fontId") is not None:
            face = font_by_id(int(p["fontId"]))
            if face:
                tk["face"] = face
        if p.get("glow"):
            tk["glow_sh"] = glow_shadows(float(p["glow"]), p.get("glowColor", "#ffffff"))
        if p.get("marker"):
            tk["marker"] = dict(color=p["marker"], p=1.0); tk.setdefault("color", "#111111")
        if p.get("underline"):
            tk["underline"] = dict(color=p["underline"], p=1.0)
        if p.get("pill"):
            tk["pill"] = p["pill"]; tk["pill_alpha"] = float(p.get("pillOpacity", 1.0))
        m = p.get("motion")
        if m and m != "none":
            tk["mm"] = True  # keep room for the movement
            ws = times[k][0] if k else cs
            dt = t - ws
            if m == "pop" and 0 <= dt < 0.25:
                q = dt / 0.25
                tk["scale"] = tk.get("scale", 1.0) * (0.6 + 0.5 * q / 0.6 if q < 0.6 else 1.1 - 0.1 * (q - 0.6) / 0.4)
                key.append((k, "p", round(q, 2)))
            elif m == "shake" and 0 <= dt < 0.4:
                tk["dxo"] = 0.12 * math.sin(dt * 60) * (1 - dt / 0.4)
                key.append((k, "s", round(dt, 2)))
            elif m == "bounce" and 0 <= dt < 0.45:
                tk["dy"] = tk.get("dy", 0) - 0.8 * (1 - _ease_bounce(dt / 0.45))
                key.append((k, "b", round(dt, 2)))
            elif m == "wiggle" and 0 <= dt < 0.5:
                tk["rot"] = tk.get("rot", 0) + 10 * math.sin(dt * 40) * (1 - dt / 0.5)
                key.append((k, "w", round(dt, 2)))
            elif m in ANIMATIONS and m != "none":  # any Motion-tab animation, played when the word is spoken
                d_ = float(p.get("motionDur", 0.35))
                if 0 <= dt < d_:
                    a = anim_params(m, dt / d_)
                    tk["alpha"] = tk.get("alpha", 1.0) * a["alpha"]
                    tk["scale"] = tk.get("scale", 1.0) * a["scale"]
                    tk["dxo"] = tk.get("dxo", 0) + a["dx"] * 0.8
                    tk["dy"] = tk.get("dy", 0) + a["dy"] * 0.8
                    if a["blur"]:
                        tk["blur"] = a["blur"] * 0.4
                    key.append((k, "a", round(dt / d_, 2)))
                elif dt < 0 and m in ("fade", "pop", "zoom", "blur", "rise", "slide_up", "slide_down", "slide_left", "glitch", "wipe"):
                    tk["alpha"] = tk.get("alpha", 1.0) * 0.0  # not spoken yet: hidden until it animates in
                    key.append((k, "a", "pre"))
            elif m == "pulse":
                ph = round(((t - cs) * 1.5) % 1, 2)
                tk["scale"] = tk.get("scale", 1.0) * (1 + 0.08 * math.sin(2 * math.pi * ph))
                key.append((k, "u", ph))
    return tuple(key)


def split_captions(captions, n, sess):
    """Re-chunk captions into n words each (1-3) using word timings; keeps edited text."""
    if not n or n <= 0:
        return captions
    out = []
    for cs, ce, text in captions:
        words = text.split()
        times = word_times(sess, cs, ce, text)
        for g in range(0, len(words), n):
            s = cs if g == 0 else times[g][0]
            e = times[g + n][0] if g + n < len(words) else ce
            if e - s > 0.01:
                out.append((s, e, " ".join(words[g:g + n])))
    return out


def kinetic_tokens(st, words, times, t, cs, text_color, hl, font, kw=None):
    """What a kinetic caption looks like at time t. Returns (key, tokens).
    kw: keyword word-indices (keyword styles)."""
    mode = st["mode"]
    if mode in KEYWORD_MODES:
        n = len(words)
        kw = [i for i in (kw or []) if 0 <= i < n] or [max(range(n), key=lambda i: len(words[i]))]
        lo, hi = min(kw), max(kw)
        ks = times[lo][0] if lo else cs
        q = (t - ks) / 0.22
        if 0 <= q < 1:  # keyword pops when it is spoken
            sc = 1 + 0.08 * (q / 0.5) if q < 0.5 else 1.08 - 0.08 * ((q - 0.5) / 0.5)
            phase = round(q, 2)
        else:
            sc, phase = 1.0, ("pre" if q < 0 else "done")
        face = font or st["font"]
        title = (lambda w: w[:1].upper() + w[1:]) if st.get("title") else (lambda w: w)
        big = hl_color(st, 0, hl)
        toks = []
        if mode == "keyword":  # small line(s) above/below one big keyword line
            line = 0
            if lo > 0:
                toks += [dict(text=title(w), face=face, line=line, mult=st.get("small_mult", 0.42), color=text_color)
                         for w in words[:lo]]
                line += 1
            toks += [dict(text=title(w), face=face, line=line, mult=st.get("big_mult", 1.7), color=big, scale=sc)
                     for w in words[lo:hi + 1]]
            line += 1
            if hi + 1 < n:
                toks += [dict(text=title(w), face=face, line=line, mult=st.get("small_mult", 0.42), color=text_color)
                         for w in words[hi + 1:]]
        else:  # one line, keyword(s) bigger and coloured
            for i, w in enumerate(words):
                k = i in kw
                toks.append(dict(text=w, face=face, mult=st.get("big_mult", 1.3) if k else 1.0,
                                 color=big if k else text_color, scale=sc if k else 1.0))
        return (tuple(kw), phase), toks
    n = len(words)
    wi = 0
    for k in range(1, n):
        if t >= times[k][0]:
            wi = k
    base_face = font or st["font"]
    toks = []
    if mode == "extrude":  # thick 3D depth (drawn by the renderer); one look per caption
        for w in words:
            toks.append(dict(text=w, face=base_face, color=text_color))
        return ("static",), toks
    if mode == "underline":  # underline draws itself under the key word(s) as spoken
        kw = [i for i in (kw or []) if 0 <= i < n] or [max(range(n), key=lambda i: len(words[i]))]
        prog = []
        for k, w in enumerate(words):
            tk = dict(text=w, face=base_face, color=text_color)
            if k in kw:
                p = min(1.0, max(0.0, (t - (times[k][0] if k else cs)) / 0.3))
                prog.append(round(p, 2))
                tk["underline"] = dict(color=hl_color(st, k, hl), p=p)
            toks.append(tk)
        return (tuple(kw), tuple(prog)), toks
    if mode == "neon_flicker":  # tube flickers on, then holds steady
        dt = t - cs
        a = next(al for until, al in NEON_FLICKER if dt < until)
        for w in words:
            toks.append(dict(text=w, face=base_face, color=text_color, alpha=a))
        return (a,), toks
    if mode == "liquid":  # colours flow through the letters
        phase = round(((t * 0.6) % 1) * 40) / 40
        for w in words:
            toks.append(dict(text=w, face=base_face, color=text_color))
        toks[0]["fx_phase"] = phase
        return (phase,), toks
    if mode == "chaos":  # words drop in with a bounce, each with its own tilt and colour
        from math import pi
        ws = times[wi][0] if wi else cs
        q = min(1.0, max(0.0, (t - ws) / 0.35))
        pal = st.get("palette", [text_color])
        for k, w in enumerate(words):
            tk = dict(text=w, face=base_face, visible=k <= wi, rot=CHAOS_TILT[k % len(CHAOS_TILT)],
                      color=text_color if (k % len(pal) == 0) else pal[k % len(pal)])
            if k == wi:
                tk["dy"] = -1.1 * (1 - _ease_bounce(q))
            toks.append(tk)
        return (wi, round(q, 2)), toks
    if mode == "hormozi2":  # spoken word: colour (yellow/green), pop, slight tilt
        ws = times[wi][0] if wi else cs
        q = min(1.0, max(0.0, (t - ws) / 0.2))
        sc = 0.85 + 0.27 * (q / 0.55) if q < 0.55 else 1.12 - 0.12 * ((q - 0.55) / 0.45)
        for k, w in enumerate(words):
            on = k == wi
            toks.append(dict(text=w, face=base_face, color=hl_color(st, k, hl) if on else text_color,
                             scale=sc if on else 1.0, rot=(-3 if k % 2 else 3) if on else 0))
        return (wi, round(q, 2)), toks
    if mode == "shine":  # gradient text; a light streak sweeps across shortly after it appears
        p = (t - cs - 0.15) / 0.7
        for w in words:
            toks.append(dict(text=w, face=base_face, color=text_color))
        if 0 <= p <= 1:
            toks[0]["fx_shine"] = round(p, 3)
        return (round(p, 3) if 0 <= p <= 1 else ("pre" if p < 0 else "done"),), toks
    if mode == "marker":  # highlighter paints behind the key word(s) as they are spoken
        kw = [i for i in (kw or []) if 0 <= i < n] or [max(range(n), key=lambda i: len(words[i]))]
        prog = []
        for k, w in enumerate(words):
            if k in kw:
                p = min(1.0, max(0.0, (t - (times[k][0] if k else cs)) / 0.35))
                prog.append(round(p, 2))
                toks.append(dict(text=w, face=base_face, color=st.get("hl_text", "#111111") if p > 0.5 else text_color,
                                 marker=dict(color=hl_color(st, k, hl), p=p), solid=True))
            else:
                toks.append(dict(text=w, face=base_face, color=text_color))
        return (tuple(kw), tuple(prog)), toks
    if mode == "sticker":  # each word on its own tilted sticker, popping in as spoken
        ws = times[wi][0] if wi else cs
        q = min(1.0, max(0.0, (t - ws) / 0.22))
        sc = 0.3 + 0.82 * (q / 0.6) if q < 0.6 else 1.12 - 0.12 * ((q - 0.6) / 0.4)
        for k, w in enumerate(words):
            toks.append(dict(text=w, face=base_face, color=text_color, visible=k <= wi,
                             sticker=hl_color(st, k, hl), rot=STICKER_TILT[k % len(STICKER_TILT)],
                             scale=sc if k == wi else 1.0))
        return (wi, round(q, 2)), toks
    if mode == "pop":
        ws = times[wi][0] if wi else cs
        q = min(1.0, max(0.0, (t - ws) / 0.18))
        sc = 0.8 + 0.25 * (q / 0.6) if q < 0.6 else 1.05 - 0.05 * ((q - 0.6) / 0.4)
        for k, w in enumerate(words):
            toks.append(dict(text=w, face=base_face, visible=k <= wi,
                             color=hl_color(st, k, hl) if k == wi else text_color,
                             scale=sc if k == wi else 1.0))
        return (wi, round(q, 2)), toks
    if mode == "focus":
        for k, w in enumerate(words):
            toks.append(dict(text=w, face=base_face, color=text_color,
                             blur=0.0 if k <= wi else 0.07, alpha=1.0 if k <= wi else 0.4))
        return (wi,), toks
    if mode == "poster":
        for k, w in enumerate(words):
            alt = (k // 2) % 2 == 1
            toks.append(dict(text=w, line=k // 2, visible=k <= wi,
                             face=st["alt_font"] if alt else base_face,
                             mult=st.get("alt_mult", 0.85) if alt else st.get("main_mult", 1.2),
                             upper=not alt and st.get("upper"), color=st.get("alt_color", "#ffd54a") if alt else text_color))
        return (wi,), toks
    if mode == "typewriter":
        reveals = []
        for k, w in enumerate(words):
            s, e = times[k]
            if t >= e:
                reveals.append(len(w))
            elif t >= s:
                reveals.append(max(1, int(len(w) * (t - s) / max(0.01, e - s)) + 1))
            else:
                reveals.append(0)
        last = max([k for k, r in enumerate(reveals) if r] or [0])
        typing = reveals[last] < len(words[last]) or (last + 1 < n and t >= times[last][1])
        cursor_on = typing or int((t - cs) * 2.5) % 2 == 0
        for k, w in enumerate(words):
            toks.append(dict(text=w, face=base_face, color=text_color, reveal=reveals[k],
                             cursor=(k == last and cursor_on), cursor_color=st.get("cursor", text_color)))
        return (tuple(reveals), cursor_on), toks
    if mode == "matchcut":
        ws = times[wi][0] if wi else cs
        flips = int((t - ws) / 0.09)
        vs = st["variants"]
        v = (wi + flips) % len(vs) if flips < 4 else wi % len(vs)
        face, _, color = vs[v]
        toks.append(dict(text=words[wi], face=face, color=color))
        return (wi, v), toks
    raise ValueError(mode)


def render_tokens(tokens, width, size_frac, st, opacity, box_color):
    """Draw tokens (words with their own font/size/colour/blur/scale) like render_words.
    Layout always uses the full words so the caption doesn't jump while it animates."""
    from PIL import Image, ImageChops, ImageDraw, ImageFilter
    base = max(12, int(width * size_frac * st.get("scale", 1)))
    sw = round(st.get("stroke", 0) * base)
    pad = base // 2
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))

    def L(tk, key, default=None):  # look of this word: its own style (word styles) or the caption's
        return tk["ost"].get(key, default) if tk.get("ost") else st.get(key, default)
    for tk in tokens:
        tk["size"] = max(8, int(base * tk.get("mult", 1)))
        tk["font"] = _font_obj(tk["face"], tk["size"])
        tk["sw"] = round((L(tk, "stroke", 0) or 0) * base)
        if tk.get("upper", L(tk, "upper")):
            tk["text"] = tk["text"].upper()
        tk["w"] = probe.textlength(tk["text"], font=tk["font"])
        tk["sp"] = probe.textlength(" ", font=tk["font"])  # space sized to this word's font
        if tk.get("sticker"):  # room between stickers so each one reads on its own
            tk["sp"] += tk["size"] * 0.55
    space = probe.textlength(" ", font=_font_obj(tokens[0]["face"], base))

    lines = []
    if any("line" in tk for tk in tokens):
        for tk in tokens:
            while len(lines) <= tk["line"]:
                lines.append([])
            lines[tk["line"]].append(tk)
    else:
        cur, cur_w = [], 0
        for tk in tokens:
            add = tk["w"] if not cur else cur_w + space + tk["w"]
            if cur and add + 2 * sw > width * 0.9:
                lines.append(cur)
                cur, cur_w = [tk], tk["w"]
            else:
                cur.append(tk)
                cur_w = add
        lines.append(cur)
    line_w = [sum(tk["w"] for tk in ln) + sum(tk["sp"] for tk in ln[:-1]) for ln in lines]
    line_h = [max(tk["size"] for tk in ln) * st.get("leading", 1.25) for ln in lines]

    box_w = int(max(line_w) + 2 * sw) + 2 * pad
    box_h = int(sum(line_h)) + 2 * pad
    e = style_extent(st, base) + int(base * 0.4)
    if st.get("mode") == "chaos":
        e += int(base * 1.2)  # words drop in from above
    if any(tk.get("mm") for tk in tokens):  # per-word motion needs room
        e += int(max(tk["size"] for tk in tokens) * 1.0)
    if any(tk.get("glow_sh") for tk in tokens):
        e += int(max(tk["size"] for tk in tokens) * 0.5)
    if st.get("extrude"):
        e += int(base * st["extrude"]["depth"] * 1.2)
    W, H = box_w + 2 * e, box_h + 2 * e
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    if opacity > 0:
        img = Image.alpha_composite(img, _box(W, H, [e, e, e + box_w - 1, e + box_h - 1],
                                              int(st.get("radius", 0.5) * base), box_color, opacity, st.get("feather", 0)))
    y = e + pad
    for li, ln in enumerate(lines):
        if st.get("align") == "right":
            x = e + box_w - pad - sw - line_w[li]
        elif st.get("align") == "left":
            x = e + pad + sw
        else:
            x = e + (box_w - line_w[li]) / 2
        for tk in ln:
            tk["x"], tk["cy"] = x, y + line_h[li] / 2 + tk.get("dy", 0) * tk["size"]
            tk["x"] += tk.get("dxo", 0) * tk["size"]  # per-word sideways motion (shake)
            x += tk["w"] + tk["sp"]
        y += line_h[li]

    def draw_tok(draw, tk, fill, stroke=0, dx=0, dy=0):
        txt = tk["text"] if tk.get("reveal") is None else tk["text"][:tk["reveal"]]
        if not txt:
            return
        dx += tk.get("ox", 0) * tk["size"]  # entrance offset (caption_effects)
        dy += tk.get("oy", 0) * tk["size"]
        if tk.get("cdy"):  # wave: every letter at its own height
            from caption_effects import draw_wave
            return draw_wave(draw, tk, txt, fill, stroke, dx, dy)
        sc = tk.get("scale", 1.0)
        if abs(sc - 1) > 1e-3:
            f = _font_obj(tk["face"], max(6, int(tk["size"] * sc)))
            draw.text((tk["x"] + tk["w"] / 2 + dx, tk["cy"] + dy), txt, font=f, fill=fill, anchor="mm",
                      stroke_width=int(stroke * sc), stroke_fill=fill)
        else:
            draw.text((tk["x"] + dx, tk["cy"] + dy), txt, font=tk["font"], fill=fill, anchor="lm",
                      stroke_width=stroke, stroke_fill=fill)

    def finish(layer, tk):
        if tk.get("blur"):
            layer = layer.filter(ImageFilter.GaussianBlur(tk["blur"] * base))
        if tk.get("alpha", 1) < 1:
            layer.putalpha(layer.getchannel("A").point(lambda v: int(v * tk["alpha"])))
        return layer

    vis = [tk for tk in tokens if tk.get("visible", True)]

    def rotate(layer, tk):  # tilt a word around its own centre
        if tk.get("fx") or tk.get("fy") or tk.get("band"):  # flip squash / mask clip
            from caption_effects import token_geometry
            layer = token_geometry(layer, tk)
        r = tk.get("rot", 0)
        if not r:
            return layer
        return layer.rotate(-r, center=(tk["x"] + tk["w"] / 2, tk["cy"]), resample=Image.BICUBIC)

    def sticker_box(tk):
        sc = tk.get("scale", 1.0)
        hw, hh = tk["w"] / 2 * sc + tk["size"] * 0.28 * sc, tk["size"] * 0.62 * sc
        cx = tk["x"] + tk["w"] / 2
        return [cx - hw, tk["cy"] - hh, cx + hw, tk["cy"] + hh]

    for tk in vis:  # shadows/glows of every word first (from the word's own style if it has one)
        for dx, dy, blur, color, alpha in L(tk, "shadows", []) or []:
            mask = Image.new("L", (W, H), 0)
            md = ImageDraw.Draw(mask)
            if tk.get("sticker"):  # stickers cast the shadow, not the letters
                b = sticker_box(tk)
                md.rounded_rectangle([b[0] + dx * base, b[1] + dy * base, b[2] + dx * base, b[3] + dy * base],
                                     radius=int(tk["size"] * 0.22), fill=255)
            else:
                draw_tok(md, tk, 255, tk["sw"], dx * base, dy * base)
            if blur or tk.get("blur"):
                mask = mask.filter(ImageFilter.GaussianBlur(blur * base / 2 + tk.get("blur", 0) * base))
            mask = rotate(mask, tk)
            layer = Image.new("RGBA", (W, H), hex_rgb(color) + (0,))
            layer.putalpha(mask.point(lambda v, a=alpha * tk.get("alpha", 1): int(v * a)))
            img = Image.alpha_composite(img, layer)

    for tk in vis:  # per-word glow (word styles)
        for dx, dy, blur, color, alpha in tk.get("glow_sh", []):
            mask = Image.new("L", (W, H), 0)
            draw_tok(ImageDraw.Draw(mask), tk, 255, sw, dx * base, dy * base)
            mask = mask.filter(ImageFilter.GaussianBlur(max(0.5, blur * tk["size"] / 2)))
            mask = rotate(mask, tk)
            layer = Image.new("RGBA", (W, H), hex_rgb(color) + (0,))
            layer.putalpha(mask.point(lambda v, a=alpha: int(v * a)))
            img = Image.alpha_composite(img, layer)
    ex = st.get("extrude")
    if ex:  # 3D depth: copies of each word stepped back diagonally, darkening with depth
        steps = max(2, int(base * ex["depth"]))
        c0, c1 = hex_rgb(ex["color"]), hex_rgb(ex.get("dark", ex["color"]))
        for j in range(steps, 0, -1):
            f = j / steps
            col = tuple(int(c0[k] + (c1[k] - c0[k]) * f) for k in range(3))
            for tk in vis:
                mask = Image.new("L", (W, H), 0)
                draw_tok(ImageDraw.Draw(mask), tk, 255, sw, ex["dx"] * j, ex["dy"] * j)
                mask = rotate(mask, tk)
                layer = Image.new("RGBA", (W, H), col + (0,))
                layer.putalpha(mask)
                img = Image.alpha_composite(img, layer)
    text_alpha = Image.new("L", (W, H), 0)  # where the letters are (for the shine sweep)
    for tk in vis:  # then sticker / marker, outline and fill of every word
        layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        if tk.get("sticker"):
            d.rounded_rectangle(sticker_box(tk), radius=int(tk["size"] * 0.22), fill=hex_rgb(tk["sticker"]) + (255,))
        if tk.get("pill"):  # coloured background pill behind this word
            sc = tk.get("scale", 1.0); cxp = tk["x"] + tk["w"] / 2
            hw, hh = tk["w"] / 2 * sc + tk["size"] * 0.18, tk["size"] * 0.6 * sc
            d.rounded_rectangle([cxp - hw, tk["cy"] - hh, cxp + hw, tk["cy"] + hh], radius=int(tk["size"] * 0.22),
                                fill=hex_rgb(tk["pill"]) + (int(255 * tk.get("pill_alpha", 1.0)),))
        mk = tk.get("marker")
        if mk and mk["p"] > 0:  # highlighter stroke painted left -> right behind the word
            x0 = tk["x"] - tk["size"] * 0.12
            x1 = x0 + (tk["w"] + tk["size"] * 0.24) * min(1, mk["p"])
            d.rounded_rectangle([x0, tk["cy"] - tk["size"] * 0.34, x1, tk["cy"] + tk["size"] * 0.42],
                                radius=int(tk["size"] * 0.14), fill=hex_rgb(mk["color"]) + (int(255 * 0.92),))
        ul = tk.get("underline")
        if ul and ul["p"] > 0:  # underline drawn left -> right under the word
            x0 = tk["x"]
            x1 = x0 + tk["w"] * min(1, ul["p"])
            yb = tk["cy"] + tk["size"] * 0.42
            d.rounded_rectangle([x0, yb, max(x0 + 2, x1), yb + max(3, tk["size"] * 0.11)],
                                radius=int(tk["size"] * 0.06), fill=hex_rgb(ul["color"]) + (255,))
        if tk["sw"]:
            draw_tok(d, tk, hex_rgb(L(tk, "stroke_color", "#000000")) + (255,), tk["sw"])
        if L(tk, "liquid") and not tk.get("solid"):  # flowing horizontal multi-colour fill
            m = Image.new("L", (W, H), 0)
            draw_tok(ImageDraw.Draw(m), tk, 255)
            cols = [hex_rgb(c) for c in L(tk, "liquid")] + [hex_rgb(L(tk, "liquid")[0])]
            ph = (tokens[0].get("fx_phase") or 0.0)
            row = Image.new("RGBA", (W, 1))
            for xx in range(W):
                u = ((xx / max(1, W)) * 0.5 + ph) % 1 * (len(cols) - 1)  # half a cycle across the caption
                i0 = int(u); fr = u - i0
                a_, b_ = cols[i0], cols[min(i0 + 1, len(cols) - 1)]
                row.putpixel((xx, 0), tuple(int(a_[k] + (b_[k] - a_[k]) * fr) for k in range(3)) + (255,))
            layer.paste(row.resize((W, H)), (0, 0), m)
            text_alpha = ImageChops.lighter(text_alpha, m)
        elif L(tk, "gradient") and not tk.get("solid"):  # vertical gradient fill
            m = Image.new("L", (W, H), 0)
            draw_tok(ImageDraw.Draw(m), tk, 255)
            top, bot = hex_rgb(tk["color"]), hex_rgb(L(tk, "gradient"))
            y0, y1 = tk["cy"] - tk["size"] * 0.5, tk["cy"] + tk["size"] * 0.5
            col = Image.new("RGBA", (1, H))
            for yy in range(H):
                tt = min(1, max(0, (yy - y0) / max(1, y1 - y0)))
                col.putpixel((0, yy), tuple(int(top[k] + (bot[k] - top[k]) * tt) for k in range(3)) + (255,))
            layer.paste(col.resize((W, H)), (0, 0), m)
            text_alpha = ImageChops.lighter(text_alpha, m)
        else:
            m = Image.new("L", (W, H), 0)
            draw_tok(ImageDraw.Draw(m), tk, 255)
            draw_tok(d, tk, hex_rgb(tk["color"]) + (255,))
            text_alpha = ImageChops.lighter(text_alpha, m)
        if tk.get("cursor"):
            pre = tk["text"][:tk.get("reveal") or 0] if tk.get("reveal") is not None else tk["text"]
            cx = tk["x"] + probe.textlength(pre, font=tk["font"]) + base * 0.06
            d.rectangle([cx, tk["cy"] - tk["size"] * 0.45, cx + max(2, base * 0.08), tk["cy"] + tk["size"] * 0.45],
                        fill=hex_rgb(tk.get("cursor_color", "#ffffff")) + (255,))
        img = Image.alpha_composite(img, rotate(finish(layer, tk), tk))

    if st.get("glow"):  # letters lit from inside
        img = apply_text_glow(img, text_alpha, base, *st["glow"])
    shine = tokens[0].get("fx_shine") if tokens else None
    if shine is not None and 0 <= shine <= 1:  # glossy light streak across the letters
        band = Image.new("L", (W, H), 0)
        cx = -0.25 * W + 1.5 * W * shine
        bw, sk = max(10, W * 0.07), H * 0.35
        ImageDraw.Draw(band).polygon([(cx - bw + sk, 0), (cx + bw + sk, 0), (cx + bw - sk, H), (cx - bw - sk, H)], fill=255)
        band = band.filter(ImageFilter.GaussianBlur(bw * 0.3))
        hl = Image.new("RGBA", (W, H), (255, 255, 255, 0))
        hl.putalpha(ImageChops.multiply(band, text_alpha))
        img = Image.alpha_composite(img, hl)
        glow = hl.filter(ImageFilter.GaussianBlur(base * 0.15))  # soft bloom around the streak
        glow.putalpha(glow.getchannel("A").point(lambda v: int(v * 0.6)))
        img = Image.alpha_composite(img, glow)
    return img, e


def burn_kinetic(video, captions, out, size_frac, y_frac, opacity, text_color, box_color,
                 x_frac, style, hl=None, font=None, anim="none", anim_dur=0.3, word_props=None):
    """Speech-synced kinetic captions: sampled every frame, rendered once per distinct look,
    played as one overlay stream (ffmpeg concat)."""
    import tempfile
    from PIL import Image
    st = STYLES[style]
    width, height = video_size(video)
    fps = steady_fps(video)
    dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                "-of", "csv=p=0", video], capture_output=True, text=True).stdout.strip() or 0)
    sess = os.path.dirname(os.path.abspath(video))
    tmp = tempfile.mkdtemp(prefix="captions_")
    try:
        blank = os.path.join(tmp, "blank.png")
        Image.new("RGBA", (width, height), (0, 0, 0, 0)).save(blank, compress_level=1)
        events, n_png, step = [], 0, 1.0 / fps
        kws = load_keywords(sess, captions) if st.get("mode") in NEEDS_KW else [None] * len(captions)
        for ci, (cs, ce, text) in enumerate(captions):
            words = text.split()
            if not words:
                continue
            times = word_times(sess, cs, ce, text)
            cache, prev, seg_start = {}, None, cs
            t = cs
            while t < ce - 1e-6:
                if word_props is not None:  # word styles: works for every style
                    key, toks = tokens_for_any(st, words, times, t, cs, text_color, hl, font, kws[ci])
                    key = (key, apply_word_props(toks, words, times, t, cs, word_props[ci]))
                else:
                    key, toks = kinetic_tokens(st, words, times, t, cs, text_color, hl, font, kws[ci])
                af = int((t - cs) * fps) if anim != "none" and t - cs < anim_dur else -1
                full = (key, af)
                if full != prev:
                    if prev is not None:
                        events[-1] = (events[-1][0], t, events[-1][2])
                    if key not in cache:
                        cache[key] = render_tokens(toks, width, size_frac, st, opacity, box_color)
                    img, m = cache[key]
                    bw = img.width - 2 * m
                    x = int(max(0, min(width - bw, width * x_frac - bw / 2)) - m)
                    y = int(height * y_frac - m)
                    if af >= 0:
                        img, ox, oy = _transform(img, anim_params(anim, min(1, (t - cs + step) / anim_dur)))
                        x, y = x + ox, y + oy
                    p = os.path.join(tmp, f"k{n_png}.png")
                    n_png += 1
                    _place((width, height), img, x, y).save(p, compress_level=1)
                    events.append((t, ce, p))
                    prev = full
                t += step
            if events:
                events[-1] = (events[-1][0], ce, events[-1][2])

        lst, t = [], 0.0
        for s, e_, p in sorted(events):
            s = max(s, t)
            if e_ - s <= 0.0005:
                continue
            if s - t > 0.001:
                lst.append((blank, s - t))
            lst.append((p, e_ - s))
            t = e_
        lst.append((blank, max(0.1, dur - t + 1)))
        listfile = os.path.join(tmp, "list.txt")
        with open(listfile, "w", encoding="utf-8") as f:
            for p, d in lst:
                f.write(f"file '{p}'\nduration {d:.4f}\n")
            f.write(f"file '{lst[-1][0]}'\n")
        cmd = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", video,
               "-f", "concat", "-safe", "0", "-i", listfile,
               "-filter_complex", f"[0:v]fps={fps}[b];[1:v]format=rgba[c];[b][c]overlay=0:0:eof_action=pass[v]",
               "-map", "[v]", "-map", "0:a?", "-c:v", "libx264", "-crf", "20", "-preset", "veryfast",
               "-pix_fmt", "yuv420p", "-c:a", "copy", "-t", f"{dur:.3f}", out]
        subprocess.run(cmd, check=True, stdin=subprocess.DEVNULL)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---- sound effects under caption entrances (synthesised, no downloads) ----
SFX = {"none": "None", "pop": "Pop", "whoosh": "Whoosh", "click": "Click"}


def _sfx_sound(kind, rate=44100):
    import math
    import random
    rnd = random.Random(7)
    if kind == "pop":
        n = int(rate * 0.07)
        return [math.sin(2 * math.pi * (900 - 600 * i / n) * i / rate) * math.exp(-i / (n / 4)) * 0.6
                for i in range(n)]
    if kind == "whoosh":
        n = int(rate * 0.28)
        out, lp = [], 0.0
        for i in range(n):
            lp += (rnd.uniform(-1, 1) - lp) * (0.02 + 0.25 * i / n)  # brightening filtered noise
            out.append(lp * math.sin(math.pi * i / n) ** 2 * 1.6)
        return out
    if kind == "click":
        n = int(rate * 0.025)
        return [(rnd.uniform(-1, 1) * 0.5 + math.sin(2 * math.pi * 2200 * i / rate) * 0.5) *
                math.exp(-i / (n / 6)) * 0.5 for i in range(n)]
    return []


def sfx_times(captions, style, sess, kind):
    """When to play a sound: every letter (typewriter + click), every word (word-synced
    styles), otherwise every caption."""
    mode = STYLES.get(style, {}).get("mode")
    out = []
    for cs, ce, text in captions:
        words = text.split()
        if mode in ANIMATED_MODES or mode in KINETIC_MODES:
            times = word_times(sess, cs, ce, text)
            for k, (s, e) in enumerate(times):
                s = cs if k == 0 else s
                if mode == "typewriter" and kind == "click":
                    nch = max(1, len(words[k]))
                    out += [s + (e - s) * j / nch for j in range(nch)]
                else:
                    out.append(s)
        else:
            out.append(cs)
    return out


def add_sfx(video_in, out, times, kind, dur):
    """Mix a synthesised sound at each time into the video's audio (video stream copied)."""
    import array
    import tempfile
    import wave
    rate = 44100
    snd = _sfx_sound(kind, rate)
    times = [t - audio_offset(video_in) for t in times]  # the mixed track starts with the audio
    track = array.array("f", bytes(4 * (int(dur * rate) + rate)))
    for t in times:
        i0 = int(t * rate)
        for j, v in enumerate(snd):
            if 0 <= i0 + j < len(track):
                track[i0 + j] += v
    pcm = array.array("h", (max(-32767, min(32767, int(v * 32767 * 0.7))) for v in track))
    fd, wav = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    try:
        with wave.open(wav, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(pcm.tobytes())
        if has_audio(video_in):
            fc = ["-filter_complex", "[0:a][1:a]amix=inputs=2:duration=first:normalize=0[a]", "-map", "[a]"]
        else:
            fc = ["-map", "1:a", "-shortest"]
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", video_in, "-i", wav,
                        "-map", "0:v"] + fc + ["-c:v", "copy", "-c:a", "aac", "-b:a", "192k", out],
                       check=True, stdin=subprocess.DEVNULL)
    finally:
        os.remove(wav)


def glow_shadows(strength, color):
    """Neon light coming out of the letters: an intense tight rim right at the edges plus a bloom
    that falls off quickly (several zero-offset layers). Mirrored by glowShadows() in the page."""
    g = max(0.0, min(1.0, float(strength)))
    a = g ** 0.85          # brightness follows the slider: ~0 at 1%, full at 100%
    k = 0.6 + 0.8 * g      # and the light spreads a little further as it gets stronger
    return [(0, 0, 0.03 * k, color, 1.0 * a), (0, 0, 0.07 * k, color, 0.9 * a),
            (0, 0, 0.15 * k, color, 0.7 * a), (0, 0, 0.32 * k, color, 0.4 * a)]


def _is_dark(color):
    r, g, b = hex_rgb(color)
    return (0.299 * r + 0.587 * g + 0.114 * b) / 255 <= 0.3


def burn_captions(video, captions, out, size_frac=1 / 16, y_frac=0.62, opacity=0.75,
                  text_color="#ffffff", box_color="#000000", x_frac=0.5, style="classic", hl=None,
                  font=None, anim="none", anim_dur=0.3, words_per=0, sfx="none", emoji=False,
                  anim_scope="line", feather=0.0, glow=0.0, glow_color="#ffffff"):
    """Re-encode the video with every caption drawn on at its time.
    words_per: show 1-3 words at a time (0 = as transcribed). sfx: sound under each entrance.
    With the defaults this behaves exactly like before (same export paths).
    feather: 0-1, background fades out towards all four edges.
    glow: 0-1, neon glow: the letters light up and light shines out of them in glow_color."""
    if ((feather and feather > 0) or (glow and glow > 0)) and style in STYLES:
        # same look + edge fade / glow, as a temporary style used by every renderer for this export only
        tmp_style = f"{style}~fx{int(round((feather or 0) * 100))}-{int(round((glow or 0) * 100))}{glow_color}"
        st = dict(STYLES[style])
        if feather and feather > 0:
            st["feather"] = float(feather)
        if glow and glow > 0:  # neon: bright letters, light shining out of them
            strong = glow >= 0.4  # only a strong glow replaces the style's dark outline / shadow
            keep = [x for x in st.get("shadows", []) if not (strong and _is_dark(x[3]))]
            st["shadows"] = keep + glow_shadows(glow, glow_color)
            if strong:
                st["stroke"] = 0  # dark outlines would block the light
            st["glow"] = (float(glow), glow_color)
        STYLES[tmp_style] = st
        try:
            return burn_captions(video, captions, out, size_frac, y_frac, opacity, text_color, box_color, x_frac,
                                 tmp_style, hl, font, anim, anim_dur, words_per, sfx, emoji, anim_scope)
        finally:
            STYLES.pop(tmp_style, None)
    sess = os.path.dirname(os.path.abspath(video))
    orig_captions = captions  # emoji are chosen per transcribed caption
    if STYLES.get(style, {}).get("mode") not in KEYWORD_MODES:
        captions = split_captions(captions, words_per, sess)
    target = out
    if sfx and sfx != "none":
        root, ext = os.path.splitext(out)
        target = root + ".nosfx" + ext
    import caption_effects as _fx  # letter/word effects and per-word entrances
    fx_kind = _fx.effect_for(style, anim, anim_scope)
    ws_orig = load_word_styles(sess, orig_captions) if not fx_kind else []
    if fx_kind:
        _fx.burn_effects(video, captions, target, size_frac, y_frac, opacity, text_color, box_color,
                         x_frac, style, hl, font, fx_kind, anim, anim_dur)
    elif any(ws_orig):  # some words have their own settings
        kw_mode = STYLES.get(style, {}).get("mode") in KEYWORD_MODES
        mapped = split_captions_map(orig_captions, 0 if kw_mode else words_per, sess)
        caps2 = [(s_, e_, t_) for s_, e_, t_, _, _ in mapped]
        props = [{k - off: v for k, v in ws_orig[src].items() if off <= k < off + len(t_.split())}
                 for _, _, t_, src, off in mapped]
        burn_kinetic(video, caps2, target, size_frac, y_frac, opacity, text_color, box_color,
                     x_frac, style, hl, font, anim, anim_dur, word_props=props)
    elif STYLES.get(style, {}).get("mode") in KINETIC_MODES:
        burn_kinetic(video, captions, target, size_frac, y_frac, opacity, text_color, box_color,
                     x_frac, style, hl, font, anim, anim_dur)
    else:
        _burn_core(video, captions, target, size_frac, y_frac, opacity, text_color, box_color,
                   x_frac, style, hl, font, anim, anim_dur)
    if emoji:
        root, ext = os.path.splitext(target)
        with_emoji = root + ".emoji" + ext
        try:
            add_emoji(target, with_emoji, orig_captions, sess, size_frac, x_frac, y_frac)
            os.replace(with_emoji, target)
        finally:
            if os.path.exists(with_emoji):
                os.remove(with_emoji)
    if target != out:
        dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                    "-of", "csv=p=0", target], capture_output=True, text=True).stdout.strip() or 0)
        try:
            add_sfx(target, out, sfx_times(captions, style, sess, sfx), sfx, dur)
        finally:
            os.remove(target)


def audio_offset(video):
    """Seconds the audio track starts after the video track (Android screen recordings often
    start audio ~0.25 s late). Speech times are measured from the audio's start."""
    import av
    with av.open(video) as container:
        def stream_start(stream):
            if stream is None or stream.start_time is None or stream.time_base is None:
                return 0.0
            return float(stream.start_time * stream.time_base)
        video_stream = next((s for s in container.streams if s.type == "video"), None)
        audio_stream = next((s for s in container.streams if s.type == "audio"), None)
        return max(0.0, stream_start(audio_stream) - stream_start(video_stream))


def has_audio(video):
    import av
    with av.open(video) as container:
        return any(stream.type == "audio" for stream in container.streams)


def _convert_audio(source_path, output_path, rate=16000, layout="mono"):
    """Extract/resample the first audio track to PCM WAV."""
    import av
    with av.open(source_path) as source:
        stream = next((s for s in source.streams if s.type == "audio"), None)
        if stream is None:
            raise ValueError(f"{os.path.basename(source_path)} has no audio track")
        with av.open(output_path, "w") as output:
            output_stream = output.add_stream("pcm_s16le", rate=rate)
            output_stream.layout = layout
            resampler = av.AudioResampler(format="s16", layout=layout, rate=rate)
            for frame in source.decode(stream):
                for converted in resampler.resample(frame):
                    for packet in output_stream.encode(converted):
                        output.mux(packet)
            for packet in output_stream.encode(None):
                output.mux(packet)


_models = {}

DEFAULT_MODEL = "large-v3-turbo"  # most accurate practical model; use --model base for speed
VOCAB_FILE = os.path.join(ROOT, "vocabulary.txt")
LOW_CONFIDENCE = 0.5  # words below this probability are flagged for review


def load_vocab():
    """App words to bias recognition toward (one per line, # = comment)."""
    if not os.path.exists(VOCAB_FILE):
        return []
    with open(VOCAB_FILE, encoding="utf-8") as f:
        return [l.strip() for l in f if l.strip() and not l.lstrip().startswith("#")]


def is_junk(seg):
    """Segments the model itself is unsure contain speech (hallucinated noise)."""
    return seg.avg_logprob < -1.0 or seg.no_speech_prob > 0.6 or seg.compression_ratio > 2.4


def transcribe_session(sess, video, model_name=None, log=print, language="en", use_vocab=False,
                       translate=False, separate_vocals=False):
    """Extract audio, transcribe, write transcript.txt + subtitles.srt + words.json.
    language: spoken language code (None = auto-detect). translate=True makes the captions English.
    Returns captions [(start, end, text)]."""
    model_name = model_name or DEFAULT_MODEL
    if not has_audio(video):
        raise ValueError(f"{os.path.basename(video)} has no audio track")
    audio = os.path.join(sess, "audio.wav")
    if separate_vocals:
        import tempfile
        try:
            import torch
            import demucs  # noqa: F401 - verifies the optional runtime is installed
        except ImportError as exc:
            raise RuntimeError("Song mode needs Demucs. Install the updated project requirements first.") from exc
        device = "cuda" if torch.cuda.is_available() else "cpu"
        log(f"extracting stereo audio for vocal separation ({device}) ...")
        with tempfile.TemporaryDirectory(prefix="demucs_", dir=sess) as work:
            source_audio = os.path.join(work, "source.wav")
            output_dir = os.path.join(work, "separated")
            _convert_audio(video, source_audio, rate=44100, layout="stereo")
            log("separating vocals from music; first run downloads the model ...")
            result = subprocess.run(
                [sys.executable, "-m", "demucs.separate", "-n", "htdemucs",
                 "--two-stems=vocals", "-d", device, "-o", output_dir, source_audio],
                capture_output=True, text=True)
            if result.returncode:
                detail = (result.stdout + "\n" + result.stderr).strip().splitlines()
                raise RuntimeError("Vocal separation failed: " + "\n".join(detail[-12:]))
            vocals = os.path.join(output_dir, "htdemucs", "source", "vocals.wav")
            if not os.path.isfile(vocals):
                raise RuntimeError("Vocal separation finished but did not produce a vocals track.")
            _convert_audio(vocals, audio, rate=16000, layout="mono")
        log("vocal track ready; sending it to Groq ...")
    else:
        log("extracting audio ...")
        _convert_audio(video, audio)

    # Keep a stable segment/word interface for caption generation and export.
    vocab = load_vocab() if use_vocab and language == "en" and not translate else []
    if vocab:
        log(f"using {len(vocab)} vocabulary words from vocabulary.txt")
    segments = info = None
    api_key = os.environ.get("GROQ_API_KEY")
    if api_key:
        try:
            from groq import Groq
            from types import SimpleNamespace

            def response_field(value, name, default=None):
                return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)

            groq_model = "whisper-large-v3" if translate else os.environ.get(
                "GROQ_WHISPER_MODEL", "whisper-large-v3")
            log(f"transcribing with Groq (model={groq_model}) ...")
            client = Groq(api_key=api_key)
            with open(audio, "rb") as audio_file:
                request = dict(file=(os.path.basename(audio), audio_file), model=groq_model,
                    response_format="verbose_json",
                    timestamp_granularities=["word", "segment"],
                    prompt=" ".join(vocab)[:900] if vocab else None)
                if translate:
                    result = client.audio.translations.create(**request)
                else:
                    result = client.audio.transcriptions.create(language=language or None, **request)
            segments = [SimpleNamespace(start=float(response_field(seg, "start", 0)),
                end=float(response_field(seg, "end", 0)), text=response_field(seg, "text", ""),
                avg_logprob=float(response_field(seg, "avg_logprob", 0.0)),
                no_speech_prob=float(response_field(seg, "no_speech_prob", 0.0)),
                compression_ratio=float(response_field(seg, "compression_ratio", 0.0)), words=[])
                for seg in (response_field(result, "segments", None) or [])]
            for word in (response_field(result, "words", None) or []):
                item = SimpleNamespace(start=float(response_field(word, "start", 0)),
                    end=float(response_field(word, "end", 0)),
                    word=response_field(word, "word", ""), probability=1.0)
                if segments:
                    def overlap(seg):
                        return max(0.0, min(seg.end, item.end) - max(seg.start, item.start))
                    owner = max(segments, key=overlap)
                    if overlap(owner) <= 0:
                        owner = min(segments, key=lambda seg: min(abs(item.start - seg.end),
                                                                  abs(item.end - seg.start)))
                    owner.words.append(item)
            info = SimpleNamespace(language=response_field(result, "language", None) or language or "unknown")
            for seg in segments:
                _reconcile_segment_words(seg, info.language)
            result_text = response_field(result, "text", "")
            if not segments and result_text.strip():
                segments = [SimpleNamespace(start=0.0, end=0.0, text=result_text,
                    avg_logprob=0.0, no_speech_prob=0.0, compression_ratio=0.0, words=[])]
        except Exception as exc:
            log(f"Groq unavailable ({exc}); switching to local Whisper")
            segments = None
    else:
        log("GROQ_API_KEY is not set; using local Whisper")

    if segments is None:
        from faster_whisper import WhisperModel
        log(f"transcribing locally (model={model_name}; first run downloads the model) ...")
        if model_name not in _models:
            _models[model_name] = WhisperModel(model_name, device="cpu", compute_type="int8")
        opts = dict(word_timestamps=True, beam_size=5, language=language or None,
                    task="translate" if translate else "transcribe",
                    condition_on_previous_text=False, vad_filter=False)
        if vocab:
            opts["hotwords"] = " ".join(vocab)
        segments, info = _models[model_name].transcribe(audio, **opts)
        segments = list(segments)
    log(f"detected language: {info.language}" + (" (translating to English)" if translate else ""))
    off = audio_offset(video)
    if off > 0.001:  # put speech times on the video's timeline
        import dataclasses
        aligned = []
        for s in segments:
            if dataclasses.is_dataclass(s):
                aligned.append(dataclasses.replace(s, start=s.start + off, end=s.end + off,
                    words=[dataclasses.replace(w, start=w.start + off, end=w.end + off)
                           for w in (s.words or [])]))
            else:
                s.start += off
                s.end += off
                for w in (s.words or []):
                    w.start += off
                    w.end += off
                aligned.append(s)
        segments = aligned
        log(f"aligned speech to video (audio starts {off:.2f}s after video)")
    # Keep every detected segment, including quiet interview and end speech.
    # Junk confidence is recorded for review, never used to silently remove text.

    words = [dict(start=round(w.start, 3), end=round(w.end, 3), word=w.word.strip(),
                  prob=round(w.probability, 3)) for s in segments for w in (s.words or [])]
    with open(os.path.join(sess, "words.json"), "w", encoding="utf-8") as f:
        json.dump(dict(model=model_name, language=info.language, words=words), f, indent=1)

    lines = [f"[{ts(s.start)} - {ts(s.end)}] {s.text.strip()}" for s in segments]
    with open(os.path.join(sess, "transcript.txt"), "w", encoding="utf-8") as f:
        f.write(f"# language: {info.language}  source: {os.path.basename(video)}  model: {model_name}\n")
        f.write("\n".join(lines) + "\n")
    log("\n".join(lines) if lines else "(no speech detected)")
    low = [f"{w['word']} ({ts(w['start'])})" for w in words if w["prob"] < LOW_CONFIDENCE]
    if low:
        log("check these words (low confidence): " + ", ".join(low))

    captions = caption_chunks(segments, language=info.language)
    write_srt(captions, os.path.join(sess, "subtitles.srt"))
    return captions


def low_confidence_words(sess, start, end):
    """Low-confidence words spoken between start and end (from words.json)."""
    p = os.path.join(sess, "words.json")
    if not os.path.exists(p):
        return []
    with open(p, encoding="utf-8") as f:
        words = json.load(f).get("words", [])
    return [w["word"] for w in words
            if w["prob"] < LOW_CONFIDENCE and w["start"] < end and w["end"] > start]


def main():
    ap = argparse.ArgumentParser(description="Transcribe a recording's audio to text")
    ap.add_argument("session", nargs="?", help="session folder (default: latest)")
    ap.add_argument("--model", default=DEFAULT_MODEL,
                    help=f"whisper model: tiny, base, small, large-v3-turbo (default: {DEFAULT_MODEL})")
    ap.add_argument("--language", default="auto", help="spoken language code, or 'auto' (default: auto)")
    ap.add_argument("--translate", action="store_true", help="output English captions from any spoken language")
    ap.add_argument("--vocab", action="store_true",
                    help="bias recognition toward words in vocabulary.txt (may hide mispronunciations)")
    args = ap.parse_args()

    sess = os.path.abspath(args.session) if args.session else latest_session()
    video = find_video(sess)
    print(f"session : {sess}")
    try:
        captions = transcribe_session(sess, video, args.model,
                                      language=None if args.language == "auto" else args.language,
                                      use_vocab=args.vocab, translate=args.translate)
    except ValueError as e:
        sys.exit(f"ERROR: {e} (recorded with --no-audio?)")

    captioned = os.path.join(sess, "video_captioned" + os.path.splitext(video)[1])
    print("drawing captions onto the video ...")
    burn_captions(video, captions, captioned)
    print(f"\nSaved: {os.path.join(sess, 'transcript.txt')}\n"
          f"       {os.path.join(sess, 'subtitles.srt')}\n       {captioned}")


if __name__ == "__main__":
    main()
