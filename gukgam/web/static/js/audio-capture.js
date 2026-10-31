// 브라우저 음성 캡처: 마이크(getUserMedia) 또는 탭 오디오(getDisplayMedia) → AudioWorklet(pcm-worklet.js)
// 16kHz mono Int16 → WebSocket(/ws/ingest/{sid}) 바이너리 프레임으로 보낸다. 연결이 끊기면 자동 재연결한다.

const MAX_BACKLOG = 50;          // 연결이 끊긴 동안 붙잡아 둘 최대 조각 수(100ms × 50 = 5초)
const MAX_BUFFERED = 1_000_000;  // 소켓 송신 대기 바이트가 이보다 많으면 버린다(지연 누적 방지)

const fmtClock = (sec) => {
  const m = Math.floor(sec / 60), s = Math.floor(sec % 60);
  return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
};
const fmtBytes = (b) => (b < 1024 * 1024 ? `${Math.round(b / 1024)}KB` : `${(b / 1024 / 1024).toFixed(1)}MB`);

export class AudioCapture {
  constructor({ wsUrl, workletUrl = '/static/js/pcm-worklet.js', onLevel = () => {}, onState = () => {} }) {
    this.wsUrl = wsUrl;
    this.workletUrl = workletUrl;
    this.onLevel = onLevel;
    this.onState = onState;
    this.running = false;
    this.kind = '';
    this.ws = null;
    this.ctx = null;
    this.stream = null;
    this.node = null;
    this.backlog = [];
    this.sentBytes = 0;
    this.dropped = 0;
    this.startedAt = 0;
    this.retry = 0;
    this.retryTimer = 0;
    this.statusTimer = 0;
    this.wsState = 'closed';
    this.serverMsg = '';
  }

  _state(text) {
    this.onState({ running: this.running, text });
  }

  _statusText() {
    const src = this.kind === 'tab' ? '탭 오디오' : '마이크';
    const t = fmtClock((Date.now() - this.startedAt) / 1000);
    const conn = { open: '보내는 중', connecting: '서버 연결 중', closed: '연결 끊김 — 다시 연결 중' }[this.wsState] || this.wsState;
    const drop = this.dropped ? ` · 버린 조각 ${this.dropped}` : '';
    return `${conn} · ${src} · ${t} · ${fmtBytes(this.sentBytes)}${drop}${this.serverMsg ? ` · ${this.serverMsg}` : ''}`;
  }

