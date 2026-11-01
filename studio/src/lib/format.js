/**
 * 展示层格式化：token / 金额 / 时长 / 时间 / 字节 / 截断。
 * 全部纯函数，无 DOM 依赖。
 */

function fmtTokens(n) { return n == null ? '—' : Number(n).toLocaleString('en-US'); }
function fmtCost(n) { if (n==null) return '—'; const v=Number(n); return `$${v.toFixed(4).replace(/0+$/,'').replace(/\.$/,'')}`; }
function fmtDur(ms) {
  if (ms==null) return '—';
  if (ms<1000) return `${Math.round(ms)}ms`;
  const sec=ms/1000; if (sec<60) return `${sec.toFixed(1)}s`;
  const m=Math.floor(sec/60); if (m<60) return `${m}m${Math.round(sec%60)}s`;
  return `${Math.floor(m/60)}h${m%60}m`;
}
function fmtBytes(n) { if(n==null)return'—'; if(n<1024)return`${n} B`; if(n<1048576)return`${(n/1024).toFixed(1)} KB`; return `${(n/1048576).toFixed(2)} MB`; }
// ledger ts: "YYYY-MM-DD HH:MM:SS.ffffff"（空格分隔，无时区）
function parseTs(ts) { if(!ts) return null; const d=new Date(String(ts).replace(' ','T')); return isNaN(d)?null:d; }
function fmtClock(ts){ const d=parseTs(ts); return d?d.toTimeString().slice(0,8):String(ts||''); }
function fmtDateTime(ts){ const d=parseTs(ts); if(!d)return'—'; const p=x=>String(x).padStart(2,'0'); return `${p(d.getMonth()+1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`; }
function relTime(ts){ const d=parseTs(ts); if(!d)return''; const diff=(Date.now()-d.getTime())/1000; if(diff<5)return'刚刚'; if(diff<60)return`${Math.floor(diff)} 秒前`; if(diff<3600)return`${Math.floor(diff/60)} 分钟前`; if(diff<86400)return`${Math.floor(diff/3600)} 小时前`; return `${Math.floor(diff/86400)} 天前`; }
function trunc(s,n){ s=String(s==null?'':s); return s.length>n?s.slice(0,n)+'…':s; }
/* 从完整项目目标中提取简洁标题：取冒号/句号前的核心短语 */
function simplifyGoal(goal){
  if(!goal) return '未命名项目';
  let s=goal.trim();
  // 优先取第一个冒号前（中英文冒号）
  const colonIdx=Math.min(...[s.indexOf('：'),s.indexOf(':')].filter(i=>i>=0).concat([Infinity]));
  if(colonIdx<Infinity && colonIdx>4) s=s.substring(0,colonIdx);
  else{
    // 否则取第一句话
    const stopIdx=Math.min(...[s.indexOf('。'),s.indexOf('. '),s.indexOf('！'),s.indexOf('!')].filter(i=>i>=0).concat([Infinity]));
    if(stopIdx<Infinity && stopIdx>6) s=s.substring(0,stopIdx);
  }
  s=s.trim();
  // 兜底：超过 28 字符截断
  return s.length>28 ? s.substring(0,28)+'…' : s;
}

export {
  fmtBytes,
  fmtClock,
  fmtCost,
  fmtDateTime,
  fmtDur,
  fmtTokens,
  parseTs,
  relTime,
  simplifyGoal,
  trunc,
};
