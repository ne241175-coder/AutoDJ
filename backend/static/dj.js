// AutoDJ 画面（Figma「DJプロジェクト」UI ページを元にした流れ）
//   ローディング → 最初の曲（画面1）→ 次の曲（画面2）→ 最後の曲 → 繋ぎ方とつなぎ目の解説（画面4）→ 再生（画面5）
// 3曲のルートはスプレッドシートの「曲リスト」（ルート曲 → 中間曲 → 曲）から作る。
// ビルド不要: React UMD + htm。API は backend/app.py、再生は engine.js（mp3 は切らずに秒数指定で鳴らす）。
import { MixPlayer, loadBuffer, audioContext } from "./engine.js";

const { useState, useEffect, useRef, useMemo } = React;
const html = htm.bind(React.createElement);

const W = 1280, H = 832;                     // Figma のフレームサイズ
const PICK = ["#00b2ff", "#00ff4d", "#ff3df2"];   // 1曲目・2曲目・3曲目の色
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
const STEPS = [
  { title: "最初の曲を選んでね", side: "right" },
  { title: "次の曲を選んでね", side: "left" },
  { title: "最後の曲を選んでね", side: "right" },
];

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
const fitFont = (text, maxW, maxSize, minSize = 18) => {
  let w = 0;
  for (const c of text || "") w += c.charCodeAt(0) > 0xff ? 1 : 0.6;
  return Math.max(minSize, Math.min(maxSize, maxW / Math.max(1, w)));
};
const pct = (x) => (x == null ? "-" : `${Math.round(x * 100)}%`);

// ---------------------------------------------------------------- データのキャッシュ
const waveCache = new Map(), detailCache = new Map();
function useCached(cache, key, url) {
  const [v, setV] = useState(key != null ? cache.get(key) || null : null);
  useEffect(() => {
    if (key == null) { setV(null); return; }
    if (cache.has(key)) { setV(cache.get(key)); return; }
    let alive = true;
    setV(null);
    api(url).then((j) => { cache.set(key, j); alive && setV(j); }).catch(() => {});
    return () => { alive = false; };
  }, [key]);
  return v;
}
const useWave = (id) => { const w = useCached(waveCache, id, `/api/tracks/${id}/wave`); return w && w.peaks; };
const useDetail = (id, auto = false) => useCached(detailCache, id == null ? null : `${id}:${auto ? 1 : 0}`,
  `/api/tracks/${id}${auto ? "?auto=1" : ""}`);
