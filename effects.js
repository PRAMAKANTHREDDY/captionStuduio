// Caption Studio: animation tiles + live preview of the letter/word effects.
// Loaded after the main page script (shares its globals: STYLES, curStyle, captions, wordTimes,
// hlFor, hlTouched, renderKey, animKey, curAnim, animScope, $ ...).
// effect states mirror caption_effects.effect_tokens(); the curves come from /api/anim_meta, which
// serves the same tr.anim_params() values the exporter uses.

let ANIMX = { groups: [], meta: {}, table: {} };

const ANIM_EASE = { pop: 'linear', bounce: 'linear', shake: 'linear',
                    spring: 'linear', flip_x: 'linear', flip_y: 'linear', mask_rise: 'linear' };
const AF = ['alpha', 'scale', 'dx', 'dy', 'blur', 'wipe', 'rgb', 'sx', 'sy', 'mask'];
const KEYFRAMES_FROM_TABLE = ['spring', 'flip_x', 'flip_y', 'mask_rise'];   // others are hand-written CSS

const isTextFx = id => (ANIMX.meta[id] || {}).kind === 'text';
const clamp01 = v => Math.min(1, Math.max(0, v));

function animCss(id, dur) {
  if (id === 'none' || isTextFx(id)) return 'none';
  return `cs-${id} ${dur}s ${ANIM_EASE[id] || 'cubic-bezier(.2,.8,.2,1)'} both`;
}

// Which per-word renderer the preview needs (same rule as caption_effects.effect_for).
function fxMode(st) {
  if (!st || st.mode) return null;                 // styles with their own word animation keep it
  const m = ANIMX.meta[curAnim];
  if (!m) return null;
  if (m.kind === 'text') return curAnim;
  return animScope === 'word' && m.word ? 'unit' : null;
}

// ---------- whole-line entrance ----------
function playAnim() {
  const el = $('capBox'), cap = $('cap');
  el.style.animation = 'none';
  cap.classList.remove('masking');
  if (fxMode(STYLES && STYLES[curStyle])) return;   // per-word effects draw every frame themselves
  void el.offsetWidth;                               // restart
  if (curAnim === 'mask_rise') {
    cap.classList.add('masking');                    // hide the part that is still below the line
    el.onanimationend = () => cap.classList.remove('masking');
  }
  el.style.animation = animCss(curAnim, parseFloat($('animDur').value));
}

function pickAnim(id, fromUser) {
  curAnim = id;
  document.querySelectorAll('.mtile').forEach(t => t.classList.toggle('on', t.dataset.id === id));
  const m = ANIMX.meta[id];
  if (fromUser && m && m.dur) { $('animDur').value = m.dur; $('animDur').oninput(); }
  refreshScope();
  renderKey = ''; animKey = '';
  playAnim();
}

// ---------- "Apply to: whole line / each word" ----------
function setScope(v) { animScope = v === 'word' ? 'word' : 'line'; refreshScope(); renderKey = ''; animKey = ''; playAnim(); }

function refreshScope() {
  const st = STYLES && STYLES[curStyle], m = ANIMX.meta[curAnim] || {}, own = !!(st && st.mode);
  const can = !!m.word && !own;
  let note = '';
  if (m.kind === 'text') note = own ? 'This style animates its words itself, so this effect is not applied.'
                                    : 'Animates letters or words as they are spoken.';
  else if (own && m.word) note = 'This style animates its words itself.';
  else if (curAnim !== 'none' && !m.word) note = 'This animation always plays on the whole line.';
  document.querySelectorAll('#animScope button').forEach(b => {
    b.disabled = !can;
    b.classList.toggle('on', can ? b.dataset.v === animScope : b.dataset.v === 'line');
  });
  $('scopeNote').textContent = note;
  // karaoke / word pop can use the highlight colour even on plain styles
  if (st && !st.mode) $('hlRow').hidden = !(curAnim === 'karaoke' || curAnim === 'word_pop');
}

// ---------- curves ----------
function animAt(id, p) {
  const rows = ANIMX.table[id], o = { alpha: 1, scale: 1, dx: 0, dy: 0, blur: 0, sx: 1, sy: 1, mask: 0 };
  if (!rows) return o;
  const x = clamp01(p) * (rows.length - 1), i = Math.min(rows.length - 2, Math.floor(x)), f = x - i;
  AF.forEach((k, j) => { o[k] = rows[i][j] + (rows[i + 1][j] - rows[i][j]) * f; });
  return o;
}

function contrastInk(hex) {
  const n = parseInt(hex.slice(1), 16), r = n >> 16, g = (n >> 8) & 255, b = n & 255;
  return 0.299 * r + 0.587 * g + 0.114 * b > 150 ? '#000000' : '#ffffff';
}

