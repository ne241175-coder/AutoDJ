// AutoDJ 仮UI（ビルド不要: React UMD + htm）。API は backend/app.py。
import { MixPlayer } from "./engine.js";

const { useState, useEffect, useRef, useMemo, useCallback } = React;
const html = htm.bind(React.createElement);

const ROLE_COLOR = {
  chorus: "var(--c-chorus)", chorus_drop: "var(--c-drop)", verse: "var(--c-verse)",
  prechorus: "var(--c-pre)", bridge: "var(--c-bridge)", intro: "var(--c-edge)",
  outro: "var(--c-edge)", interlude: "var(--c-inter)", other: "var(--c-other)",
};
const fmt = (t) => {
  if (t == null || isNaN(t)) return "-";
  const m = Math.floor(t / 60), s = t - m * 60;
  return `${m}:${s.toFixed(1).padStart(4, "0")}`;
};
const api = async (url, opts) => {
  const r = await fetch(url, opts);
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || `${r.status}`);
  return j;
};

function SectionBar({ detail, start, end, height = 18 }) {
  if (!detail) return html`<div class="secbar loading" style=${{ height }}></div>`;
  const dur = detail.duration || 1;
  return html`<div class="secbar" style=${{ height }}>
    ${detail.sections.map((s, i) => html`<div key=${i} class="sec" title=${`${s.label} ${fmt(s.start)}〜${fmt(s.end)}`}
      style=${{ left: `${(s.start / dur) * 100}%`, width: `${(((s.end ?? dur) - s.start) / dur) * 100}%`,
                background: ROLE_COLOR[s.role] || ROLE_COLOR.other }}>
      <span>${s.label}</span></div>`)}
    ${start != null && html`<div class="dim" style=${{ left: 0, width: `${(start / dur) * 100}%` }}></div>`}
    ${end != null && html`<div class="dim" style=${{ left: `${(end / dur) * 100}%`, right: 0 }}></div>`}
    ${start != null && html`<div class="mark in" style=${{ left: `${(start / dur) * 100}%` }}></div>`}
    ${end != null && html`<div class="mark out" style=${{ left: `${(end / dur) * 100}%` }}></div>`}
  </div>`;
}

function Library({ tracks, onAdd, filter, setFilter }) {
  const shown = tracks.filter((t) => !filter || (t.title || "").toLowerCase().includes(filter.toLowerCase()));
  return html`<section class="panel lib">
    <h2>ライブラリ <small>${tracks.length}曲</small></h2>
    <input class="search" placeholder="曲名で絞り込み" value=${filter} onInput=${(e) => setFilter(e.target.value)} />
    <ul class="tracks">
      ${shown.map((t) => html`<li key=${t.id} class=${!t.analyzed || !t.file_exists ? "off" : ""}>
        <div class="tmain">
          <div class="ttitle">${t.title}</div>
          <div class="tmeta">
            ${t.bpm ? html`<span>${Math.round(t.bpm)} BPM</span>` : null}
            ${t.camelot ? html`<span class="cam">${t.camelot}</span>` : null}
            ${t.has_sheet_structure ? html`<span class="badge sheet">シート構成</span>` : null}
            ${!t.analyzed ? html`<span class="badge warn">未解析</span>` : null}
            ${!t.file_exists ? html`<span class="badge warn">音源なし</span>` : null}
          </div>
        </div>
        <button class="add" disabled=${!t.analyzed || !t.file_exists} onClick=${() => onAdd(t.id)} title="セットリストに追加">＋</button>
      </li>`)}
    </ul>
  </section>`;
}

function Routes({ routes, onUse }) {
  const [open, setOpen] = useState(false);
  if (!routes.length) return null;
  const ok = routes.filter((r) => r.available);
  return html`<section class="panel routes">
    <h2 onClick=${() => setOpen(!open)} class="clickable">シートのルート <small>${ok.length}/${routes.length} 本が取り込み済み ${open ? "▲" : "▼"}</small></h2>
    ${open && html`<ul>
      ${routes.map((r, i) => html`<li key=${i} class=${r.available ? "" : "off"}>
        <span>${r.chain.join(" → ")}</span>
        ${r.memo ? html`<small class="memo">${r.memo}</small>` : null}
        ${r.available
          ? html`<button onClick=${() => onUse(r.chain)}>このルートで組む</button>`
          : html`<small class="warn">未取り込み: ${r.missing.join(", ")}</small>`}
      </li>`)}
    </ul>`}
  </section>`;
}

