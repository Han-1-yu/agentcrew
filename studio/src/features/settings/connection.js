/**
 * 组 4「连接」：后端 API 地址（可选）+ 连通性测试 + 当前数据通道说明。
 */
import { h } from '../../lib/dom.js';
import { S } from '../../lib/store.js';

function buildConnectionGroup(cfg) {
  const connStatus = h('div', { class: 'set-inline-status', id: 'set-conn-status' });
  const testConnBtn = h('button', { class: 'btn btn-sm', id: 'set-test-conn', type: 'button' }, '测试连接');
  const apiInput = h('input', { id: 'set-api', class: 'set-input', type: 'text', value: cfg.apiBaseUrl || '', placeholder: 'http://localhost:8765 — 设置后启用 SSE 实时推送' });

  const refreshBtnState = () => {
    const v = apiInput.value.trim();
    testConnBtn.disabled = !v;
    testConnBtn.title = v ? '' : '请先填写后端 API 地址';
  };
  apiInput.addEventListener('input', refreshBtnState);
  refreshBtnState();

  testConnBtn.addEventListener('click', async () => {
    const base = apiInput.value.trim();
    if (!base) return;
    testConnBtn.disabled = true;
    const orig = testConnBtn.textContent;
    testConnBtn.textContent = '测试中…';
    connStatus.className = 'set-inline-status pending';
    connStatus.textContent = '正在连接…';
    try {
      const r = await window.studio.providerTest({ base_url: base, models_path: '/providers' });
      if (r && r.ok) {
        connStatus.className = 'set-inline-status ok';
        connStatus.textContent = `连接正常 · ${r.latency_ms}ms`;
      } else {
        connStatus.className = 'set-inline-status err';
        connStatus.textContent = `连接失败：${(r && r.error) || (r && r.status ? 'HTTP ' + r.status : '未知错误')}`;
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
    h('div', { class: 'set-conn-mode' }, `当前数据连接模式：${modeText}`));
}

export { buildConnectionGroup };
