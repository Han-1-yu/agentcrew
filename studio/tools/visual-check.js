'use strict';
// 视觉回归：electron tools/visual-check.js [--update]
// 基线取自 e2e 的 16 张截图（e2e-out），与 visual-baseline/ 逐张比对。
//
// 为什么不用 pixelmatch：本项目承诺「零框架、零打包器、唯一依赖 electron」，
// 所以只用 Electron 自带的 nativeImage 解码 PNG。
// 比较分两级：160px 看整页版式是否位移，480px + 16px 分块看局部是否被改（按钮换色、
// 间距变化都会顶到分块差）；同代码重复跑 e2e 的实测噪声为 0，因此阈值可以定得很严。

const fs = require('fs');
const path = require('path');
const { app, nativeImage } = require('electron');

const ROOT = path.join(__dirname, '..');
const CUR_DIR = path.join(ROOT, 'e2e-out');
const BASE_DIR = path.join(ROOT, 'visual-baseline');
// 两级分辨率：全局看版式（160），局部看细节（480 + 16px 分块）
const GLOBAL_WIDTH = 160;
const LOCAL_WIDTH = 480;
const TILE = 16;

// 阈值（同一份代码重复跑 e2e 的实测噪声为 0，因此可以定得比较严）
const MAX_MEAN_DELTA = 1.0;    // 全局平均通道差
const MAX_DIFF_RATIO = 0.005;  // 全局明显差异像素占比
// 分块阈值取 20：看板上有「3 分钟前」这类相对时间文案，两次 e2e 之间会自然变化，
// 实测单块最高 15.2；而真的换一次强调色会到 42。20 落在噪声与真实改动之间。
const MAX_TILE_DELTA = 20;

const update = process.argv.includes('--update');

function decode(file, width) {
  const img = nativeImage.createFromPath(file);
  if (img.isEmpty()) return null;
  const small = img.resize({ width, quality: 'good' });
  const size = small.getSize();
  return { bmp: small.toBitmap(), width: size.width, height: size.height };
}

/** 逐像素比较：返回全局平均差、明显差异像素占比、以及最大分块平均差。 */
function compare(a, b) {
  if (a.width !== b.width || a.height !== b.height) {
    return { mean: 255, ratio: 1, maxTile: 255, sizeMismatch: true };
  }
  const n = Math.min(a.bmp.length, b.bmp.length);
  let sum = 0, diffPx = 0, px = 0;
  const tilesX = Math.ceil(a.width / TILE);
  const tiles = new Float64Array(tilesX * Math.ceil(a.height / TILE));
  const tileCount = new Float64Array(tiles.length);

  for (let i = 0; i + 3 < n; i += 4) {
    const d = (Math.abs(a.bmp[i] - b.bmp[i])
      + Math.abs(a.bmp[i + 1] - b.bmp[i + 1])
      + Math.abs(a.bmp[i + 2] - b.bmp[i + 2])) / 3;
    sum += d;
    if (d > 18) diffPx += 1;
    const idx = i / 4;
    const x = idx % a.width;
    const y = Math.floor(idx / a.width);
    const t = Math.floor(y / TILE) * tilesX + Math.floor(x / TILE);
    tiles[t] += d;
    tileCount[t] += 1;
    px += 1;
  }
  let maxTile = 0;
  for (let t = 0; t < tiles.length; t += 1) {
    if (tileCount[t]) maxTile = Math.max(maxTile, tiles[t] / tileCount[t]);
  }
  return { mean: px ? sum / px : 0, ratio: px ? diffPx / px : 0, maxTile };
}

app.whenReady().then(() => {
  if (!fs.existsSync(CUR_DIR)) {
    console.error('没有找到 e2e-out/，请先跑 npm run e2e 生成截图');
    app.exit(2);
    return;
  }
  fs.mkdirSync(BASE_DIR, { recursive: true });

  const shots = fs.readdirSync(CUR_DIR).filter((f) => f.endsWith('.png')).sort();
  if (!shots.length) {
    console.error('e2e-out/ 里没有 PNG 截图');
    app.exit(2);
    return;
  }

  if (update) {
    for (const f of shots) fs.copyFileSync(path.join(CUR_DIR, f), path.join(BASE_DIR, f));
    console.log(`基线已更新：${shots.length} 张 → ${path.relative(ROOT, BASE_DIR)}/`);
    app.exit(0);
    return;
  }

  const rows = [];
  let failed = 0, missing = 0;
  for (const f of shots) {
    const basePath = path.join(BASE_DIR, f);
    if (!fs.existsSync(basePath)) {
      rows.push({ name: f, state: '新增', mean: '-', ratio: '-', missing: true });
      missing += 1;
      continue;
    }
    const g = compare(decode(basePath, GLOBAL_WIDTH), decode(path.join(CUR_DIR, f), GLOBAL_WIDTH));
    const l = compare(decode(basePath, LOCAL_WIDTH), decode(path.join(CUR_DIR, f), LOCAL_WIDTH));
    if (!g || !l) {
      rows.push({ name: f, state: '无法解码', mean: '-', ratio: '-', tile: '-' });
      failed += 1;
      continue;
    }
    const bad = g.mean > MAX_MEAN_DELTA || g.ratio > MAX_DIFF_RATIO || l.maxTile > MAX_TILE_DELTA;
    if (bad) failed += 1;
    rows.push({
      name: f,
      state: bad ? '差异过大' : (g.ratio > MAX_DIFF_RATIO / 3 || l.maxTile > MAX_TILE_DELTA / 2 ? '轻微差异' : '一致'),
      mean: g.mean.toFixed(2),
      ratio: (g.ratio * 100).toFixed(2) + '%',
      tile: l.maxTile.toFixed(1),
    });
  }

  const pad = (s, n) => String(s).padEnd(n, ' ');
  console.log(pad('截图', 30) + pad('判定', 10) + pad('全局差', 8) + pad('变化像素', 10) + '最大分块差');
  for (const r of rows) console.log(pad(r.name, 30) + pad(r.state, 10) + pad(r.mean, 8) + pad(r.ratio, 10) + r.tile);

  if (missing) console.log(`\n提示：${missing} 张截图还没有基线，跑 npm run visual:update 建立。`);
  if (failed) {
    console.log(`\n视觉回归未通过：${failed} 张超过阈值（全局差 > ${MAX_MEAN_DELTA} 或变化像素 > ${MAX_DIFF_RATIO * 100}% 或分块差 > ${MAX_TILE_DELTA}）`);
    app.exit(1);
    return;
  }
  console.log(`\n视觉回归通过：${rows.length} 张截图与基线一致（阈值：全局差 ≤ ${MAX_MEAN_DELTA}，变化像素 ≤ ${MAX_DIFF_RATIO * 100}%，分块差 ≤ ${MAX_TILE_DELTA}）`);
  app.exit(0);
});