// ---------- state at time t (mirror of effect_tokens in caption_effects.py) ----------
function fxState(mode, anim, c, t, st) {
  const words = c.text.split(/\s+/).filter(Boolean), times = wordTimes(c), n = words.length;
  let wi = 0;
  for (let k = 1; k < n; k++) if (t >= times[k][0]) wi = k;
  const ws = wi ? times[wi][0] : c.start, dur = Math.max(0.05, parseFloat($('animDur').value));
  const textColor = $('textColor').value, toks = [];

  if (mode === 'typewriter') {
    const rev = words.map((w, k) => { const [s, e] = times[k];
      return t >= e ? w.length : t >= s ? Math.max(1, Math.floor(w.length * (t - s) / Math.max(0.01, e - s)) + 1) : 0; });
    let last = 0; rev.forEach((r, k) => { if (r) last = k; });
    const typing = rev[last] < words[last].length || (last + 1 < n && t >= times[last][1]);
    const cur = typing || Math.floor((t - c.start) * 2.5) % 2 === 0;
    words.forEach((w, k) => toks.push({ text: w, reveal: rev[k], cursor: k === last && cur, cursorColor: textColor }));
    return { key: [rev, cur], toks, wi };
  }
  if (mode === 'karaoke') {
    const q = clamp01((t - ws) / 0.12), color = hlFor(st, wi);
    words.forEach((w, k) => toks.push(k === wi
      ? { text: w, color: st.stroke ? textColor : contrastInk(color), scale: 1 + 0.06 * q, pill: color } : { text: w }));
    return { key: [wi, Math.round(q * 100), color], toks, wi };
  }
  if (mode === 'word_pop') {
    const q = clamp01((t - ws) / dur), a = animAt('pop', q);
    toks.push({ text: words[wi], color: hlTouched ? $('hlColor').value : null,
                alpha: a.alpha < 1 ? a.alpha : undefined, scale: Math.abs(a.scale - 1) > 1e-3 ? a.scale : undefined });
    return { key: [wi, Math.round(q * 1000)], toks, wi };
  }
  if (mode === 'wave') {
    const freq = 1.4, lag = 0.07, amp = 0.16, tau = t - c.start;
    let g = 0;
    words.forEach(w => {
      const cdy = [];
      for (let i = 0; i < w.length; i++, g++) {
        const ph = Math.max(0, freq * tau - g * lag);
        cdy.push(+(-amp * 0.5 * (1 - Math.cos(2 * Math.PI * ph))).toFixed(4));
      }
      g++;  // the space
      toks.push({ text: w, cdy });
    });
    return { key: toks.map(tk => tk.cdy), toks, wi };
  }
  // 'unit': the chosen entrance, played on each word when it is spoken
  const lead = st.leading || 1.25, sig = [];
  words.forEach((w, k) => {
    const s = k === 0 ? c.start : times[k][0];
    if (t < s) { toks.push({ text: w, visible: false }); sig.push(0); return; }
    const a = animAt(anim, clamp01((t - s) / dur)), tk = { text: w };
    if (a.alpha < 1) tk.alpha = +a.alpha.toFixed(3);
    if (Math.abs(a.scale - 1) > 1e-3) tk.scale = +a.scale.toFixed(3);
    if (a.blur > 0) tk.blur = +a.blur.toFixed(3);
    if (a.dx) tk.ox = +(a.dx * lead).toFixed(3);
    if (a.dy) tk.oy = +(a.dy * lead).toFixed(3);
    if (Math.abs(a.sx - 1) > 1e-3) tk.fx = +Math.max(0.02, a.sx).toFixed(3);
    if (Math.abs(a.sy - 1) > 1e-3) tk.fy = +Math.max(0.02, a.sy).toFixed(3);
    if (a.mask) tk.band = true;
    toks.push(tk); sig.push(JSON.stringify(tk));
  });
  return { key: sig, toks, wi };
}

// ---------- drawing ----------
function renderFxInto(el, toks) {
  el.textContent = '';
  const frag = document.createDocumentFragment();
  toks.forEach((tk, k) => {
    if (k > 0) frag.appendChild(document.createTextNode(' '));
    const outer = document.createElement('span');
    outer.className = 'w fxw';
    let host = outer;
    if (tk.band) {                       // clipped to its own line: the word rises out of it
      outer.classList.add('fxband');
      host = document.createElement('span');
      host.style.display = 'inline-block';
      outer.appendChild(host);
    }
    if (tk.visible === false) outer.style.visibility = 'hidden';
    if (tk.color) outer.style.color = tk.color;
    if (tk.alpha !== undefined && tk.alpha < 1) outer.style.opacity = tk.alpha;
    if (tk.blur) outer.style.filter = `blur(${tk.blur}em)`;
    if (tk.pill) { outer.classList.add('fxpill'); outer.style.setProperty('--pill', tk.pill); }
    const tf = [];
    if (tk.ox || tk.oy) tf.push(`translate(${tk.ox || 0}em, ${tk.oy || 0}em)`);
    if (tk.scale || tk.fx || tk.fy) tf.push(`scale(${(tk.scale || 1) * (tk.fx || 1)}, ${(tk.scale || 1) * (tk.fy || 1)})`);
    if (tf.length) { host.style.display = 'inline-block'; host.style.transform = tf.join(' '); }

    if (tk.reveal !== undefined) {       // typewriter: letters appear, the rest keeps its space
      const a = document.createElement('span'); a.textContent = tk.text.slice(0, tk.reveal);
      const b = document.createElement('span'); b.textContent = tk.text.slice(tk.reveal); b.style.visibility = 'hidden';
      host.append(a);
      if (tk.cursor) { const cu = document.createElement('span'); cu.className = 'cursor'; cu.style.background = tk.cursorColor || '#fff'; host.append(cu); }
      host.append(b);
    } else if (tk.cdy) {                 // wave: every letter on its own height
      [...tk.text].forEach((ch, i) => {
        const s = document.createElement('span');
        s.style.display = 'inline-block'; s.textContent = ch;
        if (tk.cdy[i]) s.style.transform = `translateY(${tk.cdy[i]}em)`;
        host.appendChild(s);
      });
    } else host.textContent = tk.text;
    frag.appendChild(outer);
  });
  el.appendChild(frag);
}

