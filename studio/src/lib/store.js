/**
 * 全局状态 S、localStorage 键表、以及给自测用的 window.__st 遥测。
 * 约定：S 只被 app/ 下的装配代码整体替换（S.dash = ...），视图层只读；
 * 需要跨会话记住的东西统一走 STORE_KEYS，别在各处散写字符串。
 */
import { EVENT_TYPES } from '../contracts/enums.js';

//: localStorage 键名（前缀统一 agentcrew:，避免与主进程设置混用）
const STORE_KEYS = {
  theme: 'agentcrew:theme',
  lastRunDir: 'agentcrew:lastRunDir',
  lastPage: 'agentcrew:lastPage',
  dagView: 'agentcrew:dagView',
  nrMock: 'agentcrew:nrMock',
};

const S = {
  settings: null, env: null,
  dash: null,            // { run, tasks, agents, gates, events }
  nav: 'dashboard',
  detail: null,          // { kind:'task'|'agent', id }
  eventFilter: new Set(EVENT_TYPES),
  eventExpanded: new Set(),
  simTimer: null,
  startedAt: Date.now(),
  view: { zoom: 1, x: 0, y: 0 },   // DAG 视图变换
  runFilter: null,                  // 项目筛选
  sseConnected: false,              // SSE 连接状态
};

// 自测遥测（selftest 依赖，必须保留）
window.__st = {
  snapshots: 0, ledgerBatches: 0,
  state: () => {
    const d = S.dash || {};
    return {
      dir: (d.run && d.run.dir) || null,
      status: (d.run && d.run.status) || 'idle',
      stats: (d.run && d.run.stats) || null,
      tasks: (d.tasks || []).length,
      events: (d.events || []).length,
    };
  },
};

// 记住「上次看的是哪个 run / 哪一页」，下次启动直接回到现场
function persistState() {
  try {
    if (S.watchDir) localStorage.setItem(STORE_KEYS.lastRunDir, S.watchDir);
    if (S.nav) localStorage.setItem(STORE_KEYS.lastPage, S.nav);
  } catch {}
}

export { S, STORE_KEYS, persistState };
