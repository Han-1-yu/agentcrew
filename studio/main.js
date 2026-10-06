'use strict';
// AgentCrew Studio — Electron 主进程。
// 职责：窗口/设置/Python 探测/run 扫描与读取/CLI 子进程管理/ledger 偏移轮询/IPC。
// 只读后端产物文件与调用 CLI，不依赖 agentcrew/ 内部实现。

const { app, BrowserWindow, ipcMain, dialog, shell, Menu } = require('electron');
const path = require('path');
const fs = require('fs');
const { spawn, spawnSync } = require('child_process');
const { buildZipFromDir } = require('./zip');

const IS_SELFTEST = process.argv.includes('--selftest');

// 后端 CLI 的退出码语义（main.py EXIT_CODES）
const EXIT_MEANINGS = {
  0: '成功',
  1: '有任务失败',
  2: '计划拆解失败',
  3: '配置错误',
  4: '运行停滞',
  130: '用户取消',
};

const TEXT_EXTS = new Set([
  '.py', '.md', '.json', '.yaml', '.yml', '.html', '.htm', '.txt', '.css',
  '.js', '.ts', '.toml', '.ini', '.cfg', '.sh', '.bat', '.ps1', '.xml',
  '.csv', '.env', '.sql', '.log', '.diff', '.patch', '.gitignore',
]);
const TEXT_BASENAMES = new Set([
  'Dockerfile', 'Makefile', 'LICENSE', 'README', 'CHANGELOG', 'MANIFEST',
  'Procfile', '.dockerignore', '.gitignore', '.editorconfig',
]);

const DEFAULT_REPO_ROOT = path.resolve(__dirname, '..');

let win = null;
let settings = null;
const settingsPath = () => path.join(app.getPath('userData'), 'settings.json');

function defaultSettings() {
  return {
    repoRoot: DEFAULT_REPO_ROOT,
    outputDir: path.join(DEFAULT_REPO_ROOT, 'output'),
    pythonCmd: '',
    theme: 'dark',
    defaultMock: true,
    defaultAuto: true,
  };
}

function loadSettings() {
  const s = defaultSettings();
  try {
    const raw = JSON.parse(fs.readFileSync(settingsPath(), 'utf8'));
    Object.assign(s, raw);
  } catch { /* 首次启动或文件损坏：用默认值 */ }
  settings = s;
  return s;
}

function saveSettings(next) {
  settings = Object.assign({}, settings, next);
  fs.mkdirSync(path.dirname(settingsPath()), { recursive: true });
  fs.writeFileSync(settingsPath(), JSON.stringify(settings, null, 2), 'utf8');
  return settings;
}

// ---------- Python 探测 ----------

// 探测一条候选命令：`python --version` 可用即认为存在；并顺带解析绝对路径。
// 部分安全软件会拦截 GUI 进程创建解释器子进程（间歇性 ENOENT），所以带重试。
function tryCommand(cmd, args, retries) {
  return new Promise((resolve) => {
    let n = 0;
    const attempt = () => {
      let done = false;
      let out = '';
      let p;
      const finish = (ok) => {
        if (done) return;
        done = true;
        if (!ok && n < (retries || 0)) { n += 1; setTimeout(attempt, 300 * n); return; }
        resolve(ok ? `${cmd} ${out}`.trim() : null);
      };
      try {
        p = spawn(cmd, args, { windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] });
      } catch { return finish(false); }
      const timer = setTimeout(() => { try { p.kill(); } catch {} finish(false); }, 6000);
      p.stdout.on('data', (d) => { out += d.toString(); });
      p.stderr.on('data', (d) => { out += d.toString(); });
      p.on('error', () => { clearTimeout(timer); finish(false); });
      p.on('exit', (code) => {
        clearTimeout(timer);
        finish(code === 0 && /Python 3/i.test(out));
      });
    };
    attempt();
  });
}

