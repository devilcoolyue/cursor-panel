const $ = (s) => document.querySelector(s);
const view = $('#view');
PanelUI.init();
const openModal = (dialog, options) => PanelUI.open(dialog, options);

let token = localStorage.getItem('panelToken') || '';
let isAdmin = false;
let adminCsrf = '';
let adminSessionRequest = null;
let activeWorkspace = 'quota';
const headers = () => ({ ...(token ? { 'X-Panel-Token': token } : {}),
  ...(isAdmin && adminCsrf ? { 'X-Admin-CSRF': adminCsrf } : {}) });
const jsonHeaders = () => ({ 'Content-Type': 'application/json', ...headers() });

async function syncAdminSession() {
  if (adminSessionRequest) return adminSessionRequest;
  adminSessionRequest = (async () => {
    await AdminWorkspace.checkSession();
    applyAdminSession(AdminWorkspace.session);
  })().finally(() => { adminSessionRequest = null; });
  return adminSessionRequest;
}

function applyAdminSession(state) {
  const previous = isAdmin;
  isAdmin = state?.authenticated === true;
  adminCsrf = isAdmin ? state.csrf_token || '' : '';
  $('#admin-menu-link').hidden = !isAdmin;
  if (previous && !isAdmin) {
    accounts.forEach(account => { account.can_switch = false; });
    if (switchDlg.open) PanelUI.close(switchDlg);
  }
  if (previous !== isAdmin && accounts.length) render();
}

document.addEventListener('panel:admin-session', event => {
  applyAdminSession(event.detail);
  // Invalidate in-flight quota responses when their administrator identity changes.
  ++loadGeneration;
  loadController?.abort();
  inFlight = false;
  if (isAdmin || activeWorkspace === 'quota') load({ adminChecked: true });
});

const ALL_DEPARTMENTS = '__all_departments__';
const NEW_DEPARTMENT = '__new_department__';
const SORT_ADDED = 'added';
const SORT_REMAINING_ASC = 'remaining-asc';
const SORT_REMAINING_DESC = 'remaining-desc';
const SORT_RESET_ASC = 'reset-asc';
const SORT_RESET_DESC = 'reset-desc';
const SORT_MODES = new Set([
  SORT_ADDED, SORT_REMAINING_ASC, SORT_REMAINING_DESC, SORT_RESET_ASC, SORT_RESET_DESC
]);
const storedDepartment = localStorage.getItem('selectedDepartment');
let selectedDepartment = storedDepartment === null ? ALL_DEPARTMENTS : storedDepartment;
const storedSortMode = localStorage.getItem('accountSort');
let sortMode = SORT_MODES.has(storedSortMode) ? storedSortMode : SORT_ADDED;
let accounts = [];   // 当前部门的卡片，按索引顺序逐张替换加载结果
let departmentSummary = [];
let totalAccountCount = 0;
let searchQuery = '';
let loadGeneration = 0;
let loadController = null;
let inFlight = false;
let autoRefreshCycle = 0;          // 后台轮一遍所有账号要多久，由 /api/config 给
const refreshingAccounts = new Set();
const POLL_INTERVAL = 60000;       // 只读服务端快照，不打 cursor.com

const money = (n) => '$' + Number(n || 0).toFixed(2);
// 额度上限基本都是整美元（实测 $450 / $45 / $495），整数就别拖两位小数了
const shortMoney = (n) => {
  const v = Number(n || 0);
  return '$' + (Math.abs(v - Math.round(v)) < 0.005 ? Math.round(v) : v.toFixed(2));
};
// token 数动辄上亿，表格里按 Cursor 自己的写法缩成 M / K，精确值挂 title
const shortTokens = (n) => {
  const v = Number(n || 0);
  if (!v) return '—';
  if (v >= 1e6) return (v / 1e6).toFixed(2) + 'M';
  if (v >= 1e3) return (v / 1e3).toFixed(v >= 1e4 ? 0 : 1) + 'K';
  return String(v);
};
const exactTokens = (n) => Number(n || 0).toLocaleString('en-US') + ' tokens';
const pad2 = (n) => String(n).padStart(2, '0');
const parseDate = (iso) => {
  const value = iso ? new Date(iso) : null;
  return value && !Number.isNaN(value.getTime()) ? value : null;
};
const localDate = (iso) => {
  const value = parseDate(iso);
  return value
    ? `${value.getFullYear()}-${pad2(value.getMonth() + 1)}-${pad2(value.getDate())}`
    : '—';
};
const localDateTime = (iso) => {
  const value = parseDate(iso);
  return value
    ? `${localDate(iso)} ${pad2(value.getHours())}:${pad2(value.getMinutes())}`
    : '—';
};

function relative(ms, now = Date.now()) {
  const diff = now - ms;
  if (diff < 60000) return '刚刚';
  const minutes = Math.floor(diff / 60000);
  if (minutes < 60) return `${minutes} 分钟前`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} 小时前`;
  return `${Math.floor(hours / 24)} 天前`;
}

// 后台是错开刷的，每张卡的统计时刻都不一样，所以时间要落在卡片上而不是页头
function statTime(iso) {
  const value = parseDate(iso);
  if (!value) return '—';
  const clock = `${pad2(value.getHours())}:${pad2(value.getMinutes())}`;
  const sameDay = new Date().toDateString() === value.toDateString();
  const stamp = sameDay ? clock
    : `${pad2(value.getMonth() + 1)}-${pad2(value.getDate())} ${clock}`;
  return `${stamp} · ${relative(value.getTime())}`;
}

// ---------- 卡片显示项 ----------
// 这张表就是全部事实来源：卡片按它决定渲不渲染，设置面板也按它生成勾选行，
// 加一个开关只要在这儿多写一条。顺序 = 面板里的排列顺序
const CARD_OPTIONS = [
  { group: '卡片装饰', key: 'ribbon', label: '额度角标', hint: '左上角三角，按综合剩余分四档' },
  { group: '卡片装饰', key: 'watermark', label: '燃尽水印', hint: '额度用尽的卡片盖上「燃尽了」；卡片底色不受影响' },
  { group: '卡片装饰', key: 'cycleRing', label: '周期环', hint: '右上角环形进度，中心是距下次刷新还剩多久' },
  { group: '身份区', key: 'plan', label: '套餐徽章', hint: '名字旁边的 Pro / Business' },
  { group: '身份区', key: 'department', label: '部门标记', hint: '邮箱前面那条带竖线的部门名' },
  { group: '身份区', key: 'email', label: '邮箱', hint: '关掉便于截图外发；搜索仍然可以按邮箱匹配' },
  { group: '身份区', key: 'limit', label: '额度上限', hint: '每条额度名后面的 $450，服务端反解得出来才有' },
  { group: '信息行', key: 'statTime', label: '最后统计', hint: '这张卡上一次取数成功的时刻' },
  { group: '信息行', key: 'reset', label: '额度刷新', hint: '下次重置的日期时间和剩余时长' },
  { group: '信息行', key: 'spend', label: '本周期消费', hint: '已花金额 / 综合额度池上限' },
  { group: '信息行', key: 'onDemand', label: '按量付费', hint: '是否开启以及已用金额' },
  { group: '信息行', key: 'grok', label: 'Grok Bot 周额度', hint: '关掉只是不显示，后台照常请求这个接口' },
];

const PREFS_KEY = 'panelCardPrefs';
const OPTION_DEFAULTS = Object.fromEntries(CARD_OPTIONS.map((o) => [o.key, o.default !== false]));

function readPrefs() {
  try {
    const raw = JSON.parse(localStorage.getItem(PREFS_KEY) || '{}');
    // 只认表里还有的键、只认布尔值：删掉的开关自动失效，手改坏的值也渗不进渲染
    return Object.fromEntries(Object.entries(raw)
      .filter(([key, value]) => key in OPTION_DEFAULTS && typeof value === 'boolean'));
  } catch { return {}; }
}

// 合并而不是直接拿存量当配置：以后新增的开关自动带上默认值，
// 老用户升级后不会因为 localStorage 里缺字段就把新元素丢掉
let cardPrefs = { ...OPTION_DEFAULTS, ...readPrefs() };
const shows = (key) => cardPrefs[key] !== false;

function savePrefs() {
  try { localStorage.setItem(PREFS_KEY, JSON.stringify(cardPrefs)); } catch { /* 隐私模式写不进去，本次会话照样生效 */ }
}

const color = (r) => r <= 10 ? 'var(--bad)' : r <= 30 ? 'var(--warn)' : 'var(--ok)';

// 左上角丝带的四档，阈值跟 color() 同源——两处颜色对不上的话，卡片自己就自相矛盾了
function quotaTier(quota) {
  const r = quota?.overall?.remaining_pct ?? null;
  if (r == null) return null;
  if (r <= 0) return { cls: 'dry', text: '燃尽' };
  if (r <= 10) return { cls: 'bad', text: '告急' };
  if (r <= 30) return { cls: 'warn', text: '偏紧' };
  return { cls: 'ok', text: '充足' };
}

function ribbon(tier) {
  // 纯视觉冗余：同一件事下面三条额度条已经用百分比说过了，读屏不必再念一遍
  return tier ? `<span class="ribbon ${tier.cls}" aria-hidden="true"><b>${tier.text}</b></span>` : '';
}

const RING_R = 19;
const RING_C = 2 * Math.PI * RING_R;

// meta 行里的剩余时间。跟环中心的字同一口径，只是这里写得开，能说完整的"剩1天20小时"
function remainingUntil(iso, now = Date.now()) {
  const reset = parseDate(iso);
  if (!reset) return '';
  const remainingMs = reset.getTime() - now;
  if (remainingMs <= 0) return '已到刷新时间';
  const hours = Math.floor(remainingMs / 3600000);
  if (hours < 1) return '剩不足1小时';
  if (hours < 24) return `剩${hours}小时`;
  if (hours < 48) return `剩1天${hours - 24}小时`;
  return `剩${Math.floor(hours / 24)}天`;
}

// 环中心的字：剩余时间一律从 reset_at 现算（浏览器本地时区），不用后端向下取整的
// days_left；不足 48 小时就报到小时，免得"剩 1 天"把 47 小时和 25 小时说成一回事
function remainingParts(resetMs, now = Date.now()) {
  const left = resetMs - now;
  if (left <= 0) return { num: '已到', unit: '' };
  const hours = Math.floor(left / 3600000);
  if (hours < 1) return { num: '<1', unit: '时' };
  if (hours < 48) return { num: String(hours), unit: '时' };
  return { num: String(Math.floor(hours / 24)), unit: '天' };
}

// 周期环：环走过的比例 = 本周期已经过去的比例。
// cycle.start 缺失（升级前存的旧快照就没有）时只报剩余时间、不画弧——
// 拿 30 天猜一个周期长度画出来的弧是假的，宁可空着
function cycleRing(cycle, now = Date.now()) {
  const end = parseDate(cycle.reset_at);
  if (!end) return '';
  const start = parseDate(cycle.start);
  const span = start ? end.getTime() - start.getTime() : 0;
  const left = end.getTime() - now;
  const passed = span > 0 ? Math.min(1, Math.max(0, (span - left) / span)) : 0;
  const { num, unit } = remainingParts(end.getTime(), now);
  const soon = left > 0 && left <= 86400000;
  // 环和丝带都 aria-hidden：剩余时间 meta 行念得更全（"剩1天20小时"），
  // 额度档位额度条也念过百分比，读屏没必要再听一遍
  return `<span class="cycle-ring${soon ? ' soon' : ''}" aria-hidden="true">
    <svg viewBox="0 0 44 44">
      <circle class="ring-track" cx="22" cy="22" r="${RING_R}"/>
      <circle class="ring-fill" cx="22" cy="22" r="${RING_R}"
        stroke-dasharray="${RING_C.toFixed(1)}"
        stroke-dashoffset="${(RING_C * (1 - passed)).toFixed(1)}"/>
    </svg>
    <span class="ring-text"><b>${num}</b>${unit}</span>
  </span>`;
}

const quotaExhausted = (quota) =>
  quota?.overall?.remaining_pct != null && Number(quota.overall.remaining_pct) <= 0;
const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const SVG = (d) => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
  stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${d}</svg>`;
