// 按方案播放：在 <video> 上按播放列表跳过被剪掉的部分，不需要渲染。
// 跳转由 requestAnimationFrame 驱动（约 16ms 一次，timeupdate 只有约 250ms，会漏掉剪口），
// timeupdate 只作为后台标签页里 rAF 暂停时的兜底。

import { buildPlaylist, locatePlaylist, mapSourceTime, fromOutputTime } from "./model.js";

const EPS = 0.002;

export class PlanPlayer extends EventTarget {
  constructor(video) {
    super();
    this.video = video;
    this.mode = "plan"; // plan = 按方案播放（跳过剪掉的）；orig = 原片
    this.rate = 1;
    this.output = null;
    this.sources = {};
    this.playlist = [];
    this.cur = 0;
    this.sourceId = null;
    this.audition = null;
    this._raf = null;
    this._lastProps = "";
    this._pendingSeek = null;

    video.preload = "metadata";
    video.addEventListener("play", () => { this._startLoop(); this._emit("state"); });
    video.addEventListener("pause", () => { this._stopLoop(); this._emitTick(); this._emit("state"); });
    video.addEventListener("seeked", () => { this._emitTick(); });
    video.addEventListener("timeupdate", () => { if (!video.paused) this._check(); this._emitTick(); });
    video.addEventListener("ended", () => this._finish());
    video.addEventListener("error", () => this._emit("error", { message: this._errorMessage() }));
    video.addEventListener("loadedmetadata", () => this._emit("loaded"));
  }

  _emit(type, detail = {}) {
    this.dispatchEvent(new CustomEvent(type, { detail }));
  }

  _errorMessage() {
    const e = this.video.error;
    const ext = (this.video.currentSrc || "").split(".").pop();
    if (e?.code === 4) {
      return "浏览器播放不了这个素材。常见原因是格式（mkv、HEVC 等）浏览器不支持；审阅不影响出片，可以先转一份 H.264 的 mp4 来看：ffmpeg -i 原片 -c:v libx264 -c:a aac 预览.mp4";
    }
    return `视频加载出错（${e?.message || "代码 " + e?.code}${ext ? "，" + ext : ""}）`;
  }

  // ---- 方案与模式 ----
  /** 换了输出或者改了方案之后调用：重建播放列表，并在不 seek 的前提下把游标对回当前位置。 */
  setOutput(output, sources) {
    this.output = output;
    this.sources = sources;
    this.playlist = buildPlaylist(output, sources);
    if (this.sourceId) {
      const loc = locatePlaylist(this.playlist, this.sourceId, this.video.currentTime, this.cur);
      if (loc.index >= 0) this.cur = loc.index;
    } else {
      this.cur = 0;
    }
    this._emitTick();
  }

  setMode(mode) {
    this.mode = mode;
    this._lastProps = "";
    this._applyProps(null);
    if (mode === "plan" && !this.video.paused) this._check();
    this._emit("state");
    this._emitTick();
  }

  setRate(r) {
    this.rate = r;
    this._lastProps = "";
    this._applyProps(this.playlist[this.cur] ?? null);
    this._emit("state");
  }

  get paused() { return this.video.paused; }

  // ---- 素材加载 ----
  async load(sourceId) {
    if (this.sourceId === sourceId && this.video.src) return;
    const info = this.sources[sourceId];
    this.sourceId = sourceId;
    if (!info?.url) return;
    await new Promise((resolve) => {
      const done = () => { this.video.removeEventListener("loadedmetadata", done); this.video.removeEventListener("error", done); resolve(); };
      this.video.addEventListener("loadedmetadata", done);
      this.video.addEventListener("error", done);
      this.video.src = info.url;
    });
  }

  // ---- 控制 ----
  async play() {
    if (this.audition) this._endAudition(false);
    if (this.mode === "plan" && this.playlist.length) {
      const last = this.playlist[this.playlist.length - 1];
      const atEnd = this.sourceId === last.source && this.video.currentTime >= last.end - 0.05 && this.cur >= this.playlist.length - 1;
      if (atEnd) await this.seekOutput(0);
      else this._check(true);
    }
    try { await this.video.play(); } catch (e) { if (e.name !== "AbortError") this._emit("error", { message: `播放失败：${e.message}` }); }
  }

  pause() {
    if (this.audition) this._endAudition(false);
    this.video.pause();
  }

  toggle() { return this.video.paused ? this.play() : this.pause(); }

  /** 跳到某个素材的某个时间。播放中遇到被剪掉的位置会立刻被 _check 推到下一段保留的开头。 */
  async seekSource(sourceId, t) {
    if (sourceId !== this.sourceId) await this.load(sourceId);
    const loc = locatePlaylist(this.playlist, sourceId, t, this.cur);
    if (loc.index >= 0) this.cur = loc.index;
    this._setTime(Math.max(0, t));
  }

  async seekOutput(ot) {
    const at = fromOutputTime(this.output, Math.max(0, ot));
    if (!at) return;
    const idx = this.playlist.findIndex((e) => e.source === at.source && at.t >= e.start - EPS && at.t <= e.end + EPS && ot >= e.o0 - EPS && ot <= e.o1 + EPS);
    if (at.source !== this.sourceId) await this.load(at.source);
    if (idx >= 0) this.cur = idx;
    this._setTime(at.t);
  }