// 从一条可用命令解析出 python 的绝对路径（sys.executable）；失败返回原命令。
async function resolveAbsolute(cmd) {
  const parts = cmd.split(/\s+/);
  const out = await new Promise((resolve) => {
    let p;
    let buf = '';
    try {
      p = spawn(parts[0], [...parts.slice(1), '-c', 'import sys;print(sys.executable)'], { windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] });
    } catch { return resolve(null); }
    const timer = setTimeout(() => { try { p.kill(); } catch {} resolve(null); }, 6000);
    p.stdout.on('data', (d) => { buf += d.toString(); });
    p.on('error', () => { clearTimeout(timer); resolve(null); });
    p.on('exit', (code) => { clearTimeout(timer); resolve(code === 0 ? buf.trim().split(/\r?\n/)[0] : null); });
  });
  return out && /^[A-Za-z]:\\.+python(\.exe)?$/i.test(out) ? out : null;
}

async function detectPython(candidate) {
  const candidates = [];
  if (candidate) candidates.push(candidate);
  else if (settings.pythonCmd) candidates.push(settings.pythonCmd);
  candidates.push('python', 'python3', 'py -3');
  for (const c of candidates) {
    const ver = await tryCommand(c.split(/\s+/)[0], [...c.split(/\s+/).slice(1), '--version'], 2);
    if (ver) {
      const abs = await resolveAbsolute(c);
      return { cmd: abs || c, version: ver.replace(/^py(thon3?)?\s+(-3\s+)?/i, '') };
    }
  }
  return null;
}

function pythonSpawnSpec(extraArgs) {
  // pythonCmd 允许带前缀参数（如 "py -3"），整体按空白拆分
  const cmd = settings.pythonCmd || 'python';
  const parts = cmd.split(/\s+/);
  return { cmd: parts[0], args: [...parts.slice(1), 'main.py', ...extraArgs] };
}

// ---------- run 扫描与读取 ----------

function readJsonTolerant(file) {
  try { return { ok: true, data: JSON.parse(fs.readFileSync(file, 'utf8')) }; }
  catch (e) { return { ok: false, error: String(e && e.message || e) }; }
}

function isRunDir(dir) {
  try {
    return fs.statSync(dir).isDirectory()
      && (fs.existsSync(path.join(dir, 'run.json')) || fs.existsSync(path.join(dir, 'ledger.jsonl')));
  } catch { return false; }
}

function scanRuns(outputDir) {
  const runs = [];
  let level1 = [];
  try { level1 = fs.readdirSync(outputDir, { withFileTypes: true }); }
  catch (e) { throw new Error(`无法读取输出目录 ${outputDir}：${e.message}`); }

  for (const ent of level1) {
    if (!ent.isDirectory()) continue;
    const dir = path.join(outputDir, ent.name);
    if (isRunDir(dir)) { runs.push(summarizeRun(dir, null)); continue; }
    // 分组目录（如 output/realrun7/<run_id>）再往下看一层
    let level2 = [];
    try { level2 = fs.readdirSync(dir, { withFileTypes: true }); } catch { continue; }
    for (const ent2 of level2) {
      if (!ent2.isDirectory()) continue;
      const dir2 = path.join(dir, ent2.name);
      if (isRunDir(dir2)) runs.push(summarizeRun(dir2, ent.name));
    }
  }
  runs.sort((a, b) => (b.createdAt || b.mtime || '') .localeCompare(a.createdAt || a.mtime || ''));
  return runs;
}

function summarizeRun(runDir, group) {
  const item = {
    runId: path.basename(runDir),
    dir: runDir,
    group,
    goal: '', status: 'unknown', createdAt: '', updatedAt: '',
    stats: null, error: null, mtime: 0,
  };
  try { item.mtime = fs.statSync(path.join(runDir, 'run.json')).mtimeMs; } catch { /* 无 run.json */ }
  const r = readJsonTolerant(path.join(runDir, 'run.json'));
  if (r.ok && r.data && typeof r.data === 'object') {
    item.goal = r.data.project_goal || '';
    item.status = r.data.status || 'unknown';
    item.createdAt = r.data.created_at || '';
    item.updatedAt = r.data.updated_at || '';
    item.stats = r.data.stats || null;
  } else {
    item.error = r.ok ? 'run.json 格式异常' : `run.json 读取失败：${r.error}`;
    item.createdAt = new Date(item.mtime || 0).toISOString();
  }
  return item;
}

