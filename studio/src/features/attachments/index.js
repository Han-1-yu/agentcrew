/**
 * A5 附件区：composer 与新建运行弹窗共用的图片附件选择。
 *
 * 口径与后端一致：≤4 张、单张 ≤2.6MB、png/jpg/webp/gif（校验在主进程
 * images:pick 里做，这里只管状态与展示）。取数两条通道：
 *  - CLI：get() 返回的 path 数组交给 run:start → --image；
 *  - REST：readImageFiles(path 数组) → base64 → POST /runs 的 images。
 */
import { h } from '../../lib/dom.js';

import { toast } from '../../ui/toast.js';

const MAX_IMAGES = 4;

function fmtSize(bytes) {
  const v = Number(bytes) || 0;
  return v >= 1048576 ? `${(v / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(v / 1024))} KB`;
}

function createAttachmentArea({ onChange, compact = false } = {}) {
  let images = [];   // { path, name, mime, sizeBytes }
  const chips = h('div', { class: 'att-chips' });
  const addBtn = h('button', {
    class: 'att-add', type: 'button',
    title: `添加图片（最多 ${MAX_IMAGES} 张，单张 ≤2.6MB）`,
    'aria-label': '添加图片附件',
    onclick: () => void pick(),
  }, '＋ 图片');
  const node = h('div', { class: `att-area${compact ? ' att-compact' : ''}` }, addBtn, chips);

  function notify() { if (typeof onChange === 'function') onChange(images.slice()); }

  async function pick() {
    if (images.length >= MAX_IMAGES) {
      toast('info', '附件已满', `最多 ${MAX_IMAGES} 张图片`);
      return;
    }
    try {
      const r = await window.studio.pickImages(images.length);
      if (!r || r.canceled) return;
      if (r.error) { toast('error', '图片不可用', String(r.error)); return; }
      if (r.skipped > 0) toast('info', '已忽略部分图片', `最多 ${MAX_IMAGES} 张，超出 ${r.skipped} 张未选入`);
      const picked = (r.images || []).filter((it) => it && it.path);
      if (!picked.length) return;
      images = images.concat(picked).slice(0, MAX_IMAGES);
      render();
      notify();
    } catch (e) {
      toast('error', '选择失败', String((e && e.message) || e));
    }
  }

  function render() {
    chips.replaceChildren();
    images.forEach((img, i) => {
      chips.appendChild(h('span', { class: 'att-chip', title: img.path },
        h('span', { class: 'att-chip-name' }, img.name || 'image'),
        h('span', { class: 'att-chip-size' }, fmtSize(img.sizeBytes)),
        h('button', {
          class: 'att-chip-x', type: 'button', title: '移除', 'aria-label': `移除 ${img.name || '附件'}`,
          onclick: () => { images.splice(i, 1); render(); notify(); },
        }, '✕')));
    });
    addBtn.disabled = images.length >= MAX_IMAGES;
    addBtn.classList.toggle('is-full', images.length >= MAX_IMAGES);
    node.classList.toggle('has-images', images.length > 0);
  }

  render();

  return {
    node,
    /** 浅拷贝的附件列表（CLI 通道直接用 path）。 */
    get: () => images.slice(),
    /** REST 通道：把已选图片读成 { mime, data } 的 base64 形状。 */
    readForRest: async () => {
      if (!images.length) return [];
      const r = await window.studio.readImageFiles(images.map((it) => it.path));
      if (!r || !r.ok) throw new Error(String((r && r.error) || '读取附件失败'));
      return (r.images || []).map((it) => ({ mime: it.mime, data: it.dataBase64, path: it.path }));
    },
    clear: () => { images = []; render(); notify(); },
    isEmpty: () => images.length === 0,
  };
}

export { MAX_IMAGES, createAttachmentArea };
