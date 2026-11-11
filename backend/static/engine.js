// AutoDJ 再生エンジン（Web Audio API）
// mp3 は切らない。各曲を「この秒から、この秒まで」と指定して1本のソースで鳴らし、
// つなぎ目は音量のフェードと低音カットの切り替えだけで行う（サーバの render.py と同じ考え方）。
//
// 時間の対応:
//   曲0 の start が ミックス 0秒。
//   曲i (i>=1) の in 秒が、曲i-1 の out 秒と同じミックス時刻に来るように並べる。
//   曲i は in の L 秒手前（L = 重ねる拍数 × 曲i の1拍の長さ）から鳴り始めてフェードイン。
//   曲i-1 は out の L' 秒手前からフェードアウトして out で止まる。どちらも out / in は小節頭。

const HP_ON = 180;   // 低音カット時のハイパス周波数(Hz)
const HP_OFF = 10;   // 実質スルー
const CUT = 0.012;   // カットイン: プチッと鳴らない程度の極短いフェード(秒)

// AudioContext と読み込んだ音声は、計画を作り直しても使い回す
let sharedCtx = null;
const BUFFERS = new Map();
export function audioContext() {
  if (!sharedCtx) {
    sharedCtx = new (window.AudioContext || window.webkitAudioContext)();
    sharedCtx.master = sharedCtx.createGain();
    sharedCtx.analyser = sharedCtx.createAnalyser();
    sharedCtx.analyser.fftSize = 2048;
    sharedCtx.analyser.smoothingTimeConstant = 0.75;
    sharedCtx.master.connect(sharedCtx.analyser).connect(sharedCtx.destination);
  }
  return sharedCtx;
}
export async function loadBuffer(id, url) {
  if (BUFFERS.has(id)) return BUFFERS.get(id);
  const p = (async () => {
    const res = await fetch(url);
    if (!res.ok) throw new Error(`音声を取得できません (${res.status})`);
    return await audioContext().decodeAudioData(await res.arrayBuffer());
  })();
  BUFFERS.set(id, p);
  p.catch(() => BUFFERS.delete(id));
  return p;
}

// BPM・キーを合わせた計画は、加工済みの音声（audio_url）とその秒数（pb_*）で鳴らす
export function toPlayback(plan) {
  const items = plan.items.map((it) => ({ ...it, start: it.pb_start ?? it.start, end: it.pb_end ?? it.end }));
  const transitions = plan.transitions.map((t) => ({ ...t, out: t.pb_out ?? t.out, in: t.pb_in ?? t.in }));
  return { ...plan, items, transitions };
}

export class MixPlayer {
  constructor(plan) {
    plan = toPlayback(plan);
    this.plan = plan;
    this.ctx = null;
    this.buffers = new Map();
    this.timer = null;
    this.nodes = [];
    this.playing = false;
    this.t0 = 0;        // 再生開始したミックス時刻
    this.ctxStart = 0;  // そのときの ctx.currentTime
    this.layout = computeLayout(plan);
  }

  get duration() { return this.layout.total; }

  get analyser() { return audioContext().analyser; }

  async load(onProgress) {
    this.ctx = audioContext();
    // 同じ曲でも、テンポ・キーを変えた音声は別物として読む（audio_url ごと）
    const urls = [...new Set(this.plan.items.map((it) => it.audio_url))];
    let done = 0;
    await Promise.all(urls.map(async (url) => {
      if (!this.buffers.has(url)) {
        const it = this.plan.items.find((x) => x.audio_url === url);
        try {
          this.buffers.set(url, await loadBuffer(url, url));
        } catch (e) {
          throw new Error(`音声を取得できません: ${it.title}`);
        }
      }
      done += 1;
      onProgress && onProgress(done / urls.length);
    }));
  }

  now() {
    if (!this.playing) return this.t0;
    return this.t0 + (this.ctx.currentTime - this.ctxStart);
  }

  // つなぎ目 k の前後だけ試聴する
  async preview(k, before = 8, after = 8) {
    const t = this.layout.transitionsMix[k];
    await this.play(Math.max(0, t - before), t + after);
  }

  async play(fromMix = 0, untilMix = null) {
    await this.load();
    this.stop();
    if (this.ctx.state === "suspended") await this.ctx.resume();
    const ctx = this.ctx;
    const startAt = ctx.currentTime + 0.12;
    this.t0 = Math.max(0, Math.min(fromMix, this.layout.total - 0.05));
    this.ctxStart = startAt;
    const T0 = this.t0;
    const abs = (mix) => startAt + (mix - T0);

    for (const seg of this.layout.segments) {
      if (seg.mixEnd <= T0) continue;
      const buf = this.buffers.get(seg.audio_url);
      const src = ctx.createBufferSource();
      src.buffer = buf;
      const hp = ctx.createBiquadFilter();
      hp.type = "highpass";
      hp.Q.value = 0.7;
      const gain = ctx.createGain();
      src.connect(hp).connect(gain).connect(ctx.master);

      const skip = Math.max(0, T0 - seg.mixStart);            // 途中から再生する場合
      const offset = seg.posStart + skip;
      const dur = seg.posEnd - offset;
      if (dur <= 0.01) continue;
      const when = abs(Math.max(seg.mixStart, T0));

      // 音量（等パワーのフェード）。フェードの曲線と同じ時刻に別の予約を入れると例外になるので、
      // 曲線が鳴り始めから始まるときは最初の値の予約をしない（曲線の最初の値がそのまま使われる）
      const from = Math.max(seg.mixStart, T0);
      const fades = [seg.fadeIn, seg.fadeOut].filter((f) => f && f.mixTo - Math.max(f.mixFrom, T0) > 0.002);
      if (!fades.some((f) => Math.max(f.mixFrom, T0) <= from + 1e-4)) gain.gain.setValueAtTime(seg.gainAt(from), when);
      for (const f of fades) {
        const a = Math.max(f.mixFrom, T0), b = f.mixTo;
        const n = Math.max(2, Math.ceil((b - a) * 60));
        const curve = new Float32Array(n);
        for (let k = 0; k < n; k++) curve[k] = seg.gainAt(a + ((b - a) * k) / (n - 1));
        gain.gain.setValueCurveAtTime(curve, abs(a), b - a);
      }
      // 低音カット: 入る曲は重なりの前半、出る曲は重なりの後半
      hp.frequency.setValueAtTime(seg.hpAt(from), when);
      for (const [t, v] of seg.hpEvents) if (t > T0) hp.frequency.setValueAtTime(v, abs(t));

      src.start(when, offset, dur);
      this.nodes.push(src);
    }
    this.playing = true;
    const until = untilMix == null ? this.layout.total : Math.min(untilMix, this.layout.total);
    this.timer = setTimeout(() => { this.stop(); this.onend && this.onend(); },
                            Math.max(0, (until - T0) * 1000 + 150));
  }

