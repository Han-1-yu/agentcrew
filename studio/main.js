'use strict';
// AgentCrew Studio — Electron 主进程。
// 职责：窗口/设置/Python 探测/run 扫描与读取/CLI 子进程管理/ledger 偏移轮询/IPC。
// 只读后端产物文件与调用 CLI，不依赖 agentcrew/ 内部实现。

const { app, BrowserWindow, ipcMain, dialog, shell, Menu, nativeTheme, net } = require('electron');
const path = require('path');
const fs = require('fs');
const { spawn } = require('child_process');
const { buildZipFromDir } = require('./zip');
const { createBackendApi } = require('./backend-api');

const IS_SELFTEST = process.argv.includes('--selftest');
const IS_E2E = process.argv.includes('--e2e');
const IS_PROBE = process.argv.includes('--probe');

// 测试进程（--selftest / --e2e）使用独立 userData profile：
// 测试会走真实弹窗启动 mock run、切换主题，若与用户共用 profile，
// 会把「上次模式选择=Mock」(localStorage)、测试目标(recentGoals)、主题等写进用户配置。
// 每次先清空 test-profile 保证用例从干净状态开始；独立 profile 也不与在用的 Studio 抢单实例锁。
if (IS_SELFTEST || IS_E2E) {
  const testProfile = path.join(app.getPath('userData'), 'test-profile');
  try { fs.rmSync(testProfile, { recursive: true, force: true }); } catch { /* 忽略 */ }
  app.setPath('userData', testProfile);
}

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
let backendApi = null;
const settingsPath = () => path.join(app.getPath('userData'), 'settings.json');

function defaultSettings() {
  return {
    repoRoot: DEFAULT_REPO_ROOT,
    outputDir: path.join(DEFAULT_REPO_ROOT, 'output'),
    pythonCmd: '',
    theme: 'light',
    defaultMock: false,  // 默认真实模式：mock 产物是固定夹具，与目标无关
    defaultAuto: true,
    apiBaseUrl: '',  // P3：后端 API 地址，设置后用 SSE 替代文件 watch
    defaultModel: '',   // F2：默认模型别名 "<provider>:<model>"
    maxCostUsd: 0,     // F2：默认预算上限 USD，>0 时自动传给 CLI
    recentGoals: [],   // F4：最近目标历史（最多 5 条）
  };
}

function loadSettings() {
  const s = defaultSettings();
  try {
    // 兼容记事本等编辑器写入的 UTF-8 BOM：有 BOM 时 JSON.parse 抛错会导致全部设置静默回退默认
    let raw = fs.readFileSync(settingsPath(), 'utf8');
    if (raw.charCodeAt(0) === 0xFEFF) raw = raw.slice(1);
    Object.assign(s, JSON.parse(raw));
    // 迁移：旧字段 pythonCommand → 标准字段 pythonCmd
    if (!s.pythonCmd && s.pythonCommand) {
      // raw 是文件内容字符串，取 .pythonCommand 恒为 undefined；旧字段在解析后的 s 上。
      s.pythonCmd = s.pythonCommand;
      delete s.pythonCommand;
      try { fs.writeFileSync(settingsPath(), JSON.stringify(s, null, 2), 'utf8'); } catch { /* 非致命 */ }
    }
  } catch { /* 首次启动或文件损坏：用默认值 */ }
  settings = s;
  return s;
}

function saveSettings(next) {
  // C-22：字段白名单。此前 Object.assign({}, settings, next) 接受渲染端传来的
  // 任意键——虽然 repoRoot/outputDir 本就是可配项，但多出来的键会被原样落盘，
  // 一旦渲染端被注入即可往设置文件里塞任意字段（如伪造 apiBaseUrl 把请求
  // 引到外部）。只收 defaultSettings() 声明过的键。
  const allowed = new Set(Object.keys(defaultSettings()));
  const patch = {};
  if (next && typeof next === 'object') {
    for (const [k, v] of Object.entries(next)) {
      if (allowed.has(k)) patch[k] = v;
    }
  }
  settings = Object.assign({}, settings, patch);
  fs.mkdirSync(path.dirname(settingsPath()), { recursive: true });
  fs.writeFileSync(settingsPath(), JSON.stringify(settings, null, 2), 'utf8');
  // 仅当本次变更涉及主题时才推送 theme:changed（recentGoals/默认模型/预算等无关变更不必打扰渲染端）
  if (Object.prototype.hasOwnProperty.call(patch, 'theme')) pushThemeChanged();
  return settings;
}

// 把用户选择的 theme（light/dark/system）解析为实际生效的 light|dark
function resolveEffectiveTheme(mode) {
  if (mode === 'light') return 'light';
  if (mode === 'dark') return 'dark';
  // system：跟随操作系统
  try { return nativeTheme.shouldUseDarkColors ? 'dark' : 'light'; }
  catch { return 'light'; }
}

