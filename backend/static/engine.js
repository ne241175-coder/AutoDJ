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

export class MixPlayer {
  constructor(plan) {
    this.plan = plan;
    this.ctx = null;
    this.buffers = new Map();
    this.nodes = [];
    this.playing = false;
    this.t0 = 0;        // 再生開始したミックス時刻
    this.ctxStart = 0;  // そのときの ctx.currentTime
    this.layout = computeLayout(plan);
  }

  get duration() { return this.layout.total; }

  async load(onProgress) {
    if (!this.ctx) this.ctx = new (window.AudioContext || window.webkitAudioContext)();
    const ids = [...new Set(this.plan.items.map((it) => it.track_id))];
    let done = 0;
    for (const id of ids) {
      if (!this.buffers.has(id)) {
        const it = this.plan.items.find((x) => x.track_id === id);
        const res = await fetch(it.audio_url);
        if (!res.ok) throw new Error(`音声を取得できません: ${it.title}`);
        const buf = await this.ctx.decodeAudioData(await res.arrayBuffer());
        this.buffers.set(id, buf);
      }
      done += 1;
      onProgress && onProgress(done / ids.length);
    }
  }

  now() {
    if (!this.playing) return this.t0;
    return this.t0 + (this.ctx.currentTime - this.ctxStart);
  }

  async play(fromMix = 0) {
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
      const buf = this.buffers.get(seg.track_id);
      const src = ctx.createBufferSource();
      src.buffer = buf;
      const hp = ctx.createBiquadFilter();
      hp.type = "highpass";
      hp.Q.value = 0.7;
      const gain = ctx.createGain();
      src.connect(hp).connect(gain).connect(ctx.destination);

      const skip = Math.max(0, T0 - seg.mixStart);            // 途中から再生する場合
      const offset = seg.posStart + skip;
      const dur = seg.posEnd - offset;
      if (dur <= 0.01) continue;
      const when = abs(Math.max(seg.mixStart, T0));

      // 音量（等パワーのフェード）
      const from = Math.max(seg.mixStart, T0);
      gain.gain.setValueAtTime(seg.gainAt(from), when);
      for (const f of [seg.fadeIn, seg.fadeOut]) {
        if (!f) continue;
        const a = Math.max(f.mixFrom, T0), b = f.mixTo;
        if (b <= a) continue;
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
  }

  stop() {
    if (this.playing) this.t0 = this.now();
    for (const n of this.nodes) { try { n.stop(); } catch (e) { /* 既に停止 */ } }
    this.nodes = [];
    this.playing = false;
  }

  // ミックス時刻 → (何曲目, その曲の何秒)
  locate(mix) {
    const segs = this.layout.segments;
    for (let i = segs.length - 1; i >= 0; i--) {
      if (mix >= segs[i].mixAnchor || i === 0) return { index: i, pos: segs[i].toPos(mix) };
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
    const Lin = prev ? (prev.overlap_beats * 60) / it.bpm : 0;
    const Lout = next ? (next.overlap_beats * 60) / it.bpm : 0;
    const posStart = Math.max(0, it.start - Lin);
    const posEnd = it.end;
    const fadeIn = prev ? { mixFrom: toMix(it.start - Lin), mixTo: toMix(it.start) } : null;
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
    if (fadeIn) hpEvents.push([(fadeIn.mixFrom + fadeIn.mixTo) / 2, HP_OFF]);
    if (fadeOut) hpEvents.push([(fadeOut.mixFrom + fadeOut.mixTo) / 2, HP_ON]);
    const hpAt = (m) => {
      if (fadeIn && m < (fadeIn.mixFrom + fadeIn.mixTo) / 2) return HP_ON;
      if (fadeOut && m >= (fadeOut.mixFrom + fadeOut.mixTo) / 2) return HP_ON;
      return HP_OFF;
    };
    segs.push({
      index: i, track_id: it.track_id, title: it.title, posStart, posEnd,
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
