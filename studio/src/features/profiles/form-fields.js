/** 表单原语：标签 + 输入 + 提示 + 行内错误，以及 2/3 列网格。 */
import { h } from '../../lib/dom.js';
import { MODEL_ALIASES } from '../../contracts/providers.js';

function field(label, control, hint) {
  return h('label', { class: 'pf-field' },
    h('span', { class: 'pf-label' }, label),
    control,
    hint ? h('span', { class: 'pf-hint' }, hint) : null,
    h('span', { class: 'pf-error' }, ''));
}

function input(fieldName, props = {}) {
  // `list` 在 HTMLInputElement 上是只读引用（DOMTokenList），必须走属性而非
  // 属性赋值；h() 的默认路径是 el[k]=v，直接传会抛 "only a getter"。
  const { list, ...rest } = props;
  const el = h('input', {
    class: 'pf-input', type: 'text', 'data-field': fieldName, ...rest,
  });
  if (list) el.setAttribute('list', list);
  return el;
}

function textarea(fieldName, rows = 3, props = {}) {
  return h('textarea', {
    class: 'pf-input pf-textarea', rows, 'data-field': fieldName, ...props,
  });
}

function numberField(fieldName, label, placeholder, draft) {
  return field(label, h('input', {
    class: 'pf-input', type: 'number', step: 'any', min: '0',
    'data-field': fieldName, value: draft[fieldName] ?? '', placeholder,
  }));
}

function section(title, description, ...children) {
  return h('section', { class: 'pf-section' },
    h('div', { class: 'pf-section-head' },
      h('h4', { class: 'pf-section-title' }, title),
      description ? h('p', { class: 'pf-section-desc' }, description) : null),
    ...children);
}

function modelOptions() {
  return MODEL_ALIASES.map((m) => h('option', { value: `${m.provider}:${m.model}` }));
}

function roleLabel(role) {
  return role.title ? `${role.title}（${role.role}）` : role.role;
}

export { field, input, modelOptions, numberField, roleLabel, section, textarea };
