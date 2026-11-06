/**
 * 页面级「加载中 / 空数据 / 错误」三态反馈的统一节点。
 *
 * 此前每个页面各写一句 `h('div',{class:'ph-empty'},'加载失败')`，文案、结构、
 * 是否可重试都不一致；有些页面（events / plan / agents / gates）连空态都没有。
 * 这里给出同一套节点，样式复用 components/empty.css 的 .page-state*，调用方
 * 只需把返回的节点 append 到自己的内容容器。
 *
 * 另外提供 `isStaleRender`：异步页面在 await 之后判断「这次结果还属于当前页吗」，
 * 避免用户切走后把内容写进已卸载的 DOM（或写进新版页面造成重复控件）。
 */
import { h } from '../lib/dom.js';
import { S } from '../lib/store.js';

/** 一个可选的动作按钮：`{ label, onclick }`。 */
function _action(action, cls) {
  if (!action) return null;
  return h('button', {
    class: cls || 'btn btn-sm', type: 'button', onclick: action.onclick,
  }, action.label || '重试');
}

/** 加载中：转圈 + 文案。 */
function loadingState(text = '加载中…', action = null) {
  return h('div', { class: 'page-state is-loading' },
    h('div', { class: 'page-state-spinner' }),
    h('div', { class: 'page-state-text' }, text),
    _action(action));
}

/**
 * 空数据：与「筛选无命中」区分开。title 是主文案，detail 是补充说明，
 * action 通常是「新建运行」/「清除筛选」。
 */
function emptyState(title, detail = '', action = null) {
  return h('div', { class: 'page-state is-empty' },
    h('div', { class: 'page-state-title' }, title),
    detail ? h('div', { class: 'page-state-detail' }, detail) : null,
    _action(action, 'btn btn-sm btn-primary'));
}

/**
 * 错误：保留原始错误信息（便于排查），并默认给一个「重试」按钮。
 * onRetry 传函数会转成按钮；不传则没有按钮（只读错误展示）。
 */
function errorState(title, detail = '', onRetry = null) {
  const action = typeof onRetry === 'function'
    ? { label: '重试', onclick: onRetry }
    : (onRetry && onRetry.onclick ? onRetry : null);
  return h('div', { class: 'page-state is-error' },
    h('div', { class: 'page-state-title' }, title || '加载失败'),
    detail ? h('div', { class: 'page-state-detail' }, String(detail)) : null,
    _action(action));
}

/** 清空容器并落一个状态节点（页面头部已经渲染时用不到，供整页替换场景）。 */
function replaceWithState(container, node) {
  if (!container) return null;
  container.innerHTML = '';
  if (node) container.appendChild(node);
  return node;
}

/**
 * 异步渲染是否过期：token 是页面入口捕获的 `S.navToken`。
 * 切页或重渲染都会让 token 递增；container 被移出 DOM 时同样视为过期。
 */
function isStaleRender(token, container) {
  return token !== S.navToken || (!!container && !container.isConnected);
}

export {
  emptyState,
  errorState,
  isStaleRender,
  loadingState,
  replaceWithState,
};