const fetchDetail = async (id, auto = false) => {
  const k = `${id}:${auto ? 1 : 0}`;
  if (!detailCache.has(k)) detailCache.set(k, await api(`/api/tracks/${id}${auto ? "?auto=1" : ""}`));
  return detailCache.get(k);
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

function Tri({ size = 90, color = "#ff3df2", fill = "#d9d9d9", stroke = 6, flip = false }) {
  const s = size;
  return html`<span class="tri"><svg width=${s} height=${s} viewBox="0 0 100 100" style=${flip ? { transform: "scaleX(-1)" } : null}>
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

// 波形（ピークの棒グラフ）。colorAt(t) で時刻ごとの色、range の外は暗く、progress まで明るく
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

// ---------------------------------------------------------------- 画面1・2: 曲を選ぶ（3段階）
const ROW_H = 170;
// 円盤（Figma の楕円 1081×1136）。left = 左に半分隠れる（画面2）、right = 右に半分隠れる（画面1）
const WHEEL = {
  left: { L: -455, T: -199, centerY: 407, jx: 511, tx: 676, tw: 380, bigX: 690, bigY: 116, big: 520, titleX: 690 },
  right: { L: 675, T: -169, centerY: 399, jx: 64, tx: 225, tw: 340, bigX: 83, bigY: 116, big: 520, titleX: 83 },
};
const RX = 540, RY = 568;

function SelectScreen({ step, options, picks, byTitle, onPick, onBack, routesOk }) {
  const conf = STEPS[step];
  const G = WHEEL[conf.side];
  const [focus, setFocus] = useState(0);
  useEffect(() => setFocus(0), [step, options.map((t) => t.title).join(",")]);
  const cur = options[Math.min(focus, options.length - 1)];
  const color = PICK[step];

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
  useEffect(() => { stopPreview(); }, [cur && cur.id, step]);
  const detail = useDetail(usable(cur) ? cur.id : null);
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
    if (previewing !== cur.id) preview();
    setTimeout(() => { if (audioRef.current) audioRef.current.currentTime = t; }, 0);
  };
  const choose = (t) => { if (usable(t)) { stopPreview(); onPick(t); } };

  const acc = useRef(0);
  const onWheel = (e) => {
    acc.current += e.deltaY;
    const stepPx = 70;
    if (Math.abs(acc.current) >= stepPx) {
      const d = Math.trunc(acc.current / stepPx);
      acc.current -= d * stepPx;
      setFocus((f) => Math.max(0, Math.min(options.length - 1, f + d)));
    }
  };
  useEffect(() => {
    const key = (e) => {
      if (e.key === "ArrowDown") { setFocus((f) => Math.min(options.length - 1, f + 1)); e.preventDefault(); }
      else if (e.key === "ArrowUp") { setFocus((f) => Math.max(0, f - 1)); e.preventDefault(); }
      else if (e.key === "Enter" || e.key === "ArrowRight") { if (cur) choose(cur); }
      else if (e.key === " ") { preview(); e.preventDefault(); }
      else if ((e.key === "Backspace" || e.key === "ArrowLeft" || e.key === "Escape") && step > 0) onBack();
    };
    window.addEventListener("keydown", key);
    return () => window.removeEventListener("keydown", key);
  });

  const sgn = conf.side === "left" ? -1 : 1;   // 円盤のふちに沿う向き
  const rows = options.map((t, i) => {
    const dy = (i - focus) * ROW_H;
    if (Math.abs(i - focus) > 3) return null;
    const arc = (1 - Math.sqrt(Math.max(0, 1 - (dy / RY) ** 2))) * RX * 0.55;
    const isCur = i === focus;
    const fs = isCur ? fitFont(t.title, G.tw, 60) : fitFont(t.title, G.tw - 40, 44);
    return html`<div key=${t.id} class=${`row click${usable(t) ? "" : " off"}`}
      style=${{ top: G.centerY - G.T - ROW_H / 2, transform: `translate(${sgn * arc}px, ${dy}px)` }}
      onClick=${() => (isCur ? choose(t) : setFocus(i))}>
      <div class="sep"></div>
      <div class="jkw" style=${{ left: G.jx, boxShadow: isCur ? `0 0 0 5px ${color}, 0 0 0 10px ${color}80` : null }}>
        <${Jacket} track=${t} size=${122} />
      </div>
      <div class="ttl" style=${{ left: G.tx, fontSize: fs, fontWeight: isCur ? 700 : 500, top: (ROW_H - fs * 1.25) / 2 }}>${t.title}</div>
      ${!usable(t) && html`<div class="sub" style=${{ left: G.tx + 2, top: 122 }}>${!t.file_exists ? "音源なし" : "未解析"}</div>`}
    </div>`;
  });

  // ここまでに選んだ曲（パンくず）
  const crumbs = [0, 1, 2].map((k) => {
    const t = k < picks.length ? picks[k] : k === step ? cur : null;
    return html`<div key=${k} class="crumb">
      ${k > 0 && html`<${Tri} size=${22} color=${PICK[k]} stroke=${3} />`}
      <div class="cj2" style=${{ opacity: k > step ? 0.25 : k === step ? 0.85 : 1 }}>
        ${t ? html`<${Jacket} track=${t} size=${52} style=${{ boxShadow: `0 0 0 3px ${PICK[k]}` }} />`
            : html`<div class="empty-jk" style=${{ borderColor: PICK[k] }}>${k + 1}</div>`}
      </div>
    </div>`;
  });

  return html`<div class="screen" onWheel=${onWheel}>
    <div class="wheel" style=${{ left: G.L, top: G.T }}>${rows}
      ${!options.length && html`<div style=${{ position: "absolute", left: conf.side === "left" ? 560 : 120, top: 560, fontSize: 22, color: "#555" }}>
        ${routesOk ? "この先のルートがありません" : "ルートが読み込めません（sheet-sync を実行してください）"}</div>`}
    </div>
    <div class="knob" style=${conf.side === "left" ? { left: -61, top: G.centerY - 61 } : { left: W - 61, top: G.centerY - 61 }}></div>

    <div class="h1" style=${{ left: G.titleX, top: 32, fontSize: 38 }}>${conf.title}</div>
    <div class="stepno" style=${{ left: G.titleX, top: 84, color }}>STEP ${step + 1} / 3</div>

    ${cur && html`
      <div class="bigjk click" style=${{ left: G.bigX, top: G.bigY + 14 }} onClick=${preview} title="サビから試聴（スペースキー）">
        <${Jacket} track=${cur} size=${G.big - 30} />
        <div class=${`play${previewing === cur.id ? " on" : ""}`}>
          <svg viewBox="0 0 100 100">${previewing === cur.id
            ? html`<g fill="#fff"><rect x="28" y="22" width="15" height="56" rx="3" /><rect x="57" y="22" width="15" height="56" rx="3" /></g>`
            : html`<path d="M30 18 L82 50 L30 82 Z" fill="#fff" />`}</svg>
        </div>
      </div>
      <div class="info" style=${{ left: G.bigX, top: G.bigY + G.big - 4, width: G.big - 30 }}>
        <b>${cur.title}</b>${[cur.bpm && `${Math.round(cur.bpm)} BPM`, cur.key, cur.camelot].filter(Boolean).join("　")}</div>
      <div class="abs" style=${{ left: G.bigX, top: G.bigY + G.big + 22 }}>
        <${Wave} peaks=${peaks} width=${G.big - 30} height=${44} duration=${(detail && detail.duration) || cur.duration || 1}
          colorAt=${detail ? sectionColor(detail) : null} progress=${previewing === cur.id ? pos : null} onSeek=${seek} />
      </div>`}

    <div class="crumbs" style=${{ left: G.bigX, top: 742 }}>${crumbs}</div>
    ${step > 0 && html`<button class="back" style=${{ left: G.bigX + 270, top: 756 }} onClick=${onBack}>◀ 戻る</button>`}
    <button class="next" style=${{ left: G.bigX + (step > 0 ? 370 : 300), top: 754 }} disabled=${!usable(cur)} onClick=${() => choose(cur)}>
      決定 <${Tri} size=${44} color=${color} stroke=${4} /></button>
    <div class="wheel-hint" style=${{ left: G.bigX }}>スクロール / ↑↓ で回す・クリックか Enter で決定・スペースで試聴</div>
  </div>`;
}

// ---------------------------------------------------------------- 画面4: 繋ぎ方とつなぎ目
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

function useMixPeaks(plan, layout, n = 900) {
  const [peaks, setPeaks] = useState(null);
  useEffect(() => {
    if (!plan || !layout) return;
    let alive = true;
    (async () => {
      const ids = [...new Set(plan.items.map((x) => x.track_id))];
      const waves = new Map(), durs = new Map();
      await Promise.all(ids.map(async (id) => {
        try { waves.set(id, await fetchWave(id)); durs.set(id, (await fetchDetail(id)).duration); } catch (e) { /* なし */ }
      }));
      const out = new Array(n).fill(0);
      for (let x = 0; x < n; x++) {
        const m = (x / n) * layout.total;
        let v = 0;
        for (const s of layout.segments) {
          if (m < s.mixStart || m > s.mixEnd) continue;
          const w = waves.get(s.track_id), d = durs.get(s.track_id);
          if (!w || !d) continue;
          const pos = s.toPos(m) * s.rate;   // 元の曲の秒数
          v = Math.max(v, w[Math.min(w.length - 1, Math.floor((pos / d) * w.length))] * s.gainAt(m));
        }
        out[x] = v;
      }
      alive && setPeaks(out);
    })();
    return () => { alive = false; };
  }, [plan, layout]);
  return peaks;
}

// 1曲ぶんの帯（区間の色・名前・流す範囲・つなぎ位置）
function SongStrip({ track, item, auto, width, mark, markColor, markLabel, side }) {
  const detail = useDetail(track && track.id, auto);
  const peaks = useWave(track && track.id);
  const dur = (detail && detail.duration) || 1;
  const x = (t) => (t / dur) * width;
  return html`<div class="strip" style=${{ width }}>
    <div class="strip-head"><span class="dot" style=${{ background: markColor }}></span><b>${track ? track.title : ""}</b>
      <span>${item ? `${fmt(item.start)} 〜 ${fmt(item.end)} を流す` : ""}</span></div>
    <div class="bands">
      ${detail && detail.sections.map((s, i) => html`<div key=${i} class="band"
        style=${{ left: x(s.start), width: Math.max(1, x((s.end ?? dur)) - x(s.start)), background: ROLE_COLOR[s.role] || ROLE_COLOR.other }}>
        <span>${s.label}</span></div>`)}
      ${item && html`<div class="dimL" style=${{ width: x(item.start) }}></div><div class="dimR" style=${{ left: x(item.end) }}></div>`}
    </div>
    <div class="wv"><${Wave} peaks=${peaks} width=${width} height=${30} duration=${dur} colorAt=${detail ? sectionColor(detail) : null}
      range=${item ? [item.start, item.end] : null} /></div>
    ${mark != null && html`<div class=${`pin ${side}`} style=${{ left: x(mark) }}>
      <i style=${{ background: markColor }}></i><span style=${{ color: markColor }}>${markLabel} ${fmt(mark)}</span></div>`}
  </div>`;
}

// キーの相性の言葉（合わせた後のキーどうしで）
function keyWord(t, mark = false) {
  const d = t.matched ? t.matched.key_distance_after : (t.explain && t.explain.key ? t.explain.key.distance : null);
  const w = d === 0 ? "同じキー" : d === 1 ? "隣どうし" : d === 2 ? "2つ隣" : "遠い";
  return mark ? `${d <= 1 ? "◎" : d === 2 ? "○" : "△"} ${w}` : w;
}

function Explain({ tracksById, plan, k, auto, styles, onUseAlt, onPreview, previewing }) {
  const t = plan.transitions[k];
  const a = plan.items[k], b = plan.items[k + 1];
  const ta = tracksById.get(a.track_id), tb = tracksById.get(b.track_id);
  const ex = t.explain || {};
  const WD = 760;
  const da = useDetail(a.track_id, auto), db = useDetail(b.track_id, auto);
  const xa = da ? (t.out / da.duration) * WD : 0, xb = db ? (t.in / db.duration) * WD : 0;
  const tempo = ex.tempo || {}, key = ex.key || {};
  const tempoMark = tempo.gap <= 0.03 ? "◎" : tempo.gap <= 0.06 ? "○" : "△";
  const keyMark = key.distance === 0 ? "◎ 同じキー" : key.distance === 1 ? "◎ 隣どうし" : key.distance === 2 ? "○ 2つ隣" : "△ 遠い";
  const st = STYLE[styles[k] || "auto"];
  const h = t.human;
  // 「サビ終わり→サビ頭」を左右に分ける（シートのメモなど「→」が無いときは区間名か「指定の位置」）
  const parts = t.kind.includes("→") ? t.kind.split("→")
    : [ex.out_section ? `${ex.out_section.label}終わり` : "指定の位置", ex.in_section ? `${ex.in_section.label}頭` : "指定の位置"];
  const nAlt = h && h.nearest ? 2 : 4;
  return html`<div class="explain">
    <div class="ex-head">
      <span class="badge" style=${{ background: auto ? "#ffe600" : "#7be0a4" }}>${auto ? "自動検知" : "シートの手入力を使用"}</span>
      <span class="ex-title">つなぎ目 ${k + 1}：<b style=${{ color: PICK[k] }}>${a.title}</b> の <b>${parts[0]}</b>（${fmt(t.out)}）
        → <b style=${{ color: PICK[k + 1] }}>${b.title}</b> の <b>${parts[1]}</b>（${fmt(t.in)}）${t.manual ? "　※シートのメモ／選び直した位置" : ""}</span>
    </div>
    <div class="ex-body">
      <div class="ex-left">
        <${SongStrip} track=${ta} item=${a} auto=${auto} width=${WD} mark=${t.out} markColor=${PICK[k]} markLabel="ここで出る" side="down" />
        <svg class="link" width=${WD} height="46" viewBox=${`0 0 ${WD} 46`}>
          <defs><linearGradient id=${`lg${k}`} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0" stop-color=${PICK[k]} /><stop offset="1" stop-color=${PICK[k + 1]} /></linearGradient></defs>
          <path d=${`M ${xa} 0 C ${xa} 28, ${xb} 18, ${xb} 46`} fill="none" stroke=${`url(#lg${k})`} stroke-width="4" stroke-dasharray=${t.manual ? "6 5" : null} />
          <text x=${(xa + xb) / 2 + 8} y="28" fill=${st.color} font-size="13" font-weight="700">${st.label}</text>
        </svg>
        <${SongStrip} track=${tb} item=${b} auto=${auto} width=${WD} mark=${t.in} markColor=${PICK[k + 1]} markLabel="ここから入る" side="up" />
      </div>
      <div class="ex-right">
        <div class="why"><span>テンポ</span><b>${a.bpm.toFixed(1)} → ${(b.orig_bpm ?? b.bpm).toFixed(1)} BPM</b>
          <em>${b.rate !== 1 ? html`<span class="ok">合わせて ${b.bpm.toFixed(1)}（×${b.rate.toFixed(3)}、音程そのまま）</span>`
            : (b.match_note || []).some((x) => x.startsWith("テンポ")) ? "△ 差が大きいので合わせない"
            : `${tempoMark} 差 ${(tempo.gap * 100).toFixed(1)}%`}</em></div>
        <div class="why"><span>キー</span><b>${a.camelot} → ${b.orig_camelot ?? b.camelot}</b>
          <em>${b.semitones ? html`<span class="ok">${b.semitones > 0 ? "+" : ""}${b.semitones}半音 移調して ${b.camelot}（${keyWord(t)}）</span>`
            : keyWord(t, true)}</em></div>
        ${ex.energy && ex.energy.out != null && html`<div class="why"><span>音の勢い</span>
          <b>${pct(ex.energy.out)} → ${pct(ex.energy.in)}</b><em>${ex.energy.fit >= 0.8 ? "◎" : ex.energy.fit >= 0.5 ? "○" : "△"} つながり ${pct(ex.energy.fit)}</em></div>`}
        ${auto && html`<div class="why"><span>区切りの確かさ</span><b>出 ${pct(ex.confidence && ex.confidence.out)} / 入 ${pct(ex.confidence && ex.confidence.in)}</b>
          </div>`}
        ${h && html`<div class="why human"><span>人のメモ</span><b>${fmt(h.out)} → ${fmt(h.in)}</b>
          <em>${h.nearest ? `自動の候補で一番近いもの: 差 ${Math.abs(h.nearest.diff_out).toFixed(1)}秒 / ${Math.abs(h.nearest.diff_in).toFixed(1)}秒` : ""}</em></div>`}
        <button class="listen-btn" onClick=${() => onPreview(k)}>${previewing ? "■ 止める" : "▶ このつなぎを試聴"}</button>
        <div class="alts-h">ほかの候補（押すと切り替え）</div>
        ${(t.alternatives || []).slice(0, nAlt).map((c, i) => html`<button key=${i} class="alt" onClick=${() => onUseAlt(k, c)}>
          <span>${c.kind}</span><b>${fmt(c.out)} → ${fmt(c.in)}</b><em>${c.score.toFixed(2)}</em></button>`)}
        ${h && h.nearest && html`<button class="alt human" onClick=${() => onUseAlt(k, { out: h.nearest.out, in: h.nearest.in, kind: h.nearest.kind })}>
          <span>人のメモに近い候補</span><b>${fmt(h.nearest.out)} → ${fmt(h.nearest.in)}</b><em></em></button>`}
      </div>
    </div>
  </div>`;
}

function ConnectScreen({ tracks, selected, styles, setStyles, minSec, setMinSec, auto, setAuto, match, setMatch, onBack, onStart, onError }) {
  const byId = useMemo(() => new Map(tracks.map((t) => [t.id, t])), [tracks]);
  const [base, setBase] = useState(null);
  const [busy, setBusy] = useState(false);
  const [open, setOpen] = useState(null);       // 繋ぎ方のポップアップ
  const [sel, setSel] = useState(0);            // 解説を出しているつなぎ目
  const [links, setLinks] = useState({});       // 候補の選び直し
  const [hover, setHover] = useState(null);
  const previewer = useRef(null);
  const hoverTimer = useRef(null);

  useEffect(() => setLinks({}), [selected.join(","), auto]);
  useEffect(() => {
    let alive = true;
    setBusy(true);
    const tm = setTimeout(() => {
      post("/api/plan", { songs: selected, min_sec: minSec, chorus_grace: 15, styles, use_sheet: !auto, links,
                          match_bpm: match.bpm, match_key: match.key })
        .then((p) => { alive && setBase(p); })
        .catch((e) => alive && onError(e.message))
        .finally(() => alive && setBusy(false));
    }, 200);
    return () => { alive = false; clearTimeout(tm); };
  }, [selected.join(","), minSec, auto, JSON.stringify(links), match.bpm, match.key]);
  // 音声は先に読み込んでおく（テンポ・キーを合わせた音声はサーバが作るので少し待つ）
  useEffect(() => {
    if (!base) return;
    audioContext();
    base.items.forEach((it) => loadBuffer(it.audio_url, it.audio_url).catch(() => {}));
  }, [base && base.items.map((it) => it.audio_url).join(",")]);

  const plan = useMemo(() => (base ? withStyles(base, styles) : null), [base, styles.join(",")]);
  const layout = useMemo(() => (plan ? new MixPlayer(plan).layout : null), [plan]);

  const stopPreview = () => {
    clearTimeout(hoverTimer.current);
    if (previewer.current) previewer.current.stop();
    previewer.current = null;
    setHover(null);
  };
  useEffect(() => stopPreview, []);
  const playPreview = async (k, st) => {
    if (!base) return;
    if (previewer.current) previewer.current.stop();
    const s2 = [...styles];
    if (st) s2[k] = st;
    const pl = new MixPlayer(withStyles(base, s2));
    previewer.current = pl;
    setHover({ k, style: s2[k] || "auto", loading: true });
    try {
      await audioContext().resume();
      await pl.load();
      if (previewer.current !== pl) return;
      setHover({ k, style: s2[k] || "auto" });
      pl.onend = () => previewer.current === pl && setHover(null);
      await pl.preview(k, 8, 10);
    } catch (e) { onError(e.message); }
  };
  const tryStyle = (k, st) => {
    clearTimeout(hoverTimer.current);
    hoverTimer.current = setTimeout(() => playPreview(k, st), 280);
  };
  const choose = (k, st) => {
    const s2 = [...styles];
    s2[k] = st;
    setStyles(s2);
    setOpen(null);
    stopPreview();
  };

  const S = 166, gap = 230, x0 = (W - (3 * S + 2 * gap)) / 2;
  const xOf = (i) => x0 + i * (S + gap);
  const Y = 88;

  return html`<div class="screen" onClick=${() => { setOpen(null); }}>
    <div class="h1" style=${{ left: 55, top: 22, fontSize: 38 }}>繋ぎ方を選んでね</div>
    <div class="modes" onClick=${(e) => e.stopPropagation()}>
      <button class=${auto ? "on" : ""} onClick=${() => setAuto(true)}>自動検知</button>
      <button class=${!auto ? "on" : ""} onClick=${() => setAuto(false)}>シートの手入力</button>
    </div>

    ${selected.map((id, i) => {
      const t = byId.get(id);
      const it = plan && plan.items[i];
      return html`<div key=${id} class="cj" style=${{ left: xOf(i), top: Y, width: S }}>
        <${Jacket} track=${t} size=${S} style=${{ boxShadow: `0 0 0 4px ${PICK[i]}` }} />
        <div class="t">${t ? t.title : id}</div>
        <div class="r">${it ? `${fmt(it.start)} → ${fmt(it.end)}（${Math.round(it.end - it.start)}秒）` : busy ? "計算中…" : ""}</div>
        ${it && html`<div class="r fx">${Math.round(it.bpm)} BPM${it.rate !== 1 ? html` <b>×${it.rate.toFixed(3)}</b>` : ""}
          ・${it.camelot}${it.semitones ? html` <b>(${it.semitones > 0 ? "+" : ""}${it.semitones})</b>` : ""}</div>`}
      </div>`;
    })}
    ${selected.slice(1).map((_, k) => {
      const st = styles[k] || "auto";
      const cx = xOf(k) + S + gap / 2;
      return html`<div key=${k} class=${`arrow${sel === k ? " sel" : ""}`} style=${{ left: cx - 90, width: 180, top: Y + 20 }}>
        <button title="繋ぎ方を選ぶ" onClick=${(e) => { e.stopPropagation(); setSel(k); setOpen(open === k ? null : k); }}>
          <${Tri} size=${84} color=${STYLE[st].color} /></button>
        <div class="lab" style=${{ color: STYLE[st].color }}>${STYLE[st].label}</div>
        <button class="seebtn" onClick=${(e) => { e.stopPropagation(); setSel(k); }}>${sel === k ? "▼ 解説中" : "つなぎ目を見る"}</button>
      </div>`;
    })}

    ${open != null && (() => {
      const st = styles[open] || "auto";
      const cx = xOf(open) + S + gap / 2;
      const hv = hover && hover.k === open ? hover : null;
      return html`<div class="pop" style=${{ left: Math.max(20, Math.min(W - 355, cx - 168)), top: Y + 160, borderColor: STYLE[st].color }}
        onClick=${(e) => e.stopPropagation()} onMouseLeave=${stopPreview}>
        <h3>繋ぎ方を選択してね</h3>
        ${STYLE_ORDER.map((s) => html`<button key=${s} class=${`opt${s === st ? " on" : ""}`}
            onMouseEnter=${() => tryStyle(open, s)} onClick=${() => choose(open, s)}>
            <span class="dot" style=${{ background: STYLE[s].color }}></span>・${STYLE[s].label}</button>`)}
        <div class="desc">${hv ? STYLE[hv.style].desc : "カーソルを合わせると、そのつなぎ方で試聴できます"}</div>
        <div class="listen">${hv ? (hv.loading ? "♪ 読み込み中…" : `♪ 試聴中（${STYLE[hv.style].label}）`) : ""}</div>
      </div>`;
    })()}

    ${plan && html`<div class="abs" style=${{ left: 40, top: 360 }} onClick=${(e) => e.stopPropagation()}>
      <${Explain} tracksById=${byId} plan=${plan} k=${sel} auto=${auto} styles=${styles}
        previewing=${hover && hover.k === sel && open == null}
        onPreview=${(k) => (hover && hover.k === k ? stopPreview() : playPreview(k))}
        onUseAlt=${(k, c) => setLinks({ ...links, [k]: { out: c.out, in: c.in, kind: c.kind } })} />
    </div>`}

    <div class="ctl" style=${{ top: 776, left: 250 }} onClick=${(e) => e.stopPropagation()}>
      <span>1曲を最低 <b>${minSec}</b> 秒流す <input type="range" min="45" max="180" step="5" value=${minSec}
        onInput=${(e) => setMinSec(+e.target.value)} /></span>
      <label><input type="checkbox" checked=${match.bpm} onChange=${(e) => setMatch({ ...match, bpm: e.target.checked })} />BPMを合わせる</label>
      <label><input type="checkbox" checked=${match.key} onChange=${(e) => setMatch({ ...match, key: e.target.checked })} />キーを合わせる</label>
      <span>全体 <b>${layout ? fmt(layout.total) : "-:--"}</b></span>
      ${Object.keys(links).length > 0 && html`<button class="mini" onClick=${() => setLinks({})}>候補を元に戻す</button>`}
    </div>
    <button class="back" style=${{ left: 40, top: 772 }} onClick=${onBack}>◀ 曲を選び直す</button>
    <button class="next" style=${{ left: 1060, top: 760 }} disabled=${!plan || busy}
      onClick=${(e) => { e.stopPropagation(); stopPreview(); onStart(plan); }}>スタート <${Tri} size=${48} color="#ff3df2" stroke=${4} /></button>
  </div>`;
}

// ---------------------------------------------------------------- 画面5: 再生中
function RingViz({ player, color }) {
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
  // つなぎ目の前後は、どこでつないでいるかを表示する
  const nearK = layout.transitionsMix.findIndex((t) => now >= t - 10 && now <= t + 5);
  const nearT = nearK >= 0 ? plan.transitions[nearK] : null;

  const toggle = () => (player.playing ? player.stop() : player.play(ended ? 0 : player.now()));
  const skip = () => { if (nextT != null) player.play(Math.max(player.now(), nextT - 10)); };
  const seekSong = (i) => player.play(Math.max(0, layout.segments[i].mixAnchor - (i ? 10 : 0)));
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
          <div class="s">${fmt(x.start)} → ${fmt(x.end)}</div>
          ${i < plan.transitions.length && html`<div class="s">↓ ${plan.transitions[i].kind}・${STYLE[plan.transitions[i].style || "auto"].label}</div>`}</div>
      </div>`)}
    </div>

    <${RingViz} player=${player} color=${color} />
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

    ${nearT && !ended && html`<div class="joinbar" style=${{ borderColor: STYLE[nearT.style || "auto"].color }}>
      <span class="badge" style=${{ background: "#ffe600" }}>${nearT.explain && nearT.explain.source === "auto" ? "自動検知" : "シート"}</span>
      つなぎ目：<b>${plan.items[nearK].title}</b> の ${nearT.kind.includes("→") ? nearT.kind.split("→")[0] : ""}（${fmt(nearT.out)}）→
      <b>${plan.items[nearK + 1].title}</b> の ${nearT.kind.includes("→") ? nearT.kind.split("→")[1] : ""}（${fmt(nearT.in)}）
      ${now < layout.transitionsMix[nearK] ? html`<em>あと ${Math.ceil(layout.transitionsMix[nearK] - now)} 秒</em>` : html`<em>つないだ！</em>`}
    </div>`}

    ${tr && !ended && html`<button class="skip" onClick=${skip} title="次のつなぎ目の手前へ（→キー）">
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
  const [picks, setPicks] = useState([]);       // 選んだ曲（track）
  const [styles, setStyles] = useState(["auto", "auto"]);
  const [minSec, setMinSec] = useState(90);
  const [auto, setAuto] = useState(true);       // 発表用: 既定は「自動検知」
  const [match, setMatch] = useState({ bpm: true, key: true });   // BPM・キーを合わせる
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

  const byTitle = useMemo(() => new Map(tracks.map((t) => [t.title, t])), [tracks]);
  const step = picks.length;
  // 各ステップの候補（シートのルート）。曲が取り込まれていなければ「音源なし」で表示
  const options = useMemo(() => {
    const titles = [];
    const add = (x) => { if (x && !titles.includes(x)) titles.push(x); };
    for (const r of routes) {
      if (step === 0) add(r.root);
      else if (step === 1 && r.root === picks[0].title) add(r.middle);
      else if (step === 2 && r.root === picks[0].title && r.middle === picks[1].title) add(r.leaf);
    }
    return titles.map((x) => byTitle.get(x) || { id: `missing:${x}`, title: x, analyzed: false, file_exists: false });
  }, [routes, picks, byTitle]);

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

  let body;
  if (screen === "boot") body = html`<${Loading} sub="曲を読み込んでいます" />`;
  else if (screen === "loading") body = html`<${Loading} sub="音源を準備しています" progress=${load ? load.progress : 0} />`;
  else if (screen === "select") body = html`<${SelectScreen} key=${step} step=${step} options=${options} picks=${picks} byTitle=${byTitle}
    routesOk=${routes.length > 0}
    onPick=${(t) => { const p = [...picks, t]; setPicks(p); if (p.length === 3) setScreen("connect"); }}
    onBack=${() => setPicks(picks.slice(0, -1))} />`;
  else if (screen === "connect") body = html`<${ConnectScreen} tracks=${tracks} selected=${picks.map((t) => t.id)}
    styles=${styles} setStyles=${setStyles} minSec=${minSec} setMinSec=${setMinSec} auto=${auto} setAuto=${setAuto}
    match=${match} setMatch=${setMatch}
    onError=${setErr} onBack=${() => { setPicks(picks.slice(0, 2)); setScreen("select"); }} onStart=${start} />`;
  else if (screen === "play" && player) body = html`<${PlayScreen} tracks=${tracks} plan=${plan} player=${player}
    onBack=${() => { player.stop(); setScreen("connect"); }} />`;

  return html`<div class="viewport"><div class="stage" style=${{ transform: `translate(-50%, -50%) scale(${scale})` }}>
    ${body}
    ${err && html`<div class="err">${err}</div>`}
  </div></div>`;
}

ReactDOM.createRoot(document.getElementById("root")).render(html`<${App} />`);
