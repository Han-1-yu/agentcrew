/**
 * 供应商表单：从预设添加 / 编辑（YAML 片段 + 环境变量名 + Key）。
 */
import { BUILTIN_PROVIDERS } from '../../contracts/providers.js';

import { h, s } from '../../lib/dom.js';

import { closeModal, showModal } from '../../ui/modal.js';
import { toast } from '../../ui/toast.js';
function buildProviderFormFields(initial) {
  const nameInput = h('input', { id: 'ap-name', class: 'ap-field', type: 'text', placeholder: '如 my-relay（小写英文/数字/-）' });
  const urlInput = h('input', { id: 'ap-url', class: 'ap-field', type: 'text', placeholder: 'https://…' });
  const envInput = h('input', { id: 'ap-env', class: 'ap-field', type: 'text', placeholder: '环境变量名，如 MYRELAY_KEY' });
  const modelsInput = h('input', { id: 'ap-models', class: 'ap-field', type: 'text', placeholder: 'model-a, model-b（逗号分隔，可选）' });
  const yamlBox = h('textarea', { id: 'ap-yaml', class: 'ap-yaml', readonly: true, rows: 10 });

  nameInput.value = initial.name || '';
  urlInput.value = initial.base_url || '';
  envInput.value = initial.api_key_env || '';
  modelsInput.value = (initial.models || []).join(', ');

  // ── API Key 值输入 + 保存（写入项目根目录 .env，立即注入主进程与 CLI 子进程环境）──
  const keyInput = h('input', { id: 'ap-key', class: 'ap-field ap-key-input', type: 'password', placeholder: '粘贴 API Key 值（sk-…），仅保存在本机 .env' });
  const keyStatusEl = h('span', { class: 'ap-key-status' + (initial.keyConfigured ? ' ok' : '') },
    initial.keyConfigured == null ? '' : (initial.keyConfigured ? '已配置 ✓' : '未检测到 ✗'));
  const keyHintEl = h('span', { class: 'ap-key-hint' }, '保存后模型切换立即生效，无需重启');
  const saveKeyBtn = h('button', { class: 'btn btn-sm ap-key-save', type: 'button' }, '保存 Key');
  saveKeyBtn.addEventListener('click', async () => {
    const envName = envInput.value.trim();
    const value = keyInput.value.trim();
    if (!envName) { toast('error', '无法保存', '请先填写 API Key 环境变量名'); return; }
    if (!value) { toast('error', '无法保存', '请先粘贴 Key 值'); return; }
    saveKeyBtn.disabled = true;
    const orig = saveKeyBtn.textContent;
    saveKeyBtn.textContent = '保存中…';
    try {
      await window.studio.providerSaveKey({ env_name: envName, value });
      keyInput.value = '';
      keyStatusEl.textContent = '已配置 ✓';
      keyStatusEl.className = 'ap-key-status ok';
      toast('success', 'Key 已保存', `${envName} 已写入项目根目录 .env，切换到该供应商即可使用`);
      if (typeof initial.onKeySaved === 'function') initial.onKeySaved();
    } catch (e) {
      toast('error', '保存失败', String(e && e.message || e));
    } finally {
      saveKeyBtn.disabled = false;
      saveKeyBtn.textContent = orig;
    }
  });

  const genYaml = () => {
    const mode = initial.mode === 'override' ? 'override' : 'custom';
    const key = (nameInput.value.trim() || 'myprovider').toLowerCase().replace(/[^a-z0-9_-]/g, '-');
    const models = modelsInput.value.split(',').map(s => s.trim()).filter(Boolean);
    let lines;
    if (mode === 'override') {
      // 内置 provider 覆盖：models.providers.<key>
      lines = ['models:', '  providers:', `    ${key}:`];
      lines.push('      base_url: ' + (urlInput.value.trim() || 'https://your-endpoint'));
      if (envInput.value.trim()) lines.push('      api_key_env: ' + envInput.value.trim());
    } else {
      // 自定义 provider：models.custom_providers.<key>
      lines = ['models:', '  custom_providers:', `    ${key}:`, '      implementation: openai_compat'];
      lines.push('      base_url: ' + (urlInput.value.trim() || 'https://your-endpoint'));
      if (envInput.value.trim()) lines.push('      api_key_env: ' + envInput.value.trim());
    }
    if (models.length) {
      lines.push('      suggested_models:');
      for (const m of models) lines.push(`        - ${m}`);
    }
    yamlBox.value = lines.join('\n');
  };
  [nameInput, urlInput, envInput, modelsInput].forEach(el => el.addEventListener('input', genYaml));
  genYaml();

  return { nameInput, urlInput, envInput, modelsInput, yamlBox, genYaml,
    keyInput, keyStatusEl, keyHintEl, saveKeyBtn };
}

function copyYaml(yamlBox) {
  return async () => {
    const text = yamlBox.value;
    try { await navigator.clipboard.writeText(text); toast('success', '已复制', 'YAML 片段已写入剪贴板'); }
    catch (e) {
      yamlBox.focus(); yamlBox.select();
      try { document.execCommand('copy'); toast('success', '已复制', 'YAML 片段已写入剪贴板'); }
      catch (e2) { toast('error', '复制失败', '请手动选择文本复制'); }
    }
  };
}

