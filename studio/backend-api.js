'use strict';
// 后端 HTTP API 代理（主进程）。
//
// 为什么必须走主进程：后端 serve 没有 CORS 头，渲染进程（file:// 源）直接 fetch
// 会被浏览器拦下；主进程 net.fetch 不受 CORS 约束。顺带把凭据收在主进程：
// R36 起后端认证默认强制开启（fail-closed），Key 用 Electron safeStorage 加密后
// 落在 userData，既不进 settings.json、也不回显给渲染进程。
//
// 注意范围：这里只管**后端 API Key**。**供应商 Key**（ARK_API_KEY 等）不走
// safeStorage——它是 `main.js` 的 provider:saveKey，明文写入项目根 `.env` 以便
// CLI 与 Studio 共用（C-21：两类凭据的存储方式不同，别混着理解）。
//
// SSE 也走这里：原生 EventSource 无法设置 Authorization 头，而后端只认 header
// （rbac.extract_key 不读 query）。net.fetch 能带 header 且响应体可流式读取，
// 解析出的命名事件帧通过 webContents 推给渲染进程。

const { net, safeStorage } = require('electron');
const fs = require('fs');
const path = require('path');

function createBackendApi({ ipcMain, userDataPath, getSettings, getWin }) {
  const keyPath = () => path.join(userDataPath, 'api-key.bin');
  let apiKey = '';
  let streamCtl = null;

  function loadKey() {
    try {
      if (!safeStorage.isEncryptionAvailable()) return;
      apiKey = safeStorage.decryptString(fs.readFileSync(keyPath()));
    } catch { apiKey = ''; }
  }

  function saveKey(value) {
    const v = String(value == null ? '' : value).trim();
    if (!v) throw new Error('密钥不能为空');
    if (/\r?\n/.test(v)) throw new Error('密钥不能包含换行');
    if (!safeStorage.isEncryptionAvailable()) throw new Error('当前系统不支持安全存储，无法保存密钥');
    fs.mkdirSync(userDataPath, { recursive: true });
    fs.writeFileSync(keyPath(), safeStorage.encryptString(v));
    apiKey = v;
  }

  function clearKey() {
    apiKey = '';
    try { fs.rmSync(keyPath(), { force: true }); } catch { /* 非致命 */ }
  }

  loadKey();

  const base = () => String((getSettings() || {}).apiBaseUrl || '').trim().replace(/\/+$/, '');
  const authHeaders = () => (apiKey ? { Authorization: 'Bearer ' + apiKey } : {});

  function send(channel, payload) {
    const w = getWin();
    if (w && !w.isDestroyed()) w.webContents.send(channel, payload);
  }

  /** 单次 REST 调用。返回结构恒定为 { ok, status, data?, error? }。 */
  async function request(pathname, options = {}) {
    // P0-8：baseUrl 只认 settings.apiBaseUrl（主进程落盘的那份），渲染端传入的
    // options.baseUrl 一律忽略——此前它完全可控，等于把 safeStorage 解密后的
    // Bearer Key 发往任意主机。用户「改了地址还没保存」的探测需求走独立的
    // api:probe（不带任何凭据）。
    const b = base();
    if (!b) return { ok: false, status: 0, error: '未配置后端 API 地址' };
    // 渲染端可传任意 path：限定同源相对路径，避免主进程被当成任意 URL 的代理。
    const p = String(pathname || '');
    if (!p.startsWith('/') || p.includes('://')) {
      return { ok: false, status: 0, error: '非法请求路径' };
    }
    if (!/^https?:\/\//i.test(b)) {
      return { ok: false, status: 0, error: '后端地址仅支持 http/https' };
    }
    // 渲染端自定义 headers 不允许覆盖 Authorization：凭据只属于主进程。
    const extra = options.headers || {};
    const safeHeaders = {};
    for (const [k, v] of Object.entries(extra)) {
      if (String(k).toLowerCase() !== 'authorization') safeHeaders[k] = v;
    }
    const ctrl = new AbortController();
    const timer = setTimeout(() => { try { ctrl.abort(); } catch {} }, Number(options.timeoutMs) || 8000);
    try {
      const res = await net.fetch(b + p, {
        method: String(options.method || 'GET').toUpperCase(),
        headers: Object.assign({}, authHeaders(), safeHeaders),
        body: options.body,
        signal: ctrl.signal,
        // P0-8：后端 serve 不会重定向；follow 会让「保存的地址被劫持后 302 到
        // 任意主机」也带上 Key。重定向按错误处理。
        redirect: 'error',
      });
      const text = await res.text();
      let data = null;
      try { data = text ? JSON.parse(text) : null; } catch { data = text; }
      if (!res.ok) {
        const detail = data && typeof data === 'object' && data.error ? String(data.error) : '';
        return { ok: false, status: res.status, data, error: detail || `HTTP ${res.status}` };
      }
      return { ok: true, status: res.status, data };
    } catch (e) {
      const msg = String((e && e.message) || e);
      return { ok: false, status: 0, error: /abort/i.test(msg) ? '连接超时' : msg };
    } finally { clearTimeout(timer); }
  }

  /** 无凭据连通性探测（设置页「测试连接」专用）。
   *
   * 接受一个尚未保存的候选地址，但不注入 authHeaders——probe 的结论是
   * 「地址是否可达 / 是否需要认证」，凭据永远不会发给未保存的地址。
   */
  async function probe(baseUrlRaw) {
    const b = String(baseUrlRaw || '').trim().replace(/\/+$/, '');
    if (!/^https?:\/\//i.test(b)) {
      return { reachable: false, status: 0, error: '后端地址仅支持 http/https' };
    }
    const ctrl = new AbortController();
    const timer = setTimeout(() => { try { ctrl.abort(); } catch {} }, 8000);
    try {
      const res = await net.fetch(b + '/health', {
        method: 'GET',
        signal: ctrl.signal,
        redirect: 'error',
      });
      // 任何 HTTP 状态（含 401/403）都说明「地址可达」；401 = 可达且需认证。
      return { reachable: true, status: res.status, error: res.ok ? null : `HTTP ${res.status}` };
    } catch (e) {
      const msg = String((e && e.message) || e);
      return { reachable: false, status: 0, error: /abort/i.test(msg) ? '连接超时' : msg };
    } finally { clearTimeout(timer); }
  }

  /** 把一帧 SSE 文本解析成 { event, data } 并推给渲染进程。 */
  function dispatchFrame(runId, frame) {
    let event = 'message';
    const dataLines = [];
    for (const line of frame.split('\n')) {
      if (!line || line.startsWith(':')) continue;   // 注释 / 心跳
      const colon = line.indexOf(':');
      const field = colon === -1 ? line : line.slice(0, colon);
      let value = colon === -1 ? '' : line.slice(colon + 1);
      if (value.startsWith(' ')) value = value.slice(1);
      if (field === 'event') event = value;
      else if (field === 'data') dataLines.push(value);
    }
    if (!dataLines.length) return;
    const raw = dataLines.join('\n');
    let data;
    try { data = JSON.parse(raw); } catch { data = raw; }
    send('sse:frame', { runId, event, data });
  }

  function stopStream() {
    if (!streamCtl) return;
    try { streamCtl.ctrl.abort(); } catch { /* 已结束 */ }
    streamCtl = null;
  }

  /** 建立 SSE 长连接。只在拿到响应头后返回，流读取转后台。 */
  async function startStream(runIdRaw) {
    stopStream();
    const runId = String(runIdRaw || '').trim();
    const b = base();
    if (!b) return { ok: false, status: 0, error: '未配置后端 API 地址' };
    if (!runId) return { ok: false, status: 0, error: '缺少 run id' };

    const ctrl = new AbortController();
    streamCtl = { runId, ctrl };
    const url = `${b}/runs/${encodeURIComponent(runId)}/events`;
    let res;
    try {
      res = await net.fetch(url, {
        method: 'GET',
        headers: Object.assign({}, authHeaders(), { Accept: 'text/event-stream' }),
        signal: ctrl.signal,
        redirect: 'error',
      });
    } catch (e) {
      if (streamCtl && streamCtl.ctrl === ctrl) streamCtl = null;
      return { ok: false, status: 0, error: String((e && e.message) || e) };
    }
    if (!res.ok || !res.body || !res.body.getReader) {
      if (streamCtl && streamCtl.ctrl === ctrl) streamCtl = null;
      let detail = `HTTP ${res.status}`;
      if (!res.ok) {
        try { const j = await res.json(); if (j && j.error) detail += `：${j.error}`; } catch { /* 忽略 */ }
      }
      return { ok: false, status: res.status, error: res.ok ? '响应不是可读流' : detail };
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    (async () => {
      let buf = '';
      let reason = 'closed';
      try {
        while (!ctrl.signal.aborted) {
          const { done, value } = await reader.read();
          if (done) break;
          buf = (buf + decoder.decode(value, { stream: true })).replace(/\r\n/g, '\n');
          let cut;
          while ((cut = buf.indexOf('\n\n')) !== -1) {
            const frame = buf.slice(0, cut);
            buf = buf.slice(cut + 2);
            dispatchFrame(runId, frame);
          }
        }
      } catch (e) {
        reason = ctrl.signal.aborted ? 'stopped' : String((e && e.message) || e);
      } finally {
        // 正常关流时 flush TextDecoder：多字节 UTF-8 字符（中文 delta）恰好
        // 落在 TCP 块边界时，不 flush 会变成 U+FFFD；同时补发没有 \n\n 的残帧。
        if (!ctrl.signal.aborted) {
          try {
            buf += decoder.decode();
            if (buf.includes('data:')) dispatchFrame(runId, buf);
          } catch { /* 残帧不完整，丢弃 */ }
        }
        // cancel() 返回 Promise；abort 之后它会 reject，不接住就是一条
        // UnhandledPromiseRejection（反复切 run 会刷屏）。
        try {
          const p = reader.cancel();
          if (p && typeof p.catch === 'function') p.catch(() => {});
        } catch { /* 已关闭 */ }
        if (streamCtl && streamCtl.ctrl === ctrl) streamCtl = null;
        send('sse:closed', { runId, reason });
      }
    })();
    return { ok: true, status: res.status };
  }

  ipcMain.handle('api:keyStatus', () => ({ configured: !!apiKey }));
  ipcMain.handle('api:keySave', (_e, opts) => {
    try { saveKey(opts && opts.value); return { ok: true, configured: true }; }
    catch (e) { return { ok: false, error: String((e && e.message) || e) }; }
  });
  ipcMain.handle('api:keyClear', () => { clearKey(); return { ok: true, configured: false }; });
  ipcMain.handle('api:request', (_e, opts) =>
    request(opts && opts.path, (opts && opts.options) || {}));
  // P0-8：测试连接专用——接受候选地址，但绝不携带凭据。
  ipcMain.handle('api:probe', (_e, opts) => probe(opts && opts.baseUrl));
  ipcMain.handle('api:stream:start', (_e, opts) => startStream(opts && opts.runId));
  ipcMain.handle('api:stream:stop', () => { stopStream(); return { ok: true }; });

  return { stopStream, isKeyConfigured: () => !!apiKey };
}

module.exports = { createBackendApi };