const ICON = {
  department: SVG('<path d="M6 22V4c0-.6.4-1 1-1h10c.6 0 1 .4 1 1v18"/><path d="M6 12H4c-.6 0-1 .4-1 1v9h18V8c0-.6-.4-1-1-1h-2"/><path d="M10 6h4M10 10h4M10 14h4M10 18h4"/>'),
  key: SVG('<circle cx="7.5" cy="16.5" r="5"/><path d="m11.5 13 8.5-8.5"/><path d="m17 7 2.5 2.5"/><path d="M20 4l2 2"/>'),
  refresh: SVG('<path d="M21 12a9 9 0 1 1-2.64-6.36"/><path d="M21 3v6h-6"/>'),
  chevron: SVG('<path d="m9 18 6-6-6-6"/>'),
  external: SVG('<path d="M7 17 17 7"/><path d="M9 7h8v8"/>'),
  switchAccount: SVG('<path d="M7 7h14l-4-4M17 17H3l4 4M21 7l-4 4M3 17l4-4"/>'),
  copy: SVG('<rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>'),
  trash: SVG('<path d="M3 6h18"/><path d="M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2"/><path d="M18 6v14a1 1 0 0 1-1 1H7a1 1 0 0 1-1-1V6"/>'),
};

const GROK_URL = 'https://cursor.com/download/bot';
const GROK_TIP = 'Grok Bot 是 x.ai 的独立 App，使用 Cursor 账号登录，额度单独结算，'
  + '不占用上方的 Cursor Models / Other Models，按周重置。'
  + '拥有额度但尚未使用时显示剩余 100%。';

// 这条提示不走通用的 data-tip：里面的下载地址要能点，而 ::after 伪元素塞不进链接
function grokHint() {
  return `<span class="grok-hint">Grok Bot 周额度<span class="grok-card"><span
    class="grok-card-inner">${esc(GROK_TIP)}<br>下载地址：<a href="${GROK_URL}"
    target="_blank" rel="noopener noreferrer">${GROK_URL}${ICON.external}</a>
  </span></span></span>`;
}

const quotaLimitText = q => `${q.limit_source === 'exhaustion' ? '≈' : ''}${shortMoney(q.limit_usd)}`;
function quotaLimitHint(q) {
  if (q.limit_source === 'exhaustion') return '已耗尽且未使用按量付费，消费接近此额度档位（超额不超过 1%）；参考同套餐账号估算，并非官方确认上限。';
  if (q.limit_source === 'history') return '参考本账号同一账期此前推算的上限。';
  if (q.limit_inferred) return '参考账期重叠、容量相符的同套餐账号估算的上限。';
  return '根据本账号消费金额和用量比例推算的上限。';
}

// 本次反解、同账期历史及同套餐参考上限；耗尽消费辅助估算须明确标出近似值。
function bar(name, q, id, group) {
  const r = q.remaining_pct;
  const limit = !shows('limit') || q.limit_usd == null ? ''
    : `<span class="q-limit" title="${esc(quotaLimitHint(q))}">${esc(quotaLimitText(q))}</span>`;
  return `<button type="button" class="q-row tip left" data-detail="${esc(id)}"
      data-group="${esc(group)}" data-tip="查看详情 · 本周期用了哪些模型">
    <span class="q-name">${name}${limit}</span>
    <span class="q-val" style="color:${color(r)}">剩 ${r.toFixed(1)}%</span>
    <span class="q-go" aria-hidden="true">${ICON.chevron}</span>
    <div class="track"><div class="fill" style="width:${Math.min(100, q.used_pct)}%;background:${color(r)}"></div></div>
  </button>`;
}

const overspent = (overall, spend) =>
  overall.limit_usd != null && spend.total > overall.limit_usd + 0.005;

// 超出上限的部分标红。Cursor 允许小幅超一点（实测用满的账号停在 $495.3 上下），
// 这时"剩 0.0%"和"$495.32 / $495"说的是同一件事，红字把超出说清楚
function spendValue(overall, spend) {
  const amount = overspent(overall, spend)
    ? `<span style="color:var(--bad)">${money(spend.total)}</span>`
    : money(spend.total);
  return overall.limit_usd == null ? amount
    : `${amount} / ${esc(quotaLimitText(overall))}`;
}

// 分母用综合额度池，跟上面那条「综合 剩 x%」同口径。订阅价 $20 是另一回事——
// 它只是"包含额度"，超出部分走 Cursor 赠送额度，摆在分母上会让人以为超支了 10 倍
function spendTip(overall, plan, spend) {
  const included = `订阅本身含额度 ${money(plan.included_usd)}`;
  if (overall.limit_usd == null) return included;
  const over = overspent(overall, spend)
    ? `已超出上限 ${money(spend.total - overall.limit_usd)}。` : '';
  return `${over}${included}，超出的走 Cursor 赠送额度。分母 ${quotaLimitText(overall)} 是本周期两类模型额度的合计上限。${quotaLimitHint(overall)}`;
}

// 部门 + 邮箱那一行。两个都关掉时连容器一起省掉，免得留一条 2px 的空行
function identityMeta(department, email) {
  const dept = shows('department')
    ? `<span class="department-mark">${esc(departmentName(department))}</span>` : '';
  const mail = shows('email')
    ? `<span class="email" title="${esc(email)}">${esc(email)}</span>` : '';
  return dept || mail ? `<div class="identity-meta">${dept}${mail}</div>` : '';
}

function actions(a) {
  return `<div class="acts">
    ${a.can_switch === true ? `<button class="icon-btn tip below" data-switch-id="${esc(a.id)}" aria-label="切换账号"
      data-tip="切换本机 Cursor 账号">${ICON.switchAccount}</button>` : ''}
    <button class="icon-btn tip below" data-dept-id="${esc(a.id)}" data-label="${esc(a.label)}"
      data-department="${esc(a.department)}" aria-label="调整分组"
      data-tip="调整所属部门">${ICON.department}</button>
    <button class="icon-btn tip below" data-edit="${esc(a.id)}" data-label="${esc(a.label)}"
      data-department="${esc(a.department)}" aria-label="重新授权"
      data-tip="重新授权 · 更新桌面会话">${ICON.key}</button>
    <button class="icon-btn tip below" data-one="${esc(a.id)}" aria-label="刷新账号"
      data-tip="刷新这个账号的额度">${ICON.refresh}</button>
    <button class="icon-btn tip below right danger" data-del="${esc(a.id)}" aria-label="删除账号"
      data-tip="删除账号">${ICON.trash}</button>
  </div>`;
}

