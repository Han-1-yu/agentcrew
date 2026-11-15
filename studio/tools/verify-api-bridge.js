'use strict';
// 后端 API 代理的端到端验证（无窗口 Electron 主进程）。
//
// 现有 selftest / e2e 都跑在「CLI + 文件监听」默认路径上，完全不覆盖 HTTP API
// 通道。这里起一个要求 Bearer 认证的假后端，验证四件事：
//   1. 无 Key 会被 401 拦截，带 Key 能拿到 JSON（认证头确实注入了）
//   2. SSE 命名事件帧（connected / task_transition / agent_stream_delta）能解析
//   3. 心跳注释帧不会混进业务事件
//   4. C10 指引 / R62 回滚的 REST 路径（GET / POST / DELETE / dry_run 预览）
//      确实经 api:request 打到了后端，且非 2xx 会带回 status
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

  const requests = [];
  const server = http.createServer((req, res) => {
    const send = (status, payload) => {
      res.writeHead(status, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify(payload));
    };
    if ((req.headers.authorization || '') !== 'Bearer test-key') {
      send(401, { error: 'Unauthorized' });
      return;
    }
    let raw = '';
    req.on('data', chunk => { raw += chunk; });
    req.on('end', () => {
      const url = (req.url || '').split('?')[0];
      const method = req.method || 'GET';
      requests.push({ method, url, body: raw });

      if (url === '/providers') {
        send(200, { providers: [{ name: 'p1' }] });
        return;
      }
      if (url === '/runs/R1/events') {
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
      if (url === '/runs/R1/guidance' && method === 'GET') {
        send(200, {
          run_id: 'R1', pending: 1,
          items: [{ id: 'GD-1', ts: 't', author: 'ops', text: '改绿色',
            consumed_at: null, target: 'T-003', expires_at: null,
            withdrawn_at: null, status: 'pending' }],
        });
        return;
      }
      if (url === '/runs/R1/guidance' && method === 'POST') {
        send(202, { id: 'GD-2', count: 2, target: 'T-003', expires_at: null });
        return;
      }
      if (url === '/runs/R1/guidance/GD-1' && method === 'DELETE') {
        send(200, { id: 'GD-1', withdrawn: true, pending: 1 });
        return;
      }
      if (url === '/runs/R1/guidance/compact' && method === 'POST') {
        send(409, { error: 'run 仍在运行' });
        return;
      }
      if (url === '/runs/R1/rewind' && method === 'POST') {
        send(200, {
          run_id: 'R1', target: 'T-002',
          affected: ['T-002', 'T-FINAL'], snapshot_dir: null,
          discarded_artifacts: 0, status_before: 'completed',
          status_after: 'completed', instruction: '', dry_run: true,
        });
        return;
      }
      send(404, {});
    });
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

  // preload 的 apiRequest(path, options) 会包成 { path, options } 再 invoke，
  // 这里必须按同一形状调用 ipcMain.handle，否则 method/body 会被当成默认值。
  const call = (pathname, options = {}) =>
    handlers['api:request'](null, { path: pathname, options });
  const json = (obj) => ({
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(obj),
  });

  if (safeStorage.isEncryptionAvailable()) {
    const saved = handlers['api:keySave'](null, { value: 'test-key' });
    check('Key 加密保存', saved && saved.ok, saved);
    const withKey = await handlers['api:request'](null, { path: '/providers' });
    check('带 Key → REST 返回数据',
      withKey.ok === true && withKey.data && withKey.data.providers[0].name === 'p1', withKey);

    const list = await call('/runs/R1/guidance');
    check('GET 指引列表带 status / target 字段',
      list.ok === true && list.data && list.data.items[0].status === 'pending'
        && list.data.items[0].target === 'T-003', list);

    const posted = await call(
      '/runs/R1/guidance',
      json({ text: '改绿色', target: 'T-003', expires_in_seconds: 600 }));
    const postReq = requests.find(r => r.method === 'POST' && r.url === '/runs/R1/guidance');
    check('POST 指引 → 202 且请求体带定向与过期',
      posted.ok === true && posted.status === 202
        && !!postReq && postReq.body.includes('T-003')
        && postReq.body.includes('expires_in_seconds'), posted);

    const removed = await call('/runs/R1/guidance/GD-1', { method: 'DELETE' });
    check('DELETE 指引 → 撤回成功',
      removed.ok === true && removed.data && removed.data.withdrawn === true, removed);

    const compacted = await call('/runs/R1/guidance/compact', { method: 'POST' });
    check('运行中压缩 → 409 且带回 status',
      compacted.ok === false && compacted.status === 409, compacted);

    const rewound = await call(
      '/runs/R1/rewind',
      json({ task: 'T-002', instruction: '', dry_run: true }));
    check('rewind dry_run → 影响集可读',
      rewound.ok === true && rewound.data && rewound.data.affected.length === 2
        && rewound.data.dry_run === true, rewound);
  } else {
    check('Key 加密保存（本机不支持 safeStorage，跳过认证链）', true, 'skipped');
    check('带 Key → REST 返回数据（跳过）', true, 'skipped');
    for (const name of ['指引列表', '指引写入', '指引撤回', '压缩拒绝', '回滚预览']) {
      check(`${name}（跳过）`, true, 'skipped');
    }
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