  async _getStream(kind) {
    const md = navigator.mediaDevices;
    if (!md || (kind === 'mic' && !md.getUserMedia) || (kind === 'tab' && !md.getDisplayMedia)) {
      throw new Error('이 브라우저(또는 http 주소)에서는 음성 캡처를 쓸 수 없습니다. https 또는 localhost 로 접속하세요.');
    }
    if (kind === 'tab') {
      let stream;
      try {
        stream = await md.getDisplayMedia({
          video: true,
          audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false },
          selfBrowserSurface: 'exclude',
          systemAudio: 'include',
        });
      } catch (e) {
        throw new Error(e && e.name === 'NotAllowedError' ? '화면·탭 공유가 취소되었습니다.' : `탭 오디오를 가져오지 못했습니다: ${e.message || e}`);
      }
      if (!stream.getAudioTracks().length) {
        stream.getTracks().forEach((t) => t.stop());
        throw new Error('소리가 공유되지 않았습니다. 공유 창에서 탭을 고르고 ‘탭 오디오 공유’를 켜세요.');
      }
      // 영상은 쓰지 않는다(공유 표시는 유지되도록 트랙만 비활성화)
      stream.getVideoTracks().forEach((t) => { t.enabled = false; });
      return stream;
    }
    try {
      return await md.getUserMedia({
        audio: { channelCount: 1, echoCancellation: false, noiseSuppression: false, autoGainControl: true },
        video: false,
      });
    } catch (e) {
      throw new Error(e && e.name === 'NotAllowedError' ? '마이크 사용이 허용되지 않았습니다. 브라우저 주소창의 권한 설정을 확인하세요.' : `마이크를 열지 못했습니다: ${e.message || e}`);
    }
  }

  async start(kind = 'mic') {
    try {
      await this._start(kind);
    } catch (e) {
      // 권한 거부·장치 오류: '장치 준비 중…' 에 멈춰 있지 않게 실패를 상태 줄에 남긴다
      this._state(`시작하지 못했습니다: ${(e && e.message) || e}`);
      throw e;
    }
  }

  async _start(kind) {
    if (this.running) this.stop();
    this.kind = kind;
    this.retry = 0;
    this._state('장치 준비 중…');
    this.stream = await this._getStream(kind);
    try {
      const AC = window.AudioContext || window.webkitAudioContext;
      this.ctx = new AC({ latencyHint: 'interactive' });
      if (!this.ctx.audioWorklet) throw new Error('이 브라우저는 AudioWorklet 을 지원하지 않습니다.');
      await this.ctx.audioWorklet.addModule(this.workletUrl);
      const src = this.ctx.createMediaStreamSource(this.stream);
      this.node = new AudioWorkletNode(this.ctx, 'pcm16-downsampler', {
        numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [1],
        processorOptions: { targetRate: 16000, chunkMs: 100 },
      });
      const mute = this.ctx.createGain();
      mute.gain.value = 0; // 스피커로 되돌려 내보내지 않는다(되울림 방지), 그래프만 유지
      src.connect(this.node).connect(mute).connect(this.ctx.destination);
      this.node.port.onmessage = (e) => this._onChunk(e.data);
      if (this.ctx.state === 'suspended') await this.ctx.resume();
    } catch (e) {
      this._cleanupMedia();
      throw e;
    }
    for (const t of this.stream.getAudioTracks()) t.addEventListener('ended', () => { if (this.running) { this.stop(); this._state('공유가 끝나 보내기를 멈췄습니다.'); } });
    this.running = true;
    this.startedAt = Date.now();
    this.sentBytes = 0;
    this.dropped = 0;
    this.backlog = [];
    this._openWs();
    this.statusTimer = setInterval(() => this._state(this._statusText()), 1000);
    this._state(this._statusText());
  }

  _openWs() {
    clearTimeout(this.retryTimer);
    if (!this.running) return;
    this.wsState = 'connecting';
    let ws;
    try {
      ws = new WebSocket(this.wsUrl);
    } catch (e) {
      this._scheduleRetry(`연결 실패: ${e.message || e}`);
      return;
    }
    ws.binaryType = 'arraybuffer';
    this.ws = ws;
    ws.onopen = () => {
      // 재시도 간격은 서버가 'ready' 로 받아들였을 때만 초기화한다(수락 뒤 4409 로 닫는 경우 백오프 유지)
      this.wsState = 'open';
      this.serverMsg = '';
      ws.send(JSON.stringify({ type: 'hello', sample_rate: 16000, channels: 1, format: 's16le', source: this.kind }));
      while (this.backlog.length && ws.readyState === 1) {
        const b = this.backlog.shift();
        ws.send(b);
        this.sentBytes += b.byteLength;
      }
      this._state(this._statusText());
    };
    ws.onmessage = (e) => {
      if (typeof e.data !== 'string') return;
      try {
        const m = JSON.parse(e.data);
        if (m.type === 'error') this.serverMsg = m.message || m.detail || '서버 오류';
        else if (m.type === 'ready') { this.serverMsg = ''; this.retry = 0; }
        else if (m.type === 'status' && (m.detail || m.message)) this.serverMsg = m.detail || m.message;
      } catch { this.serverMsg = e.data.slice(0, 80); }
      this._state(this._statusText());
    };
    ws.onclose = (e) => {
      if (this.ws !== ws) return;
      this.wsState = 'closed';
      if (!this.running) return;
      const why = e.reason || this.serverMsg || ({ 4401: '로그인이 필요합니다', 4403: '운영자 권한이 필요합니다', 4404: '세션을 찾을 수 없습니다', 4400: '잘못된 음성 형식입니다', 4409: '수집 중이 아닙니다' }[e.code] || '');
      if ([4400, 4401, 4403, 4404].includes(e.code)) {
        // 다시 연결해도 소용없는 오류: 멈추고 알린다
        this.stop();
        this._state(`보내기를 멈췄습니다: ${why}`);
        return;
      }
      this._scheduleRetry(why);
    };
    ws.onerror = () => { /* onclose 에서 처리 */ };
  }

  _scheduleRetry(why) {
    if (!this.running) return;
    const delay = Math.min(10000, 1000 * 2 ** this.retry);
    this.retry += 1;
    if (why) this.serverMsg = why;
    this._state(this._statusText());
    clearTimeout(this.retryTimer);
    this.retryTimer = setTimeout(() => this._openWs(), delay);
  }

  /** 수집이 재개됐을 때: 기다리던 재연결을 바로 시도한다(이미 연결돼 있으면 아무것도 안 함) */
  reconnectNow() {
    if (!this.running || this.wsState === 'open' || this.wsState === 'connecting') return;
    this.retry = 0;
    this._openWs();
  }

  _onChunk(msg) {
    if (!msg || !msg.pcm) return;
    this.onLevel(msg.level || 0);
    if (!this.running) return;
    const ws = this.ws;
    if (ws && ws.readyState === 1) {
      if (ws.bufferedAmount > MAX_BUFFERED) { this.dropped += 1; return; }
      ws.send(msg.pcm);
      this.sentBytes += msg.pcm.byteLength;
    } else {
      this.backlog.push(msg.pcm);
      if (this.backlog.length > MAX_BACKLOG) { this.backlog.shift(); this.dropped += 1; }
    }
  }

  _cleanupMedia() {
    try { this.node && this.node.port.postMessage('flush'); } catch { /* 무시 */ }
    try { this.node && this.node.disconnect(); } catch { /* 무시 */ }
    this.node = null;
    if (this.stream) this.stream.getTracks().forEach((t) => t.stop());
    this.stream = null;
    if (this.ctx) this.ctx.close().catch(() => {});
    this.ctx = null;
  }

  stop() {
    const was = this.running;
    this.running = false;
    clearTimeout(this.retryTimer);
    clearInterval(this.statusTimer);
    this._cleanupMedia();
    if (this.ws) {
      const ws = this.ws;
      this.ws = null;
      try { ws.close(1000, 'client stop'); } catch { /* 무시 */ }
    }
    this.backlog = [];
    this.onLevel(0);
    if (was) this._state(`보내기를 멈췄습니다 · 보낸 양 ${fmtBytes(this.sentBytes)}`);
  }
}
