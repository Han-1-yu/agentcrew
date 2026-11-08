'use strict';
// 后端 API 代理的端到端验证（无窗口 Electron 主进程）。
//
// 现有 selftest / e2e 都跑在「CLI + 文件监听」默认路径上，完全不覆盖 HTTP API
// 通道。这里起一个要求 Bearer 认证的假后端，验证三件事：
//   1. 无 Key 会被 401 拦截，带 Key 能拿到 JSON（认证头确实注入了）
//   2. SSE 命名事件帧（connected / task_transition / agent_stream_delta）能解析
//   3. 心跳注释帧不会混进业务事件
//
// 用法（本会话需清掉宿主注入的 ELECTRON_RUN_AS_NODE）：
//   env -u ELECTRON_RUN_AS_NODE -u NODE_OPTIONS \
//     ./node_modules/electron/dist/electron.exe tools/verify-api-bridge.js

const http = require('http');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { app, safeStorage } = require('electron');
const { createBackendApi } = require('../backend-api');

const checks = [];
const check = (name, ok, extra) => checks.push({ name, ok: !!ok, extra });

async function run() {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'ac-api-verify-'));
  app.setPath('userData', tmp);

  const server = http.createServer((req, res) => {
    if ((req.headers.authorization || '') !== 'Bearer test-key') {
      res.writeHead(401, { 'Content-Type': 'application/json' });
      res.end('{"error":"Unauthorized"}');
      return;
    }
    if (req.url === '/providers') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ providers: [{ name: 'p1' }] }));
      return;
    }
    if (req.url === '/runs/R1/events') {
      res.writeHead(200, {
        'Content-Type': 'text/event-stream',
        'Cache-Control': 'no-cache',
        Connection: 'keep-alive',
      });
      res.write('event: connected\ndata: {"run_id":"R1"}\n\n');
      res.write('event: task_transition\ndata: {"run_id":"R1","task_id":"T1","to_status":"succeeded"}\n\n');
      res.write('event: agent_stream_delta\ndata: {"run_id":"R1","task_id":"T1","role":"backend_dev","delta":"hello 世界"}\n\n');
      res.write(': ping\n\n');
      return;   // 故意不 end：模拟长连接
    }
    res.writeHead(404, { 'Content-Type': 'application/json' });
    res.end('{}');
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const port = server.address().port;

  const handlers = {};
  const frames = [];
  const fakeWin = {
    isDestroyed: () => false,
    webContents: { send: (ch, payload) => { if (ch === 'sse:frame') frames.push(payload); } },
  };
  createBackendApi({
    ipcMain: { handle: (ch, fn) => { handlers[ch] = fn; } },
    userDataPath: tmp,
    getSettings: () => ({ apiBaseUrl: `http://127.0.0.1:${port}` }),
    getWin: () => fakeWin,
  });

  const noKey = await handlers['api:request'](null, { path: '/providers' });
  check('无 Key → 401', noKey.ok === false && noKey.status === 401, noKey);

  if (safeStorage.isEncryptionAvailable()) {
    const saved = handlers['api:keySave'](null, { value: 'test-key' });
    check('Key 加密保存', saved && saved.ok, saved);
    const withKey = await handlers['api:request'](null, { path: '/providers' });
    check('带 Key → REST 返回数据',
      withKey.ok === true && withKey.data && withKey.data.providers[0].name === 'p1', withKey);
  } else {
    check('Key 加密保存（本机不支持 safeStorage，跳过认证链）', true, 'skipped');
    check('带 Key → REST 返回数据（跳过）', true, 'skipped');
  }

  const started = await handlers['api:stream:start'](null, { runId: 'R1' });
  check('SSE 长连接建立', started.ok === true, started);
  await new Promise((resolve) => setTimeout(resolve, 500));
  const names = frames.map((f) => f.event);
  check('解析 connected 命名帧', names.includes('connected'), names);
  check('解析 task_transition 命名帧', names.includes('task_transition'), names);
  const delta = frames.find((f) => f.event === 'agent_stream_delta');
  check('流式 delta 载荷完整（含中文）',
    !!delta && delta.data.delta === 'hello 世界' && delta.data.task_id === 'T1', delta);
  check('心跳注释帧未混入业务事件', !names.includes('message'), names);

  handlers['api:stream:stop'](null);
  server.close();
  try { fs.rmSync(tmp, { recursive: true, force: true }); } catch { /* 非致命 */ }

  for (const c of checks) {
    console.log(`${c.ok ? 'PASS' : 'FAIL'}  ${c.name}${c.ok ? '' : ' — ' + JSON.stringify(c.extra)}`);
  }
  const failed = checks.filter((c) => !c.ok).length;
  console.log(`API 代理验证：${checks.length - failed}/${checks.length} 通过`);
  app.exit(failed ? 1 : 0);
}

app.whenReady().then(run).catch((e) => {
  console.error('验证脚本异常:', e);
  app.exit(1);
});