function Setlist({ ids, byId, setIds, minSec, setMinSec, onPlan, busy }) {
  const move = (i, d) => {
    const a = [...ids]; const j = i + d;
    if (j < 0 || j >= a.length) return;
    [a[i], a[j]] = [a[j], a[i]]; setIds(a);
  };
  return html`<section class="panel set">
    <h2>セットリスト <small>${ids.length}曲</small></h2>
    ${ids.length === 0 && html`<p class="hint">左のライブラリの「＋」か、シートのルートから曲を追加してください。</p>`}
    <ol>
      ${ids.map((id, i) => html`<li key=${i}>
        <span class="num">${i + 1}</span><span class="stitle">${byId[id]?.title ?? id}</span>
        <span class="smeta">${byId[id]?.bpm ? Math.round(byId[id].bpm) + " BPM" : ""} ${byId[id]?.camelot ?? ""}</span>
        <span class="ctrl">
          <button onClick=${() => move(i, -1)} title="上へ">↑</button>
          <button onClick=${() => move(i, 1)} title="下へ">↓</button>
          <button onClick=${() => setIds(ids.filter((_, k) => k !== i))} title="外す">×</button>
        </span>
      </li>`)}
    </ol>
    <div class="planrow">
      <label>1曲の最低再生 <input type="number" min="0" step="5" value=${minSec}
        onInput=${(e) => setMinSec(Number(e.target.value))} /> 秒</label>
      <button class="primary" disabled=${ids.length < 2 || busy} onClick=${onPlan}>${busy ? "計画中…" : "つなぎを計画"}</button>
    </div>
  </section>`;
}

function PlanView({ plan, details, onPreview, current }) {
  if (!plan) return null;
  return html`<section class="panel plan">
    <h2>再生計画 <small>合計 ${fmt(plan.items.reduce((a, it) => a + it.end - it.start, 0))}</small></h2>
    ${plan.warnings.map((w, i) => html`<p key=${i} class="warnline">※ ${w}</p>`)}
    ${plan.items.map((it, i) => html`<div key=${i}>
      <div class=${"item" + (current === i ? " now" : "")}>
        <div class="ihead">
          <span class="num">${i + 1}</span>
          <span class="ititle">${it.title}</span>
          <span class="irange">${fmt(it.start)} 〜 ${fmt(it.end)}（${Math.round(it.end - it.start)}秒）</span>
          <span class="imeta">${Math.round(it.bpm)} BPM ・ ${it.camelot} ・ 構造: ${it.structure_source === "sheet" ? "シート" : "自動"}</span>
        </div>
        <${SectionBar} detail=${details[it.track_id]} start=${it.start} end=${it.end} />
        <div class="flow">${it.sections_played.join(" → ")}</div>
      </div>
      ${i < plan.transitions.length && html`<div class="trans">
        <div class="tline"></div>
        <div class="tbody">
          <span class="tkind">${plan.transitions[i].manual ? "📝 " : ""}${plan.transitions[i].kind}</span>
          <span>${fmt(plan.transitions[i].out)} → ${fmt(plan.transitions[i].in)}</span>
          <span class="treason">${plan.transitions[i].reasons.join(" / ")} ・ スコア ${plan.transitions[i].score}
            ・ ${plan.transitions[i].overlap_beats === 4 ? "1小節で混ぜる" : "1拍で切り替え"}</span>
          <button onClick=${() => onPreview(i)}>▶ つなぎ目を試聴</button>
        </div>
      </div>`}
    </div>`)}
  </section>`;
}

