'use strict';
// 极简 ZIP 写入器：仅 STORE（不压缩），无第三方依赖。
// 用于把 run 的 artifacts/ 目录打包导出；文件均来自本次 run 的文本产物，体积可控。

const CRC_TABLE = (() => {
  const t = new Int32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = (c & 1) ? (0xedb88320 ^ (c >>> 1)) : (c >>> 1);
    t[n] = c;
  }
  return t;
})();

function crc32(buf) {
  let c = -1;
  for (let i = 0; i < buf.length; i++) c = CRC_TABLE[(c ^ buf[i]) & 0xff] ^ (c >>> 8);
  return (c ^ -1) >>> 0;
}

function dosTimeDate(d) {
  // DOS 时间字段精度 2 秒；ZIP 用本地时间
  const time = ((d.getHours() & 0x1f) << 11) | ((d.getMinutes() & 0x3f) << 5) | ((d.getSeconds() / 2) & 0x1f);
  const date = (((d.getFullYear() - 1980) & 0x7f) << 9) | (((d.getMonth() + 1) & 0x0f) << 5) | (d.getDate() & 0x1f);
  return { time, date };
}

function u16(v) { const b = Buffer.alloc(2); b.writeUInt16LE(v >>> 0); return b; }
function u32(v) { const b = Buffer.alloc(4); b.writeUInt32LE(v >>> 0); return b; }

// entries: [{ name, data:Buffer, mtime:Date }]
function zipStore(entries) {
  const locals = [];
  const centrals = [];
  let offset = 0;
  for (const e of entries) {
    const nameBuf = Buffer.from(e.name, 'utf8');
    const crc = crc32(e.data);
    const { time, date } = dosTimeDate(e.mtime || new Date());
    const flag = 0x0800; // UTF-8 文件名
    const local = Buffer.concat([
      u32(0x04034b50), u16(20), u16(flag), u16(0), u16(time), u16(date),
      u32(crc), u32(e.data.length), u32(e.data.length), u16(nameBuf.length), u16(0),
      nameBuf, e.data,
    ]);
    const central = Buffer.concat([
      u32(0x02014b50), u16(20), u16(20), u16(flag), u16(0), u16(time), u16(date),
      u32(crc), u32(e.data.length), u32(e.data.length), u16(nameBuf.length),
      u16(0), u16(0), u16(0), u16(0), u32(0), u32(offset), nameBuf,
    ]);
    locals.push(local);
    centrals.push(central);
    offset += local.length;
  }
  const centralSize = centrals.reduce((s, c) => s + c.length, 0);
  const eocd = Buffer.concat([
    u32(0x06054b50), u16(0), u16(0), u16(entries.length), u16(entries.length),
    u32(centralSize), u32(offset), u16(0),
  ]);
  return Buffer.concat([...locals, ...centrals, eocd]);
}

// 收集 dir 下全部文件（相对路径用正斜杠，ZIP 规范要求），统一加 prefixPrefix 前缀
function buildZipFromDir(dir, prefix) {
  const fs = require('fs');
  const path = require('path');
  const entries = [];
  const walk = (rel) => {
    let ents = [];
    try { ents = fs.readdirSync(path.join(dir, rel), { withFileTypes: true }); } catch { return; }
    for (const ent of ents) {
      const rel2 = rel ? `${rel}/${ent.name}` : ent.name;
      if (ent.isDirectory()) walk(rel2);
      else {
        let data;
        try { data = fs.readFileSync(path.join(dir, rel2)); } catch { continue; }
        let mtime = new Date();
        try { mtime = fs.statSync(path.join(dir, rel2)).mtime; } catch { /* 用当前时间 */ }
        entries.push({ name: prefix ? `${prefix}/${rel2}` : rel2, data, mtime });
      }
    }
  };
  walk('');
  if (!entries.length) throw new Error('产物目录为空或不存在');
  return zipStore(entries);
}

module.exports = { zipStore, buildZipFromDir, crc32 };