function skeleton(account = null, note = '') {
  const head = account && account.id
    ? `<div class="card-head">
        <div class="name-line"><span class="label">${esc(account.label)}</span></div>
        ${identityMeta(account.department, account.email)}
      </div>`
    : `<div class="skel skel-line" style="width:38%;height:14px"></div>
       <div class="skel skel-line" style="width:58%;margin-bottom:22px"></div>`;
  return `<div class="card" aria-busy="true"${account?.id ? ` data-account-id="${esc(account.id)}"` : ''}>
    ${head}
    <div class="skel skel-line" style="width:100%"></div>
    <div class="skel skel-line" style="width:100%"></div>
    <div class="skel skel-line" style="width:100%;margin-bottom:22px"></div>
    <div class="skel skel-line" style="width:72%"></div>
    <div class="skel skel-line" style="width:64%"></div>
    <div class="skel" style="width:80%"></div>
    ${note ? `<div class="skel-note">${esc(note)}</div>` : ''}
  </div>`;
}

// 刷新失败但还留着上一份数据时的说明。这里刻意不说"会话失效"——被限流时
// 说失效会把人骗去重新粘贴 cookie，而那次粘贴同样会被挡住。
const STALE_TEXT = {
  rate_limited: 'Cursor 暂时限制了请求，后台会自动重试',
  expired: '刷新时被判为会话失效，正在确认',
  network: '刷新时连不上 Cursor，后台会自动重试',
};
const staleText = (a) =>
  `${STALE_TEXT[a.error_kind] || a.error || '刷新失败'}；上面仍是上一次统计的数据`;

function card(a) {
  // 单卡刷新期间整张卡退回骨架。试过"保留数据 + aria-busy + 按钮转圈"，
  // 数据一个字没变，肉眼根本看不出点没点动——闪烁的骨架才是唯一看得出来的信号。
  if (refreshingAccounts.has(a.id)) return skeleton(a, '正在向 Cursor 重新取数…');
  if (a.pending) return skeleton(a, '排队等待后台更新…');

  if (!a.ok) {
    return `<div class="card dead" data-account-id="${esc(a.id)}">${actions(a)}
      <div class="card-head">
        <div class="name-line"><span class="label">${esc(a.label)}</span></div>
        ${identityMeta(a.department, a.email)}
      </div>
      <div class="err">${a.expired ? '桌面授权已失效，点钥匙图标重新粘贴有效 Cookie 授权' : esc(a.error)}</div>
      ${shows('statTime') && a.ok_at ? `<div class="meta"><div><span>最后统计</span>
        <b>${esc(statTime(a.ok_at))}</b></div></div>` : ''}
    </div>`;
  }

  const d = a.data, q = d.quota, c = d.cycle, p = d.plan, s = d.spend_usd, od = d.on_demand;
  const grok = d.grok_weekly;
  const exhausted = quotaExhausted(q);
  const cycleRemaining = remainingUntil(c.reset_at);
  // meta 行逐条收集：全关掉时连 .meta 容器一起省掉，否则会剩一条 border-top
  // 的横线孤零零挂在额度条下面。三元一律配 '' 收尾——模板串里写 cond && html，
  // 条件为假时进页面的是字符串 "false"
  const meta = [];
  if (shows('statTime')) {
    meta.push(`<div><span>最后统计</span><b>${esc(statTime(a.ok_at))}</b></div>`);
  }
  if (shows('reset')) {
    meta.push(`<div><span>额度刷新</span><b>${localDateTime(c.reset_at)}${cycleRemaining ? ` · ${cycleRemaining}` : ''}</b></div>`);
  }
  if (shows('spend')) {
    meta.push(`<div><span class="tip left" data-tip="${esc(spendTip(q.overall, p, s))}">本周期消费</span>
      <b>${spendValue(q.overall, s)}</b></div>`);
  }
  if (shows('onDemand')) {
    meta.push(`<div><span>按量付费</span><b>${od.enabled ? '开启 · 已用 ' + money(od.used_usd) : '关闭'}</b></div>`);
  }
  if (shows('grok') && grok) {
    meta.push(`<div>
      ${grokHint()}
      <b style="color:${color(grok.remaining_pct)}">剩 ${grok.remaining_pct.toFixed(1)}%<span style="color:var(--dimmer);font-weight:400"> · ${localDate(grok.reset_at)} 重置</span></b>
    </div>`);
  }
  return `<div class="card${exhausted ? ' exhausted' : ''}" data-account-id="${esc(a.id)}">
    ${shows('watermark') && exhausted ? '<span class="burnout-watermark" aria-hidden="true"><span>燃</span><span>尽</span><span>了</span></span>' : ''}
    ${shows('ribbon') ? ribbon(quotaTier(q)) : ''}
    ${shows('cycleRing') ? cycleRing(c) : ''}
    ${actions(a)}
    <div class="card-head">
      <div class="name-line">
        <span class="label">${esc(a.label)}</span>
        ${shows('plan') ? `<span class="plan">${esc(p.name || p.membership_type || '未知套餐')}</span>` : ''}
      </div>
      ${identityMeta(a.department, d.email)}
    </div>
    <div class="quota">
      ${bar('Cursor Models', q.cursor_models, a.id, 'cursor_models')}
      ${bar('Other Models', q.other_models, a.id, 'other_models')}
      ${bar('综合', q.overall, a.id, 'overall')}
    </div>
    ${meta.length ? `<div class="meta">${meta.join('')}</div>` : ''}
    ${a.stale ? `<div class="stale">⚠ ${esc(staleText(a))}</div>` : ''}
  </div>`;
}

const departmentName = (department) => department || '未分组';

function departmentStats() {
  return departmentSummary.map((item) => [item.department || '', item.count])
    .sort(([a], [b]) => {
      if (!a) return 1;
      if (!b) return -1;
      return a.localeCompare(b, 'zh-CN');
    });
}

let departmentTabsKey = '';
function renderDepartmentTabs() {
  const stats = departmentStats();
  const tabs = [[ALL_DEPARTMENTS, '全部账号', totalAccountCount],
    ...stats.map(([department, count]) => [department, departmentName(department), count])];
  const tabsEl = $('#department-tabs');
  const tabsKey = JSON.stringify(tabs);
  if (tabsEl && departmentTabsKey !== tabsKey) {
    departmentTabsKey = tabsKey;
    tabsEl.innerHTML = tabs.map(([department, name, count]) => {
      const isAll = department === ALL_DEPARTMENTS;
      const isSelected = activeWorkspace === 'quota' && department === selectedDepartment;
      return `
      <button class="dept-nav-item" type="button" role="tab"
        data-department-filter="${esc(department)}"
        aria-selected="${isSelected}">
        <span class="dept-icon-box">
          ${isAll 
            ? `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/></svg>`
            : `<span class="dept-dot"></span>`
          }
        </span>
        <span class="dept-name">${esc(name)}</span>
        <span class="dept-count">${count}</span>
      </button>`;
    }).join('');
  }
  tabsEl?.querySelectorAll('[data-department-filter]').forEach((button) => {
    button.setAttribute('aria-selected', String(activeWorkspace === 'quota'
      && button.dataset.departmentFilter === selectedDepartment));
  });
  GlassMotion.selection(tabsEl, tabsEl?.querySelector('[aria-selected="true"]'));

  const titleEl = $('#current-view-title');
  if (titleEl) {
    titleEl.textContent = activeWorkspace === 'admin' ? '系统管理'
      : selectedDepartment === ALL_DEPARTMENTS ? '全部账号' : departmentName(selectedDepartment);
  }
}

function populateDepartmentSelect(select, newInput, current = '') {
  const departments = departmentStats()
    .map(([department]) => department)
    .filter(Boolean);
  if (current && !departments.includes(current)) departments.push(current);
  departments.sort((a, b) => a.localeCompare(b, 'zh-CN'));
  select.innerHTML = [
    '<option value="">不选择（未分组）</option>',
    ...departments.map((department) =>
      `<option value="${esc(department)}">${esc(department)}</option>`),
    `<option value="${NEW_DEPARTMENT}" data-action>＋ 新建部门…</option>`
  ].join('');
  select.value = current || '';
  PanelUI.select.refresh(select);
  newInput.value = '';
  newInput.hidden = true;
}

function toggleNewDepartment(select, newInput) {
  newInput.hidden = select.value !== NEW_DEPARTMENT;
  if (!newInput.hidden) newInput.focus();
}

function readDepartment(select, newInput) {
  return select.value === NEW_DEPARTMENT ? newInput.value.trim() : select.value;
}

const accountOrder = (account) => Number.isFinite(account._order)
  ? account._order : Number.MAX_SAFE_INTEGER;

function overallRemaining(account) {
  if (account.pending || !account.ok) return null;
  const remaining = account.data?.quota?.overall?.remaining_pct;
  return typeof remaining === 'number' && Number.isFinite(remaining) ? remaining : null;
}

function resetTime(account) {
  if (account.pending || !account.ok) return null;
  const raw = account.data?.cycle?.reset_at;
  const d = parseDate(raw);
  return d ? d.getTime() : null;
}