function readRunSnapshot(runDir) {
  const runR = readJsonTolerant(path.join(runDir, 'run.json'));
  const tasksR = readJsonTolerant(path.join(runDir, 'tasks.json'));
  return {
    run: runR.ok ? runR.data : null,
    tasks: tasksR.ok ? (tasksR.data && tasksR.data.tasks) || [] : null,
    errors: {
      run: runR.ok ? null : `run.json：${runR.error}`,
      tasks: tasksR.ok ? null : `tasks.json：${tasksR.error}`,
    },
  };
}

// ---------- ledger 增量读取（字节偏移，只消费到最后一个换行） ----------

function readLedgerDelta(ledgerPath, offset) {
  let size = 0;
  try { size = fs.statSync(ledgerPath).size; } catch { return { offset, events: [] }; }
  if (size <= offset) return { offset, events: [] };
  const len = Math.min(size - offset, 8 * 1024 * 1024);
  const buf = Buffer.alloc(len);
  const fd = fs.openSync(ledgerPath, 'r');
  let bytesRead = 0;
  try { bytesRead = fs.readSync(fd, buf, 0, len, offset); } finally { fs.closeSync(fd); }
  const chunk = buf.slice(0, bytesRead);
  const lastNl = chunk.lastIndexOf(0x0a);
  if (lastNl === -1) return { offset, events: [] }; // 行未写完，下次再读
  const newOffset = offset + lastNl + 1;
  const events = [];
  let bad = 0;
  for (const line of chunk.slice(0, lastNl).toString('utf8').split('\n')) {
    const s = line.trim();
    if (!s) continue;
    try { events.push(JSON.parse(s)); } catch { bad += 1; }
  }
  return { offset: newOffset, events, bad };
}

function ledgerHistory(ledgerPath, maxLines) {
  let text = '';
  try { text = fs.readFileSync(ledgerPath, 'utf8'); } catch { return { offset: 0, events: [], skipped: 0 }; }
  const trimmed = text.endsWith('\n') ? text : text.slice(0, text.lastIndexOf('\n') + 1);
  const all = trimmed.split('\n').filter((l) => l.trim());
  const offset = Buffer.byteLength(trimmed, 'utf8');
  const slice = all.length > maxLines ? all.slice(all.length - maxLines) : all;
  const events = [];
  for (const line of slice) {
    try { events.push(JSON.parse(line)); } catch { /* 跳过坏行 */ }
  }
  return { offset, events, skipped: Math.max(0, all.length - slice.length) };
}

// ---------- CLI 子进程管理 ----------

const procs = new Map(); // key -> { key, kind, proc, runDir, runId, outputDir, startedAt, killed }
globalThis.__procs = procs; // selftest 检查存活进程用
let procSeq = 0;

function send(channel, payload) {
  if (win && !win.isDestroyed()) win.webContents.send(channel, payload);
}