function openAddProviderModal() {
  const presets = [
    ...BUILTIN_PROVIDERS.map(p => ({ key: p.key, label: p.label, base_url: p.base_url || '', api_key_env: p.api_key_env || '' })),
    { key: '__custom', label: '空白自定义', base_url: '', api_key_env: '' },
  ];
  const sel = h('select', { id: 'ap-preset', class: 'ap-field' },
    h('option', { value: '' }, '选择预设…'),
    ...presets.map(p => h('option', { value: p.key }, p.label)));
  const f = buildProviderFormFields({ mode: 'custom', name: '', base_url: '', api_key_env: '', models: [] });

  sel.addEventListener('change', () => {
    const p = presets.find(x => x.key === sel.value);
    if (p && p.key !== '__custom') { f.nameInput.value = p.key; f.urlInput.value = p.base_url; f.envInput.value = p.api_key_env; }
    else if (p && p.key === '__custom') { f.nameInput.value = ''; f.urlInput.value = ''; f.envInput.value = ''; }
    f.genYaml();
  });

  showModal(h('div', { class: 'ap-modal' },
    h('h3', { class: 'modal-title' }, '从预设添加供应商'),
    h('div', { class: 'ap-hint' }, '第一步：选择预设；第二步：确认名称/base_url/环境变量名；第三步：复制 YAML 片段。'),
    h('div', { class: 'ap-field-row' }, h('label', {}, '预设'), sel),
    h('div', { class: 'ap-field-row' }, h('label', {}, 'Provider key'), f.nameInput),
    h('div', { class: 'ap-field-row' }, h('label', {}, 'Base URL'), f.urlInput),
    h('div', { class: 'ap-field-row' }, h('label', {}, 'API Key 环境变量名'), f.envInput),
    h('div', { class: 'ap-field-row' },
      h('label', {}, 'API Key 值（写入 .env，立即生效）'),
      h('div', { class: 'ap-key-row' }, f.keyInput, f.saveKeyBtn, f.keyStatusEl), f.keyHintEl),
    h('div', { class: 'ap-field-row' }, h('label', {}, '建议模型（逗号分隔）'), f.modelsInput),
    h('div', { class: 'ap-field-row' },
      h('label', {}, 'agentcrew.yaml 片段（只读）'),
      f.yamlBox,
      h('div', { class: 'ap-warn' }, 'Studio 不直接修改 agentcrew.yaml，复制后请粘贴到项目根目录 agentcrew.yaml。')),
    h('div', { class: 'modal-actions' },
      h('button', { class: 'btn', onclick: closeModal }, '关闭'),
      h('button', { class: 'btn btn-primary', onclick: copyYaml(f.yamlBox) }, '一键复制'))));
}

/* 编辑现有 provider：内置 → models.providers.<name> 覆盖片段；自定义 → custom_providers 片段 */
function openEditProviderModal(p) {
  const builtin = !!p._builtin;
  const f = buildProviderFormFields({
    mode: builtin ? 'override' : 'custom',
    name: p.name || '',
    base_url: p.base_url || '',
    api_key_env: p.api_key_env || '',
    models: p.suggested_models || [],
    keyConfigured: p._status === 'configured' ? true : p._status === 'missing' ? false : null,
    // 保存成功后同步卡片状态点，无需整页刷新
    onKeySaved: () => { p._status = 'configured'; },
  });
  // 内置 provider 的 key 不建议改（覆盖是按 key 匹配的）
  if (builtin) f.nameInput.readOnly = true;

  showModal(h('div', { class: 'ap-modal' },
    h('h3', { class: 'modal-title' }, `编辑供应商：${p.label || p.name}`),
    h('div', { class: 'ap-hint' }, builtin
      ? '内置供应商：修改将生成 models.providers.<name> 覆盖片段（不可在 custom_providers 中重名）。'
      : '自定义供应商：修改将生成 models.custom_providers.<name> 片段。'),
    h('div', { class: 'ap-field-row' }, h('label', {}, 'Provider key' + (builtin ? '（内置，不可改）' : '')), f.nameInput),
    h('div', { class: 'ap-field-row' }, h('label', {}, 'Base URL'), f.urlInput),
    h('div', { class: 'ap-field-row' }, h('label', {}, 'API Key 环境变量名'), f.envInput),
    h('div', { class: 'ap-field-row' },
      h('label', {}, 'API Key 值（写入 .env，立即生效）'),
      h('div', { class: 'ap-key-row' }, f.keyInput, f.saveKeyBtn, f.keyStatusEl), f.keyHintEl),
    h('div', { class: 'ap-field-row' }, h('label', {}, '建议模型（逗号分隔）'), f.modelsInput),
    h('div', { class: 'ap-field-row' },
      h('label', {}, 'agentcrew.yaml 片段（只读）'),
      f.yamlBox,
      h('div', { class: 'ap-warn' }, 'Studio 不直接修改 agentcrew.yaml，复制后请粘贴到项目根目录 agentcrew.yaml。')),
    h('div', { class: 'modal-actions' },
      h('button', { class: 'btn', onclick: closeModal }, '关闭'),
      h('button', { class: 'btn btn-primary', onclick: copyYaml(f.yamlBox) }, '一键复制'))));
}

/* ============================================================
   V7 配额管理模态框
   ============================================================ */

export { buildProviderFormFields, copyYaml, openAddProviderModal, openEditProviderModal };
