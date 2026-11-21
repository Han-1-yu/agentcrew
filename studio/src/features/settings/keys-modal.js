/**
 * Q1：多密钥管理弹窗（R25 RBAC 的前端入口）。
 *
 * 端点：GET /keys（清单 + 角色/作用域说明）、POST /keys（新建，明文**只回
 * 一次**）、DELETE /keys/{id|name}（吊销，幂等）。后端未启用多密钥（单
 * legacy key）时返回 503，弹窗如实提示——不伪造列表。
 * 入口：设置 → 连接 的「多密钥管理」按钮；设置页的单 Key 快捷输入保留。
 */
import { apiFetch } from '../../lib/api.js';
import { copyText } from '../../lib/clipboard.js';
import { h } from '../../lib/dom.js';

import { closeModal, showModal } from '../../ui/modal.js';
import { toast } from '../../ui/toast.js';

function explain(err) {
  return String((err && (err.detail || err.message)) || err || '未知错误');
}

function fmtTime(iso) {
  const t = Date.parse(String(iso || ''));
  return Number.isFinite(t) ? new Date(t).toLocaleString() : '—';
}

/** 新建表单：name + 角色（scopes 跟随后端 ROLE_SCOPES，不在此覆写）。 */
function buildCreateForm(roles, onDone) {
  const nameInput = h('input', {
    id: 'keys-name', class: 'nr-input', type: 'text', placeholder: '密钥名称（如 ci-bot）',
    'aria-label': '密钥名称',
  });
  const roleSel = h('select', { id: 'keys-role', class: 'nr-select', 'aria-label': '角色' });
  for (const [role, scopes] of Object.entries(roles || {})) {
    roleSel.appendChild(h('option', { value: role }, `${role}（${(scopes || []).join(', ') || '无作用域'}）`));
  }
  const result = h('div', { class: 'keys-created' });
  const createBtn = h('button', {
    class: 'btn btn-primary btn-sm', type: 'button',
    onclick: async () => {
      createBtn.disabled = true;
      try {
        const r = await apiFetch('/keys', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name: nameInput.value.trim(), role: roleSel.value, scopes: [] }),
          timeoutMs: 15000,
        });
        const plaintext = String((r && r.key) || '');
        const copyBtn = h('button', {
          class: 'btn btn-sm', type: 'button',
          onclick: async () => {
            const ok = await copyText(plaintext);
            toast(ok ? 'success' : 'error', ok ? '已复制' : '复制失败', ok ? '密钥明文已进剪贴板' : '剪贴板不可用');
          },
        }, '复制');
        result.replaceChildren(h('div', { class: 'keys-created-box' },
          h('div', { class: 'keys-created-warn' }, String((r && r.notice) || '密钥明文只显示这一次，请立即保存')),
          h('div', { class: 'keys-created-plain mono' }, plaintext),
          copyBtn));
        toast('success', '密钥已创建', String(((r && r.record) || {}).name || ''));
        if (typeof onDone === 'function') onDone();
      } catch (err) {
        toast('error', '创建失败', explain(err));
      } finally { createBtn.disabled = false; }
    },
  }, '新建密钥');
  return h('div', { class: 'keys-create' },
    h('div', { class: 'nr-row' },
      h('label', { class: 'nr-label', for: 'keys-name' }, '名称'), nameInput),
    h('div', { class: 'nr-row' },
      h('label', { class: 'nr-label', for: 'keys-role' }, '角色（决定作用域）'), roleSel),
    h('div', { class: 'keys-create-actions' }, createBtn),
    result);
}

async function renderList(box, onDone) {
  box.replaceChildren(h('div', { class: 'flow-validate-pending' }, '正在读取密钥清单…'));
  let payload;
  try {
    payload = await apiFetch('/keys');
  } catch (err) {
    box.replaceChildren(h('div', { class: 'flow-validate-err' }, `密钥清单不可读：${explain(err)}`));
    return;
  }
  const keys = (payload && payload.keys) || [];
  if (!keys.length) {
    box.replaceChildren(h('div', { class: 'notif-empty' }, '密钥库为空（只有服务端自身的 legacy 单密钥）。'));
    return;
  }
  const rows = keys.map((k) => {
    const revoked = !!k.revoked_at;
    return h('tr', {},
      h('td', {}, String(k.name || '')),
      h('td', { class: 'mono' }, String(k.id || '')),
      h('td', {}, String(k.role || '')),
      h('td', {}, String((k.scopes || []).join(', ') || '—')),
      h('td', {}, fmtTime(k.created_at)),
      h('td', {}, revoked ? h('span', { class: 'keys-badge off' }, '已吊销') : h('span', { class: 'keys-badge on' }, '有效')),
      revoked ? h('td', {}) : h('td', {},
        h('button', {
          class: 'btn btn-sm btn-danger', type: 'button',
          onclick: async () => {
            try {
              await apiFetch(`/keys/${encodeURIComponent(k.id || k.name)}`, { method: 'DELETE' });
              toast('success', '已吊销', String(k.name || k.id));
              await renderList(box, onDone);
            } catch (err) { toast('error', '吊销失败', explain(err)); }
          },
        }, '吊销')));
  });
  box.replaceChildren(h('table', { class: 'flow-node-table keys-table' },
    h('thead', {}, h('tr', {},
      h('th', {}, '名称'), h('th', {}, 'ID'), h('th', {}, '角色'),
      h('th', {}, '作用域'), h('th', {}, '创建于'), h('th', {}, '状态'), h('th', {}, ''))),
    h('tbody', {}, ...rows)));
}

function openKeysModal() {
  const listBox = h('div', { class: 'keys-list' });
  let roles = {};
  const reloadList = () => { void renderList(listBox, null); };
  showModal(h('div', { class: 'keys-dialog' },
    h('h3', { class: 'modal-title' }, 'API Key 管理（多密钥）'),
    h('div', { class: 'confirm-text', style: 'color:var(--text-ter)' },
      '服务端密钥库（RBAC）里的密钥：按角色授予作用域；吊销立即生效。明文只在创建时返回一次。'),
    listBox,
    h('h4', { class: 'keys-subtitle' }, '新建密钥'),
    buildCreateForm(roles, reloadList),
    h('div', { class: 'modal-actions' },
      h('button', { class: 'btn', type: 'button', onclick: closeModal }, '关闭'))));
  // 先拿清单（含角色表），角色下拉需要 roles 数据
  void (async () => {
    try {
      const payload = await apiFetch('/keys');
      roles = (payload && payload.roles) || {};
    } catch { /* 503 等：列表区已显示错误 */ }
    await renderList(listBox, null);
    // roles 到位后重建表单
    const formHost = document.querySelector('.keys-dialog');
    if (formHost) {
      const old = formHost.querySelector('.keys-create');
      if (old) old.replaceWith(buildCreateForm(roles, reloadList));
    }
  })();
}

export { openKeysModal };
