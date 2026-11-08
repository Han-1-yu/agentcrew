/**
 * 组 4「连接」：后端 API 地址（可选）+ API Key + 连通性测试 + 当前数据通道说明。
 *
 * R36 起后端认证默认强制开启（fail-closed），没有 Key 的所有请求都会 401。
 * Key 只写不回显：输入框永远为空，值单向传给主进程 safeStorage 加密保存，
 * 不进 settings.json、不进 collectSettings、不随设置页重新渲染带回来。
 */
import { h } from '../../lib/dom.js';
import { S } from '../../lib/store.js';

import { toast } from '../../ui/toast.js';

import { readUrl } from '../../lib/validate.js';

function keyStatusText() {
  return S.apiKeyConfigured ? '已配置 ✓（留空表示不修改）' : '未配置 ✗ — 后端会拒绝所有请求';
}

function buildConnectionGroup(cfg) {
  const connStatus = h('div', { class: 'set-inline-status', id: 'set-conn-status' });
  const testConnBtn = h('button', { class: 'btn btn-sm', id: 'set-test-conn', type: 'button' }, '测试连接');
  const apiInput = h('input', { id: 'set-api', class: 'set-input', type: 'text', value: cfg.apiBaseUrl || '', placeholder: 'http://localhost:8765 — 设置后启用 SSE 实时推送' });

  const keyInput = h('input', {
    id: 'set-api-key', class: 'set-input', type: 'password', value: '',
    autocomplete: 'new-password', placeholder: '输入 serve 的 API Key（不回显）',
  });
  const keySaveBtn = h('button', { class: 'btn btn-sm', id: 'set-key-save', type: 'button' }, '保存密钥');
  const keyClearBtn = h('button', { class: 'btn btn-sm', id: 'set-key-clear', type: 'button' }, '清除');
  const keyStatus = h('div', { class: 'set-inline-status', id: 'set-key-status' }, keyStatusText());

  const refreshBtnState = () => {
    const v = apiInput.value.trim();
    testConnBtn.disabled = !v;
    testConnBtn.title = v ? '' : '请先填写后端 API 地址';
    apiInput.classList.remove('input-error');
  };
  apiInput.addEventListener('input', refreshBtnState);
  refreshBtnState();

  keySaveBtn.addEventListener('click', async () => {
    const value = keyInput.value.trim();
    if (!value) {
      keyStatus.className = 'set-inline-status err';
      keyStatus.textContent = '请输入要保存的密钥';
      return;
    }
    keySaveBtn.disabled = true;
    try {
      const r = await window.studio.apiKeySave(value);
      if (!r || !r.ok) throw new Error((r && r.error) || '保存失败');
      S.apiKeyConfigured = true;
      keyInput.value = '';
      keyStatus.className = 'set-inline-status ok';
      keyStatus.textContent = keyStatusText();
      // 之前因 401 回退到文件监听了，配好 Key 要立刻重连，否则要等切 run。
      window.dispatchEvent(new Event('studio:reconnect'));
      toast('success', '密钥已保存', '已加密存入本机配置，不会回显');
    } catch (e) {
      keyStatus.className = 'set-inline-status err';
      keyStatus.textContent = `保存失败：${String(e.message || e)}`;
    } finally { keySaveBtn.disabled = false; }
  });

  keyClearBtn.addEventListener('click', async () => {
    try {
      await window.studio.apiKeyClear();
      S.apiKeyConfigured = false;
      keyInput.value = '';
      keyStatus.className = 'set-inline-status';
      keyStatus.textContent = keyStatusText();
      window.dispatchEvent(new Event('studio:reconnect'));
      toast('info', '密钥已清除', '后端请求将不再携带认证头');
    } catch (e) {
      keyStatus.className = 'set-inline-status err';
      keyStatus.textContent = `清除失败：${String(e.message || e)}`;
    }
  });

  testConnBtn.addEventListener('click', async () => {
    // 与保存时同一口径：非法地址不要拿去拼请求，先给行内错误。
    const parsed = readUrl(apiInput, { required: true, label: '后端 API 地址' });
    if (parsed.error) {
      apiInput.classList.add('input-error');
      connStatus.className = 'set-inline-status err';
      connStatus.textContent = parsed.error;
      return;
    }
    const base = parsed.value;
    testConnBtn.disabled = true;
    const orig = testConnBtn.textContent;
    testConnBtn.textContent = '测试中…';
    connStatus.className = 'set-inline-status pending';
    connStatus.textContent = '正在连接…';
    try {
      // 走主进程代理：绕开 CORS，并自动带上已保存的 API Key。
      const r = await window.studio.apiRequest('/providers', {
        baseUrl: base, method: 'GET', timeoutMs: 8000,
      });
      if (r && r.ok) {
        connStatus.className = 'set-inline-status ok';
        connStatus.textContent = '连接正常 · 认证通过';
      } else {
        const detail = (r && r.error) || (r && r.status ? 'HTTP ' + r.status : '未知错误');
        connStatus.className = 'set-inline-status err';
        connStatus.textContent = r && r.status === 401
          ? `连接失败：401 — 请先在上方保存 API Key（${detail}）`
          : `连接失败：${detail}`;
      }
    } catch (e) {
      connStatus.className = 'set-inline-status err';
      connStatus.textContent = `连接失败：${String(e && e.message || e)}`;
    } finally {
      testConnBtn.disabled = false;
      testConnBtn.textContent = orig;
      refreshBtnState();
    }
  });

  const modeText = S.sseConnected ? 'SSE 已连接' : (S.realMode && S.watchDir ? '文件监听' : '演示模式');

  return h('section', { class: 'settings-group' },
    h('h3', { class: 'settings-group-title' }, '连接'),
    h('div', { class: 'settings-field' },
      h('label', { for: 'set-api' }, '后端 API 地址'),
      h('div', { class: 'set-row' }, apiInput, testConnBtn),
      connStatus),
    h('div', { class: 'settings-field' },
      h('label', { for: 'set-api-key' }, 'API Key（本机加密保存，不回显）'),
      h('div', { class: 'set-row' }, keyInput, keySaveBtn, keyClearBtn),
      keyStatus),
    h('div', { class: 'set-conn-mode' }, `当前数据连接模式：${modeText}`));
}

export { buildConnectionGroup };
