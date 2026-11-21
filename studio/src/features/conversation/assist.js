/**
 * 对话页的辅助层：A3 guidance 对账、A5 输入图片缩略图、A1 新对话空态。
 *
 * 独立于 index.js 的渲染管线（那里只认 buildMessages 的结果），刷新走
 * studio:refresh 事件由 boot 解耦转发，避免模块互相依赖成环。
 */
import { apiFetch } from '../../lib/api.js';
import { h } from '../../lib/dom.js';
import { trunc } from '../../lib/format.js';
import { currentRunId, normalizeGuidanceItem } from '../../lib/guidance.js';
import { S } from '../../lib/store.js';

function refreshSoon() {
  window.dispatchEvent(new Event('studio:refresh'));
}

/* ============================================================
   A3：guidance 附加层（乐观插入 + 4s 对账）
   ------------------------------------------------------------
   ledger 只记 id 级审计事件，指引正文在 guidance.jsonl 里。发送
   成功先乐观插入 user 气泡（lib/messages.js 渲染为指引气泡），再由
   低频轮询与 GET 列表对账：pending→consumed（chip 变「已被 T-xxx
   吸收」）。POST 超时的 local- 项保留 12s 宽限期，没出现在服务端
   列表就丢弃——at-most-once，前端永不自动重发（方案 §3.2）。
   ============================================================ */
const GUIDANCE_POLL_MS = 4000;
const GUIDANCE_LOCAL_TTL_MS = 12000;
let guidanceTimer = null;

function guidanceEligible() {
  return !!(S.realMode && S.settings && S.settings.apiBaseUrl);
}

/** composer 发送成功后调用：立即插入气泡并启动对账。 */
function appendGuidanceLocal(item) {
  const list = S.convGuidance || (S.convGuidance = []);
  list.push({
    id: String((item && item.id) || ''),
    text: String((item && item.text) || ''),
    ts: '',                       // 未知时刻 → 排到时间线末尾，对账后归位
    author: 'studio',
    target: (item && item.target) || null,
    status: 'pending',
    optimistic: !!(item && item.optimistic),
    createdAt: Date.now(),
  });
  ensureGuidancePoll();
  refreshSoon();
}

async function reconcileGuidance() {
  if (!guidanceEligible()) return;
  const ph = document.getElementById('placeholder-view');
  if (S.nav !== 'conversation' || !ph || ph.classList.contains('hidden')) return;
  const run = S.dash && S.dash.run;
  if (!run) return;
  const rid = currentRunId(run, S.watchDir);
  if (!rid) return;
  let items = [];
  try {
    const payload = await apiFetch(`/runs/${encodeURIComponent(rid)}/guidance`);
    items = ((payload && Array.isArray(payload.items)) ? payload.items : [])
      .map((g) => normalizeGuidanceItem(g)).filter((g) => g.id);
  } catch { /* 对账失败静默：下一轮再试，别打扰对话 */ }
  const serverIds = new Set(items.map((g) => g.id));
  const now = Date.now();
  const locals = (S.convGuidance || []).filter((g) => String(g.id || '').startsWith('local-')
    && (now - (g.createdAt || 0)) < GUIDANCE_LOCAL_TTL_MS
    && !serverIds.has(g.id));
  const next = items.concat(locals);
  const before = (S.convGuidance || []).map((g) => `${g.id}:${g.status}:${g.consumedTask || ''}`).join('|');
  const after = next.map((g) => `${g.id}:${g.status}:${g.consumedTask || ''}`).join('|');
  S.convGuidance = next;
  if (before !== after) refreshSoon();
}

/** 低频对账：run 活跃或还有未落地的乐观项时才发请求。 */
function ensureGuidancePoll() {
  if (guidanceTimer) return;
  guidanceTimer = setInterval(() => {
    const run = S.dash && S.dash.run;
    const active = !!run && ['running', 'awaiting_approval', 'planning', 'preparing'].includes(String(run.status));
    const hasLocal = (S.convGuidance || []).some((g) => String(g.id || '').startsWith('local-'));
    if (active || hasLocal) void reconcileGuidance();
  }, GUIDANCE_POLL_MS);
}

/* ============================================================
   A5：随 run 输入图片（<runDir>/context/images/）缩略图
   ============================================================ */
let imagesLoadedFor = '';

async function loadRunImages(run) {
  const dir = run && run.dir;
  if (!dir || !S.realMode) return;
  const key = String(dir);
  if (imagesLoadedFor === key) return;
  imagesLoadedFor = key;
  try {
    const list = ((await window.studio.listRunImages(dir)) || []).slice(0, 4);
    const src = {};
    for (const img of list) {
      try {
        const r = await window.studio.readRunImage(dir, img.name);
        if (r && r.dataBase64) src[img.name] = `data:${r.mime};base64,${r.dataBase64}`;
      } catch { /* 单张失败只少一张缩略图 */ }
    }
    S.convImages = { dir: key, src, names: list.map((i) => i.name) };
    refreshSoon();
  } catch { /* 无图 / 目录不可读：不出缩略图 */ }
}

/* ============================================================
   A1：新对话空态（模板 chips + 焦点落输入框）
   ============================================================ */
const NEW_CHAT_TEMPLATES = [
  '开发一个短链接生成服务，含 REST API、缓存与部署文档',
  '为现有项目做一次代码评审并输出改进清单',
  '调研一项技术选型并输出对比报告',
  '写一份产品 PRD：目标用户、核心流程与验收标准',
];

function fillComposer(text) {
  const el = document.getElementById('cmp-input');
  if (!el) return;
  el.value = text;
  el.dispatchEvent(new Event('input'));
  el.focus();
  try { el.setSelectionRange(el.value.length, el.value.length); } catch { /* 忽略 */ }
}

function renderNewChatState(container) {
  container.innerHTML = '';
  delete container.dataset.convRun;
  container.appendChild(h('div', { class: 'page-head' },
    h('div', {},
      h('h2', {}, '新对话'),
      h('div', { class: 'plan-goal' }, '在下方输入目标直接开始；也可以选一个模板填入输入框'))));
  const chips = h('div', { class: 'new-chat-chips' });
  container.appendChild(h('div', { class: 'conv-wrap new-chat-wrap' }, chips));
  (async () => {
    let recents = [];
    try { recents = ((await window.studio.recentGoals()) || []).slice(0, 3); } catch { /* 无历史 */ }
    const dedup = new Set();
    for (const label of [...recents, ...NEW_CHAT_TEMPLATES]) {
      const text = String(label || '').trim();
      if (!text || dedup.has(text)) continue;
      dedup.add(text);
      chips.appendChild(h('button', {
        class: 'new-chat-chip', type: 'button', title: text,
        onclick: () => fillComposer(text),
      }, trunc(text, 44)));
    }
    if (!chips.childElementCount) chips.remove();
  })();
  requestAnimationFrame(() => { const el = document.getElementById('cmp-input'); if (el) el.focus(); });
}

export {
  appendGuidanceLocal,
  ensureGuidancePoll,
  loadRunImages,
  renderNewChatState,
};
