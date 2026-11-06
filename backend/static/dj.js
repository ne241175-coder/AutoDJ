// AutoDJ 画面（Figma「DJプロジェクト」UI ページを元にした流れ）
//   ローディング（ディスクが回る）→ 曲を選ぶ（画面1・2）→ 繋ぎ方を選ぶ（画面4）→ 再生（画面5）
// ビルド不要: React UMD + htm。API は backend/app.py、再生は engine.js（mp3 は切らずに秒数指定で鳴らす）。
import { MixPlayer, loadBuffer, audioContext } from "./engine.js";

const { useState, useEffect, useRef, useMemo, useCallback } = React;
const html = htm.bind(React.createElement);

const W = 1280, H = 832;                     // Figma のフレームサイズ
const PICK = ["#00b2ff", "#00ff4d", "#ff3df2", "#ffe600", "#ff7a00", "#a46bff", "#ff4d4d", "#00e5c7", "#ffffff", "#9dff3d"];
const MAX_PICK = 10;
const STYLE = {
  fade: { label: "フェードイン", color: "#ff3df2", desc: "長めに重ねて、じわっと入れ替える" },
  cut: { label: "カットイン", color: "#2f6bff", desc: "小節の頭でスパッと切り替える" },
  auto: { label: "おまかせ", color: "#ffe600", desc: "曲に合わせて自動で（テンポが近ければ1小節かけて混ぜる）" },
};
const STYLE_ORDER = ["fade", "cut", "auto"];
const ROLE_COLOR = {
  chorus: "#ff7a59", chorus_drop: "#d98a6c", verse: "#4f7cff", prechorus: "#2fd0b8", bridge: "#b07cff",
  intro: "#6b7280", outro: "#6b7280", interlude: "#4b5160", other: "#7b8392",
};