function sortedAccounts() {
  return [...accounts].sort((a, b) => {
    if (sortMode === SORT_ADDED) return accountOrder(a) - accountOrder(b);
    if (sortMode === SORT_RESET_ASC || sortMode === SORT_RESET_DESC) {
      const aTime = resetTime(a);
      const bTime = resetTime(b);
      if (aTime === null && bTime === null) {
        const pendingOrder = Number(Boolean(a.pending)) - Number(Boolean(b.pending));
        return pendingOrder || accountOrder(a) - accountOrder(b);
      }
      if (aTime === null) return 1;
      if (bTime === null) return -1;
      const delta = sortMode === SORT_RESET_ASC ? aTime - bTime : bTime - aTime;
      return delta || accountOrder(a) - accountOrder(b);
    }
    const aRemaining = overallRemaining(a);
    const bRemaining = overallRemaining(b);
    if (aRemaining === null && bRemaining === null) {
      const pendingOrder = Number(Boolean(a.pending)) - Number(Boolean(b.pending));
      return pendingOrder || accountOrder(a) - accountOrder(b);
    }
    if (aRemaining === null) return 1;
    if (bRemaining === null) return -1;
    const delta = sortMode === SORT_REMAINING_ASC
      ? aRemaining - bRemaining : bRemaining - aRemaining;
    return delta || accountOrder(a) - accountOrder(b);
  });
}

function filteredAccounts() {
  const list = sortedAccounts();
  if (!searchQuery) return list;
  const q = searchQuery.toLowerCase();
  return list.filter((a) => {
    const label = (a.label || '').toLowerCase();
    const email = (a.email || a.data?.email || '').toLowerCase();
    const dept = (a.department || '').toLowerCase();
    return label.includes(q) || email.includes(q) || dept.includes(q);
  });
}

let searchRenderTimer = null;

function scheduleSearchRender() {
  clearTimeout(searchRenderTimer);
  searchRenderTimer = setTimeout(() => {
    searchRenderTimer = null;
    render();
  }, 90);
}

function clearSearch() {
  clearTimeout(searchRenderTimer);
  searchRenderTimer = null;
  searchQuery = '';
  const input = $('#search-input');
  if (input) input.value = '';
  const clearBtn = $('#search-clear');
  if (clearBtn) clearBtn.hidden = true;
  $('.search-wrap')?.classList.remove('has-query');
  const filterStatWrap = $('#filter-stat-wrap');
  if (filterStatWrap) filterStatWrap.hidden = true;
  render();
}

function render() {
  renderDepartmentTabs();
  // 徽章报的是**当前视图**的账号数，不是全库总数：选中 21 人的部门时页头写 44，
  // 会跟侧边栏同一个部门那一栏的数字当场自相矛盾。全库总数在侧边栏"全部账号"
  // 那一栏，已经有地方说了。搜索命中数归下面的 .filter-stat 管，这里不掺和
  const total = accounts.length;
  const totalBadge = $('#account-total-badge');
  if (totalBadge) totalBadge.textContent = `${total} 个账号`;
  const list = filteredAccounts();

  const filterStatEl = $('#filter-stat');
  const filterStatWrap = $('#filter-stat-wrap');
  if (filterStatEl) {
    if (searchQuery) {
      filterStatEl.innerHTML = `匹配 <b>${list.length}</b> / ${total} 个账号`;
      filterStatEl.hidden = false;
      if (filterStatWrap) filterStatWrap.hidden = false;
    } else {
      filterStatEl.hidden = true;
      filterStatEl.textContent = '';
      if (filterStatWrap) filterStatWrap.hidden = true;
    }
  }

  if (!accounts.length) {
    const message = totalAccountCount
      ? `${departmentName(selectedDepartment)}暂无账号。`
      : '还没有账号。';
    view.innerHTML = `<div class="empty"><p>${esc(message)}</p>
      <button class="primary" onclick="document.getElementById('add').click()">+ 添加账号</button></div>`;
    return;
  }

  if (searchQuery && !list.length) {
    view.innerHTML = `<div class="empty search-empty">
      <p>未找到与 “<strong>${esc(searchQuery)}</strong>” 相关的账号</p>
      <button type="button" class="subtle-btn" id="empty-clear-search">清空搜索条件</button>
    </div>`;
    const btn = $('#empty-clear-search');
    if (btn) btn.onclick = clearSearch;
    return;
  }

  view.innerHTML = `<div class="grid">${list.map(card).join('')}</div>`;
}

function showSkeletons(n) {
  view.innerHTML = `<div class="grid">${Array.from(
    { length: Math.max(1, n) }, () => skeleton()
  ).join('')}</div>`;
}

function stamp() {
  const ages = accounts.map((a) => a.age).filter((v) => typeof v === 'number');
  const stampEl = $('#stamp');
  if (stampEl) {
    if (ages.length) {
      stampEl.textContent = `${relative(Date.now() - Math.min(...ages) * 1000)}更新`;
    } else {
      stampEl.textContent = '已就绪';
    }
  }
  const parts = [];
  if (autoRefreshCycle > 0) {
    parts.push(`后台每 ${Math.max(1, Math.round(autoRefreshCycle / 60))} 分钟错峰轮询一次`);
  }
  parts.push('快照错峰读取，无需手动频繁刷新');
  const metaBox = $('#system-meta');
  if (metaBox) {
    metaBox.setAttribute('data-tip', parts.join(' · '));
  }
}

// ---------- 拉数据 ----------
// 页面只读服务端快照，永远不触发对 cursor.com 的回源——回源是后台调度器的事。
// 所以这里一个请求就能把整组卡片拿回来，不再需要多个 worker 逐卡请求。
async function load({ silent = false, arrival = false, addedId = null, adminChecked = false } = {}) {
  if (silent && (inFlight || refreshingAccounts.size)) return;
  const generation = ++loadGeneration;
  if (loadController) loadController.abort();
  loadController = new AbortController();
  const { signal } = loadController;
  inFlight = true;
  const lastCountKey = `lastCount:${selectedDepartment}`;
  if (!silent) {
    $('#stamp').textContent = '';
    showSkeletons(accounts.length || Number(localStorage.getItem(lastCountKey) || 1));
  }
  try {
    if (!adminChecked) await syncAdminSession();
    if (generation !== loadGeneration) return;
    let payload = null;
    for (let attempt = 0; attempt < 2; attempt += 1) {
      const params = new URLSearchParams();
      if (selectedDepartment !== ALL_DEPARTMENTS) {
        params.set('department', selectedDepartment);
      }
      const suffix = params.size ? '?' + params.toString() : '';
      const r = await fetch('/api/accounts' + suffix, { headers: headers(), signal });
      if (generation !== loadGeneration) return;
      if (r.status === 401) {
        if (activeWorkspace === 'quota' && !$('#auth').open) openModal($('#auth'));
        view.innerHTML = '';
        return;
      }
      const data = await r.json();
      if (generation !== loadGeneration) return;
      if (!r.ok) throw new Error(data.detail || '读取账号失败');
      departmentSummary = data.departments || [];
      totalAccountCount = data.total || 0;
      // 选中的部门可能已经被删空了，退回"全部"再拉一次
      const available = new Set(departmentSummary.map((item) => item.department || ''));
      if (selectedDepartment !== ALL_DEPARTMENTS && !available.has(selectedDepartment)) {
        selectedDepartment = ALL_DEPARTMENTS;
        localStorage.setItem('selectedDepartment', selectedDepartment);
        continue;
      }
      payload = data;
      break;
    }
    if (!payload || generation !== loadGeneration) return;

    accounts = payload.accounts.map((account, order) => ({ ...account, _order: order }));
    if (switchDlg.open && !accounts.some(account => account.id === switchAccountId && account.can_switch === true)) {
      PanelUI.close(switchDlg);
    }
    localStorage.setItem(lastCountKey, accounts.length);
    render();
    if (addedId) {
      const added = findAccountCard(addedId);
      added?.scrollIntoView({ block: 'nearest', behavior: 'instant' });
      if (added) GlassMotion.arrive([added], { added: true });
    } else if (arrival) GlassMotion.arrive(view.querySelectorAll('.card'));
    stamp();
  } catch (e) {
    if (e.name === 'AbortError') return;
    if (silent) return;                    // 轮询失败就等下一轮，别把页面清空
    view.innerHTML = `<div class="empty">读取失败：${esc(e.message)}</div>`;
  } finally {
    if (generation === loadGeneration) inFlight = false;
  }
}

// 后台在错开刷新，页面开着就定期把快照捞回来，不用人去点
async function pollAccounts() {
  await syncAdminSession();
  if (!document.hidden && activeWorkspace === 'quota') load({ silent: true, adminChecked: true });
}
setInterval(() => {
  if (!document.hidden) pollAccounts();
}, POLL_INTERVAL);
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) pollAccounts();
});

function findAccountCard(id) {
  return view.querySelector(`.card[data-account-id="${CSS.escape(id)}"]`);
}

function replaceAccountCard(id) {
  const current = findAccountCard(id);
  const account = accounts.find((item) => item.id === id);
  if (!current || !account) return null;
  const focused = current.contains(document.activeElement) ? document.activeElement : null;
  const focusKey = focused && ['one', 'edit', 'del', 'deptId', 'group'].find((key) => key in focused.dataset);
  const template = document.createElement('template');
  template.innerHTML = card(account);
  const replacement = template.content.firstElementChild;
  const active = document.activeElement;
  current.replaceWith(replacement);
  const grid = replacement.parentElement;
  filteredAccounts().forEach((item, index) => {
    const element = findAccountCard(item.id);
    if (element && grid.children[index] !== element) grid.insertBefore(element, grid.children[index] || null);
  });
  if (focusKey && !document.querySelector('dialog[open]')) {
    [...replacement.querySelectorAll('button')].find((button) =>
      button.dataset[focusKey] === focused.dataset[focusKey])?.focus({ preventScroll: true });
  } else if (active.isConnected && document.activeElement !== active && !document.querySelector('dialog[open]')) {
    active.focus({ preventScroll: true });
  }
  return replacement;
}