// GUI 进程直接 spawn 解释器可能被安全软件/沙箱间歇性拦截（ENOENT 或启动后被杀），
// 所以：候选命令链逐个尝试，每条重试两次，并把子进程输出落盘便于诊断。
function spawnCli(kind, args, outputDir, extraEnv) {
  const key = `p${++procSeq}`;
  const rec = { key, kind, proc: null, live: false, runDir: null, runId: null, outputDir, startedAt: Date.now(), killed: false };
  procs.set(key, rec);

  const logDir = path.join(__dirname, 'logs');
  try { fs.mkdirSync(logDir, { recursive: true }); } catch { /* 忽略 */ }
  const logPath = path.join(logDir, `${key}-${kind}.log`);
  const logLine = (s) => { try { fs.appendFileSync(logPath, s + '\n'); } catch { /* 忽略 */ } };

  const candidates = [...new Set([settings.pythonCmd, 'python', 'python3'].filter(Boolean))];
  let ci = 0;
  let tries = 0;

  const tryNext = () => {
    if (rec.killed) { procs.delete(key); return; }
    if (ci >= candidates.length) {
      send('proc:error', { key, message: `无法启动 Python CLI（尝试过：${candidates.join('、')}）。请到设置里手动配置可用的 Python 命令。` });
      procs.delete(key);
      return;
    }
    const cmd = candidates[ci];
    const parts = cmd.split(/\s+/);
    let proc;
    try {
      proc = spawn(parts[0], [...parts.slice(1), 'main.py', ...args], {
        cwd: settings.repoRoot,
        windowsHide: true,
        stdio: ['ignore', 'pipe', 'pipe'],
        env: extraEnv ? Object.assign({}, process.env, extraEnv) : undefined,
      });
    } catch (e) {
      scheduleFallback(`spawn 抛错：${e.message}`);
      return;
    }
    rec.proc = proc;
    proc.stdout.on('data', (d) => {
      rec.live = true;
      const text = d.toString('utf8');
      logLine(`[out] ${text.trim()}`);
      send('proc:log', { key, stream: 'out', text });
    });
    proc.stderr.on('data', (d) => {
      rec.live = true;
      const text = d.toString('utf8');
      logLine(`[err] ${text.trim()}`);
      send('proc:log', { key, stream: 'err', text });
    });
    proc.on('error', (e) => {
      if (rec.live || rec.killed) return; // 已正常启动后的错误不再回退
      scheduleFallback(`spawn 失败：${e.message}`);
    });
    proc.on('exit', (code) => {
      logLine(`[exit] code=${code}`);
      discoverRunDir(rec, true);
      send('proc:exit', { key, code, meaning: EXIT_MEANINGS[code] || `退出码 ${code}` });
      setTimeout(() => { if (procs.get(key) === rec) procs.delete(key); }, 60_000);
    });
  };

  const scheduleFallback = (why) => {
    if (rec.killed) return;
    tries += 1;
    logLine(`[fallback] ${why}`);
    if (tries % 2 === 0) ci += 1; // 每条候选尝试两次后换下一条
    setTimeout(tryNext, 350);
  };

  tryNext();
  if (kind === 'run') discoverRunDir(rec, false); // resume 的 runDir 由调用方直接给定
  return { key };
}

// 在 outputDir 下发现本次新产生的 run 目录（启动时间之后新建）
function discoverRunDir(rec, once = false) {
  if (rec.runDir || !rec.outputDir) return;
  let names = [];
  try { names = fs.readdirSync(rec.outputDir, { withFileTypes: true }); } catch { names = []; }
  for (const ent of names) {
    if (!ent.isDirectory()) continue;
    const dir = path.join(rec.outputDir, ent.name);
    let st = null;
    try { st = fs.statSync(dir); } catch { continue; }
    if (st.mtimeMs + 1000 < rec.startedAt) continue;
    if (!fs.existsSync(path.join(dir, 'run.json'))) continue;
    rec.runDir = dir;
    rec.runId = ent.name;
    send('proc:run-dir', { key: rec.key, runDir: dir, runId: ent.name });
    return;
  }
  if (!once && !rec.poller) {
    discoverRunDir(rec, true);
    rec.poller = setInterval(() => {
      if (rec.runDir || rec.proc.exitCode !== null) { clearInterval(rec.poller); return; }
      discoverRunDir(rec, true);
    }, 400);
  }
}

function killProc(key) {
  const rec = procs.get(key);
  if (!rec) return false;
  if (rec.proc && rec.proc.exitCode !== null) return false;
  rec.killed = true;
  if (rec.proc) {
    try { rec.proc.kill(); } catch { /* 已退出 */ }
    // Windows 上（尤其 Store 版 Python 的 alias stub）kill 后可能收不到 exit 事件，
    // 或只杀掉 stub 而真进程还活着：5s 后仍无退出通知就按进程树强杀。
    const pid = rec.proc.pid;
    setTimeout(() => {
      if (!rec.proc || rec.proc.exitCode !== null || !pid) return;
      try {
        spawn('taskkill', ['/PID', String(pid), '/T', '/F'], { windowsHide: true, stdio: 'ignore' });
      } catch { /* 尽力而为 */ }
    }, 5000);
  }
  // Windows 下 kill 是硬终止，run.json 可能停留在 running：补一次离线 cancel 收尾
  if (rec.runDir) offlineCancel(rec.runDir);
  return true;
}