function Player({ player, onState }) {
  const [t, setT] = useState(0);
  const [playing, setPlaying] = useState(false);
  const raf = useRef(0);
  useEffect(() => {
    const tick = () => {
      if (player) {
        const now = player.now();
        setT(now);
        setPlaying(player.playing);
        if (player.playing && now >= player.duration) player.stop();
        onState && onState(player.playing ? player.locate(now).index : null);
      }
      raf.current = requestAnimationFrame(tick);
    };
    raf.current = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf.current);
  }, [player]);
  if (!player) return null;
  const seek = (e) => {
    const r = e.currentTarget.getBoundingClientRect();
    player.play(((e.clientX - r.left) / r.width) * player.duration);
  };
  const loc = player.locate(t);
  const marks = player.layout.transitionsMix;
  return html`<footer class="player">
    <button class="big" onClick=${() => (playing ? player.stop() : player.play(t >= player.duration - 0.1 ? 0 : t))}>
      ${playing ? "■" : "▶"}</button>
    <div class="pinfo">
      <div class="pnow">${player.plan.items[loc.index]?.title} <small>（曲中 ${fmt(loc.pos)}）</small></div>
      <div class="ptrack" onClick=${seek}>
        <div class="pfill" style=${{ width: `${(t / player.duration) * 100}%` }}></div>
        ${marks.map((m, i) => html`<div key=${i} class="pmark" style=${{ left: `${(m / player.duration) * 100}%` }}></div>`)}
      </div>
    </div>
    <div class="ptime">${fmt(t)} / ${fmt(player.duration)}</div>
  </footer>`;
}

function App() {
  const [tracks, setTracks] = useState([]);
  const [routes, setRoutes] = useState([]);
  const [ids, setIds] = useState([]);
  const [minSec, setMinSec] = useState(90);
  const [plan, setPlan] = useState(null);
  const [details, setDetails] = useState({});
  const [player, setPlayer] = useState(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");
  const [filter, setFilter] = useState("");
  const [current, setCurrent] = useState(null);

  useEffect(() => {
    api("/api/tracks").then(setTracks).catch((e) => setMsg(e.message));
    api("/api/routes").then(setRoutes).catch(() => {});
  }, []);
  const byId = useMemo(() => Object.fromEntries(tracks.map((t) => [t.id, t])), [tracks]);
  const byTitle = useMemo(() => Object.fromEntries(tracks.map((t) => [t.title, t.id])), [tracks]);

  const makePlan = useCallback(async () => {
    setBusy(true); setMsg("");
    try {
      if (player) player.stop();
      const p = await api("/api/plan", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ songs: ids, min_sec: minSec }) });
      setPlan(p);
      const need = [...new Set(p.items.map((x) => x.track_id))].filter((id) => !details[id]);
      const got = await Promise.all(need.map((id) => api(`/api/tracks/${id}`)));
      setDetails((d) => ({ ...d, ...Object.fromEntries(got.map((g) => [g.id, g])) }));
      const pl = new MixPlayer(p);
      setPlayer(pl);
      setMsg("音声を読み込み中…");
      await pl.load((r) => setMsg(`音声を読み込み中… ${Math.round(r * 100)}%`));
      setMsg("");
    } catch (e) { setMsg(e.message); }
    setBusy(false);
  }, [ids, minSec, player, details]);

  const preview = (k) => player && player.play(Math.max(0, player.mixTimeOfTransition(k) - 10));

  return html`<div class="app">
    <header><h1>AutoDJ</h1><span class="sub">つなぎ計画（mp3 は切らずに秒数指定で再生）</span>
      ${msg && html`<span class="msg">${msg}</span>`}</header>
    <main>
      <div class="left">
        <${Routes} routes=${routes} onUse=${(chain) => setIds(chain.map((c) => byTitle[c]))} />
        <${Library} tracks=${tracks} filter=${filter} setFilter=${setFilter} onAdd=${(id) => setIds([...ids, id])} />
      </div>
      <div class="right">
        <${Setlist} ids=${ids} byId=${byId} setIds=${setIds} minSec=${minSec} setMinSec=${setMinSec}
          onPlan=${makePlan} busy=${busy} />
        <${PlanView} plan=${plan} details=${details} onPreview=${preview} current=${current} />
      </div>
    </main>
    <${Player} player=${player} onState=${setCurrent} />
  </div>`;
}

ReactDOM.createRoot(document.getElementById("root")).render(html`<${App} />`);
