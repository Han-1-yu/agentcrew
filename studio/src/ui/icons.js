/**
 * 内联 SVG 图标工厂（14px 线性图标，颜色取 currentColor）。
 */
import { h } from '../lib/dom.js';

function iconPause(){ return h('svg',{width:13,height:13,viewBox:'0 0 24 24',fill:'none',stroke:'currentColor','stroke-width':2,'stroke-linecap':'round'},h('path',{d:'M9 5v14M15 5v14'})); }
function iconX(){ return h('svg',{width:13,height:13,viewBox:'0 0 24 24',fill:'none',stroke:'currentColor','stroke-width':2},h('circle',{cx:12,cy:12,r:9}),h('path',{d:'M15 9l-6 6M9 9l6 6'})); }
function iconPlay(){ return h('svg',{width:13,height:13,viewBox:'0 0 24 24',fill:'currentColor'},h('path',{d:'M7 5l12 7-12 7z'})); }

export { iconPause, iconPlay, iconX };