function offlineCancel(runDir, attempt) {
  try {
    const spec = pythonSpawnSpec(['cancel', path.basename(runDir), '--output-dir', path.dirname(runDir)]);
    const p = spawn(spec.cmd, spec.args, { cwd: settings.repoRoot, windowsHide: true, stdio: 'ignore' });
    p.on('error', () => {
      if ((attempt || 0) < 3) setTimeout(() => offlineCancel(runDir, (attempt || 0) + 1), 400);
    });
  } catch { /* 收尾失败不致命，UI 仍会刷新 */ }
}

function deleteRunSync(runDir) {
  // P0-4：调用 CLI delete --force（非交互环境），同步等待完成
  try {
    const spec = pythonSpawnSpec(['delete', path.basename(runDir), '--force',
      '--output-dir', path.dirname(runDir)]);
    const result = spawnSync(spec.cmd, spec.args, {
      cwd: settings.repoRoot, windowsHide: true, encoding: 'utf-8', timeout: 15000,
    });
    if (result.status !== 0) {
      throw new Error((result.stderr || result.stdout || '').trim() || `退出码 ${result.status}`);
    }
    return { ok: true };
  } catch (e) {
    throw new Error(String(e && e.message || e));
  }
}

// ---------- run 目录监视（ledger 增量 + run/tasks 快照 mtime 变化） ----------

const watchers = new Map(); // runDir -> { timer, offset, mtimeRun, mtimeTasks }

function mtimeOf(file) { try { return fs.statSync(file).mtimeMs; } catch { return -1; } }

function watchRun(runDir, tail) {
  // 已在监视的 run 重建监视器：渲染进程每次 selectRun 都会清空自己的事件缓存，
  // 重建才能把 tail 历史重发一遍（否则重复选中/切换回来时事件面板为空）。
  if (watchers.has(runDir)) unwatchRun(runDir);
  const ledgerPath = path.join(runDir, 'ledger.jsonl');
  let offset = 0;
  if (tail) {
    const h = ledgerHistory(ledgerPath, 400);
    offset = h.offset;
    if (h.events.length || h.skipped) {
      send('watch:ledger', { dir: runDir, offset, events: h.events, skipped: h.skipped, initial: true });
    }
  }
  const w = {
    offset,
    mtimeRun: mtimeOf(path.join(runDir, 'run.json')),
    mtimeTasks: mtimeOf(path.join(runDir, 'tasks.json')),
    timer: setInterval(() => {
      const delta = readLedgerDelta(ledgerPath, w.offset);
      if (delta.offset !== w.offset || delta.events.length) {
        w.offset = delta.offset;
        if (delta.events.length) send('watch:ledger', { dir: runDir, offset: w.offset, events: delta.events });
      }
      const mr = mtimeOf(path.join(runDir, 'run.json'));
      const mt = mtimeOf(path.join(runDir, 'tasks.json'));
      if (mr !== w.mtimeRun || mt !== w.mtimeTasks) {
        w.mtimeRun = mr; w.mtimeTasks = mt;
        send('watch:snapshot', { dir: runDir, ...readRunSnapshot(runDir) });
      }
    }, 500),
  };
  watchers.set(runDir, w);
  return { ok: true };
}

function unwatchRun(runDir) {
  const w = watchers.get(runDir);
  if (w) { clearInterval(w.timer); watchers.delete(runDir); }
}

// ---------- 产物 ----------

function listArtifacts(runDir) {
  const artDir = path.join(runDir, 'artifacts');
  const m = readJsonTolerant(path.join(artDir, 'MANIFEST.json'));
  const entries = [];
  if (m.ok && m.data && Array.isArray(m.data.entries)) {
    for (const e of m.data.entries) {
      const full = path.join(artDir, e.path || '');
      let exists = false; let size = e.size_bytes || 0;
      try { const st = fs.statSync(full); exists = true; size = st.size; } catch { /* 产物缺失 */ }
      entries.push({
        path: e.path, kind: e.kind || '', sizeBytes: size, exists,
        role: e.produced_by_role || '', task: e.produced_by_task || '',
      });
    }
  } else {
    // 无 MANIFEST：退化为目录遍历
    const walk = (rel) => {
      let ents = [];
      try { ents = fs.readdirSync(path.join(artDir, rel), { withFileTypes: true }); } catch { return; }
      for (const ent of ents) {
        const rel2 = rel ? `${rel}/${ent.name}` : ent.name;
        if (ent.isDirectory()) walk(rel2);
        else {
          let size = 0;
          try { size = fs.statSync(path.join(artDir, rel2)).size; } catch { /* 忽略 */ }
          entries.push({ path: rel2, kind: '', sizeBytes: size, exists: true, role: '', task: '' });
        }
      }
    };
    walk('');
  }
  return { entries, manifestError: m.ok ? null : `MANIFEST.json：${m.error}` };
}

