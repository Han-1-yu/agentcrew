/**
 * 组 2「模型与预算」：默认模型（下拉 + 自定义）、单次运行预算上限。
 */
import { MODEL_ALIASES, modelAlias } from '../../contracts/providers.js';

import { h } from '../../lib/dom.js';

function buildModelBudgetGroup(cfg) {
  // 当前默认模型不在内置别名列表里时，回填到自定义输入框
  const aliasList = MODEL_ALIASES.map((m) => modelAlias(m));
  const customVal = (cfg.defaultModel && aliasList.indexOf(cfg.defaultModel) < 0) ? cfg.defaultModel : '';

  return h('section', { class: 'settings-group' },
    h('h3', { class: 'settings-group-title' }, '模型与预算'),
    h('div', { class: 'settings-field' },
      h('label', { for: 'set-model' }, '默认模型'),
      h('div', { class: 'set-row' },
        h('select', { id: 'set-model', class: 'set-input' },
          h('option', { value: '', selected: !cfg.defaultModel }, '选择模型…'),
          ...MODEL_ALIASES.map((m) => h('option', { value: modelAlias(m), selected: cfg.defaultModel === modelAlias(m) }, m.label))),
        h('input', { id: 'set-model-custom', class: 'set-input', type: 'text', value: customVal, placeholder: '自定义 provider:model' }))),
    h('div', { class: 'settings-field' },
      h('label', { for: 'set-maxcost' }, '预算上限 (USD)'),
      h('input', { id: 'set-maxcost', class: 'set-input', type: 'number', min: '0', step: '0.01', value: cfg.maxCostUsd || '', placeholder: '0 = 不限制' })));
}

export { buildModelBudgetGroup };