const fmt = (t) => {
  if (t == null || isNaN(t)) return "-:--";
  t = Math.max(0, t);
  const m = Math.floor(t / 60), s = Math.floor(t - m * 60);
  return `${m}:${String(s).padStart(2, "0")}`;
};
const api = async (url, opts) => {
  const r = await fetch(url, opts);
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || `${r.status}`);
  return j;
};
const post = (url, body) => api(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
const hashHue = (s) => { let h = 0; for (const c of s || "") h = (h * 31 + c.charCodeAt(0)) % 360; return h; };
const usable = (t) => t && t.analyzed && t.file_exists;
// 文字幅のざっくり見積もり（全角=1、半角=0.6）で、枠に収まる文字サイズにする
const fitFont = (text, maxW, maxSize, minSize = 18) => {
  let w = 0;
  for (const c of text || "") w += c.charCodeAt(0) > 0xff ? 1 : 0.6;
  return Math.max(minSize, Math.min(maxSize, maxW / Math.max(1, w)));
};

// ---------------------------------------------------------------- データのキャッシュ
const waveCache = new Map(), detailCache = new Map();
function useCached(cache, id, url) {
  const [v, setV] = useState(id != null ? cache.get(id) || null : null);
  useEffect(() => {
    if (id == null) { setV(null); return; }
    if (cache.has(id)) { setV(cache.get(id)); return; }
    let alive = true;
    setV(null);
    api(url(id)).then((j) => { cache.set(id, j); alive && setV(j); }).catch(() => {});
    return () => { alive = false; };
  }, [id]);
  return v;
}
const useWave = (id) => { const w = useCached(waveCache, id, (i) => `/api/tracks/${i}/wave`); return w && w.peaks; };
const useDetail = (id) => useCached(detailCache, id, (i) => `/api/tracks/${i}`);
const fetchDetail = async (id) => {
  if (!detailCache.has(id)) detailCache.set(id, await api(`/api/tracks/${id}`));
  return detailCache.get(id);
};
const fetchWave = async (id) => {
  if (!waveCache.has(id)) waveCache.set(id, await api(`/api/tracks/${id}/wave`));
  return waveCache.get(id).peaks;
};

function useScale() {
  const [s, setS] = useState(1);
  useEffect(() => {
    const f = () => setS(Math.min(window.innerWidth / W, window.innerHeight / H));
    f();
    window.addEventListener("resize", f);
    return () => window.removeEventListener("resize", f);
  }, []);
  return s;
}

function useRaf(fn, on = true) {
  const ref = useRef(fn);
  ref.current = fn;
  useEffect(() => {
    if (!on) return;
    let id;
    const loop = () => { ref.current(); id = requestAnimationFrame(loop); };
    id = requestAnimationFrame(loop);
    return () => cancelAnimationFrame(id);
  }, [on]);
}

// ---------------------------------------------------------------- 部品
function Jacket({ track, size, round, style }) {
  const [err, setErr] = useState(false);
  useEffect(() => setErr(false), [track && track.id]);
  const st = { width: size, height: size, ...style };
  const cls = `jk${round ? " round" : ""}`;
  if (!track) return html`<div class=${cls} style=${st}></div>`;
  if (!track.jacket || err) {
    const h = hashHue(track.title);
    const small = size < 90;   // 小さいときは頭の2文字だけ大きく
    return html`<div class=${cls + " gen"} style=${{ ...st, fontSize: small ? size * 0.34 : Math.max(10, size * 0.13),
      background: `linear-gradient(135deg, hsl(${h},75%,52%), hsl(${(h + 70) % 360},70%,22%))` }}>${small ? [...track.title].slice(0, 2).join("") : track.title}</div>`;
  }
  // 動画サムネイルは 16:9 の上下に黒帯があるので、拡大して正方形に切り取る
  return html`<div class=${cls} style=${st}>
    <img src=${track.jacket} draggable="false" onError=${() => setErr(true)}
      style=${track.jacket_kind === "video" ? { transform: "scale(1.34)" } : null} /></div>`;
}

function Tri({ size = 90, color = "#ff3df2", fill = "#d9d9d9", stroke = 6 }) {
  // Figma の三角（右向き、塗りはグレー、枠が色）
  const s = size;
  return html`<span class="tri"><svg width=${s} height=${s} viewBox="0 0 100 100">
    <path d="M14 8 L92 50 L14 92 Z" fill=${fill} stroke=${color} stroke-width=${(stroke * 100) / s} stroke-linejoin="round" /></svg></span>`;
}

function Vinyl({ label = "#ffe600", size = 260 }) {
  const grooves = [];
  for (let r = 46; r < 98; r += 4) grooves.push(html`<circle key=${r} cx="100" cy="100" r=${r} fill="none" stroke="#2a2a2a" stroke-width="1" />`);
  return html`<svg class="vinyl" width=${size} height=${size} viewBox="0 0 200 200">
    <circle cx="100" cy="100" r="99" fill="#0b0b0b" stroke="#333" stroke-width="1.5" />
    ${grooves}
    <path d="M100 4 A96 96 0 0 1 190 70" fill="none" stroke="rgba(255,255,255,.18)" stroke-width="6" stroke-linecap="round" />
    <circle cx="100" cy="100" r="36" fill=${label} />
    <circle cx="100" cy="100" r="22" fill="none" stroke="rgba(0,0,0,.25)" stroke-width="2" />
    <circle cx="100" cy="100" r="5" fill="#000" />
  </svg>`;
}

function Loading({ text = "LOADING", sub, progress }) {
  return html`<div class="loading"><div class="wrap">
    <${Vinyl} />
    <div class="txt">${text}</div>
    ${progress != null && html`<div class="bar"><i style=${{ width: `${Math.round(progress * 100)}%` }}></i></div>`}
    <div class="sub">${sub || ""}</div>
  </div></div>`;
}

// 波形（ピークの棒グラフ）。color(t) で時刻ごとの色、range の外は暗く、progress まで明るく
function Wave({ peaks, width, height, duration, colorAt, range, progress, onSeek, markers = [] }) {
  const ref = useRef();
  useEffect(() => {
    const c = ref.current;
    if (!c) return;
    const dpr = 2;
    c.width = width * dpr;
    c.height = height * dpr;
    const g = c.getContext("2d");
    g.scale(dpr, dpr);
    g.clearRect(0, 0, width, height);
    if (!peaks || !peaks.length) {
      g.fillStyle = "#222";
      g.fillRect(0, height / 2 - 1, width, 2);
      return;
    }
    const n = Math.min(peaks.length, Math.floor(width / 3));
    const bw = width / n;
    for (let k = 0; k < n; k++) {
      const i = Math.floor((k / n) * peaks.length);
      const t = (k / n) * duration;
      const p = Math.max(0.04, peaks[i]);
      const h = p * (height - 4);
      const inR = !range || (t >= range[0] && t <= range[1]);
      const played = progress != null && t <= progress;
      g.globalAlpha = !inR ? 0.22 : progress == null || played ? 1 : 0.55;
      g.fillStyle = colorAt ? colorAt(t) : "#fff";
      g.fillRect(k * bw + 0.5, (height - h) / 2, Math.max(1, bw - 1.2), h);
    }
    g.globalAlpha = 1;
    for (const m of markers) {
      g.fillStyle = m.color;
      g.fillRect((m.t / duration) * width - 1, 0, 2, height);
    }
    if (progress != null) {
      g.fillStyle = "#fff";
      g.fillRect((progress / duration) * width - 1, 0, 2, height);
    }
  }, [peaks, width, height, duration, range && range[0], range && range[1], progress, markers, colorAt]);
  const click = (e) => {
    if (!onSeek) return;
    const r = e.currentTarget.getBoundingClientRect();
    onSeek(((e.clientX - r.left) / r.width) * duration);
  };
  return html`<canvas ref=${ref} class=${onSeek ? "click" : ""} style=${{ width, height, display: "block" }} onClick=${click}></canvas>`;
}

const sectionColor = (detail) => (t) => {
  if (!detail) return "#fff";
  const s = detail.sections.find((x) => t >= x.start && t < (x.end ?? Infinity));
  return s ? ROLE_COLOR[s.role] || ROLE_COLOR.other : "#888";
};
const firstChorus = (detail) => {
  const c = detail && detail.sections.find((s) => s.role === "chorus");
  return c ? c.start : detail ? Math.min(45, (detail.duration || 60) / 3) : 0;
};

// ---------------------------------------------------------------- 画面1・2: 曲を選ぶ
const ROW_H = 170, CENTER_Y = 407, WHEEL_L = -455, WHEEL_T = -199, RX = 540, RY = 568;

function SelectScreen({ tracks, routes, selected, setSelected, onNext }) {
  const [q, setQ] = useState("");
  const list = useMemo(() => tracks.filter((t) => !q || t.title.toLowerCase().includes(q.toLowerCase())), [tracks, q]);
  const [focus, setFocus] = useState(0);
  const cur = list[Math.min(focus, list.length - 1)];
  useEffect(() => { if (focus >= list.length) setFocus(Math.max(0, list.length - 1)); }, [list.length]);

  const byId = useMemo(() => new Map(tracks.map((t) => [t.id, t])), [tracks]);
  const byTitle = useMemo(() => new Map(tracks.map((t) => [t.title, t])), [tracks]);
  // シートのルートで「最後に選んだ曲」から次につながる曲
  const linkable = useMemo(() => {
    const last = byId.get(selected[selected.length - 1]);
    const out = new Set();
    if (!last) return out;
    for (const r of routes) {
      if (r.root === last.title && r.middle) out.add(r.middle);
      if (r.middle === last.title && r.leaf) out.add(r.leaf);
    }
    return out;
  }, [selected, routes, byId]);

  const toggle = (t) => {
    if (!usable(t)) return;
    setSelected((s) => (s.includes(t.id) ? s.filter((x) => x !== t.id) : s.length >= MAX_PICK ? s : [...s, t.id]));
  };

  // 試聴（ジャケットを押すとサビから）
  const audioRef = useRef(null);
  const [previewing, setPreviewing] = useState(null);
  const [pos, setPos] = useState(null);
  const stopPreview = () => {
    if (audioRef.current) { audioRef.current.pause(); audioRef.current = null; }
    setPreviewing(null);
    setPos(null);
  };
  useEffect(() => stopPreview, []);
  useEffect(() => { stopPreview(); }, [cur && cur.id]);
  const detail = useDetail(cur && usable(cur) ? cur.id : null);
  const peaks = useWave(cur && cur.file_exists ? cur.id : null);
  const preview = () => {
    if (!cur || !cur.file_exists) return;
    if (previewing === cur.id) return stopPreview();
    stopPreview();
    const a = new Audio(`/api/audio/${cur.id}`);
    a.currentTime = firstChorus(detail);
    a.volume = 0.9;
    a.play().catch(() => {});
    audioRef.current = a;
    setPreviewing(cur.id);
  };
  useRaf(() => { if (audioRef.current) setPos(audioRef.current.currentTime); }, previewing != null);
  const seek = (t) => {
    if (!cur) return;
    if (previewing !== cur.id) { preview(); }
    setTimeout(() => { if (audioRef.current) audioRef.current.currentTime = t; }, 0);
  };

  // ホイール・キー操作
  const acc = useRef(0);
  const onWheel = (e) => {
    acc.current += e.deltaY;
    const step = 70;
    if (Math.abs(acc.current) >= step) {
      const d = Math.trunc(acc.current / step);
      acc.current -= d * step;
      setFocus((f) => Math.max(0, Math.min(list.length - 1, f + d)));
    }
  };
  useEffect(() => {
    const key = (e) => {
      if (e.target.tagName === "INPUT") return;
      if (e.key === "ArrowDown") { setFocus((f) => Math.min(list.length - 1, f + 1)); e.preventDefault(); }
      else if (e.key === "ArrowUp") { setFocus((f) => Math.max(0, f - 1)); e.preventDefault(); }
      else if (e.key === "Enter" && cur) toggle(cur);
      else if (e.key === " ") { preview(); e.preventDefault(); }
      else if (e.key === "ArrowRight" && selected.length >= 2) onNext();
    };
    window.addEventListener("keydown", key);
    return () => window.removeEventListener("keydown", key);
  });

  const rows = [];
  for (let i = Math.max(0, focus - 3); i <= Math.min(list.length - 1, focus + 3); i++) {
    const t = list[i];
    const dy = (i - focus) * ROW_H;
    const arc = (1 - Math.sqrt(Math.max(0, 1 - (dy / RY) ** 2))) * RX * 0.55;   // 円盤に沿って少し左へ
    const isCur = i === focus;
    const k = selected.indexOf(t.id);
    const color = k >= 0 ? PICK[k % PICK.length] : null;
    const fs = isCur ? fitFont(t.title, 380, 62) : fitFont(t.title, 330, 44);
    rows.push(html`<div key=${t.id} class=${`row click${usable(t) ? "" : " off"}`}
      style=${{ top: CENTER_Y - WHEEL_T - ROW_H / 2, transform: `translate(${-arc}px, ${dy}px)` }}
      onClick=${() => (isCur ? toggle(t) : setFocus(i))}>
      <div class="sep"></div>
      <div class="jkw" style=${color ? { boxShadow: `0 0 0 5px ${color}, 0 0 0 10px ${color}80` } : null}>
        <${Jacket} track=${t} size=${122} />
        ${color && html`<div class="num" style=${{ background: color }}>${k + 1}</div>`}
      </div>
      <div class="ttl" style=${{ fontSize: fs, fontWeight: isCur ? 700 : 500, top: linkable.has(t.title) ? 52 : (ROW_H - fs * 1.25) / 2 }}>${t.title}</div>
      ${linkable.has(t.title) && html`<div class="link" style=${{ top: 26 }}>シートでつながる曲</div>`}
      ${!usable(t) && html`<div class="sub" style=${{ top: 122 }}>${!t.file_exists ? "音源なし" : "未解析"}</div>`}
    </div>`);
  }

  const pickedTracks = selected.map((id) => byId.get(id)).filter(Boolean);
  return html`<div class="screen" onWheel=${onWheel}>
    <div class="wheel">${rows}
      ${!list.length && html`<div style=${{ position: "absolute", left: 560, top: 560, fontSize: 22, color: "#555" }}>該当する曲がありません</div>`}
    </div>
    <div class="knob"></div>
    <div class="wheel-hint">スクロール / ↑↓ で回す・クリックか Enter で選ぶ・スペースで試聴</div>

    <div class="h1" style=${{ left: 690, top: 32, fontSize: 38 }}>好きな曲を選択してね</div>
    <input class="search" type="text" placeholder="曲名で探す" value=${q} onInput=${(e) => { setQ(e.target.value); setFocus(0); }} />

    ${!tracks.length ? html`<div class="empty-lib">まだ曲がありません。<br/>ターミナルで <code>sheet-fetch</code> と
      <code>analyze --set sheet</code> を実行してから、この画面を開き直してください。</div>`
    : cur && html`
      <div class="bigjk click" onClick=${preview} title="サビから試聴（スペースキー）">
        <${Jacket} track=${cur} size=${520} />
        <div class=${`play${previewing === cur.id ? " on" : ""}`}>
          <svg viewBox="0 0 100 100">${previewing === cur.id
            ? html`<g fill="#fff"><rect x="28" y="22" width="15" height="56" rx="3" /><rect x="57" y="22" width="15" height="56" rx="3" /></g>`
            : html`<path d="M30 18 L82 50 L30 82 Z" fill="#fff" />`}</svg>
        </div>
      </div>
      <div class="info"><b>${cur.title}</b>${[cur.artist, cur.bpm && `${Math.round(cur.bpm)} BPM`, cur.key, cur.camelot]
        .filter(Boolean).join("　")}</div>
      <div class="abs" style=${{ left: 690, top: 672 }}>
        <${Wave} peaks=${peaks} width=${520} height=${62} duration=${(detail && detail.duration) || cur.duration || 1}
          colorAt=${detail ? sectionColor(detail) : null} progress=${previewing === cur.id ? pos : null} onSeek=${seek} />
      </div>`}

    <div class="tray">
      ${pickedTracks.length ? pickedTracks.map((t, k) => html`<div key=${t.id} class="chip click" title=${t.title}
          onClick=${() => setFocus(list.findIndex((x) => x.id === t.id))}>
          <${Jacket} track=${t} size=${54} style=${{ boxShadow: `0 0 0 3px ${PICK[k % PICK.length]}` }} />
          <button class="x" onClick=${(e) => { e.stopPropagation(); toggle(t); }}>✕</button>
        </div>`)
      : html`<span class="empty">曲を2つ以上選んでね（シートのルートなら3曲）</span>`}
    </div>
    <button class="next" style=${{ left: 1100, top: 754 }} disabled=${selected.length < 2} onClick=${onNext}>
      次へ <${Tri} size=${44} color="#ff3df2" stroke=${4} /></button>
  </div>`;
}

// ---------------------------------------------------------------- 画面4: 繋ぎ方を選ぶ
// サーバの mixplan.apply_styles と同じ決め方（試聴をすぐ鳴らすため画面側でも計算する）
function withStyles(plan, styles) {
  const p = JSON.parse(JSON.stringify(plan));
  p.transitions.forEach((t, k) => {
    const st = STYLE[styles[k]] ? styles[k] : "auto";
    const a = p.items[k], b = p.items[k + 1];
    t.style = st;
    if (st === "cut") t.overlap_beats = 0;
    else if (st === "fade") {
      const room = Math.min((t.in * b.bpm) / 60, (((a.end - a.start) / 3) * a.bpm) / 60);
      t.overlap_beats = Math.floor(Math.max(1, Math.min(t.tempo_gap <= 0.06 ? 16 : 8, room)));
    } else t.overlap_beats = t.tempo_gap <= 0.06 ? 4 : 1;
  });
  return p;
}

function mixColorAt(layout) {
  return (m) => {
    const segs = layout.segments;
    let k = 0;
    for (let i = 0; i < segs.length; i++) if (m >= segs[i].mixAnchor) k = i;
    return PICK[k % PICK.length];
  };
}

// 計画全体の波形（各曲の波形を、ミックスの時間軸に並べ直す）
function useMixPeaks(plan, layout, n = 900) {
  const [peaks, setPeaks] = useState(null);
  useEffect(() => {
    if (!plan || !layout) return;
    let alive = true;
    (async () => {
      const ids = [...new Set(plan.items.map((x) => x.track_id))];
      const waves = new Map();
      const details = new Map();
      await Promise.all(ids.map(async (id) => {
        try { waves.set(id, await fetchWave(id)); details.set(id, await fetchDetail(id)); } catch (e) { /* なし */ }
      }));
      const out = new Array(n).fill(0);
      for (let x = 0; x < n; x++) {
        const m = (x / n) * layout.total;
        let v = 0;
        for (const s of layout.segments) {
          if (m < s.mixStart || m > s.mixEnd) continue;
          const w = waves.get(s.track_id), d = details.get(s.track_id);
          if (!w || !d) continue;
          const pos = s.toPos(m);
          v = Math.max(v, w[Math.min(w.length - 1, Math.floor((pos / d.duration) * w.length))] * s.gainAt(m));
        }
        out[x] = v;
      }
      alive && setPeaks(out);
    })();
    return () => { alive = false; };
  }, [plan, layout]);
  return peaks;
}

function ConnectScreen({ tracks, selected, styles, setStyles, minSec, setMinSec, grace, setGrace, onBack, onStart, onError }) {
  const byId = useMemo(() => new Map(tracks.map((t) => [t.id, t])), [tracks]);
  const [base, setBase] = useState(null);       // サーバの計画（つなぎ位置）
  const [busy, setBusy] = useState(false);
  const [open, setOpen] = useState(null);       // 開いているつなぎ目
  const [hover, setHover] = useState(null);     // 試聴中 {k, style}
  const previewer = useRef(null);
  const hoverTimer = useRef(null);

  // つなぎ位置はサーバで計算（最低秒数などが変わったら作り直す）
  useEffect(() => {
    let alive = true;
    setBusy(true);
    const tm = setTimeout(() => {
      post("/api/plan", { songs: selected, min_sec: minSec, chorus_grace: grace ? 15 : 0, styles })
        .then((p) => { alive && setBase(p); })
        .catch((e) => alive && onError(e.message))
        .finally(() => alive && setBusy(false));
    }, 250);
    return () => { alive = false; clearTimeout(tm); };
  }, [selected.join(","), minSec, grace]);
  // 音声は先に読み込んでおく（スタートや試聴をすぐ鳴らすため）
  useEffect(() => {
    audioContext();
    selected.forEach((id) => loadBuffer(id, `/api/audio/${id}`).catch(() => {}));
  }, [selected.join(",")]);

  const plan = useMemo(() => (base ? withStyles(base, styles) : null), [base, styles.join(",")]);
  const layout = useMemo(() => (plan ? new MixPlayer(plan).layout : null), [plan]);
  const mixPeaks = useMixPeaks(plan, layout);

  const stopPreview = () => {
    clearTimeout(hoverTimer.current);
    if (previewer.current) previewer.current.stop();
    previewer.current = null;
    setHover(null);
  };
  useEffect(() => stopPreview, []);
  const tryStyle = (k, st) => {
    clearTimeout(hoverTimer.current);
    hoverTimer.current = setTimeout(async () => {
      if (!base) return;
      if (previewer.current) previewer.current.stop();
      const s2 = [...styles];
      s2[k] = st;
      const pl = new MixPlayer(withStyles(base, s2));
      previewer.current = pl;
      setHover({ k, style: st, loading: true });
      try {
        await audioContext().resume();
        await pl.load();
        if (previewer.current !== pl) return;
        setHover({ k, style: st });
        pl.onend = () => previewer.current === pl && setHover(null);
        await pl.preview(k, 7, 9);
      } catch (e) { onError(e.message); }
    }, 280);
  };
  const choose = (k, st) => {
    const s2 = [...styles];
    s2[k] = st;
    setStyles(s2);
    setOpen(null);
    stopPreview();
  };

  const n = selected.length;
  const gap = n <= 3 ? 146 : 120;
  const S = Math.max(150, Math.min(297, (1170 - (n - 1) * gap) / n));
  const totalW = 55 * 2 + n * S + (n - 1) * gap;
  const xOf = (i) => 55 + i * (S + gap);

  return html`<div class="screen" onClick=${() => { setOpen(null); stopPreview(); }}>
    <div class="h1" style=${{ left: 55, top: 53 }}>繋ぎ方を選んでね</div>
    <div class="legend">${STYLE_ORDER.map((k) => html`<span key=${k}><i style=${{ background: STYLE[k].color }}></i>${STYLE[k].label}</span>`)}</div>

    <div class="chain" style=${{ top: 160 }}>
      <div style=${{ position: "relative", width: totalW, height: 400 }}>
        ${selected.map((id, i) => {
          const t = byId.get(id);
          const it = plan && plan.items[i];
          return html`<div key=${id} class="cj" style=${{ left: xOf(i), top: 35, width: S }}>
            <${Jacket} track=${t} size=${S} style=${{ boxShadow: `0 0 0 4px ${PICK[i % PICK.length]}` }} />
            <div class="t">${t ? t.title : id}</div>
            <div class="r">${it ? `${fmt(it.start)} → ${fmt(it.end)}（${Math.round(it.end - it.start)}秒）` : busy ? "計算中…" : ""}</div>
            <div class="r">${it ? it.sections_played.join(" → ") : ""}</div>
          </div>`;
        })}
        ${selected.slice(1).map((_, k) => {
          const st = styles[k] || "auto";
          const tr = plan && plan.transitions[k];
          const cx = xOf(k) + S + gap / 2;
          const sz = Math.min(90, gap - 30);
          return html`<div key=${k} class="arrow" style=${{ left: cx - 70, width: 140, top: 35 + S / 2 - sz / 2 }}>
            <button title="繋ぎ方を選ぶ" onClick=${(e) => { e.stopPropagation(); stopPreview(); setOpen(open === k ? null : k); }}>
              <${Tri} size=${sz} color=${STYLE[st].color} /></button>
            <div class="lab" style=${{ color: STYLE[st].color }}>${STYLE[st].label}</div>
            ${tr && html`<div class="kind">${tr.kind}${tr.manual ? "（シート）" : ""}<br/>${fmt(tr.out)} → ${fmt(tr.in)}</div>`}
          </div>`;
        })}
      </div>
    </div>

    ${open != null && (() => {
      const st = styles[open] || "auto";
      const cx = xOf(open) + S + gap / 2;
      const chain = document.querySelector(".chain");
      const sl = chain ? chain.scrollLeft : 0;
      const left = Math.max(20, Math.min(W - 355, cx - sl - 168));
      const hv = hover && hover.k === open ? hover : null;
      return html`<div class="pop" style=${{ left, top: 160 + 35 + S * 0.62, borderColor: STYLE[st].color }}
        onClick=${(e) => e.stopPropagation()} onMouseLeave=${stopPreview}>
        <h3>繋ぎ方を選択してね</h3>
        ${STYLE_ORDER.map((k) => html`<button key=${k} class=${`opt${k === st ? " on" : ""}`}
            onMouseEnter=${() => tryStyle(open, k)} onClick=${() => choose(open, k)}>
            <span class="dot" style=${{ background: STYLE[k].color }}></span>・${STYLE[k].label}</button>`)}
        <div class="desc">${hv ? STYLE[hv.style].desc : "カーソルを合わせると、そのつなぎ方で試聴できます"}</div>
        <div class="listen">${hv ? (hv.loading ? "♪ 読み込み中…" : `♪ 試聴中（${STYLE[hv.style].label}）`) : ""}</div>
      </div>`;
    })()}

    <div class="mixwave">
      ${layout && html`<${Wave} peaks=${mixPeaks} width=${1170} height=${56} duration=${layout.total}
        colorAt=${mixColorAt(layout)}
        markers=${layout.transitionsMix.map((t, k) => ({ t, color: STYLE[styles[k] || "auto"].color }))} />`}
    </div>
    <div class="ctl" onClick=${(e) => e.stopPropagation()}>
      <span>1曲を最低 <b>${minSec}</b> 秒流す <input type="range" min="45" max="180" step="5" value=${minSec}
        onInput=${(e) => setMinSec(+e.target.value)} /></span>
      <label><input type="checkbox" checked=${grace} onChange=${(e) => setGrace(e.target.checked)} />サビ終わりなら少し早めに次へ行ってOK</label>
      <span>全体 <b>${layout ? fmt(layout.total) : "-:--"}</b></span>
    </div>
    ${plan && plan.warnings.length > 0 && html`<div class="warns">${plan.warnings.map((w, i) => html`<div key=${i}>※ ${w}</div>`)}</div>`}

    <button class="back" style=${{ left: 55, top: 776 }} onClick=${onBack}>◀ 曲を選び直す</button>
    <button class="next" style=${{ left: 1040, top: 762 }} disabled=${!plan || busy}
      onClick=${(e) => { e.stopPropagation(); stopPreview(); onStart(plan); }}>スタート <${Tri} size=${48} color="#ff3df2" stroke=${4} /></button>
  </div>`;
}

// ---------------------------------------------------------------- 画面5: 再生中
function RingViz({ player, color, playing }) {
  const ref = useRef();
  const data = useRef(null);
  useRaf(() => {
    const c = ref.current;
    if (!c) return;
    const S = 760, dpr = 2;
    if (c.width !== S * dpr) { c.width = S * dpr; c.height = S * dpr; }
    const g = c.getContext("2d");
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, S, S);
    const an = player.analyser;
    if (!data.current || data.current.length !== an.frequencyBinCount) data.current = new Uint8Array(an.frequencyBinCount);
    an.getByteFrequencyData(data.current);
    const N = 120, R0 = 304, L = 34;
    g.lineCap = "round";
    g.strokeStyle = color;
    g.lineWidth = 3.2;
    for (let i = 0; i < N; i++) {
      // 低音〜高音を左右対称に並べる
      const j = i < N / 2 ? i : N - 1 - i;
      const bin = Math.floor(2 + (j / (N / 2)) ** 1.7 * 300);
      const v = data.current[bin] / 255;
      const len = 3 + v * v * L;
      const a = (i / N) * Math.PI * 2 - Math.PI / 2;
      g.globalAlpha = 0.35 + v * 0.65;
      g.beginPath();
      g.moveTo(S / 2 + Math.cos(a) * R0, S / 2 + Math.sin(a) * R0);
      g.lineTo(S / 2 + Math.cos(a) * (R0 + len), S / 2 + Math.sin(a) * (R0 + len));
      g.stroke();
    }
  }, true);
  return html`<canvas ref=${ref} class="ringviz"></canvas>`;
}