// 向渲染进程推送当前主题态（仅在窗口存在时）
function pushThemeChanged() {
  if (!win || win.isDestroyed()) return;
  const mode = (settings && settings.theme) || 'light';
  win.webContents.send('theme:changed', { mode, effective: resolveEffectiveTheme(mode) });
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

/**
 * C-20：异步跑一次 CLI 子进程，替代 ``spawnSync``。
 *
 * 同步版会阻塞主进程事件循环——删除一个产物多的 run、或 schedule 列表遇到慢盘，
 * 整个窗口（含 SSE 推送、文件监听回调）最长冻结 15 秒。改成 spawn + Promise 后
 * 主进程始终可响应，IPC handler 直接返回 Promise（``ipcMain.handle`` 原生支持）。
 *
 * 返回结构统一为 ``{ status, stdout, stderr }``：``status=-1`` 表示启动失败或超时。
 */
function runCli(args, { timeoutMs = 15000 } = {}) {
  return new Promise((resolve) => {
    let spec;
    try {
      spec = pythonSpawnSpec(args);
    } catch (e) {
      resolve({ status: -1, stdout: '', stderr: String((e && e.message) || e) });
      return;
    }
    const child = spawn(spec.cmd, spec.args, { cwd: settings.repoRoot, windowsHide: true });
    let stdout = '';
    let stderr = '';
    let settled = false;
    const finish = (status, extra) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve({ status, stdout, stderr: stderr + (extra || '') });
    };
    const timer = setTimeout(() => {
      try { child.kill(); } catch { /* 已退出 */ }
      finish(-1, `\n超时（${timeoutMs}ms）`);
    }, timeoutMs);
    child.stdout?.on('data', (buf) => { stdout += buf.toString('utf-8'); });
    child.stderr?.on('data', (buf) => { stderr += buf.toString('utf-8'); });
    child.on('error', (e) => finish(-1, String((e && e.message) || e)));
    child.on('close', (code) => finish(code === null ? -1 : code));
  });
}

// 跑 `main.py schedule ...` 并返回 stdout（只校验退出码，不解析 JSON）。
async function runSchedule(extraArgs, outputDir) {
  const args = ['schedule', ...extraArgs, '--output-dir', outputDir || settings.outputDir];
  const result = await runCli(args);
  if (result.status !== 0) {
    throw new Error((result.stderr || result.stdout || '').trim() || `退出码 ${result.status}`);
  }
  return result.stdout || '';
}

// 跑 `main.py schedule ... --json`，从 stdout 里截取 JSON 对象（容错缩进多行输出）。
async function runScheduleJson(extraArgs, outputDir) {
  const out = await runSchedule(extraArgs, outputDir);
  const start = out.indexOf('{');
  const end = out.lastIndexOf('}');
  if (start < 0 || end <= start) throw new Error('无 JSON 输出');
  return JSON.parse(out.slice(start, end + 1));
}

// 定时任务到期检查：每分钟 tick 一次，到期日程由后端起 run 子进程。
function startScheduleTicker() {
  if (scheduleTicker) return;
  scheduleTicker = setInterval(() => {
    // C-20：异步化后必须接住 rejection，否则变成 unhandledRejection。
    runSchedule(['tick', '--json']).catch(() => { /* 到点触发失败不致命，下轮再试 */ });
  }, 60 * 1000);
  scheduleTicker.unref && scheduleTicker.unref();
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
  // C-1：混用 ISO 字符串与 mtime 数字调 localeCompare 会抛 TypeError
  // （Number.prototype 没有 localeCompare），导致 runs:list 整体 reject、
  // 列表/下拉/启动轮询全废。统一转数值比较，缺失 createdAt 时用 mtime。
  const sortKey = (item) => {
    if (item.createdAt) {
      const ts = Date.parse(item.createdAt);
      if (!Number.isNaN(ts)) return ts;
    }
    return Number(item.mtime) || 0;
  };
  runs.sort((a, b) => sortKey(b) - sortKey(a));
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
    // C-1：created_at 缺失时用 run.json 的 mtime 补一个 ISO 串，避免
    // 排序/展示拿到空值（旧 run 或手改过的 run.json 常见）。
    item.createdAt = r.data.created_at
      || (item.mtime ? new Date(item.mtime).toISOString() : '');
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
let scheduleTicker = null;

function send(channel, payload) {
  if (win && !win.isDestroyed()) win.webContents.send(channel, payload);
}

// IPC 参数兜底：渲染端理论上都传对象，但误传 undefined/null 时不该让主进程
// 抛 TypeError（invoke 会 reject 成渲染端的「未处理异常」提示）。
const objArg = (v) => (v && typeof v === 'object' && !Array.isArray(v) ? v : {});

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
      // 保留尾部若干行：秒退（如参数错误）时渲染端凭它提示原因
      rec.stderrTail = ((rec.stderrTail || '') + text).split('\n').slice(-8).join('\n');
      send('proc:log', { key, stream: 'err', text });
    });
    proc.on('error', (e) => {
      if (rec.live || rec.killed) return; // 已正常启动后的错误不再回退
      scheduleFallback(`spawn 失败：${e.message}`);
    });
    proc.on('exit', (code) => {
      logLine(`[exit] code=${code}`);
      discoverRunDir(rec, true);
      send('proc:exit', {
        key, code,
        meaning: EXIT_MEANINGS[code] || `退出码 ${code}`,
        hadRunDir: !!rec.runDir,
        killed: !!rec.killed,
        stderrTail: (rec.stderrTail || '').slice(-400),
      });
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
      if (rec.runDir || !rec.proc || rec.proc.exitCode !== null) { clearInterval(rec.poller); return; }
      discoverRunDir(rec, true);
    }, 400);
  }
}