async function refreshOne(id) {
  const original = accounts.find((account) => account.id === id);
  if (!original || refreshingAccounts.has(id)) return;
  const requestAdminCsrf = adminCsrf;
  const current = findAccountCard(id);
  const widths = [...(current?.querySelectorAll('.fill') || [])].map((fill) => fill.style.width);
  // 骨架没有按钮，焦点会掉到 body；记下来，等真卡回来再放回刷新按钮上
  const refocus = !!current?.contains(document.activeElement);
  refreshingAccounts.add(id);
  replaceAccountCard(id);
  let updated = false;
  let notice = '';
  try {
    const r = await fetch(`/api/accounts/${encodeURIComponent(id)}/refresh`,
                          { method: 'POST', headers: headers() });
    if (r.status === 401) {
      if (activeWorkspace === 'quota' && requestAdminCsrf === adminCsrf) openModal($('#auth'));
      return;
    }
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || '刷新失败');
    if (requestAdminCsrf !== adminCsrf) d.account.can_switch = false;
    // The department may have changed while this request was in flight.
    const index = accounts.findIndex((account) => account.id === id);
    if (index >= 0) accounts[index] = { ...d.account, _order: accounts[index]._order };
    updated = d.account.ok && !d.account.stale && !d.notice;
    notice = d.notice || '';
  } catch (e) {
    const account = accounts.find((item) => item.id === id);
    if (account) Object.assign(account, {
      stale: !!account.ok, pending: false, error_kind: 'error', error: e.message,
    });
  } finally {
    refreshingAccounts.delete(id);
    const replacement = replaceAccountCard(id);
    if (refocus && !document.querySelector('dialog[open]')) {
      replacement?.querySelector('[data-one]')?.focus({ preventScroll: true });
    }
    if (updated) GlassMotion.refreshed(replacement, widths);
    stamp();
    if (notice) toast(notice);
  }
}

// ---------- 模型明细 ----------
// 点额度行才会向 cursor.com 要一次明细。**别做成打开页面就批量拉**：42 个账号
// 一起拉就是又一次请求洪峰，而这正是本项目最大的风险（按 IP 限流）的来源。
const detailDlg = $('#detail-dlg');
const detailBody = $('#detail-body');
const detailMore = $('#detail-more');
// 底部那个箭头只在真的还有内容时亮。内容换了、尺寸变了都要重算——弹窗开合时
// 玻璃皮肤那段形变会一路改高度，所以 ResizeObserver 也得盯着
function updateDetailMore() {
  detailMore.hidden = detailBody.scrollHeight - detailBody.scrollTop - detailBody.clientHeight <= 4;
}
const setDetailBody = (html) => { detailBody.innerHTML = html; updateDetailMore(); };
detailBody.addEventListener('scroll', updateDetailMore, { passive: true });
new ResizeObserver(updateDetailMore).observe(detailBody);
let detailRequest = 0;
let detailController = null;
new MutationObserver(() => {
  if (!detailDlg.open) {
    detailRequest += 1;
    detailController?.abort();
  }
}).observe(detailDlg, { attributes: true, attributeFilter: ['open'] });

// 模型名长短不一，骨架也跟着长短不一，比一排等宽方块更像正在填的表
const SKEL_WIDTHS = ['64%', '52%', '71%', '45%', '58%', '49%'];

function detailSkeletonGroup(rows) {
  return `<div class="detail-group">
    <div class="detail-skel-head">
      <span class="skel" style="width:104px;height:13px"></span>
      <span class="skel" style="width:180px"></span>
    </div>
    <div class="detail-skel-row head">
      <span class="skel" style="width:28px"></span>
      <span class="skel"></span><span class="skel"></span><span class="skel"></span>
      <span class="skel"></span><span class="skel"></span>
    </div>
    ${SKEL_WIDTHS.slice(0, rows).map((w) => `<div class="detail-skel-row">
      <span class="skel" style="width:${w}"></span>
      <span class="skel"></span><span class="skel"></span><span class="skel"></span>
      <span class="skel"></span><span class="skel"></span>
    </div>`).join('')}
  </div>`;
}

// 综合视图等下会出两组表，骨架就先占两组的位置，填上数据时不会整块跳一下
const detailSkeleton = (group) => group === 'overall'
  ? detailSkeletonGroup(5) + detailSkeletonGroup(3)
  : detailSkeletonGroup(5);

function detailRows(models) {
  if (!models.length) return '<div class="detail-empty">本周期还没有这类模型的用量。</div>';
  return `<div class="detail-scroll"><table class="detail">
    <thead><tr><th>模型</th><th>输入</th><th>输出</th>
      <th title="写入缓存的 token，按各家自己的缓存写单价计费">缓存写</th>
      <th title="命中缓存读回的 token，单价通常只有输入的 1/10，但量大，往往是花费大头">缓存读</th>
      <th>花费</th></tr></thead>
    <tbody>${models.map((m) => `<tr>
        <td class="model">${esc(m.model)}</td>
        <td title="${esc(exactTokens(m.input_tokens))}">${shortTokens(m.input_tokens)}</td>
        <td title="${esc(exactTokens(m.output_tokens))}">${shortTokens(m.output_tokens)}</td>
        <td title="${esc(exactTokens(m.cache_write_tokens))}">${shortTokens(m.cache_write_tokens)}</td>
        <td title="${esc(exactTokens(m.cache_read_tokens))}">${shortTokens(m.cache_read_tokens)}</td>
        <td class="cost${m.spend_usd ? '' : ' zero'}">${money(m.spend_usd)}</td>
      </tr>`).join('')}</tbody>
  </table></div>`;
}

function detailGroup(group, quota) {
  const limit = quota && quota.limit_usd != null
    ? ` / 上限 ${esc(quotaLimitText(quota))}` : '';
  return `<div class="detail-group">
    <div class="detail-group-head">
      <span class="detail-group-name">${esc(group.name)}</span>
      <span class="detail-group-sum">已用 ${money(group.spend_usd)}${limit}
        · ${shortTokens(group.total_tokens)} tokens</span>
    </div>
    ${detailRows(group.models)}
  </div>`;
}

function renderDetail(d, group) {
  const groups = group === 'overall' ? d.groups : d.groups.filter((g) => g.key === group);
  const parts = [`统计窗口 ${localDateTime(d.cycle_start)} 起`];
  if (d.fetched_at) parts.push(`取数于 ${statTime(d.fetched_at)}`);
  $('#detail-note').textContent = parts.join(' · ');
  const totals = group === 'overall' && d.totals.model_count
    ? `<div class="detail-total">
        <span>本周期合计 ${d.totals.model_count} 个模型 ·
          ${shortTokens(d.totals.total_tokens)} tokens</span>
        <span>${money(d.totals.spend_usd)}</span>
      </div>` : '';
  setDetailBody(groups.map((g) => detailGroup(g, (d.quota || {})[g.key])).join('') + totals);
}

async function openDetail(id, group, source) {
  const account = accounts.find((a) => a.id === id);
  const generation = ++detailRequest;
  detailController?.abort();
  detailController = new AbortController();
  // 分类名由组头负责显示，标题只认账号，免得单组视图里同一个词写两遍
  $('#detail-title').textContent = `${account ? account.label : id} · 本周期模型明细`;
  $('#detail-note').textContent = '正在从 Cursor 取本周期明细…';
  setDetailBody(detailSkeleton(group));
  openModal(detailDlg, {
    opener: () => findAccountCard(id)?.querySelector(`[data-group="${CSS.escape(group)}"]`) || source,
    transition: GlassMotion.detail,
  });
  try {
    const r = await fetch(`/api/accounts/${encodeURIComponent(id)}/usage-detail`,
                          { headers: headers(), signal: detailController.signal });
    if (generation !== detailRequest || !detailDlg.open) return;
    if (r.status === 401) { PanelUI.close(detailDlg); openModal($('#auth')); return; }
    const d = await r.json();
    if (generation !== detailRequest) return;   // 关掉又点了别的，别让旧响应覆盖
    if (!r.ok) throw new Error(d.detail || '读取明细失败');
    renderDetail(d, group);
  } catch (e) {
    if (e.name === 'AbortError' || generation !== detailRequest || !detailDlg.open) return;
    $('#detail-note').textContent = '';
    setDetailBody(`<div class="detail-empty">${esc(e.message)}</div>`);
  }
}

let toastTimer = null;
function toast(text) {
  const el = $('#stamp');
  clearTimeout(toastTimer);
  el.textContent = text;
  toastTimer = setTimeout(stamp, 4000);
}