function PlayScreen({ tracks, plan, player, onBack }) {
  const byId = useMemo(() => new Map(tracks.map((t) => [t.id, t])), [tracks]);
  const [now, setNow] = useState(0);
  const [playing, setPlaying] = useState(player.playing);
  useRaf(() => { setNow(player.now()); setPlaying(player.playing); });
  const layout = player.layout;
  const { index } = player.locate(now);
  const it = plan.items[index];
  const tr = plan.transitions[index];
  const nextT = layout.transitionsMix[index];
  const mixPeaks = useMixPeaks(plan, layout);
  const ended = !playing && now >= layout.total - 0.2;

  const toggle = () => (player.playing ? player.stop() : player.play(ended ? 0 : player.now()));
  const skip = () => {
    if (nextT == null) return;
    player.play(Math.max(player.now(), nextT - 6));
  };
  const seekSong = (i) => player.play(Math.max(0, layout.segments[i].mixAnchor - (i ? 2 : 0)));
  useEffect(() => {
    const key = (e) => {
      if (e.key === " ") { toggle(); e.preventDefault(); }
      else if (e.key === "ArrowRight") skip();
      else if (e.key === "ArrowLeft") seekSong(index);
      else if (e.key === "Escape") onBack();
    };
    window.addEventListener("keydown", key);
    return () => window.removeEventListener("keydown", key);
  });

  // つなぎ目では、出る曲と入る曲のジャケットを音量に合わせて重ねる
  const layers = layout.segments
    .map((s, i) => ({ i, g: now >= s.mixStart && now <= s.mixEnd ? s.gainAt(now) : 0 }))
    .filter((x) => x.g > 0.01);
  if (!layers.length) layers.push({ i: index, g: 1 });
  const color = PICK[index % PICK.length];

  return html`<div class="screen">
    <button class="back" style=${{ left: 36, top: 20 }} onClick=${onBack}>◀ 繋ぎ方へ</button>
    <div class="queue">
      ${plan.items.map((x, i) => html`<div key=${i} class=${`q click${i === index ? " on" : ""}`} onClick=${() => seekSong(i)}>
        <${Jacket} track=${byId.get(x.track_id)} size=${56} style=${{ boxShadow: i === index ? `0 0 0 3px ${PICK[i % PICK.length]}` : null }} />
        <div style=${{ minWidth: 0 }}><div class="t">${x.title}</div>
          <div class="s">${fmt(x.start)} → ${fmt(x.end)}${i < plan.transitions.length ? `・${STYLE[plan.transitions[i].style || "auto"].label}` : ""}</div></div>
      </div>`)}
    </div>

    <${RingViz} player=${player} color=${color} playing=${playing} />
    <div class=${`disc click${playing ? "" : " paused"}`} onClick=${toggle} title="クリックで一時停止／再生（スペースキー）">
      ${layers.map((l) => html`<div key=${l.i} class="layer" style=${{ opacity: layers.length > 1 ? Math.sqrt(l.g) : 1 }}>
        <${Jacket} track=${byId.get(plan.items[l.i].track_id)} size=${589} round=${true} /></div>`)}
      <div class="grooves"></div>
      <div class="hole"></div>
      <div class="pausemark"><svg width="110" height="110" viewBox="0 0 100 100"><circle cx="50" cy="50" r="48" fill="rgba(0,0,0,.55)" />
        <path d="M38 28 L76 50 L38 72 Z" fill="#fff" /></svg></div>
    </div>
    <div class="nowtitle" style=${{ fontSize: fitFont(it.title, 900, 46) }}>${ended ? "おしまい" : it.title}</div>
    <div class="nowsub">${ended ? "ディスクを押すと最初から" : `${fmt(player.locate(now).pos)} / この曲は ${fmt(it.end)} まで`}</div>

    ${tr && !ended && html`<button class="skip" onClick=${skip} title="次の曲へ進む（→キー）">
      <${Tri} size=${50} color=${STYLE[tr.style || "auto"].color} /><${Tri} size=${50} color=${STYLE[tr.style || "auto"].color} /><${Tri} size=${50} color=${STYLE[tr.style || "auto"].color} />
    </button>`}
    ${tr && !ended && html`<div class="upnext">
      <span>次（あと ${fmt(nextT - now)}）</span><b>${plan.items[index + 1].title}</b>
      <span style=${{ color: STYLE[tr.style || "auto"].color, fontWeight: 700 }}>${STYLE[tr.style || "auto"].label}</span>・${tr.kind}
    </div>`}

    <div class="times"><span>${fmt(now)}</span><span>${fmt(layout.total)}</span></div>
    <div class="timeline"><${Wave} peaks=${mixPeaks} width=${1200} height=${34} duration=${layout.total} colorAt=${mixColorAt(layout)}
      progress=${now} onSeek=${(t) => player.play(t)}
      markers=${layout.transitionsMix.map((t, k) => ({ t, color: STYLE[plan.transitions[k].style || "auto"].color }))} /></div>
  </div>`;
}

