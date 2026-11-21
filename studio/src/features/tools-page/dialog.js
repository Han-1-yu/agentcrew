/**
 * Q2：单工具试跑弹窗（JSON 入参 → POST /tools/{name}/test → 结果面板）。
 *
 * 三条诚实边界：
 * 1. 后端 409 = 工具执行未开启（或执行后端不可用），原样展示后端文案，
 *    不把「没允许跑」画成「跑了但失败」；
 * 2. 400 / 404 = 入参或工具名不合法，属于请求错误，也不冒充执行结果；
 * 3. 200 + `ok=false` 才是「工具真的跑了但失败」——UI 用不同色调区分。
 */
import { apiFetch } from '../../lib/api.js';
import { copyText } from '../../lib/clipboard.js';
import { h } from '../../lib/dom.js';
import { argsTemplate, parseArgs, resultSummary } from '../../lib/tool-lab.js';

import { closeModal, showModal } from '../../ui/modal.js';
import { toast } from '../../ui/toast.js';

function explain(err) {
  return String((err && (err.detail || err.message)) || err || '未知错误');
}

/** 把执行结果铺进结果区：摘要 + 原样输出（纯文本，不进 HTML）。 */
function renderResult(box, payload, onCopy) {
  const summary = resultSummary(payload);
  const text = payload && payload.result != null ? String(payload.result) : '';
  box.replaceChildren(
    h('div', { class: `tool-run-summary ${summary.tone}` },
      h('span', { class: 'tool-run-dot' }),
      h('span', {}, summary.title),
      summary.detail ? h('span', { class: 'tool-run-meta' }, summary.detail) : null,
      text ? h('button', {
        class: 'btn btn-sm', type: 'button', onclick: () => void onCopy(text),
      }, '复制输出') : null),
    text ? h('pre', { class: 'tool-run-output' }, text) : null,
    !payload || payload.ok ? null : h('div', { class: 'tool-run-error' },
      String(payload.error || '工具返回失败，但未带错误信息')));
}

function openTrialDialog(tool, options = {}) {
  const { enabled = true } = options;
  const initial = JSON.stringify(argsTemplate(tool), null, 2);
  const input = h('textarea', {
    class: 'tool-args-input', rows: 8, spellcheck: 'false', value: initial,
    'aria-label': '试跑入参 JSON',
  });
  const hint = h('div', { class: 'tool-args-hint' },
    `工具：${tool.name} · 类别：${tool.kind || '—'} · 每次试跑都在 <output>/_tool_trial/ 下新建独立沙箱`);
  const resultBox = h('div', { class: 'tool-run-box' });
  const copy = async (text) => {
    const ok = await copyText(text);
    toast(ok ? 'success' : 'error', ok ? '已复制输出' : '复制失败', ok ? '' : '剪贴板不可用，可手动选中复制');
  };

  const runBtn = h('button', {
    class: 'btn btn-primary', type: 'button', disabled: !enabled,
    title: enabled ? '在独立沙箱里执行一次（不碰任何 run 目录）' : '工具执行未开启：需在服务端配置 tools_enabled=true',
    onclick: async () => {
      const parsed = parseArgs(input.value);
      if (!parsed.ok) {
        resultBox.replaceChildren(h('div', { class: 'tool-run-error' }, parsed.error));
        input.focus();
        return;
      }
      runBtn.disabled = true;
      const label = runBtn.textContent;
      runBtn.textContent = '试跑中…';
      resultBox.replaceChildren(h('div', { class: 'tool-run-pending' }, '正在沙箱里执行…'));
      try {
        const payload = await apiFetch(
          `/tools/${encodeURIComponent(tool.name)}/test`,
          {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ args: parsed.args }),
            timeoutMs: 90000,
          });
        renderResult(resultBox, payload, copy);
      } catch (err) {
        // 409 / 400 / 404 都是服务端的明确答复，照实说；传输层错误另行提示。
        const code = (err && err.code) || '';
        const title = code === 'HTTP_409'
          ? '后端拒绝试跑'
          : (code === 'HTTP_400' || code === 'HTTP_404' ? '请求不合法' : '试跑请求失败');
        resultBox.replaceChildren(
          h('div', { class: 'tool-run-error' }, `${title}：${explain(err)}`));
      } finally {
        runBtn.disabled = !enabled;
        runBtn.textContent = label;
      }
    },
  }, '试跑');

  showModal(h('div', { class: 'tool-trial-modal' },
    h('h3', { class: 'modal-title' }, `试跑工具：${tool.name}`),
    enabled ? null : h('div', { class: 'tool-run-error' },
      '工具执行未开启（tools_enabled=false）：清单可以看，试跑会被后端拒绝。'),
    h('div', { class: 'tool-desc' }, tool.description || '（后端未提供说明）'),
    hint,
    input,
    h('div', { class: 'tool-args-actions' },
      h('button', {
        class: 'btn btn-sm', type: 'button',
        onclick: () => { input.value = initial; input.focus(); },
      }, '重置模板'),
      h('button', {
        class: 'btn btn-sm', type: 'button',
        onclick: () => {
          try {
            input.value = JSON.stringify(JSON.parse(input.value), null, 2);
          } catch {
            toast('error', '格式化失败', '当前内容不是合法 JSON');
          }
        },
      }, '格式化')),
    resultBox,
    h('div', { class: 'modal-actions' },
      h('button', { class: 'btn', type: 'button', onclick: closeModal }, '关闭'),
      runBtn)), { wide: true });
  input.focus();
}

export { openTrialDialog };
