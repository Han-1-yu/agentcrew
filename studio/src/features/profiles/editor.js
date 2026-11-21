/**
 * 执行体档案编辑器：新建 / 编辑 / 复制。
 *
 * 保存只提交「字段对象」（JSON 是 YAML 子集），由后端 `agents --save` 做
 * 落盘、备份与绑定校验；前端不生成 YAML，也不直接写文件。
 */
import { h } from '../../lib/dom.js';
import {
  draftFromProfile, effectivePreview, emptyDraft, toProfilePayload, validateDraft,
} from '../../lib/profile-form.js';
import { saveAgents } from '../../lib/agent-binding.js';
import { closeModal, showModal } from '../../ui/modal.js';
import { toast } from '../../ui/toast.js';

import { field, input, modelOptions, numberField, roleLabel, section, textarea } from './form-fields.js';
import { icon } from './icons.js';
import { TEMPLATES } from './templates.js';

const NUMBER_FIELDS = [
  ['cost_ceiling_usd', '单任务成本上限（$）', '0.05'],
  ['run_cost_ceiling_usd', '整场成本上限（$）', '0.5'],
  ['max_calls_per_task', '单任务调用上限', '8'],
  ['max_rpm', '每分钟调用（RPM）', '30'],
  ['max_concurrency', '并发上限', '2'],
  ['temperature', '温度（0–2）', '0.3'],
  ['max_output_tokens', '最大输出 token', '4096'],
];

function templateBar(onPick) {
  return h('div', { class: 'pf-templates' },
    h('span', { class: 'pf-templates-label' }, '从模板开始'),
    ...TEMPLATES.map((tpl) => h('button', {
      class: 'pf-chip-btn', type: 'button',
      onclick: () => {
        onPick({ ...emptyDraft(), ...tpl.draft });
        toast('info', tpl.label, '模板已填入，保存前可继续修改');
      },
    }, tpl.label)));
}

