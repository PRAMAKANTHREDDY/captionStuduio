"""
Caption effects: letter- and word-level animation for any plain style and font.

  * Text effects     Typewriter, Karaoke, Word pop, Wave
  * Each-word scope  any entrance animation (Pop, Rise, Blur in, Spring, 3D flip, Mask rise ...)
                     plays on every word as it is spoken, instead of once for the whole line

Whole-line entrances (Spring, 3D flip, Mask rise, ...) live in transcribe.anim_params.
The live preview is effects.js; it mirrors effect_tokens() below and reads the same animation
curves from anim_table(), so the preview and the exported video use the same numbers.
"""

import math
import os
import shutil
import subprocess

import transcribe as tr

TEXT_EFFECTS = ("typewriter", "karaoke", "word_pop", "wave")

# Entrances that can also be played on every word (they only use alpha/scale/shift/blur/squash).
WORD_SCOPE = ("fade", "pop", "zoom", "slide_up", "slide_down", "slide_left", "bounce", "shake",
              "blur", "rise", "spring", "flip_x", "flip_y", "mask_rise")


GROUPS = [("subtle", "Subtle"), ("dynamic", "Dynamic"), ("energy", "High energy"),
          ("text", "Letter & word effects")]

_GROUP = {
    "fade": "subtle", "blur": "subtle", "rise": "subtle", "wipe": "subtle",
    "pop": "dynamic", "zoom": "dynamic", "slide_up": "dynamic", "slide_down": "dynamic",
    "slide_left": "dynamic", "spring": "dynamic", "flip_x": "dynamic", "flip_y": "dynamic",
    "mask_rise": "dynamic",
    "bounce": "energy", "shake": "energy", "glitch": "energy",
    "typewriter": "text", "karaoke": "text", "word_pop": "text", "wave": "text",
}
_DUR = {"spring": 0.6, "flip_x": 0.5, "flip_y": 0.5, "mask_rise": 0.45, "word_pop": 0.25}
_FIELDS = ("alpha", "scale", "dx", "dy", "blur", "wipe", "rgb", "sx", "sy", "mask")
_DEFAULT = dict(alpha=1.0, scale=1.0, dx=0.0, dy=0.0, blur=0.0, wipe=1.0, rgb=0.0, sx=1.0, sy=1.0, mask=0.0)
TABLE_STEPS = 60


def anim_meta():
    """What the page needs to group and enable animation tiles."""
    meta = {}
    for aid, name in tr.ANIMATIONS.items():
        if aid == "none":
            meta[aid] = dict(name=name, group=None, kind="none", word=False)
            continue
        meta[aid] = dict(name=name, group=_GROUP.get(aid, "dynamic"),
                         kind="text" if aid in TEXT_EFFECTS else "transform",
                         word=aid in WORD_SCOPE)
        if aid in _DUR:
            meta[aid]["dur"] = _DUR[aid]
    return {"groups": GROUPS, "meta": meta}


def anim_table(anim):
    """Animation curve sampled at TABLE_STEPS+1 points: rows of _FIELDS. Single source of truth
    for the preview (the export reads tr.anim_params directly)."""
    rows = []
    for i in range(TABLE_STEPS + 1):
        a = dict(_DEFAULT, **tr.anim_params(anim, i / TABLE_STEPS))
        rows.append([round(float(a[f]), 5) for f in _FIELDS])
    return rows


def all_tables():
    return {aid: anim_table(aid) for aid in tr.ANIMATIONS if aid != "none" and aid not in TEXT_EFFECTS}


def effect_for(style, anim, scope):
    """Which per-word renderer an export needs: a text effect id, "unit" (an entrance on each
    word) or None (use the normal whole-caption paths). Styles that animate words themselves
    keep their own animation."""
    if tr.STYLES.get(style, {}).get("mode"):
        return None
    if anim in TEXT_EFFECTS:
        return anim
    if scope == "word" and anim in WORD_SCOPE:
        return "unit"
    return None


def _contrast(color):
    r, g, b = tr.hex_rgb(color)
    return "#000000" if 0.299 * r + 0.587 * g + 0.114 * b > 150 else "#ffffff"


