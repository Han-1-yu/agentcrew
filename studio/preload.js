'use strict';
// 预加载脚本：通过 contextBridge 暴露最小 IPC 面，渲染进程不直接接触 Node。
const { contextBridge, ipcRenderer } = require('electron');

const invoke = (channel, ...args) => ipcRenderer.invoke(channel, ...args);

contextBridge.exposeInMainWorld('studio', {
  // 设置与探测
  getSettings: () => invoke('settings:get'),
  setSettings: (next) => invoke('settings:set', next),
  detectPython: () => invoke('python:detect'),
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
  providerTest: (opts) => invoke('provider:test', opts),
  // F4：最近目标
  recentGoals: () => invoke('goals:recent'),
  // 主进程推送
  on: (channel, cb) => {
    const wrapped = (_e, payload) => { try { cb(payload); } catch (err) { console.error('回调异常', channel, err); } };
    ipcRenderer.on(channel, wrapped);
    return () => ipcRenderer.removeListener(channel, wrapped);
  },
});