function openProfileEditor({
  profile = null, mode = 'create', roles = [], profiles = [], tools = [], onSaved,
}) {
  const editingKey = mode === 'edit' ? (profile && profile.key) || '' : '';
  const draft = mode === 'duplicate'
    ? { ...draftFromProfile(profile), key: `${profile.key}-copy` }
    : (profile ? draftFromProfile(profile) : emptyDraft());
  const title = mode === 'edit' ? '编辑执行体档案'
    : mode === 'duplicate' ? '复制执行体档案' : '新建执行体档案';

  const form = h('form', { class: 'pf-form', onsubmit: (e) => e.preventDefault() });
  if (mode === 'create') form.appendChild(templateBar((next) => applyDraft(next)));

  // ── 基本信息 ──
  const keyInput = input('key', {
    value: draft.key, placeholder: 'fast-writer', disabled: !!editingKey,
  });
  form.appendChild(section('基本信息', 'key 是编排里的引用名；title / description 是给 PM 看的菜单文案。',
    h('div', { class: 'pf-grid-2' },
      field('key', keyInput, '小写字母开头，保存后不可改名'),
      field('名称', input('title', { value: draft.title, placeholder: '快速写手' }))),
    field('能力描述', textarea('description', 2, {
      value: draft.description,
      placeholder: '一句话说明擅长什么——PM 规划时会按这句话挑人',
    })),
    field('能力标签', input('capabilities', {
      value: draft.capabilities, placeholder: 'text、summary、code（逗号或顿号分隔）',
    }), '纯提示字段，不参与硬路由')));

  // ── 模型 ──
  form.appendChild(section('模型', '留空表示沿用角色模型 / 角色绑定 / 全局默认。',
    h('datalist', { id: 'pf-model-options' }, ...modelOptions()),
    h('div', { class: 'pf-grid-2' },
      field('模型别名', input('model', {
        value: draft.model, placeholder: 'deepseek:deepseek-chat（留空 = 继承角色）',
        list: 'pf-model-options',
      })),
      field('备用模型', input('fallback', {
        value: draft.fallback, placeholder: '备用别名，逗号分隔（可选）',
      })))));

  // ── 工具与边界 ──
  // 候选 = 工具目录 ∪ 档案已引用的工具。后者必须有：MCP / 自定义工具不在
  // 内置目录里，若只渲染目录候选，保存时会把它们悄悄删掉。
  const catalog = new Map(tools.map((tool) => [tool.name, tool]));
  const toolNames = [...new Set([
    ...tools.map((tool) => tool.name), ...draft.tools,
  ])];
  const fallbackTools = toolNames.length ? [] : ['read_file', 'write_file'];
  const toolPicker = h('div', { class: 'pf-tool-picker' },
    ...[...toolNames, ...fallbackTools].map((name) => {
      const tool = catalog.get(name) || {
        name, kind: fallbackTools.includes(name) ? '' : '自定义', description: '',
      };
      const checked = draft.tools.includes(name);
      return h('label', { class: 'pf-tool', title: tool.description || '' },
        h('input', {
          type: 'checkbox', value: name, checked, 'data-tool': name,
        }),
        h('span', { class: 'pf-tool-name mono' }, name),
        h('span', { class: 'pf-tool-kind' }, tool.kind || ''));
    }));
  if (!tools.length) {
    toolPicker.appendChild(h('span', { class: 'pf-hint' },
      '工具目录暂不可用（后端未连接）；已引用的工具仍会保留。'));
  }
  const inheritRadio = h('input', {
    type: 'radio', name: 'pf-tools-mode', value: 'inherit',
    checked: draft.toolsMode !== 'list',
  });
  const listRadio = h('input', {
    type: 'radio', name: 'pf-tools-mode', value: 'list',
    checked: draft.toolsMode === 'list',
  });
  const syncToolVisibility = () => {
    toolPicker.classList.toggle('is-off', !listRadio.checked);
  };
  inheritRadio.addEventListener('change', syncToolVisibility);
  listRadio.addEventListener('change', syncToolVisibility);
  syncToolVisibility();
  form.appendChild(section('工具与写入边界',
    '工具只能从角色授权里收窄，不能扩权；路径域用于 split_by_path 的分工隔离。',
    h('div', { class: 'pf-mode-row' },
      h('label', { class: 'pf-radio' }, inheritRadio, h('span', {}, '继承角色工具')),
      h('label', { class: 'pf-radio' }, listRadio, h('span', {}, '指定白名单'))),
    toolPicker,
    field('写入范围', textarea('path_scope', 3, {
      value: draft.path_scope,
      placeholder: 'src/\ntests/\n（留空 = 不限，仍受任务契约约束）',
    }), '每行一个相对路径前缀，如 src/')));

  // ── 预算与限流 ──
  form.appendChild(section('预算与限流', '留空 = 不额外限制；这些值只作用于该执行体。',
    h('div', { class: 'pf-grid-3' },
      ...NUMBER_FIELDS.map(([name, label, placeholder]) =>
        numberField(name, label, placeholder, draft)))));

  // ── 生效预览 ──
  const previewBody = h('div', { class: 'pf-preview-body' });
  const previewRole = h('select', { class: 'pf-input pf-select' },
    ...roles.map((role) => h('option', { value: role.role }, roleLabel(role))));
  const preferred = roles.find(
    (role) => profile && (profile.used_by || []).includes(role.role)) || roles[0];
  if (preferred) previewRole.value = preferred.role;
  const updatePreview = () => {
    const role = roles.find((r) => r.role === previewRole.value) || roles[0] || {};
    const { rows, warnings } = effectivePreview(toProfilePayload(readDraft()), role);
    previewBody.innerHTML = '';
    previewBody.appendChild(h('dl', { class: 'pf-preview-rows' },
      ...rows.flatMap((row) => [
        h('dt', {}, row.k),
        h('dd', {}, row.v, row.note ? h('span', { class: 'pf-note' }, row.note) : null),
      ])));
    for (const warning of warnings) {
      previewBody.appendChild(h('p', { class: 'pf-warn-line' }, icon('warn', 14), warning));
    }
  };
  previewRole.addEventListener('change', updatePreview);
  form.appendChild(section('生效预览', '按当前表单和所选角色，算出真正会生效的模型与工具。',
    h('div', { class: 'pf-preview-head' }, h('span', { class: 'pf-hint' }, '按角色预览'), previewRole),
    previewBody));

  // ── 提交区 ──
  const errorBox = h('div', { class: 'pf-form-errors hidden' });
  const saveBtn = h('button', { class: 'btn btn-primary', type: 'submit' },
    mode === 'edit' ? '保存修改' : '创建档案');
  form.appendChild(errorBox);
  form.appendChild(h('div', { class: 'modal-actions pf-form-actions' },
    h('button', { class: 'btn', type: 'button', onclick: closeModal }, '取消'), saveBtn));

  function readDraft() {
    const value = (name) => {
      const el = form.querySelector(`[data-field="${name}"]`);
      return el ? el.value : '';
    };
    const modeInput = form.querySelector('input[name="pf-tools-mode"]:checked');
    return {
      key: value('key'), title: value('title'), description: value('description'),
      capabilities: value('capabilities'), model: value('model'),
      fallback: value('fallback'),
      toolsMode: modeInput && modeInput.value === 'list' ? 'list' : 'inherit',
      tools: [...form.querySelectorAll('[data-tool]:checked')].map((el) => el.value),
      path_scope: value('path_scope'),
      cost_ceiling_usd: value('cost_ceiling_usd'),
      run_cost_ceiling_usd: value('run_cost_ceiling_usd'),
      max_calls_per_task: value('max_calls_per_task'),
      max_rpm: value('max_rpm'), max_concurrency: value('max_concurrency'),
      temperature: value('temperature'), max_output_tokens: value('max_output_tokens'),
    };
  }

  function applyDraft(next) {
    for (const [name, value] of Object.entries(next)) {
      const el = form.querySelector(`[data-field="${name}"]`);
      if (el) el.value = value ?? '';
    }
    const picked = next.tools || [];
    form.querySelectorAll('[data-tool]').forEach((el) => {
      el.checked = picked.includes(el.value);
    });
    const mode = next.toolsMode === 'list' ? 'list' : 'inherit';
    const radio = form.querySelector(`input[name="pf-tools-mode"][value="${mode}"]`);
    if (radio) radio.checked = true;
    syncToolVisibility();
    updatePreview();
  }

  function markErrors(errors) {
    form.querySelectorAll('.pf-error').forEach((el) => { el.textContent = ''; });
    form.querySelectorAll('[data-field]').forEach((el) => el.classList.remove('is-bad'));
    errorBox.innerHTML = '';
    errorBox.classList.toggle('hidden', !Object.keys(errors).length);
    for (const [name, message] of Object.entries(errors)) {
      const el = form.querySelector(`[data-field="${name}"]`);
      if (el) {
        el.classList.add('is-bad');
        const holder = el.closest('.pf-field')?.querySelector('.pf-error');
        if (holder) holder.textContent = message;
      }
      errorBox.appendChild(h('div', { class: 'pf-form-error' }, icon('warn', 14), message));
    }
  }

  form.addEventListener('input', updatePreview);
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const current = readDraft();
    const { errors, warnings } = validateDraft(current, {
      existingKeys: profiles.map((p) => p.key), editingKey,
    });
    markErrors(errors);
    if (Object.keys(errors).length) return;

    saveBtn.disabled = true;
    saveBtn.textContent = '保存中…';
    try {
      const payload = toProfilePayload(current);
      const result = await saveAgents(JSON.stringify(payload), { force: mode === 'edit' });
      const notes = [...(result.warnings || []), ...warnings];
      toast('success', mode === 'edit' ? '已保存' : '已创建',
        notes.length ? notes[0] : `${payload.key} 已写入 agents.d`);
      closeModal();
      if (onSaved) await onSaved(result);
    } catch (e) {
      errorBox.classList.remove('hidden');
      errorBox.innerHTML = '';
      errorBox.appendChild(h('div', { class: 'pf-form-error' },
        icon('warn', 14), String(e.message || e)));
    } finally {
      saveBtn.disabled = false;
      saveBtn.textContent = mode === 'edit' ? '保存修改' : '创建档案';
    }
  });

  showModal(h('div', { class: 'pf-editor' },
    h('h3', { class: 'modal-title' }, title),
    h('p', { class: 'pf-editor-sub' },
      mode === 'create'
        ? '保存到 agents.d/，不重写 agentcrew.yaml；创建后可在设置页绑定到角色。'
        : (profile && profile.source === 'yaml'
          ? '这份档案定义在 agentcrew.yaml；保存会在 agents.d 创建同名覆盖版本，原文不动。'
          : '修改会写入 agents.d，旧版本自动留一份 .bak。')),
    form), { wide: true });

  requestAnimationFrame(() => {
    updatePreview();
    keyInput.focus();
  });
}

export { openProfileEditor };