def _clamp01(v):
    return min(1.0, max(0.0, v))


# ---------------------------------------------------------------- token states
def effect_tokens(effect, anim, anim_dur, st, words, times, t, cs, text_color, hl, font):
    """What the caption looks like at time t. Returns (key, tokens) for tr.render_tokens;
    equal keys mean equal pictures. Mirrored by fxState() in effects.js."""
    n = len(words)
    face = font or st["font"]
    wi = 0
    for k in range(1, n):
        if t >= times[k][0]:
            wi = k
    ws = cs if wi == 0 else times[wi][0]

    if effect == "typewriter":
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
        toks = [dict(text=w, face=face, color=text_color, reveal=reveals[k],
                     cursor=(k == last and cursor_on), cursor_color=text_color)
                for k, w in enumerate(words)]
        return (tuple(reveals), cursor_on), toks

    if effect == "karaoke":  # whole line stays; the spoken word gets a colour pill and swells
        q = _clamp01((t - ws) / 0.12)
        color = tr.hl_color(st, wi, hl)
        toks = []
        outlined = bool(st.get("stroke"))  # outlined styles keep their own letters: dark ink would merge with the outline
        for k, w in enumerate(words):
            if k == wi:
                toks.append(dict(text=w, face=face, color=text_color if outlined else _contrast(color),
                                 solid=not outlined, scale=1 + 0.06 * q,
                                 marker=dict(p=1.0, color=color)))  # pill is full width at once
            else:
                toks.append(dict(text=w, face=face, color=text_color))
        return (wi, round(q, 2)), toks

    if effect == "word_pop":  # one word at a time, punches in with a scale overshoot
        q = _clamp01((t - ws) / max(0.05, anim_dur))
        prm = tr.anim_params("pop", q)
        tk = dict(text=words[wi], face=face, color=hl or text_color)
        if prm["alpha"] < 1:
            tk["alpha"] = prm["alpha"]
        if abs(prm["scale"] - 1) > 1e-3:
            tk["scale"] = prm["scale"]
        return (wi, round(q, 3)), [tk]

    if effect == "wave":  # letters ride a sine wave that travels along the line
        freq, lag, amp = 1.4, 0.07, 0.16
        tau, g, toks, sig = t - cs, 0, [], []
        for w in words:
            cdy = []
            for _ in w:
                ph = max(0.0, freq * tau - g * lag)
                cdy.append(round(-amp * 0.5 * (1 - math.cos(2 * math.pi * ph)), 4))
                g += 1
            g += 1  # the space
            toks.append(dict(text=w, face=face, color=text_color, cdy=cdy))
            sig.append(tuple(cdy))
        return tuple(sig), toks

    if effect == "unit":  # the chosen entrance, played on each word when it is spoken
        toks, sig = [], []
        lead = st.get("leading", 1.25)
        for k, w in enumerate(words):
            s = cs if k == 0 else times[k][0]
            tk = dict(text=w, face=face, color=text_color)
            if t < s:
                tk["visible"] = False
                toks.append(tk)
                sig.append(0)
                continue
            q = _clamp01((t - s) / max(0.05, anim_dur))
            prm = tr.anim_params(anim, q)
            if prm["alpha"] < 1:
                tk["alpha"] = round(prm["alpha"], 3)
            if abs(prm["scale"] - 1) > 1e-3:
                tk["scale"] = round(prm["scale"], 3)
            if prm["blur"] > 0:
                tk["blur"] = round(prm["blur"], 3)
            if prm["dx"]:
                tk["ox"] = round(prm["dx"] * lead, 3)
            if prm["dy"]:
                tk["oy"] = round(prm["dy"] * lead, 3)
            if abs(prm.get("sx", 1) - 1) > 1e-3:
                tk["fx"] = round(max(0.02, prm["sx"]), 3)
            if abs(prm.get("sy", 1) - 1) > 1e-3:
                tk["fy"] = round(max(0.02, prm["sy"]), 3)
            if prm.get("mask"):
                tk["band"] = True
            toks.append(tk)
            sig.append(tuple(sorted((a, b) for a, b in tk.items() if a in
                                    ("alpha", "scale", "blur", "ox", "oy", "fx", "fy", "band"))))
        return tuple(sig), toks

    raise ValueError(effect)