view.addEventListener('click', async (e) => {
  const btn = e.target.closest('button');
  if (!btn) return;
  const { del, one, edit, deptId, switchId, detail, group, label, department } = btn.dataset;
  if (btn.dataset.showAll !== undefined) {
    selectDepartment(ALL_DEPARTMENTS);
    return;
  }
  if (del !== undefined) {
    const account = accounts.find((item) => item.id === del);
    const removed = await PanelUI.confirm({
      title: '删除账号',
      message: '确认从面板移除这个账号？只删除服务端的账号记录，不影响 Cursor 账号本身。',
      subject: account?.label || del,
      detail: account?.email || del,
      tone: 'danger',
      confirmText: '删除账号',
      pendingText: '删除中…',
      onConfirm: async () => {
        const response = await fetch('/api/accounts/' + encodeURIComponent(del), {
          method: 'DELETE', headers: headers()
        });
        if (response.status === 401) {
          openModal($('#auth'));
          throw new Error('面板口令已失效，请重新输入口令后重试。');
        }
        if (!response.ok) {
          const data = await response.json().catch(() => ({}));
          throw new Error(data.detail || '删除失败，请稍后重试。');
        }
      },
    });
    if (removed) { await load(); toast('已删除账号'); }
  } else if (switchId !== undefined) {
    openSwitchDialog(switchId);
  } else if (one !== undefined) {
    refreshOne(one);
  } else if (detail !== undefined) {
    openDetail(detail, group, btn);
  } else if (deptId !== undefined) {
    openDepartmentDialog(deptId, label, department);
  } else if (edit !== undefined) {
    openDialog(label, department, true);
  }
});

function closeMobileSidebar() {
  setSkinMenuOpen(false);
  $('#sidebar')?.classList.remove('open');
  $('#sidebar-backdrop')?.classList.remove('open');
}
function openMobileSidebar() {
  $('#sidebar')?.classList.add('open');
  $('#sidebar-backdrop')?.classList.add('open');
}

function selectDepartment(department) {
  const wasAdmin = activeWorkspace === 'admin';
  if (wasAdmin) setWorkspace('quota');
  if (department === selectedDepartment) {
    closeMobileSidebar();
    if (wasAdmin) load({ silent: true });
    return;
  }
  selectedDepartment = department;
  localStorage.setItem('selectedDepartment', department);
  renderDepartmentTabs();
  closeMobileSidebar();
  load({ arrival: true });
}

$('#mobile-menu-btn')?.addEventListener('click', openMobileSidebar);
$('#mobile-close-btn')?.addEventListener('click', closeMobileSidebar);
$('#sidebar-backdrop')?.addEventListener('click', closeMobileSidebar);

function setWorkspace(next, updateHistory = true) {
  const changed = activeWorkspace !== next;
  activeWorkspace = next;
  const admin = next === 'admin';
  document.body.classList.toggle('admin-workspace-active', admin);
  $('#quota-workspace').hidden = admin;
  $('#admin-workspace').hidden = !admin;
  $('.topbar-right').hidden = admin;
  $('#account-total-badge').hidden = admin;
  $('#open-prefs').hidden = admin;
  if (admin) $('#admin-menu-link').setAttribute('aria-current', 'page');
  else $('#admin-menu-link').removeAttribute('aria-current');
  document.title = admin ? '系统管理 · Cursor 额度' : 'Cursor 额度面板';
  if (updateHistory && location.hash !== `#${next}`) history.pushState(null, '', `#${next}`);
  renderDepartmentTabs();
  closeMobileSidebar();
  if (changed) window.scrollTo({ top: 0, behavior: 'instant' });
  AdminWorkspace.setActive(admin);
  if (admin && $('#auth').open) PanelUI.close($('#auth'));
}

function workspaceFromLocation() {
  return location.hash === '#admin' || (!location.hash && /^\/admin\/?$/.test(location.pathname))
    ? 'admin' : 'quota';
}

$('#admin-menu-link').addEventListener('click', event => {
  if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey || event.button !== 0) return;
  event.preventDefault();
  setWorkspace('admin');
});
$('#admin-workspace').addEventListener('click', event => {
  const back = event.target.closest('[data-admin-back]');
  if (!back) return;
  event.preventDefault();
  setWorkspace('quota');
  load({ silent: true });
});
window.addEventListener('popstate', () => {
  setWorkspace(workspaceFromLocation(), false);
  if (activeWorkspace === 'quota') load({ silent: true });
});

// ---------- 界面风格 ----------
// 这张表是皮肤的唯一事实来源，地位跟下面的 CARD_OPTIONS 一样：侧边栏的切换按钮
// 照它生成，存进 localStorage 的值也照它校验。加一套皮肤 = 这儿多写一条 +
// 新增一个 css/skins/*.css + index.html 里多一行 <link>，别的地方都不用碰。
//
// 风格和明暗是两个正交的维度（html 上是 data-skin × data-theme 两个属性），
// **不要把它们合并成"经典深色/玻璃浅色"这样的四选一**：那样每加一套皮肤，
// 选项数量就翻一倍，而且"跟随系统"没法落在其中任何一个上。
//
// featured 保留常用的两个入口，其余风格进入箭头菜单；色卡取值统一放在 tokens.css。
const SKINS = [
  {
    key: 'classic',
    name: '经典',
    hint: '实色卡片，信息密度优先',
    featured: true,
  },
  {
    key: 'glass',
    name: '液态玻璃',
    hint: '半透明层叠、背景色斑，仿 iOS 控制中心',
    featured: true,
  },
  { key: 'cyberpunk', name: '赛博朋克', hint: '霓虹青与电光洋红' },
  { key: 'graphite', name: '石墨极简', hint: '黑白灰与利落线条' },
  { key: 'verdant', name: '青野绿意', hint: '自然青绿与柔和中性色' },
  { key: 'blueprint', name: '工程蓝图', hint: '制图网格与精密刻度' },
];
// 改这个默认值要连 index.html 头部那段引导脚本里的 'classic' 一起改，
// 那是全页唯一一处必须跟这里保持一致的重复
const DEFAULT_SKIN = 'classic';
const SKIN_STORAGE_KEY = 'panelSkin';
const SKIN_KEYS = new Set(SKINS.map((s) => s.key));

const storedSkin = localStorage.getItem(SKIN_STORAGE_KEY);
let activeSkin = SKIN_KEYS.has(storedSkin) ? storedSkin : DEFAULT_SKIN;
let skinMenuPinned = false;
let skinMenuCloseTimer;

function renderSkinSwitch() {
  const box = $('#skin-switch');
  if (!box) return;
  box.innerHTML = SKINS.filter((skin) => skin.featured).map((skin) => {
    return `<button type="button" class="skin-btn"
      data-skin-set="${esc(skin.key)}" title="${esc(skin.hint)}" aria-pressed="false">
      <span class="skin-swatch" style="background:var(--skin-swatch-${esc(skin.key)})" aria-hidden="true"></span>
      <span class="skin-name">${esc(skin.name)}</span>
    </button>`;
  }).join('') + `<button type="button" class="skin-more" id="skin-more" title="更多风格"
    aria-label="更多风格" aria-haspopup="menu" aria-controls="skin-menu" aria-expanded="false">
    ${ICON.chevron}
  </button>`;
  $('#skin-menu').innerHTML = SKINS.filter((skin) => !skin.featured).map((skin) => `
    <button type="button" class="skin-menu-item" role="menuitemradio" aria-checked="false" tabindex="-1"
      data-skin-set="${esc(skin.key)}" title="${esc(skin.hint)}">
      <span class="skin-swatch" style="background:var(--skin-swatch-${esc(skin.key)})" aria-hidden="true"></span>
      <span class="skin-name">${esc(skin.name)}</span>
      <svg class="skin-menu-check" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 6 9 17l-5-5"/></svg>
    </button>`).join('');
}

function setSkinMenuOpen(open) {
  clearTimeout(skinMenuCloseTimer);
  $('#skin-menu').hidden = !open;
  $('#skin-more').setAttribute('aria-expanded', String(open));
  if (!open) skinMenuPinned = false;
}

function focusSkinMenuItem(last = false) {
  const items = [...$('#skin-menu').querySelectorAll('[data-skin-set]')];
  const selected = items.find((item) => item.dataset.skinSet === activeSkin);
  (last ? items[items.length - 1] : selected || items[0])?.focus();
}

function updateSkin(key) {
  activeSkin = SKIN_KEYS.has(key) ? key : DEFAULT_SKIN;
  try { localStorage.setItem(SKIN_STORAGE_KEY, activeSkin); } catch { /* 隐私模式写不进去，本次会话照样生效 */ }
  document.documentElement.setAttribute('data-skin', activeSkin);
  const skin = SKINS.find((item) => item.key === activeSkin);
  $('#skin-picker').querySelectorAll('[data-skin-set]').forEach((btn) => {
    const on = btn.dataset.skinSet === activeSkin;
    btn.classList.toggle('active', on);
    btn.setAttribute(btn.getAttribute('role') === 'menuitemradio' ? 'aria-checked' : 'aria-pressed', String(on));
  });
  $('#skin-more').classList.toggle('active', !skin.featured);
  $('#skin-more').setAttribute('aria-label', skin.featured ? '更多风格' : `更多风格，当前：${skin.name}`);
  $('#skin-current').textContent = skin.featured ? '' : skin.name;
  $('#skin-current').hidden = !!skin.featured;
}

renderSkinSwitch();

$('#skin-picker').addEventListener('click', (e) => {
  const btn = e.target.closest('[data-skin-set]');
  if (!btn) return;
  updateSkin(btn.dataset.skinSet);
  setSkinMenuOpen(false);
  if (btn.closest('#skin-menu')) $('#skin-more').focus();
});