  seekRelative(deltaSec) {
    const s = this.state();
    if (this.mode === "plan" && s.outTime != null) return this.seekOutput(Math.min(Math.max(0, s.outTime + deltaSec), s.outDuration));
    return this.seekSource(this.sourceId, Math.min(Math.max(0, this.video.currentTime + deltaSec), this.video.duration || Infinity));
  }

  _setTime(t) {
    const v = this.video;
    if (Math.abs(v.currentTime - t) < 1e-3) { this._emitTick(); return; }
    v.currentTime = t;
    this._emitTick(); // seeked 之前先让界面跟上
  }

  /** 试听 [from, to]（素材时间，原片，不跳过）；到点自动暂停。 */
  async auditionRange(sourceId, from, to) {
    this.audition = { until: to };
    this._lastProps = "";
    if (sourceId !== this.sourceId) await this.load(sourceId);
    this._applyProps(null);
    this._setTime(Math.max(0, from));
    try { await this.video.play(); } catch { /* 被新的操作打断 */ }
    this._emit("state");
  }

  _endAudition(pause = true) {
    this.audition = null;
    if (pause) this.video.pause();
    this._lastProps = "";
    this._emit("auditionend");
    this._emit("state");
  }

  // ---- 播放循环 ----
  _startLoop() {
    if (this._raf != null) return;
    const loop = () => {
      this._raf = null;
      this._check();
      this._emitTick();
      if (!this.video.paused && !this.video.ended) this._raf = requestAnimationFrame(loop);
    };
    this._raf = requestAnimationFrame(loop);
  }

  _stopLoop() {
    if (this._raf != null) cancelAnimationFrame(this._raf);
    this._raf = null;
  }

  /**
   * 跳过逻辑。force=true 时（刚按下播放）即使暂停也把位置推出被剪掉的区间。
   * 提前量 lead ≈ 两帧的播放量：seek 本身要时间，晚一点就会把剪掉的头几十毫秒播出来。
   */
  _check(force = false) {
    const v = this.video;
    if (this.audition) {
      if (v.currentTime >= this.audition.until || v.ended) this._endAudition();
      return;
    }
    const pl = this.playlist;
    if (this.mode !== "plan" || !pl.length || (v.paused && !force) || v.seeking) return;
    const t = v.currentTime;
    let e = pl[this.cur];
    if (!e || e.source !== this.sourceId || t < e.start - EPS || t >= e.end + EPS) {
      const loc = locatePlaylist(pl, this.sourceId, t, this.cur);
      if (loc.index < 0) { this._finish(); return; }
      this.cur = loc.index;
      e = pl[this.cur];
      if (!loc.inside) { this._jump(e.source, e.start, null); this._applyProps(e); return; }
    }
    const lead = Math.max(0.025, 0.035 * v.playbackRate);
    if (t >= e.end - lead) {
      const next = pl[this.cur + 1];
      if (!next) { if (t >= e.end - EPS) this._finish(); return; }
      this.cur += 1;
      const contiguous = next.source === e.source && Math.abs(next.start - e.end) < 1e-3;
      if (!contiguous) this._jump(next.source, next.start, { from: e.end, to: next.start, source: e.source });
      this._applyProps(next);
      return;
    }
    this._applyProps(e);
  }

  async _jump(source, t, skipped) {
    if (skipped) this._emit("skip", skipped);
    if (source !== this.sourceId) {
      const wasPlaying = !this.video.paused;
      await this.load(source);
      this.video.currentTime = t;
      if (wasPlaying) this.video.play().catch(() => {});
    } else {
      this.video.currentTime = t;
    }
  }

  _applyProps(e) {
    const speed = this.mode === "plan" && e ? e.speed : 1;
    const mute = this.mode === "plan" && e ? e.mute : false;
    const key = `${speed}|${mute}|${this.rate}`;
    if (key === this._lastProps) return;
    this._lastProps = key;
    this.video.playbackRate = Math.min(Math.max(this.rate * speed, 0.0625), 16);
    this.video.muted = mute;
  }

  _finish() {
    const wasPlaying = !this.video.paused;
    this.video.pause();
    if (wasPlaying) this._emit("ended");
    this._emitTick();
  }

  // ---- 状态 ----
  state() {
    const v = this.video;
    const srcTime = v.currentTime || 0;
    let outTime = null, outDuration = this.playlist.length ? this.playlist[this.playlist.length - 1].o1 : 0;
    if (this.output && this.sourceId) {
      const e = this.playlist[this.cur];
      if (this.mode === "plan" && e && e.source === this.sourceId && srcTime >= e.start - EPS && srcTime <= e.end + EPS) {
        outTime = e.o0 + (srcTime - e.start) / e.speed;
      } else {
        const m = mapSourceTime(this.output, this.sourceId, srcTime);
        outTime = m.kept ? m.out : null;
      }
    }
    return { playing: !v.paused, sourceId: this.sourceId, srcTime, outTime, outDuration, mode: this.mode, rate: this.rate, auditioning: !!this.audition, duration: v.duration || 0 };
  }

  _emitTick() {
    this._emit("tick", this.state());
  }
}