  stop() {
    clearTimeout(this.timer);
    if (this.playing) this.t0 = this.now();
    for (const n of this.nodes) { try { n.stop(); } catch (e) { /* 既に停止 */ } }
    this.nodes = [];
    this.playing = false;
  }

  // ミックス時刻 → (何曲目, その曲の何秒)
  locate(mix) {
    const segs = this.layout.segments;
    for (let i = segs.length - 1; i >= 0; i--) {
      // pos は元の曲の秒数（テンポを変えた音声なら倍率を戻す）
      if (mix >= segs[i].mixAnchor || i === 0) return { index: i, pos: segs[i].toPos(mix) * segs[i].rate };
    }
    return { index: 0, pos: 0 };
  }

  mixTimeOfTransition(k) { return this.layout.transitionsMix[k]; }
}

export function computeLayout(plan) {
  const { items, transitions } = plan;
  const segs = [];
  const transitionsMix = [];
  let base = 0;              // 曲i の anchor が来るミックス時刻
  for (let i = 0; i < items.length; i++) {
    const it = items[i];
    const anchor = it.start;  // 曲0: start / 曲i: in（= start）
    const b = base;           // この曲用に値を固定（base はループで書き換わる）
    const toMix = (p) => b + (p - anchor);
    const prev = i > 0 ? transitions[i - 1] : null;
    const next = i < transitions.length ? transitions[i] : null;
    const len = (t) => (!t ? 0 : t.overlap_beats > 0 ? (t.overlap_beats * 60) / it.bpm : CUT);
    const Lin = len(prev);
    const Lout = len(next);
    const posStart = Math.max(0, it.start - Lin);
    const posEnd = it.end;
    // 曲の頭（in が 0 秒付近）から入るときは、in より前に音が無いので、鳴り始め（0 秒）からフェードインする。
    // フェードの開始を鳴り始めより前にすると、音量の予約（setValueCurveAtTime）が重なって再生が止まる。
    const fadeIn = prev && it.start - posStart > 0.005 ? { mixFrom: toMix(posStart), mixTo: toMix(it.start) } : null;
    const fadeOut = next ? { mixFrom: toMix(it.end - Lout), mixTo: toMix(it.end) } : null;
    const gainAt = (m) => {
      let g = 1;
      if (fadeIn && m < fadeIn.mixTo) {
        const x = Math.min(1, Math.max(0, (m - fadeIn.mixFrom) / (fadeIn.mixTo - fadeIn.mixFrom)));
        g *= Math.sin((x * Math.PI) / 2);
      }
      if (fadeOut && m > fadeOut.mixFrom) {
        const x = Math.min(1, Math.max(0, (m - fadeOut.mixFrom) / (fadeOut.mixTo - fadeOut.mixFrom)));
        g *= Math.cos((x * Math.PI) / 2);
      }
      return g;
    };
    const hpEvents = [];
    // 低音の入れ替え（カットインのときはしない）
    const swapIn = fadeIn && prev.overlap_beats > 0;
    const swapOut = fadeOut && next.overlap_beats > 0;
    if (swapIn) hpEvents.push([(fadeIn.mixFrom + fadeIn.mixTo) / 2, HP_OFF]);
    if (swapOut) hpEvents.push([(fadeOut.mixFrom + fadeOut.mixTo) / 2, HP_ON]);
    const hpAt = (m) => {
      if (swapIn && m < (fadeIn.mixFrom + fadeIn.mixTo) / 2) return HP_ON;
      if (swapOut && m >= (fadeOut.mixFrom + fadeOut.mixTo) / 2) return HP_ON;
      return HP_OFF;
    };
    segs.push({
      index: i, track_id: it.track_id, audio_url: it.audio_url, rate: it.rate || 1, title: it.title, posStart, posEnd,
      mixStart: toMix(posStart), mixEnd: toMix(posEnd), mixAnchor: toMix(anchor),
      fadeIn, fadeOut, gainAt, hpAt, hpEvents, toPos: (m) => anchor + (m - b),
    });
    if (next) {
      transitionsMix.push(toMix(it.end));
      base = toMix(it.end);
    }
  }
  const last = segs[segs.length - 1];
  return { segments: segs, total: last ? last.mixEnd : 0, transitionsMix };
}