function killProc(key) {
  const rec = procs.get(key);
  if (!rec) return false;
  if (rec.proc && rec.proc.exitCode !== null) return false;
  rec.killed = true;
  // kill 之后 Windows 上可能收不到 exit 事件，目录发现 poller 会永久残留。
  if (rec.poller) { clearInterval(rec.poller); rec.poller = null; }
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

async function deleteRun(runDir) {
  // P0-4：调用 CLI delete --force（非交互环境）；C-20 起改为异步，删除大 run
  // 时不再冻结主进程（产物目录可能有上千文件，同步等待会卡住整个窗口）。
  const result = await runCli(['delete', path.basename(runDir), '--force',
    '--output-dir', path.dirname(runDir)]);
  if (result.status !== 0) {
    throw new Error((result.stderr || result.stdout || '').trim() || `退出码 ${result.status}`);
  }
  return { ok: true };
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
  if (!isText) {
    // R61：图像产物（kind=image）回 base64 供渲染端预览；超限仍按二进制处理。
    const mime = IMAGE_MIME_BY_EXT[ext];
    if (mime && st.size <= MAX) {
      const imgBuf = fs.readFileSync(full);
      return { binary: true, image: true, mime,
               dataBase64: imgBuf.toString('base64'),
               sizeBytes: st.size, path: relPath };
    }
    return { binary: true, sizeBytes: st.size, path: relPath };
  }
  const truncated = st.size > MAX;
  const fd = fs.openSync(full, 'r');
  let buf;
  try {
    buf = Buffer.alloc(Math.min(st.size, MAX));
    fs.readSync(fd, buf, 0, buf.length, 0);
  } finally { fs.closeSync(fd); }
  return { binary: false, content: buf.toString('utf8'), sizeBytes: st.size, truncated, path: relPath };
}

// C4：随 run 输入图片（<runDir>/context/images/）的预览读取。
// 文件名白名单 <NN>.<ext>，杜绝路径穿越；平台只写这个目录，名字形状固定。
const IMAGE_MIME_BY_EXT = {
  '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
  '.webp': 'image/webp', '.gif': 'image/gif',
};

function listRunImages(runDir) {
  const imgDir = path.resolve(runDir, 'context', 'images');
  const out = [];
  let ents = [];
  try { ents = fs.readdirSync(imgDir, { withFileTypes: true }); } catch { return out; }
  for (const ent of ents) {
    if (!ent.isFile()) continue;
    const mime = IMAGE_MIME_BY_EXT[path.extname(ent.name).toLowerCase()];
    if (!mime) continue;
    let sizeBytes = 0;
    try { sizeBytes = fs.statSync(path.join(imgDir, ent.name)).size; } catch { continue; }
    out.push({ name: ent.name, mime, sizeBytes });
  }
  return out.sort((a, b) => a.name.localeCompare(b.name));
}

function readRunImage(runDir, name) {
  const imgDir = path.resolve(runDir, 'context', 'images');
  if (!/^\d{2}\.(png|jpe?g|webp|gif)$/i.test(String(name))) {
    throw new Error('非法的图片文件名');
  }
  const full = path.resolve(imgDir, String(name));
  if (full !== imgDir && !full.startsWith(imgDir + path.sep)) {
    throw new Error('非法的图片路径');
  }
  const mime = IMAGE_MIME_BY_EXT[path.extname(String(name)).toLowerCase()];
  const buf = fs.readFileSync(full);
  return { name: String(name), mime, sizeBytes: buf.length, dataBase64: buf.toString('base64') };
}

// 流式正文回溯：读 streams/ 下所有 <task_id>.md，返回 { taskId: 正文 }。
// 正文是模型输出、属不可信文本，渲染端须用 textContent/pre 消费（已保证）。
function loadStreams(runDir) {
  const streamsDir = path.join(runDir, 'streams');
  const streams = {};
  let ents = [];
  try { ents = fs.readdirSync(streamsDir); } catch { return streams; }
  for (const name of ents) {
    if (!name.endsWith('.md')) continue;
    const taskId = name.slice(0, -3);
    try {
      streams[taskId] = fs.readFileSync(path.join(streamsDir, name), 'utf8');
    } catch { /* 单文件读失败只跳过，不影响其它 */ }
  }
  return streams;
}

// ---------- 供应商模型列表拉取（借鉴 cc-switch model-fetch） ----------

// 为 base_url 生成 /models 候选地址并按序尝试：
// 1) 直接拼 /models；2) 未带版本号时补 /v1/models（如 Anthropic）；
// 3) 剥离 /anthropic、/compatible-mode 等兼容子路径后兜底。
function buildModelsUrlCandidates(baseUrl, family) {
  const b = String(baseUrl).replace(/\/+$/, '');
  const cands = [];
  const push = (u) => { if (u && !cands.includes(u)) cands.push(u); };

  push(b + '/models');
  const hasVersionSegment = /\/v\d+(?:\.\d+)?(?:beta)?(?:\/|$)/.test(b) || /\/openai(?:\/|$)/.test(b);
  if (!hasVersionSegment) push(b + '/v1/models');
  // 兼容子路径兜底：.../anthropic → .../models、.../anthropic/v1/models
  const compat = b.replace(/\/(anthropic|claude|compatible-mode)(\/v\d+(?:\.\d+)?)?$/i, '');
  if (compat !== b) { push(compat.replace(/\/+$/, '') + '/v1/models'); push(compat.replace(/\/+$/, '') + '/models'); }
  return cands;
}

// 解析三种常见模型列表响应格式
function parseModelsPayload(json) {
  if (!json || typeof json !== 'object') return null;
  // OpenAI / Anthropic / 智谱 v4：{ data: [{ id, owned_by }] }
  if (Array.isArray(json.data)) {
    const out = json.data.map((d) => ({
      id: String(d.id || d.slug || d.name || '').trim(),
      owned_by: d.owned_by || d.ownedBy || null,
    })).filter(x => x.id);
    return out.length ? out : null;
  }
  // 智谱 Responses 旧端点 / Cohere：{ models: [{ slug | name | id }] }
  if (Array.isArray(json.models)) {
    const out = json.models.map((d) => (typeof d === 'string' ? { id: d, owned_by: null } : {
      id: String(d.slug || d.id || d.name || '').trim(),
      owned_by: d.owned_by || d.ownedBy || d.provider || null,
    })).filter(x => x.id);
    return out.length ? out : null;
  }
  return null;
}

// ---------- IPC ----------

function registerIpc() {
  // 后端 HTTP/SSE 统一由主进程代理：绕开 CORS、凭据只留主进程（见 backend-api.js）。
  backendApi = createBackendApi({
    ipcMain,
    userDataPath: app.getPath('userData'),
    getSettings: () => settings,
    getWin: () => win,
  });

  // 角色绑定矩阵：直接问 CLI（与执行期同一套解析），主进程不解析 YAML
  ipcMain.handle('agents:config', async () => {
    try {
      const spec = pythonSpawnSpec(['agents', '--json']);
      const stdout = await new Promise((resolve, reject) => {
        const child = spawn(spec.cmd, spec.args, { cwd: settings.repoRoot, windowsHide: true });
        let out = '', err = '';
        child.stdout.on('data', (d) => { out += d.toString('utf8'); });
        child.stderr.on('data', (d) => { err += d.toString('utf8'); });
        child.on('error', reject);
        child.on('close', (code) => (code === 0 ? resolve(out)
          : reject(new Error(err || `main.py agents 退出码 ${code}`))));
      });
      const line = stdout.trim().split(/\r?\n/).filter(Boolean).pop() || '{}';
      return { ok: true, data: JSON.parse(line) };
    } catch (e) {
      return { ok: false, error: String((e && e.message) || e) };
    }
  });

  // 导出执行体档案 YAML 片段（可粘贴进 agentcrew.yaml 复用）
  ipcMain.handle('agents:export', async () => {
    // C-20：异步执行，避免解析大型 YAML 时冻结主进程（最长 15s）。
    const result = await runCli(['agents', '--yaml']);
    if (result.status !== 0) {
      return { ok: false, error: (result.stderr || result.stdout || '').trim() || `退出码 ${result.status}` };
    }
    return { ok: true, yaml: result.stdout };
  });

  // ── 定时任务（Automations）──
  ipcMain.handle('schedule:list', async () => {
    try { return { ok: true, data: await runScheduleJson(['list', '--json']) }; }
    catch (e) { return { ok: false, error: String((e && e.message) || e) }; }
  });
  ipcMain.handle('schedule:add', async (_e, arg) => {
    const p = objArg(arg);
    const a = ['add', '--goal', String(p.goal || '')];
    if (p.mock) a.push('--mock');
    if (p.mode) a.push('--mode', String(p.mode));
    if (p.daily) { a.push('--daily', String(p.daily)); if (p.weekdays) a.push('--weekdays', String(p.weekdays)); }
    else if (p.at) a.push('--at', String(p.at));
    try { await runSchedule(a, p.outputDir || null); return { ok: true }; }
    catch (e) { return { ok: false, error: String((e && e.message) || e) }; }
  });
  ipcMain.handle('schedule:remove', async (_e, arg) => {
    const p = objArg(arg);
    try { await runSchedule(['remove', String(p.id || '')], p.outputDir || null); return { ok: true }; }
    catch (e) { return { ok: false, error: String((e && e.message) || e) }; }
  });
  ipcMain.handle('schedule:tick', async () => {
    try { return { ok: true, data: await runScheduleJson(['tick', '--json']) }; }
    catch (e) { return { ok: false, error: String((e && e.message) || e) }; }
  });

  ipcMain.handle('settings:get', () => {
    const repoOk = fs.existsSync(path.join(settings.repoRoot, 'main.py'));
    const outOk = fs.existsSync(settings.outputDir);
    return {
      settings,
      effectiveTheme: resolveEffectiveTheme(settings.theme),
      apiKeyConfigured: !!(backendApi && backendApi.isKeyConfigured()),
      env: { repoOk, outOk, electron: process.versions.electron, node: process.versions.node },
    };
  });
  ipcMain.handle('settings:set', (_e, next) => saveSettings(next || {}));
  ipcMain.handle('python:detect', async (_e, candidate) => detectPython(candidate || null));

  ipcMain.handle('runs:list', () => scanRuns(settings.outputDir));
  ipcMain.handle('run:load', (_e, dir) => readRunSnapshot(String(dir)));
  ipcMain.handle('ledger:history', (_e, dir, maxLines) =>
    ledgerHistory(path.join(String(dir), 'ledger.jsonl'), Number(maxLines) || 400));

  ipcMain.handle('run:start', (_e, arg) => {
    const p = objArg(arg);
    let outputDir = p.outputDir || settings.outputDir;
    // P3：多项目/工作区 — 指定 project 时在 outputDir/project/ 下创建 run
    if (p.project) {
      const safeName = String(p.project).replace(/[<>:"/\\|?*\x00-\x1f]/g, '_').trim();
      if (safeName) {
        outputDir = path.join(outputDir, safeName);
        try { fs.mkdirSync(outputDir, { recursive: true }); } catch { /* 忽略 */ }
      }
    }
    const args = ['run'];
    if (p.demo) args.push('--demo');
    else args.push('--goal', String(p.goal || ''));
    if (p.mock) args.push('--mock');
    // 执行模式（对齐 WorkBuddy 的 Agent / Plan / Ask）：agent=自动执行、plan=计划审查
    // （挂起审批，批准后执行）、ask=只读规划（只生成计划，可 resume 续跑）。
    const mode = p.mode || (p.auto === false ? 'plan' : 'agent');
    if (mode === 'ask') args.push('--plan-only');
    else if (mode === 'plan') args.push('--gate', 'plan', '--approval-mode', 'deferred');
    else args.push('--auto');
    // B1：模型选择 —— 弹窗显式选择优先，否则回落设置页默认模型（形如 openai:gpt-4o）。
    // CLI 参数名为 --default-model（main.py _add_run_flags）；传 --model 会被 argparse 拒绝（退出码 2）
    // Mock 模式下不调用真实模型，--default-model 不参与 CLI 参数
    const chosenModel = (p.model && String(p.model).trim()) || (settings.defaultModel && String(settings.defaultModel).trim());
    if (chosenModel && !p.mock) args.push('--default-model', chosenModel);
    // P0-3：成本预算硬上限 —— 弹窗显式优先，否则回落设置页 maxCostUsd
    const chosenMaxCost = (p.maxCost && Number(p.maxCost) > 0) ? Number(p.maxCost)
      : (settings.maxCostUsd && Number(settings.maxCostUsd) > 0 ? Number(settings.maxCostUsd) : 0);
    if (chosenMaxCost > 0) args.push('--max-cost', String(chosenMaxCost));
    if (p.maxLlmCalls && Number(p.maxLlmCalls) > 0) args.push('--max-llm-calls', String(p.maxLlmCalls));
    args.push('--no-live', '--output-dir', outputDir);
    // spawnCli 恒返回 { key }；启动失败由 proc:error / proc:exit 推送，
    // 原来这里的 if (r.error) 是永远不触发的死分支。
    const r = spawnCli('run', args, outputDir, p.env || null);
    // F4：记录最近目标（去重、最多 5 条、非 demo）
    if (!p.demo && p.goal && String(p.goal).trim()) {
      try {
        const goal = String(p.goal).trim();
        const list = (Array.isArray(settings.recentGoals) ? settings.recentGoals : []).filter(g => g !== goal);
        list.unshift(goal);
        if (list.length > 5) list.length = 5;
        saveSettings({ recentGoals: list });
      } catch { /* 非致命 */ }
    }
    return { key: r.key };
  });

  ipcMain.handle('run:resume', (_e, arg) => {
    const p = objArg(arg);
    const runDir = String(p.runDir || '');
    const args = ['resume', path.basename(runDir)];
    if (p.mock) args.push('--mock');
    if (p.auto) args.push('--auto');
    args.push('--no-live', '--output-dir', path.dirname(runDir));
    // spawnCli 恒返回 { key }；启动失败由 proc:error 推送，没有 r.error。
    const r = spawnCli('resume', args, path.dirname(runDir));
    // 启动秒退时 spawnCli 的 fallback 会 procs.delete(key)，这里拿不到记录；
    // 直接解引用会抛 TypeError。
    const rec = procs.get(r.key);
    if (rec) {
      rec.runDir = runDir;
      rec.runId = path.basename(runDir);
      send('proc:run-dir', { key: r.key, runDir, runId: rec.runId });
    }
    return { key: r.key };
  });

  ipcMain.handle('run:cancel', (_e, p) => ({ killed: killProc(String(objArg(p).key)) }));
  ipcMain.handle('run:cancelOffline', (_e, p) => { offlineCancel(String(objArg(p).runDir)); return { ok: true }; });
  // C-20：删除大 run（产物上千文件）不再阻塞主进程。
  ipcMain.handle('run:delete', async (_e, p) => deleteRun(String(objArg(p).runDir)));
  ipcMain.handle('procs:list', () => [...procs.values()]
    .filter((r) => r.proc && r.proc.exitCode === null)
    .map((r) => ({ key: r.key, kind: r.kind, runDir: r.runDir, runId: r.runId })));

  ipcMain.handle('watch:start', (_e, dir, tail) => watchRun(String(dir), !!tail));
  ipcMain.handle('watch:stop', (_e, dir) => { unwatchRun(String(dir)); return { ok: true }; });

  ipcMain.handle('artifacts:list', (_e, dir) => listArtifacts(String(dir)));
  ipcMain.handle('artifact:read', (_e, dir, relPath) => readArtifact(String(dir), String(relPath)));
  ipcMain.handle('images:list', (_e, dir) => listRunImages(String(dir)));
  ipcMain.handle('images:read', (_e, dir, name) => readRunImage(String(dir), String(name)));
  ipcMain.handle('streams:load', (_e, dir) => loadStreams(String(dir)));

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

  ipcMain.handle('shell:showItemInFolder', (_e, p) => { shell.showItemInFolder(String(objArg(p).fullPath)); return { ok: true }; });
  ipcMain.handle('shell:openPath', (_e, p) => {
    // C-22：只允许打开 outputDir 之下的路径。此前渲染端传任意 fullPath
    // 都会交给 shell.openPath（等于用一个渲染端可控的参数触发系统级打开，
    // 可指向任意本地文件/目录）。
    const target = String(objArg(p).fullPath || '');
    const root = path.resolve(settings.outputDir || '');
    const resolved = path.resolve(target);
    const rel = path.relative(root, resolved);
    if (!target || rel.startsWith('..') || path.isAbsolute(rel)) {
      throw new Error('只允许打开输出目录内的文件');
    }
    return shell.openPath(resolved);
  });
  // 在系统浏览器中打开外部 URL（如获取 API Key 的官方页面）；仅放行 http/https，避免任意协议
  ipcMain.handle('shell:openExternal', async (_e, p) => {
    const url = String(objArg(p).url || '');
    if (!/^https?:\/\//i.test(url)) { throw new Error('仅支持 http/https 链接'); }
    await shell.openExternal(url);
    return { ok: true };
  });
  ipcMain.handle('app:relaunch', () => { app.relaunch(); app.quit(); });

  // ---------- 真实模式操作：模型切换 / 任务操作 / 门禁审批 ----------
  // 直接修改 run.json / tasks.json，适用于非 running 状态的 run；running 状态拒绝。

  function writeJsonAtomic(file, data) {
    const tmp = file + '.tmp';
    fs.writeFileSync(tmp, JSON.stringify(data, null, 2), 'utf8');
    fs.renameSync(tmp, file);
  }

  ipcMain.handle('model:switch', (_e, arg) => {
    const p = objArg(arg);
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

  ipcMain.handle('task:action', (_e, arg) => {
    const p = objArg(arg);
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

  ipcMain.handle('gate:action', (_e, arg) => {
    const p = objArg(arg);
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

  // ---------- F3：供应商管理 ----------
  // 只读环境变量是否存在，绝不返回值本身
  ipcMain.handle('provider:keyStatus', (_e, envName) => {
    const name = String(envName || '').trim();
    if (!name) return { configured: false, note: '无需密钥' };
    const v = process.env[name];
    return { configured: !!(v && String(v).trim()) };
  });

  // 保存 API Key：写入项目根目录 .env（后端 config.py load_dotenv 的标准读取位置），
  // 同时注入主进程 process.env —— keyStatus / 在线拉取立即生效，CLI 子进程继承。
  // .env 已被 .gitignore 忽略；Key 值只落盘本机，绝不回传渲染进程。
  ipcMain.handle('provider:saveKey', (_e, opts) => {
    // C-21：这里**明文**写入项目根 `.env`，是有意为之——Studio 是本机开发工具，
    // `.env` 与 CLI 共用同一份，加密后 CLI 就读不到了。README「设计边界」已如实
    // 声明这个边界。在不改变存储方式的前提下能做的两条防线：
    //   1) 严格校验环境变量名（防写入任意键 / 注入别的配置项）；
    //   2) 文件权限收到 0600（Linux/macOS 下同机其他用户不可读；Windows 由 ACL 决定）。
    const o = objArg(opts);
    const envName = String(o.env_name || o.envName || '').trim();
    const value = String(o.value == null ? '' : o.value).trim();
    if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(envName)) throw new Error(`环境变量名非法：${envName}`);
    if (!value) throw new Error('Key 值不能为空');
    if (/\r?\n/.test(value)) throw new Error('Key 值不能包含换行');
    const envPath = path.join(settings.repoRoot, '.env');
    let lines = [];
    try { lines = fs.readFileSync(envPath, 'utf8').split(/\r?\n/); } catch { /* .env 不存在：新建 */ }
    const probe = new RegExp(`^${envName}\\s*=`);
    let replaced = false;
    for (let i = 0; i < lines.length; i++) {
      if (probe.test(lines[i])) { lines[i] = `${envName}=${value}`; replaced = true; break; }
    }
    if (!replaced) {
      if (lines.length && lines[lines.length - 1].trim() !== '') lines.push('');
      lines.push(`${envName}=${value}`);
    }
    try { fs.mkdirSync(settings.repoRoot, { recursive: true }); } catch { /* 已存在 */ }
    fs.writeFileSync(envPath, lines.join('\n') + '\n', { encoding: 'utf8', mode: 0o600 });
    // mode 只在创建时生效；已存在的 .env 显式收紧权限（Windows 上无实际效果，不报错）。
    try { fs.chmodSync(envPath, 0o600); } catch { /* 平台不支持或不归本进程管 */ }
    process.env[envName] = value;
    return { ok: true, env_name: envName, replaced, path: envPath };
  });

  // 用 Electron net.fetch 测试 provider 连通性（主进程天然绕开渲染进程 CORS）
  // C-17：渲染端传入的地址只认 http(s)（此前无 scheme 校验，file:// 等会被
  // net.fetch 直接请求）。内网地址刻意放行：本地 ollama / 局域网网关是合法
  // 场景，凭据发给用户自己配置的 provider 也是该功能本意。
  const providerUrlError = (url) => {
    const s = String(url == null ? '' : url).trim();
    if (!s) return '缺少端点地址';
    if (!/^https?:\/\//i.test(s)) return '端点地址仅支持 http/https';
    return null;
  };

  ipcMain.handle('provider:test', async (_e, opts) => {
    const o = objArg(opts);
    const base_url = o.base_url == null ? null : String(o.base_url);
    if (!base_url) return { ok: false, latency_ms: 0, status: 0, error: '该 provider 无 HTTP 端点（mock/cli）' };
    const urlErr = providerUrlError(base_url);
    if (urlErr) return { ok: false, latency_ms: 0, status: 0, error: urlErr };
    const models_path = o.models_path ? String(o.models_path) : '/models';
    const url = base_url.replace(/\/+$/, '') + (models_path.startsWith('/') ? models_path : '/' + models_path);
    const ac = new AbortController();
    const timer = setTimeout(() => { try { ac.abort(); } catch {} }, 10000);
    const t0 = Date.now();
    try {
      const res = await net.fetch(url, { method: 'GET', signal: ac.signal, redirect: 'follow' });
      return { ok: res.ok, latency_ms: Date.now() - t0, status: res.status, error: res.ok ? null : `HTTP ${res.status}` };
    } catch (e) {
      const msg = String(e && e.message || e);
      const aborted = /abort/i.test(msg);
      return { ok: false, latency_ms: Date.now() - t0, status: 0, error: aborted ? '连接超时（10s）' : msg };
    } finally {
      clearTimeout(timer);
    }
  });

  // 从供应商实时拉取可用模型列表（借鉴 cc-switch model-fetch）。
  // Key 只在主进程从环境变量读取，绝不回传渲染进程。
  // 兼容三种响应：OpenAI/Anthropic/智谱v4 {data:[{id,owned_by}]}、智谱Responses/Cohere {models:[{slug|name}]}。
  ipcMain.handle('provider:fetchModels', async (_e, opts) => {
    const o = objArg(opts);
    const baseUrl = o.base_url == null ? '' : String(o.base_url).trim();
    if (!baseUrl) return { ok: false, models: [], error: '该 provider 无 HTTP 端点（mock/cli）' };
    const urlErr = providerUrlError(baseUrl);
    if (urlErr) return { ok: false, models: [], error: urlErr };
    const family = String(o.family || 'openai_compat');
    const apiKey = o.api_key_env ? (process.env[String(o.api_key_env)] || '').trim() : '';

    const candidates = buildModelsUrlCandidates(baseUrl, family);
    let lastErr = '所有候选端点均不可用';
    let lastStatus = 0;
    for (const url of candidates) {
      const headers = {};
      if (apiKey) {
        if (family === 'anthropic') {
          headers['x-api-key'] = apiKey;
          headers['anthropic-version'] = '2023-06-01';
        } else {
          headers['Authorization'] = 'Bearer ' + apiKey;
        }
      }
      const ac = new AbortController();
      const timer = setTimeout(() => { try { ac.abort(); } catch {} }, 10000);
      try {
        const res = await net.fetch(url, { method: 'GET', signal: ac.signal, redirect: 'follow', headers });
        if (!res.ok) { lastErr = `HTTP ${res.status}`; lastStatus = res.status; continue; }
        const json = await res.json();
        const models = parseModelsPayload(json);
        if (!models) { lastErr = '响应格式无法解析（供应商可能未开放模型列表接口）'; lastStatus = res.status; continue; }
        return { ok: true, models, url, status: res.status, authed: !!apiKey };
      } catch (e) {
        const msg = String(e && e.message || e);
        lastErr = /abort/i.test(msg) ? '连接超时（10s）' : msg;
      } finally {
        clearTimeout(timer);
      }
    }
    return { ok: false, models: [], status: lastStatus, error: lastErr };
  });

  // 批量端点测速（借鉴 cc-switch EndpointSpeedTest），返回每个 URL 的延迟与状态
  // C-17：数量上限 + 分批并发 + 逐个 scheme 校验——此前渲染端可一次发起
  // 无上限的并发请求，且 file:// 等非法 scheme 不经检查直接进 net.fetch。
  ipcMain.handle('provider:testEndpoints', async (_e, opts) => {
    const o = objArg(opts);
    const MAX_ENDPOINTS = 20;
    const BATCH = 5;
    const rawUrls = Array.isArray(o.urls) ? o.urls.map(u => String(u)).filter(Boolean) : [];
    if (!rawUrls.length) return { ok: false, results: [], error: '无候选端点' };
    if (rawUrls.length > MAX_ENDPOINTS) {
      return { ok: false, results: [], error: `候选端点过多（最多 ${MAX_ENDPOINTS} 个）` };
    }
    const bad = rawUrls.find(u => providerUrlError(u));
    if (bad) return { ok: false, results: [], error: `${providerUrlError(bad)}：${bad}` };
    const urls = rawUrls;
    const results = [];
    for (let i = 0; i < urls.length; i += BATCH) {
      const batch = urls.slice(i, i + BATCH);
      const batchResults = await Promise.all(batch.map(async (url) => {
        const ac = new AbortController();
        const timer = setTimeout(() => { try { ac.abort(); } catch {} }, 8000);
        const t0 = Date.now();
        try {
          const res = await net.fetch(url, { method: 'GET', signal: ac.signal, redirect: 'follow' });
          return { url, ok: res.ok, latency_ms: Date.now() - t0, status: res.status, error: res.ok ? null : `HTTP ${res.status}` };
        } catch (e) {
          const msg = String(e && e.message || e);
          return { url, ok: false, latency_ms: Date.now() - t0, status: 0, error: /abort/i.test(msg) ? '超时' : msg };
        } finally {
          clearTimeout(timer);
        }
      }));
      results.push(...batchResults);
    }
    return { ok: true, results };
  });

  // F4：最近目标列表（也可通过 settings:get 读取）
  ipcMain.handle('goals:recent', () => (Array.isArray(settings.recentGoals) ? settings.recentGoals : []));
}

// ---------- 窗口 ----------

function createWindow() {
  win = new BrowserWindow({
    width: 1440,
    height: 900,
    minWidth: 1080,
    minHeight: 680,
    backgroundColor: '#070B16',
    title: 'AgentCrew Studio',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      // C-22：显式开启沙箱（渲染进程跑在 OS 级沙箱内）。preload 只用
      // contextBridge + ipcRenderer，不依赖 Node 原生能力，可安全开启。
      sandbox: true,
      webSecurity: true,
      backgroundThrottling: false, // 实时监控面板：后台/遮挡时也要持续刷新与推送
    },
  });
  // C-22：窗口打开守卫。渲染端任何 target="_blank" / window.open（例如把
  // 任务说明里的外链当按钮点）此前会在 Electron 里新开一个无 preload 的窗口
  // 直接渲染外部站点；站内 iframe 也没有导航限制。
  win.webContents.setWindowOpenHandler(({ url }) => {
    // 只允许 http(s) 交给系统浏览器，其余（file:、javascript:、data:）一律拒绝。
    try {
      const u = new URL(url);
      if (u.protocol === 'http:' || u.protocol === 'https:') void shell.openExternal(url);
    } catch { /* 非法 URL：静默拒绝 */ }
    return { action: 'deny' };
  });
  win.webContents.on('will-navigate', (event, url) => {
    // 只允许留在应用自身页面（file:// 下的 index.html）；跨源导航一律拦下。
    const target = String(url || '');
    if (!target.startsWith('file://')) {
      event.preventDefault();
      try {
        const u = new URL(target);
        if (u.protocol === 'http:' || u.protocol === 'https:') void shell.openExternal(target);
      } catch { /* 忽略 */ }
    }
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
    // 定时任务：每分钟到期检查（非 selftest/e2e 探针环境，避免测试抖动）
    if (!IS_SELFTEST && !IS_E2E) startScheduleTicker();
    // system 模式：监听操作系统明暗主题变化，变化时推送给渲染进程
    nativeTheme.on('updated', () => {
      if (settings && settings.theme === 'system') pushThemeChanged();
    });
    if (IS_SELFTEST) {
      try { require('./selftest').run(win); }
      catch (e) { console.error('selftest 启动失败:', e); app.exit(1); }
    }
    if (IS_E2E) {
      try { require('./e2e').run(win); }
      catch (e) { console.error('e2e 启动失败:', e); app.exit(1); }
    }
    if (IS_PROBE) {
      try { require('./probe-edge').run(win); }
      catch (e) { console.error('probe 启动失败:', e); app.exit(1); }
    }
  });
  app.on('window-all-closed', () => {
    for (const w of watchers.values()) clearInterval(w.timer);
    watchers.clear();
    for (const r of procs.values()) {
      r.killed = true;
      if (r.poller) { clearInterval(r.poller); r.poller = null; }
      if (r.proc && r.proc.exitCode === null) { try { r.proc.kill(); } catch { /* 已退出 */ } }
    }
    app.quit();
  });
  app.on('before-quit', () => {
    if (backendApi) backendApi.stopStream();
    for (const r of procs.values()) {
      r.killed = true;
      if (r.poller) { clearInterval(r.poller); r.poller = null; }
      if (r.proc && r.proc.exitCode === null) { try { r.proc.kill(); } catch { /* 已退出 */ } }
    }
  });
}