// ---------------------------------------------------------------- 全体
function App() {
  const scale = useScale();
  const [screen, setScreen] = useState("boot");
  const [tracks, setTracks] = useState([]);
  const [routes, setRoutes] = useState([]);
  const [selected, setSelected] = useState([]);
  const [styles, setStyles] = useState([]);
  const [minSec, setMinSec] = useState(90);
  const [grace, setGrace] = useState(true);
  const [player, setPlayer] = useState(null);
  const [plan, setPlan] = useState(null);
  const [load, setLoad] = useState(null);
  const [err, setErr] = useState(null);

  useEffect(() => {
    const t0 = Date.now();
    Promise.all([api("/api/tracks"), api("/api/routes").catch(() => [])])
      .then(([t, r]) => { setTracks(t); setRoutes(r); })
      .catch((e) => setErr(`サーバに接続できません: ${e.message}`))
      .finally(() => setTimeout(() => setScreen("select"), Math.max(0, 1100 - (Date.now() - t0))));
  }, []);
  useEffect(() => { if (err) { const t = setTimeout(() => setErr(null), 6000); return () => clearTimeout(t); } }, [err]);
  // つなぎ目の数に合わせて styles をそろえる
  useEffect(() => {
    setStyles((s) => Array.from({ length: Math.max(0, selected.length - 1) }, (_, k) => s[k] || "auto"));
  }, [selected.length]);

  const start = async (p) => {
    audioContext().resume();   // クリックした瞬間に音を出せる状態にする（ブラウザの制限）
    if (player) player.stop();
    const pl = new MixPlayer(p);
    setPlan(p);
    setLoad({ progress: 0 });
    setScreen("loading");
    try {
      await pl.load((x) => setLoad({ progress: x }));
      setPlayer(pl);
      setScreen("play");
      await pl.play(0);
    } catch (e) {
      setErr(e.message);
      setScreen("connect");
    } finally {
      setLoad(null);
    }
  };
  const back = () => { if (player) player.stop(); setScreen("connect"); };

  let body;
  if (screen === "boot") body = html`<${Loading} sub="曲を読み込んでいます" />`;
  else if (screen === "loading") body = html`<${Loading} sub="音源を準備しています" progress=${load ? load.progress : 0} />`;
  else if (screen === "select") body = html`<${SelectScreen} tracks=${tracks} routes=${routes} selected=${selected} setSelected=${setSelected}
    onNext=${() => setScreen("connect")} />`;
  else if (screen === "connect") body = html`<${ConnectScreen} tracks=${tracks} selected=${selected} styles=${styles} setStyles=${setStyles}
    minSec=${minSec} setMinSec=${setMinSec} grace=${grace} setGrace=${setGrace} onError=${setErr}
    onBack=${() => setScreen("select")} onStart=${start} />`;
  else if (screen === "play" && player) body = html`<${PlayScreen} tracks=${tracks} plan=${plan} player=${player} onBack=${back} />`;

  return html`<div class="viewport"><div class="stage" style=${{ transform: `translate(-50%, -50%) scale(${scale})` }}>
    ${body}
    ${err && html`<div class="err">${err}</div>`}
  </div></div>`;
}

ReactDOM.createRoot(document.getElementById("root")).render(html`<${App} />`);
