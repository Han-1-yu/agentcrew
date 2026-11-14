/**
 * 模态框原语：焦点进入/圈定/恢复、可取消隐藏定时器、二次确认。
 */
import { $, h } from '../lib/dom.js';

let hideTimer = null;
let returnFocus = null;

const FOCUSABLE = 'input:not([disabled]), textarea:not([disabled]), select:not([disabled]), button:not([disabled]), [href], [tabindex]:not([tabindex="-1"])';

function focusables(root) {
  return [...root.querySelectorAll(FOCUSABLE)].filter(el => el.offsetParent !== null || el === document.activeElement);
}

function modalElement() { return $('#modal'); }

function showModal(node) {
  if (hideTimer) { clearTimeout(hideTimer); hideTimer = null; }
  const m = modalElement();
  const mask = $('#modal-mask');
  if (m.classList.contains('hidden')) returnFocus = document.activeElement;
  m.innerHTML = '';
  m.appendChild(node);
  m.tabIndex = -1;
  m.setAttribute('aria-hidden', 'false');
  const title = m.querySelector('.modal-title');
  if (title) {
    if (!title.id) title.id = 'modal-title';
    m.setAttribute('aria-labelledby', title.id);
  } else {
    m.removeAttribute('aria-labelledby');
  }
  mask.classList.remove('hidden');
  m.classList.remove('hidden');
  requestAnimationFrame(() => {
    m.classList.add('show');
    mask.classList.add('show');
    const target = focusables(m)[0] || m;
    target.focus({ preventScroll: true });
  });
}

function closeModal() {
  const m = modalElement();
  if (!m || m.classList.contains('hidden')) return;
  m.classList.remove('show');
  $('#modal-mask').classList.remove('show');
  m.setAttribute('aria-hidden', 'true');
  if (hideTimer) clearTimeout(hideTimer);
  hideTimer = setTimeout(() => {
    m.classList.add('hidden');
    $('#modal-mask').classList.add('hidden');
    hideTimer = null;
    if (returnFocus && returnFocus.isConnected) {
      try { returnFocus.focus({ preventScroll: true }); } catch { /* 元素已不可聚焦 */ }
    }
    returnFocus = null;
  }, 200);
}

function anyOverlayOpen() {
  const m = document.getElementById('modal');
  const d = document.getElementById('detail-panel');
  return (!!m && !m.classList.contains('hidden')) || (!!d && !d.classList.contains('hidden'));
}

$('#modal-mask').addEventListener('click', closeModal);
$('#modal').addEventListener('keydown', (event) => {
  const m = event.currentTarget;
  if (event.key === 'Escape') {
    event.preventDefault();
    event.stopPropagation();
    closeModal();
    return;
  }
  if (event.key !== 'Tab') return;
  const items = focusables(m);
  if (!items.length) { event.preventDefault(); m.focus(); return; }
  const first = items[0];
  const last = items[items.length - 1];
  if (event.shiftKey && (document.activeElement === first || document.activeElement === m)) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
});

function confirmAction(title, message, onOk, okLabel) {
  showModal(h('div', {},
    h('h3', { class: 'modal-title' }, title),
    h('div', { class: 'confirm-text' }, message),
    h('div', { class: 'modal-actions' },
      h('button', { class: 'btn', type: 'button', onclick: closeModal }, '取消'),
      h('button', { class: 'btn btn-danger', type: 'button', onclick: () => { closeModal(); onOk(); } }, okLabel || '确认'))));
}

export { anyOverlayOpen, closeModal, confirmAction, showModal };