function readArtifact(runDir, relPath) {
  const artDir = path.resolve(runDir, 'artifacts');
  const full = path.resolve(artDir, relPath);
  if (full !== artDir && !full.startsWith(artDir + path.sep)) throw new Error('非法的产物路径');
  const st = fs.statSync(full); // 不存在时抛出，由调用方兜底
  const MAX = 2 * 1024 * 1024;
  const ext = path.extname(relPath).toLowerCase();
  const base = path.basename(relPath);
  const isText = TEXT_EXTS.has(ext) || TEXT_BASENAMES.has(base);
  if (!isText) return { binary: true, sizeBytes: st.size, path: relPath };
  const truncated = st.size > MAX;
  const fd = fs.openSync(full, 'r');
  let buf;
  try {
    buf = Buffer.alloc(Math.min(st.size, MAX));
    fs.readSync(fd, buf, 0, buf.length, 0);
  } finally { fs.closeSync(fd); }
  return { binary: false, content: buf.toString('utf8'), sizeBytes: st.size, truncated, path: relPath };
}

// ---------- IPC ----------

function registerIpc() {
  ipcMain.handle('settings:get', () => {
    const repoOk = fs.existsSync(path.join(settings.repoRoot, 'main.py'));
    const outOk = fs.existsSync(settings.outputDir);
    return { settings, env: { repoOk, outOk, electron: process.versions.electron, node: process.versions.node } };
  });
  ipcMain.handle('settings:set', (_e, next) => saveSettings(next || {}));
  ipcMain.handle('python:detect', async (_e, candidate) => detectPython(candidate || null));

  ipcMain.handle('runs:list', () => scanRuns(settings.outputDir));
  ipcMain.handle('run:load', (_e, dir) => readRunSnapshot(String(dir)));
  ipcMain.handle('ledger:history', (_e, dir, maxLines) =>
    ledgerHistory(path.join(String(dir), 'ledger.jsonl'), Number(maxLines) || 400));

  ipcMain.handle('run:start', (_e, p) => {
    const outputDir = p.outputDir || settings.outputDir;
    const args = ['run'];
    if (p.demo) args.push('--demo');
    else args.push('--goal', String(p.goal || ''));
    if (p.mock) args.push('--mock');
    if (p.auto) args.push('--auto');
    // P0-3：成本预算硬上限
    if (p.maxCost && Number(p.maxCost) > 0) args.push('--max-cost', String(p.maxCost));
    if (p.maxLlmCalls && Number(p.maxLlmCalls) > 0) args.push('--max-llm-calls', String(p.maxLlmCalls));
    args.push('--no-live', '--output-dir', outputDir);
    const r = spawnCli('run', args, outputDir, p.env || null);
    if (r.error) throw new Error(r.error);
    return { key: r.key };
  });

  ipcMain.handle('run:resume', (_e, p) => {
    const runDir = String(p.runDir);
    const args = ['resume', path.basename(runDir)];
    if (p.mock) args.push('--mock');
    if (p.auto) args.push('--auto');
    args.push('--no-live', '--output-dir', path.dirname(runDir));
    const r = spawnCli('resume', args, path.dirname(runDir));
    if (r.error) throw new Error(r.error);
    const rec = procs.get(r.key);
    rec.runDir = runDir;
    rec.runId = path.basename(runDir);
    send('proc:run-dir', { key: r.key, runDir, runId: rec.runId });
    return { key: r.key };
  });

  ipcMain.handle('run:cancel', (_e, p) => ({ killed: killProc(String(p.key)) }));
  ipcMain.handle('run:cancelOffline', (_e, p) => { offlineCancel(String(p.runDir)); return { ok: true }; });
  ipcMain.handle('run:delete', (_e, p) => deleteRunSync(String(p.runDir)));
  ipcMain.handle('procs:list', () => [...procs.values()]
    .filter((r) => r.proc && r.proc.exitCode === null)
    .map((r) => ({ key: r.key, kind: r.kind, runDir: r.runDir, runId: r.runId })));

  ipcMain.handle('watch:start', (_e, dir, tail) => watchRun(String(dir), !!tail));
  ipcMain.handle('watch:stop', (_e, dir) => { unwatchRun(String(dir)); return { ok: true }; });

  ipcMain.handle('artifacts:list', (_e, dir) => listArtifacts(String(dir)));
  ipcMain.handle('artifact:read', (_e, dir, relPath) => readArtifact(String(dir), String(relPath)));

  ipcMain.handle('artifacts:export', async (_e, dir) => {
    const runDir = String(dir);
    const artDir = path.join(runDir, 'artifacts');
    const defaultName = `${path.basename(path.dirname(runDir))}-${path.basename(runDir)}-artifacts.zip`;
    const r = await dialog.showSaveDialog(win, {
      title: '导出产物',
      defaultPath: defaultName,
      filters: [{ name: 'ZIP 压缩包', extensions: ['zip'] }],
    });
    if (r.canceled || !r.filePath) return { canceled: true };
    const zip = buildZipFromDir(artDir, 'artifacts');
    fs.writeFileSync(r.filePath, zip);
    shell.showItemInFolder(r.filePath);
    return { saved: r.filePath, size: zip.length };
  });

  ipcMain.handle('shell:showItemInFolder', (_e, p) => { shell.showItemInFolder(String(p.fullPath)); return { ok: true }; });
  ipcMain.handle('shell:openPath', (_e, p) => shell.openPath(String(p.fullPath)));
  ipcMain.handle('app:relaunch', () => { app.relaunch(); app.quit(); });

  // ---------- 真实模式操作：模型切换 / 任务操作 / 门禁审批 ----------
  // 直接修改 run.json / tasks.json，适用于非 running 状态的 run；running 状态拒绝。

  function writeJsonAtomic(file, data) {
    const tmp = file + '.tmp';
    fs.writeFileSync(tmp, JSON.stringify(data, null, 2), 'utf8');
    fs.renameSync(tmp, file);
  }

  ipcMain.handle('model:switch', (_e, p) => {
    const runDir = String(p.runDir);
    const alias = String(p.alias || '');
    if (!alias) throw new Error('alias 不能为空');
    const runFile = path.join(runDir, 'run.json');
    const r = readJsonTolerant(runFile);
    if (!r.ok) throw new Error('run.json 读取失败：' + r.error);
    if (r.data.status === 'running') throw new Error('运行中无法切换模型，请先暂停');
    if (!r.data.config_snapshot) r.data.config_snapshot = {};
    r.data.config_snapshot.default_model = alias;
    r.data.model = alias;
    r.data.updated_at = new Date().toISOString();
    writeJsonAtomic(runFile, r.data);
    return { ok: true, alias };
  });

  ipcMain.handle('task:action', (_e, p) => {
    const runDir = String(p.runDir);
    const taskId = String(p.taskId);
    const action = String(p.action); // retry | skip | manual
    if (!['retry', 'skip', 'manual'].includes(action)) throw new Error('未知操作：' + action);
    const tasksFile = path.join(runDir, 'tasks.json');
    const t = readJsonTolerant(tasksFile);
    if (!t.ok) throw new Error('tasks.json 读取失败：' + t.error);
    const tasks = (t.data && t.data.tasks) || [];
    const task = tasks.find(x => x.id === taskId);
    if (!task) throw new Error('任务不存在：' + taskId);
    if (action === 'retry') {
      task.status = 'ready';
      task.attempts = (task.attempts || 0) + 1;
      task.error = null;
    } else if (action === 'skip') {
      task.status = 'skipped';
    } else if (action === 'manual') {
      task.status = 'manual';
    }
    writeJsonAtomic(tasksFile, t.data);
    return { ok: true, taskId, action, status: task.status };
  });

  ipcMain.handle('gate:action', (_e, p) => {
    const runDir = String(p.runDir);
    const layer = String(p.layer); // G1 | G2 | G3
    const action = String(p.action); // approve | reject
    if (!['approve', 'reject'].includes(action)) throw new Error('未知操作：' + action);
    const runFile = path.join(runDir, 'run.json');
    const r = readJsonTolerant(runFile);
    if (!r.ok) throw new Error('run.json 读取失败：' + r.error);
    if (r.data.status === 'running') throw new Error('运行中无法直接操作门禁，请通过 CLI');
    if (!r.data.gate_results) r.data.gate_results = {};
    r.data.gate_results[layer] = {
      verdict: action === 'approve' ? 'approve' : 'reject',
      manually: true,
      at: new Date().toISOString(),
    };
    r.data.updated_at = new Date().toISOString();
    writeJsonAtomic(runFile, r.data);
    return { ok: true, layer, action };
  });
}