$('#skin-more').addEventListener('pointerenter', (e) => {
  if (e.pointerType === 'mouse') setSkinMenuOpen(true);
});
$('#skin-picker').addEventListener('pointerenter', () => clearTimeout(skinMenuCloseTimer));
$('#skin-picker').addEventListener('pointerleave', () => {
  if (!skinMenuPinned && !$('#skin-menu').contains(document.activeElement)) {
    skinMenuCloseTimer = setTimeout(() => setSkinMenuOpen(false), 180);
  }
});
$('#skin-more').addEventListener('click', () => {
  if (skinMenuPinned) {
    setSkinMenuOpen(false);
    return;
  }
  setSkinMenuOpen(true);
  skinMenuPinned = true;
  focusSkinMenuItem();
});
$('#skin-more').addEventListener('keydown', (e) => {
  if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;
  e.preventDefault();
  setSkinMenuOpen(true);
  skinMenuPinned = true;
  focusSkinMenuItem(e.key === 'ArrowUp');
});
$('#skin-menu').addEventListener('keydown', (e) => {
  const items = [...$('#skin-menu').querySelectorAll('[data-skin-set]')];
  const index = items.indexOf(document.activeElement);
  let next;
  if (e.key === 'ArrowDown') next = (index + 1) % items.length;
  else if (e.key === 'ArrowUp') next = (index - 1 + items.length) % items.length;
  else if (e.key === 'Home') next = 0;
  else if (e.key === 'End') next = items.length - 1;
  else if (e.key === 'Tab') {
    // 菜单项不进页面的 Tab 顺序，从触发按钮继续向前或向后移动。
    $('#skin-more').focus();
    setSkinMenuOpen(false);
    return;
  } else return;
  e.preventDefault();
  items[next]?.focus();
});
$('#skin-picker').addEventListener('focusout', (e) => {
  if (!$('#skin-picker').contains(e.relatedTarget)) setSkinMenuOpen(false);
});
document.addEventListener('pointerdown', (e) => {
  if (!$('#skin-picker').contains(e.target)) setSkinMenuOpen(false);
});
document.addEventListener('keydown', (e) => {
  if (e.key !== 'Escape' || $('#skin-menu').hidden) return;
  e.preventDefault();
  if ($('#skin-menu').contains(document.activeElement)) $('#skin-more').focus();
  setSkinMenuOpen(false);
});

// 属性其实已经由 index.html 的引导脚本打上去了，这里再跑一次是为了两件事：
// 把存量里的脏值纠正回默认皮肤，以及把切换按钮渲染出来
updateSkin(activeSkin);

// ---------- 主题模式切换 (浅色 / 深色 / 自动) ----------
const THEME_STORAGE_KEY = 'panelTheme';
let activeThemeMode = localStorage.getItem(THEME_STORAGE_KEY) || 'system';

function updateTheme(mode) {
  activeThemeMode = mode;
  localStorage.setItem(THEME_STORAGE_KEY, mode);
  const isDark = mode === 'dark' || (mode === 'system' && window.matchMedia('(prefers-color-scheme: dark)').matches);
  document.documentElement.setAttribute('data-theme', isDark ? 'dark' : 'light');
  document.querySelectorAll('[data-theme-set]').forEach((btn) => {
    btn.classList.toggle('active', btn.dataset.themeSet === mode);
    btn.setAttribute('aria-pressed', String(btn.dataset.themeSet === mode));
  });
  GlassMotion.selection($('.theme-switch'), $('.theme-btn.active'));
}

window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
  if (activeThemeMode === 'system') updateTheme('system');
});

document.addEventListener('click', (e) => {
  const btn = e.target.closest('[data-theme-set]');
  if (btn) updateTheme(btn.dataset.themeSet);
});

updateTheme(activeThemeMode);

// ---------- 卡片显示项面板 ----------
const PREF_GROUP_ICONS = {
  '卡片装饰': `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="m12 3-1.9 5.8a2 2 0 0 1-1.3 1.3L3 12l5.8 1.9a2 2 0 0 1 1.3 1.3L12 21l1.9-5.8a2 2 0 0 1 1.3-1.3L21 12l-5.8-1.9a2 2 0 0 1-1.3-1.3L12 3z"/></svg>`,
  '身份区': `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M19 21v-2a4 4 0 0 0-4-4H9a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>`,
  '信息行': `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/><line x1="6" y1="20" x2="6" y2="14"/></svg>`
};

function updatePrefsBadge() {
  const activeCount = CARD_OPTIONS.filter((o) => shows(o.key)).length;
  const countBadge = $('#prefs-active-count');
  if (countBadge) countBadge.textContent = `${activeCount}/${CARD_OPTIONS.length} 开启`;
}

function renderPrefs() {
  const groups = [];
  for (const option of CARD_OPTIONS) {
    const last = groups[groups.length - 1];
    if (last && last.name === option.group) last.items.push(option);
    else groups.push({ name: option.group, items: [option] });
  }

  updatePrefsBadge();

  $('#prefs-body').innerHTML = groups.map((group) => {
    const icon = PREF_GROUP_ICONS[group.name] || '';
    const activeInGroup = group.items.filter((o) => shows(o.key)).length;
    return `<div class="pref-section" data-group="${esc(group.name)}">
      <div class="pref-section-head">
        <span class="pref-section-title">${icon}<span>${esc(group.name)}</span></span>
        <span class="pref-section-count">${activeInGroup}/${group.items.length}</span>
      </div>
      <div class="pref-grid">
        ${group.items.map((option) => {
          const isChecked = shows(option.key);
          return `<label class="pref-tile ${isChecked ? 'is-active' : ''}">
            <div class="pref-tile-text">
              <div class="pref-tile-title">${esc(option.label)}</div>
              <div class="pref-tile-hint" title="${esc(option.hint)}">${esc(option.hint)}</div>
            </div>
            <div class="capsule-switch">
              <input type="checkbox" data-pref="${esc(option.key)}"${isChecked ? ' checked' : ''}>
              <span class="capsule-track">
                <span class="capsule-thumb"></span>
              </span>
            </div>
          </label>`;
        }).join('')}
      </div>
    </div>`;
  }).join('');
}

// 勾完立刻重画。render() 只是拿手上这份快照重排一遍，不回源、不惊动调度器
function applyPrefs() {
  savePrefs();
  render();
}

$('#open-prefs').addEventListener('click', () => {
  renderPrefs();
  openModal($('#prefs-dlg'));
});

$('#prefs-body').addEventListener('change', (e) => {
  const box = e.target.closest('[data-pref]');
  if (!box) return;
  cardPrefs = { ...cardPrefs, [box.dataset.pref]: box.checked };
  const tile = box.closest('.pref-tile');
  if (tile) tile.classList.toggle('is-active', box.checked);

  // 更新组内计数
  const section = box.closest('.pref-section');
  if (section) {
    const total = section.querySelectorAll('.pref-tile').length;
    const active = section.querySelectorAll('.pref-tile.is-active').length;
    const countEl = section.querySelector('.pref-section-count');
    if (countEl) countEl.textContent = `${active}/${total}`;
  }
  updatePrefsBadge();
  applyPrefs();
});

$('#preset-all')?.addEventListener('click', () => {
  cardPrefs = Object.fromEntries(CARD_OPTIONS.map((o) => [o.key, true]));
  renderPrefs();
  applyPrefs();
});

$('#preset-compact')?.addEventListener('click', () => {
  const COMPACT_KEYS = new Set(['ribbon', 'plan', 'department', 'spend', 'reset']);
  cardPrefs = Object.fromEntries(CARD_OPTIONS.map((o) => [o.key, COMPACT_KEYS.has(o.key)]));
  renderPrefs();
  applyPrefs();
});

$('#prefs-reset').addEventListener('click', () => {
  cardPrefs = { ...OPTION_DEFAULTS };
  renderPrefs();
  applyPrefs();
});

$('#department-tabs').addEventListener('click', (e) => {
  const tab = e.target.closest('[data-department-filter]');
  if (tab) selectDepartment(tab.dataset.departmentFilter);
});

$('#sort-order').value = sortMode;
PanelUI.select.refresh($('#sort-order'));
$('#sort-order').addEventListener('change', (e) => {
  sortMode = SORT_MODES.has(e.target.value) ? e.target.value : SORT_ADDED;
  localStorage.setItem('accountSort', sortMode);
  render();
});

