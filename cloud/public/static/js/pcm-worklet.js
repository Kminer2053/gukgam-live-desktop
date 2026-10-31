// AudioWorklet 처리기: 브라우저 표본율(44.1k/48k 등) 입력 → 16kHz mono Int16(s16le) 다운샘플.
// chunkMs(기본 100ms)마다 { pcm: ArrayBuffer, level: RMS(0~1) } 를 메인 스레드로 보낸다.
// 다운샘플은 구간 평균(박스 필터) 방식 — 음성인식용으로 충분하고 가볍다.

class Pcm16Downsampler extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const o = (options && options.processorOptions) || {};
    this.target = o.targetRate || 16000;
    // sampleRate 는 AudioWorkletGlobalScope 전역(컨텍스트 표본율)
    this.ratio = Math.max(1, sampleRate / this.target);
    this.chunk = Math.max(160, Math.round((this.target * (o.chunkMs || 100)) / 1000));
    this.buf = new Int16Array(this.chunk);
    this.len = 0;
    this.acc = 0;
    this.cnt = 0;
    this.phase = 0;
    this.sumSq = 0;
    this.nLevel = 0;
    this.port.onmessage = (e) => {
      if (e.data === 'flush') this.flush();
    };
  }

  emit() {
    const level = Math.sqrt(this.sumSq / Math.max(1, this.nLevel));
    this.sumSq = 0;
    this.nLevel = 0;
    const out = this.len === this.chunk ? this.buf : this.buf.slice(0, this.len);
    this.buf = new Int16Array(this.chunk);
    this.len = 0;
    this.port.postMessage({ pcm: out.buffer, level }, [out.buffer]);
  }

  flush() {
    if (this.len) this.emit();
  }

  process(inputs) {
    const input = inputs[0];
    if (!input || !input.length || !input[0]) return true;
    const chs = input.length;
    const n = input[0].length;
    for (let i = 0; i < n; i += 1) {
      let x = 0;
      for (let c = 0; c < chs; c += 1) x += input[c][i];
      x /= chs;
      this.sumSq += x * x;
      this.nLevel += 1;
      this.acc += x;
      this.cnt += 1;
      this.phase += 1;
      if (this.phase >= this.ratio) {
        this.phase -= this.ratio;
        let v = this.acc / this.cnt;
        this.acc = 0;
        this.cnt = 0;
        if (v > 1) v = 1; else if (v < -1) v = -1;
        this.buf[this.len] = v < 0 ? v * 0x8000 : v * 0x7fff;
        this.len += 1;
        if (this.len === this.chunk) this.emit();
      }
    }
    return true;
  }
}

registerProcessor('pcm16-downsampler', Pcm16Downsampler);