# ---------------------------------------------------------------- hooks used by tr.render_tokens
def draw_wave(draw, tk, txt, fill, stroke, dx, dy):
    """Draw a word letter by letter, each at its own height (tk["cdy"], fractions of the size)."""
    font = tr._font_obj(tk["face"], max(6, int(tk["size"] * tk.get("scale", 1.0))))
    x = tk["x"] + dx
    for i, ch in enumerate(txt):
        off = tk["cdy"][i] if i < len(tk["cdy"]) else 0
        draw.text((x, tk["cy"] + dy + off * tk["size"]), ch, font=font, fill=fill, anchor="lm",
                  stroke_width=stroke, stroke_fill=fill)
        x += draw.textlength(ch, font=font)


def token_geometry(layer, tk):
    """Squash a word around its centre (3D flip) and keep it inside its own line (mask reveal)."""
    from PIL import Image
    fx, fy = tk.get("fx", 1.0), tk.get("fy", 1.0)
    if abs(fx - 1) > 1e-3 or abs(fy - 1) > 1e-3:
        cx, cy = tk["x"] + tk["w"] / 2, tk["cy"]
        layer = layer.transform(layer.size, Image.AFFINE,
                                (1 / fx, 0, cx - cx / fx, 0, 1 / fy, cy - cy / fy), resample=Image.BICUBIC)
    if tk.get("band"):
        y0, y1 = tk["cy"] - tk["size"] * 0.7, tk["cy"] + tk["size"] * 0.7
        keep = Image.new("L", layer.size, 0)
        keep.paste(255, (0, max(0, int(y0)), layer.width, max(0, int(y1))))
        if layer.mode == "L":
            layer = Image.composite(layer, Image.new("L", layer.size, 0), keep)
        else:
            layer = layer.copy()
            layer.putalpha(Image.composite(layer.getchannel("A"), Image.new("L", layer.size, 0), keep))
    return layer


# ---------------------------------------------------------------- export
def burn_effects(video, captions, out, size_frac, y_frac, opacity, text_color, box_color,
                 x_frac, style, hl, font, effect, anim, anim_dur):
    """Render per-word / per-letter captions: every distinct look becomes one transparent
    full-frame PNG, played as one overlay stream (same approach as tr.burn_kinetic)."""
    import tempfile
    from PIL import Image
    st = tr.STYLES[style]
    width, height = tr.video_size(video)
    fps = tr.steady_fps(video)
    dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                "-of", "csv=p=0", video], capture_output=True, text=True).stdout.strip() or 0)
    sess = os.path.dirname(os.path.abspath(video))
    tmp = tempfile.mkdtemp(prefix="captions_")
    try:
        blank = os.path.join(tmp, "blank.png")
        Image.new("RGBA", (width, height), (0, 0, 0, 0)).save(blank, compress_level=1)
        events, n_png, step = [], 0, 1.0 / fps
        for cs, ce, text in captions:
            words = text.split()
            if not words:
                continue
            times = tr.word_times(sess, cs, ce, text)
            cache, prev = {}, None
            t = cs
            while t < ce - 1e-6:
                key, toks = effect_tokens(effect, anim, anim_dur, st, words, times, t, cs,
                                          text_color, hl, font)
                if key != prev:
                    if prev is not None:
                        events[-1] = (events[-1][0], t, events[-1][2])
                    if key not in cache:
                        cache[key] = tr.render_tokens(toks, width, size_frac, st, opacity, box_color)
                    img, m = cache[key]
                    bw = img.width - 2 * m
                    x = int(max(0, min(width - bw, width * x_frac - bw / 2)) - m)
                    y = int(height * y_frac - m)
                    p = os.path.join(tmp, f"e{n_png}.png")
                    n_png += 1
                    tr._place((width, height), img, x, y).save(p, compress_level=1)
                    events.append((t, ce, p))
                    prev = key
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