// called from tick(): draws the caption for time t; returns the state (for sound timing)
function fxFrame(c, t, st, idx) {
  // paused before the first caption: show its finished look so styling is visible
  const tt = t < c.start ? c.end - 0.01 : t;
  const k = fxState(fxMode(st), curAnim, c, tt, st);
  const key = [idx, JSON.stringify(k.key), c.text, curStyle, curAnim, animScope, $('animDur').value,
               $('hlColor').value, $('textColor').value, fontFace].join('|');
  if (key !== renderKey) { renderFxInto($('capText'), k.toks); renderKey = key; applyStyle(); }
  return k;
}

// ---------- animation tiles ----------
const DEMO = { start: 0, end: 1.8, text: 'Hello there world', times: [[0, 0.6], [0.6, 1.2], [1.2, 1.8]] };

function makeTile(id) {
  const m = ANIMX.meta[id];
  const t = document.createElement('div');
  t.className = 'mtile'; t.dataset.id = id; t.title = m.name;
  const d = document.createElement('span'); d.className = 'demo'; d.textContent = 'Aa';
  const l = document.createElement('span'); l.textContent = m.name;
  t.append(d, l);
  if (m.kind === 'text') {                       // hover: run the effect on a sample phrase
    let timer = null;
    t.onmouseenter = () => {
      const t0 = performance.now();
      d.style.whiteSpace = 'nowrap'; d.style.fontSize = '11px';
      timer = setInterval(() => {
        const tt = ((performance.now() - t0) / 1000) % 2.2;
        renderFxInto(d, fxState(id, id, DEMO, Math.min(tt, 1.79), STYLES.classic).toks);
      }, 50);
    };
    t.onmouseleave = () => { clearInterval(timer); d.textContent = 'Aa'; d.style.fontSize = ''; };
  } else {
    if (id !== 'none') d.style.animation = animCss(id, 0.6);
    t.onmouseenter = () => { d.style.animation = 'none'; void d.offsetWidth;
      d.style.animation = id === 'none' ? 'none' : animCss(id, 1.1).replace(' both', ' infinite'); };
    t.onmouseleave = () => { d.style.animation = 'none'; };
  }
  t.onclick = () => pickAnim(id, true);
  return t;
}

function buildTiles() {
  const box = $('anims');
  box.textContent = '';
  box.appendChild(makeTile('none'));
  for (const [gid, label] of ANIMX.groups) {
    const ids = Object.keys(ANIMX.meta).filter(id => ANIMX.meta[id].group === gid);
    if (!ids.length) continue;
    const h = document.createElement('div');
    h.className = 'agroup'; h.textContent = label;
    box.appendChild(h);
    ids.forEach(id => box.appendChild(makeTile(id)));
  }
}

// CSS keyframes for the new whole-line entrances, generated from the exporter's own curves
function buildKeyframes() {
  let css = '';
  for (const id of KEYFRAMES_FROM_TABLE) {
    const rows = ANIMX.table[id];
    if (!rows) continue;
    const n = rows.length - 1;
    css += `@keyframes cs-${id} {` + rows.map((r, i) => {
      const [alpha, scale, dx, dy, , , , sx, sy] = r;
      return `${(i / n * 100).toFixed(2)}% { opacity:${alpha}; transform: translate(${(dx * 100).toFixed(2)}%, ${(dy * 100).toFixed(2)}%) ` +
             `scale(${(scale * sx).toFixed(4)}, ${(scale * sy).toFixed(4)}); }`;
    }).join('') + '}\n';
  }
  const el = document.createElement('style');
  el.textContent = css;
  document.head.appendChild(el);
}

// ---------- wiring ----------
$('animDur').oninput = () => { $('animDurVal').textContent = parseFloat($('animDur').value).toFixed(2) + 's'; };
$('animDur').oninput();
$('animDur').onchange = playAnim;
$('replay').onclick = playAnim;
document.querySelectorAll('#animScope button').forEach(b => { b.onclick = () => { if (!b.disabled) setScope(b.dataset.v); }; });

fetch('/api/anim_meta').then(r => r.json()).then(j => {
  ANIMX = j;
  buildKeyframes();
  buildTiles();
  pickAnim(curAnim);
});