// ---------- 窗口 ----------

function createWindow() {
  win = new BrowserWindow({
    width: 1440,
    height: 900,
    minWidth: 1080,
    minHeight: 680,
    backgroundColor: '#0d1117',
    title: 'AgentCrew Studio',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      backgroundThrottling: false, // 实时监控面板：后台/遮挡时也要持续刷新与推送
    },
  });
  win.removeMenu();
  Menu.setApplicationMenu(Menu.buildFromTemplate([
    {
      label: '文件',
      submenu: [{ role: 'quit', label: '退出' }],
    },
    {
      label: '查看',
      submenu: [
        { role: 'reload', label: '重新加载' },
        { role: 'forceReload', label: '强制重新加载' },
        { role: 'toggleDevTools', label: '开发者工具' },
        { type: 'separator' },
        { role: 'resetZoom', label: '重置缩放' },
        { role: 'zoomIn', label: '放大' },
        { role: 'zoomOut', label: '缩小' },
        { role: 'togglefullscreen', label: '全屏' },
      ],
    },
    {
      label: '帮助',
      submenu: [{
        label: '关于 AgentCrew Studio',
        click: () => dialog.showMessageBox(win, {
          type: 'info',
          title: '关于',
          message: 'AgentCrew Studio',
          detail: `多智能体协作平台桌面 GUI\nElectron ${process.versions.electron} / Node ${process.versions.node}\n后端：python main.py（CLI + 文件，零耦合）`,
        }),
      }],
    },
  ]));

  win.loadFile('index.html');
  if (process.env.STUDIO_DEV) win.webContents.openDevTools({ mode: 'detach' });

  win.on('close', (e) => {
    const alive = [...procs.values()].filter((r) => r.proc && r.proc.exitCode === null && !r.killed);
    if (!alive.length || win.__forceClose) return;
    e.preventDefault();
    const choice = dialog.showMessageBoxSync(win, {
      type: 'warning',
      title: '有运行尚未结束',
      message: `有 ${alive.length} 个后端运行仍在进行`,
      detail: '关闭窗口将终止这些 CLI 进程；不终止则窗口保持打开。',
      buttons: ['终止进程并关闭', '保持窗口打开'],
      defaultId: 1,
      cancelId: 1,
    });
    if (choice === 0) {
      alive.forEach((r) => { r.killed = true; try { r.proc.kill(); } catch { /* 已退出 */ } });
      win.__forceClose = true;
      win.close();
    }
  });
}

const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  app.on('second-instance', () => { if (win) { if (win.isMinimized()) win.restore(); win.focus(); } });

  app.whenReady().then(() => {
    loadSettings();
    registerIpc();
    createWindow();
    if (IS_SELFTEST) {
      try { require('./selftest').run(win); }
      catch (e) { console.error('selftest 启动失败:', e); app.exit(1); }
    }
  });
  app.on('window-all-closed', () => {
    for (const w of watchers.values()) clearInterval(w.timer);
    watchers.clear();
    for (const r of procs.values()) {
      r.killed = true;
      if (r.proc && r.proc.exitCode === null) { try { r.proc.kill(); } catch { /* 已退出 */ } }
    }
    app.quit();
  });
  app.on('before-quit', () => {
    for (const r of procs.values()) {
      r.killed = true;
      if (r.proc && r.proc.exitCode === null) { try { r.proc.kill(); } catch { /* 已退出 */ } }
    }
  });
}
