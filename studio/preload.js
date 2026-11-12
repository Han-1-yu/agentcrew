'use strict';
// 预加载脚本：通过 contextBridge 暴露最小 IPC 面，渲染进程不直接接触 Node。
const { contextBridge, ipcRenderer } = require('electron');

const invoke = (channel, ...args) => ipcRenderer.invoke(channel, ...args);

// C-22：主进程 → 渲染端的推送频道白名单。
// 与 main.js 的 send(...) 调用点、backend-api.js 的 send(...) 一一对应；
// 新增推送频道时必须同步加到这里，否则渲染端收不到（静默失败）。
const ALLOWED_EVENTS = new Set([
  'theme:changed',
  'proc:error',
  'proc:log',
  'proc:exit',
  'proc:run-dir',
  'watch:ledger',
  'watch:snapshot',
  'sse:frame',
  'sse:closed',
]);

contextBridge.exposeInMainWorld('studio', {
  // 设置与探测
  getSettings: () => invoke('settings:get'),
  setSettings: (next) => invoke('settings:set', next),
  detectPython: (candidate) => invoke('python:detect', candidate),
  agentsConfig: () => invoke('agents:config'),
  agentsExport: () => invoke('agents:export'),
  // 定时任务（Automations）
  scheduleList: () => invoke('schedule:list'),
  scheduleAdd: (p) => invoke('schedule:add', p),
  scheduleRemove: (p) => invoke('schedule:remove', p),
  scheduleTick: () => invoke('schedule:tick'),
  // 运行列表 / 快照 / 事件
  listRuns: () => invoke('runs:list'),
  loadRun: (dir) => invoke('run:load', dir),
  ledgerHistory: (dir, maxLines) => invoke('ledger:history', dir, maxLines),
  // 子进程
  startRun: (p) => invoke('run:start', p),
  resumeRun: (p) => invoke('run:resume', p),
  cancelRun: (key) => invoke('run:cancel', { key }),
  cancelOffline: (runDir) => invoke('run:cancelOffline', { runDir }),
  deleteRun: (runDir) => invoke('run:delete', { runDir }),
  listProcs: () => invoke('procs:list'),
  // 监视
  watchRun: (dir, tail) => invoke('watch:start', dir, tail),
  unwatchRun: (dir) => invoke('watch:stop', dir),
  // 产物
  listArtifacts: (dir) => invoke('artifacts:list', dir),
  readArtifact: (dir, relPath) => invoke('artifact:read', dir, relPath),
  exportArtifacts: (dir) => invoke('artifacts:export', dir),
  loadStreams: (dir) => invoke('streams:load', dir),
  // 系统
  revealFile: (fullPath) => invoke('shell:showItemInFolder', { fullPath }),
  openPath: (fullPath) => invoke('shell:openPath', { fullPath }),
  // 在系统浏览器中打开外部 https 链接（如获取 API Key 页面）
  openExternal: (url) => invoke('shell:openExternal', { url }),
  // 真实模式操作
  switchModel: (runDir, alias) => invoke('model:switch', { runDir, alias }),
  taskAction: (runDir, taskId, action) => invoke('task:action', { runDir, taskId, action }),
  gateAction: (runDir, layer, action) => invoke('gate:action', { runDir, layer, action }),
  // F3：供应商管理（只回布尔/连通性，不回传 Key 值）
  providerKeyStatus: (envName) => invoke('provider:keyStatus', envName),
  // 保存 API Key 到项目根目录 .env 并注入主进程环境（值单向传入，不回传）
  providerSaveKey: (opts) => invoke('provider:saveKey', opts),
  providerTest: (opts) => invoke('provider:test', opts),
  // 实时拉取供应商可用模型列表 / 批量端点测速（Key 仅在主进程读取）
  providerFetchModels: (opts) => invoke('provider:fetchModels', opts),
  providerTestEndpoints: (opts) => invoke('provider:testEndpoints', opts),
  // 后端 API 代理（主进程绕开 CORS；Key 只存主进程 safeStorage，不回传）
  apiRequest: (path, options) => invoke('api:request', { path, options }),
  // 无凭据连通性探测（测试连接专用，绝不携带 API Key）
  apiProbe: (baseUrl) => invoke('api:probe', { baseUrl }),
  apiStreamStart: (runId) => invoke('api:stream:start', { runId }),
  apiStreamStop: () => invoke('api:stream:stop'),
  apiKeyStatus: () => invoke('api:keyStatus'),
  apiKeySave: (value) => invoke('api:keySave', { value }),
  apiKeyClear: () => invoke('api:keyClear'),
  // F4：最近目标
  recentGoals: () => invoke('goals:recent'),
  // 主进程推送
  // C-22：频道白名单。此前 on(channel) 接受任意字符串，渲染端一旦被注入
  // 就能监听主进程内部任意频道（信息面扩大）。白名单与 main.js / backend-api.js
  // 的 send() 调用点一一对应，新增推送频道必须同步加到这里。
  on: (channel, cb) => {
    if (!ALLOWED_EVENTS.has(channel) || typeof cb !== 'function') return () => {};
    const wrapped = (_e, payload) => { try { cb(payload); } catch (err) { console.error('回调异常', channel, err); } };
    ipcRenderer.on(channel, wrapped);
    return () => ipcRenderer.removeListener(channel, wrapped);
  },
});