const searchInput = $('#search-input');
const searchClearBtn = $('#search-clear');
if (searchInput) {
  searchInput.addEventListener('input', (e) => {
    searchQuery = e.target.value.trim();
    if (searchClearBtn) searchClearBtn.hidden = !searchQuery;
    $('.search-wrap')?.classList.toggle('has-query', Boolean(searchQuery));
    // 连击时合并成一次重绘：render() 会整体重建卡片 DOM，逐字符跑会拖垮输入手感
    scheduleSearchRender();
  });
}
if (searchClearBtn) {
  searchClearBtn.addEventListener('click', clearSearch);
}
window.addEventListener('keydown', (e) => {
  if (e.key === '/' && activeWorkspace === 'quota' && !document.querySelector('dialog[open]')
      && document.activeElement?.getAttribute('role') !== 'combobox'
      && !['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement?.tagName)) {
    e.preventDefault();
    if (searchInput) {
      searchInput.focus();
      searchInput.select();
    }
  }
  if (e.key === 'Escape' && document.activeElement === searchInput) {
    searchInput.blur();
  }
});

// ---------- 添加 / 重新授权 ----------
const dlg = $('#dlg'), dstat = $('#d-status');
const say = (text, cls = '') => { dstat.className = 'status ' + cls; dstat.textContent = text; };

function openDialog(label = '', department = '', isEdit = false) {
  dlg.dataset.editing = String(isEdit);
  $('#d-title').textContent = isEdit ? '重新授权' : '添加账号';
  $('#d-label').value = label || '';
  populateDepartmentSelect($('#d-department'), $('#d-department-new'), department);
  $('#d-cookie').value = '';
  $('#d-save').disabled = false;
  say('');
  openModal(dlg);
  (isEdit ? $('#d-cookie') : $('#d-label')).focus();
}

$('#d-save').onclick = async () => {
  const isEdit = dlg.dataset.editing === 'true';
  const cookie = $('#d-cookie').value.trim();
  const department = readDepartment($('#d-department'), $('#d-department-new'));
  if (!cookie) return say('cookie 不能为空', 'bad');
  if ($('#d-department').value === NEW_DEPARTMENT && !department) {
    return say('请输入新部门名称，或选择未分组', 'bad');
  }
  $('#d-save').disabled = true;
  say('正在获取桌面凭证并验证额度…', 'busy');
  try {
    const r = await fetch('/api/accounts', {
      method: 'POST', headers: jsonHeaders(),
      body: JSON.stringify({
        cookie,
        label: $('#d-label').value.trim() || null,
        department
      })
    });
    const d = await r.json();
    if (r.ok) {
      selectedDepartment = d.department || '';
      localStorage.setItem('selectedDepartment', selectedDepartment);
      say('已保存 ' + (d.email || d.label), 'good');
      PanelUI.close(dlg);
      if (!isEdit) clearSearch();
      await load({ addedId: !isEdit ? (d.email || d.label) : null });
    } else {
      say(d.detail || '保存失败', 'bad');
      $('#d-save').disabled = false;
    }
  } catch (e) {
    say('请求失败：' + e.message, 'bad');
    $('#d-save').disabled = false;
  }
};

$('#add').onclick = () => openDialog(
  '', selectedDepartment === ALL_DEPARTMENTS ? '' : selectedDepartment
);
$('#d-department').onchange = () =>
  toggleNewDepartment($('#d-department'), $('#d-department-new'));

// ---------- 调整分组 ----------
const departmentDlg = $('#department-dlg');
let departmentAccountId = '';

function openDepartmentDialog(id, label, department) {
  departmentAccountId = id;
  $('#department-account').textContent = label;
  populateDepartmentSelect($('#department-input'), $('#department-new'), department);
  $('#department-status').textContent = '';
  $('#department-status').className = 'status';
  $('#department-save').disabled = false;
  openModal(departmentDlg);
  PanelUI.select.focus($('#department-input'));
}

$('#department-save').onclick = async () => {
  const department = readDepartment($('#department-input'), $('#department-new'));
  const status = $('#department-status');
  if ($('#department-input').value === NEW_DEPARTMENT && !department) {
    status.className = 'status bad';
    status.textContent = '请输入新部门名称，或选择未分组';
    return;
  }
  $('#department-save').disabled = true;
  status.className = 'status busy';
  status.textContent = '保存中…';
  try {
    const r = await fetch(`/api/accounts/${encodeURIComponent(departmentAccountId)}/department`, {
      method: 'PATCH', headers: jsonHeaders(), body: JSON.stringify({ department })
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || '保存失败');
    const account = accounts.find((item) => item.id === departmentAccountId);
    if (account) account.department = d.department || '';
    selectedDepartment = d.department || '';
    localStorage.setItem('selectedDepartment', selectedDepartment);
    PanelUI.close(departmentDlg);
    load();
  } catch (e) {
    status.className = 'status bad';
    status.textContent = e.message;
    $('#department-save').disabled = false;
  }
};

$('#department-input').onchange = () =>
  toggleNewDepartment($('#department-input'), $('#department-new'));

// ---------- 本机账号切换 ----------
const switchDlg = $('#switch-dlg');
let switchAccountId = '';
let switchCommands = null;
let switchRequest = 0;
let switchController = null;

function detectedDesktop() {
  const platform = navigator.userAgentData?.platform || navigator.platform || navigator.userAgent;
  if (/win/i.test(platform)) return 'windows';
  if (/mac/i.test(platform) && navigator.maxTouchPoints < 2) return 'macos';
  return '';
}

function renderSwitchCommand() {
  const system = $('#switch-system').value;
  const item = switchCommands?.[system];
  $('#switch-shell').textContent = system === 'windows' ? 'PowerShell 命令' : '终端命令';
  $('#switch-command').value = item?.command || '';
  $('#switch-script').textContent = item?.script || '';
  $('#switch-result').hidden = !item;
  $('#switch-copy').disabled = !item;
  $('#switch-copy').innerHTML = `${ICON.copy}<span>复制命令</span>`;
}

async function generateSwitchCommand() {
  if (!accounts.some(account => account.id === switchAccountId && account.can_switch === true)) {
    if (switchDlg.open) PanelUI.close(switchDlg);
    return;
  }
  const generation = ++switchRequest;
  switchController?.abort();
  switchController = new AbortController();
  switchCommands = null;
  renderSwitchCommand();
  const status = $('#switch-status');
  status.className = 'status busy';
  status.textContent = '正在检查并续期桌面会话…';
  $('#switch-generate').disabled = true;
  try {
    const response = await fetch(`/api/accounts/${encodeURIComponent(switchAccountId)}/switch-command`, {
      method: 'POST', headers: jsonHeaders(), body: '{}',
      cache: 'no-store', signal: switchController.signal,
    });
    const data = await response.json();
    if (generation !== switchRequest || !switchDlg.open) return;
    if (response.status === 401) openModal($('#auth'));
    if (!response.ok) throw new Error(response.status === 401
      ? '面板口令已失效，请重新输入后重试。' : data.detail || '生成命令失败');
    switchCommands = data.commands;
    $('#switch-account').textContent = `${data.label} · ${data.email}`;
    status.className = 'status good';
    const expiry = data.download_expires_at
      ? `链接有效至 ${new Date(data.download_expires_at * 1000).toLocaleTimeString()}，仅可下载一次。`
      : '';
    status.textContent = `桌面会话验证通过，命令已生成。${expiry}`;
    renderSwitchCommand();
  } catch (error) {
    if (error.name === 'AbortError' || generation !== switchRequest || !switchDlg.open) return;
    status.className = 'status bad';
    status.textContent = error.message;
  } finally {
    if (generation === switchRequest) $('#switch-generate').disabled = false;
  }
}

function openSwitchDialog(id) {
  const account = accounts.find(item => item.id === id);
  if (account?.can_switch !== true) return;
  switchAccountId = id;
  $('#switch-account').textContent = `${account?.label || id} · ${account?.email || id}`;
  $('#switch-system').value = detectedDesktop();
  PanelUI.select.refresh($('#switch-system'));
  $('#switch-audit').open = false;
  switchCommands = null;
  renderSwitchCommand();
  openModal(switchDlg);
  generateSwitchCommand();
}

switchDlg.addEventListener('close', () => {
  ++switchRequest;
  switchController?.abort();
  switchCommands = null;
  switchAccountId = '';
  renderSwitchCommand();
});
$('#switch-system').onchange = renderSwitchCommand;
$('#switch-generate').onclick = generateSwitchCommand;
$('#switch-copy').onclick = async () => {
  const command = $('#switch-command');
  if (!command.value) return;
  const copied = command.value;
  const generation = switchRequest;
  try {
    if (navigator.clipboard?.writeText && window.isSecureContext) {
      await navigator.clipboard.writeText(copied);
    } else {
      command.focus();
      command.select();
      if (!document.execCommand('copy')) throw new Error('请选中命令后手动复制。');
    }
    if (generation === switchRequest && switchDlg.open && command.value === copied) {
      $('#switch-copy').innerHTML = `${ICON.copy}<span>已复制</span>`;
    }
  } catch {
    if (generation !== switchRequest || !switchDlg.open) return;
    command.focus();
    command.select();
    $('#switch-status').className = 'status';
    $('#switch-status').textContent = '无法自动复制，命令已选中，请手动复制。';
  }
};

// ---------- 口令 ----------
$('#a-ok').onclick = async () => {
  token = $('#a-token').value.trim();
  const r = await fetch('/api/account-index', { headers: headers() });
  if (r.status === 401) {
    $('#a-status').className = 'status bad';
    $('#a-status').textContent = '口令不对';
    return;
  }
  localStorage.setItem('panelToken', token);
  PanelUI.close($('#auth'));
  load();
};

setWorkspace(workspaceFromLocation(), false);
Promise.all([fetch('/api/config', { cache: 'no-store' }).then(r => r.json()), syncAdminSession()]).then(([c]) => {
  autoRefreshCycle = c.auto_refresh ? (c.cycle_seconds || 0) : 0;
  if (c.needs_token && !token && !isAdmin) {
    if (activeWorkspace === 'quota') openModal($('#auth'));
  } else load({ adminChecked: true });
}).catch(() => load());
