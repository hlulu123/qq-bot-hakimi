import {
  apiGet,
  apiPost,
  apiPut,
  confirmModal,
  copyText,
  createIcon,
  escapeHtml,
  formatRelativeTime,
  formatTime,
  saveShortcutLabel,
  showToast,
} from '../api.js';

const STORAGE_SOUND_KEY = 'hajimi_req_sound';
const STORAGE_NOTIFY_KEY = 'hajimi_req_notify';

const LOCAL_ICONS = {
  userPlus: '<path d="M16 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="8.5" cy="7" r="4"/><line x1="20" y1="8" x2="20" y2="14"/><line x1="23" y1="11" x2="17" y2="11"/>',
  userCheck: '<path d="M16 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="8.5" cy="7" r="4"/><polyline points="17 11 19 13 23 9"/>',
  userX: '<path d="M16 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="8.5" cy="7" r="4"/><line x1="18" y1="8" x2="23" y2="13"/><line x1="23" y1="8" x2="18" y2="13"/>',
  volume2: '<polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><path d="M15.54 8.46a5 5 0 0 1 0 7.07"/><path d="M19.07 4.93a10 10 0 0 1 0 14.14"/>',
  volumeX: '<polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><line x1="23" y1="9" x2="17" y2="15"/><line x1="17" y1="9" x2="23" y2="15"/>',
  bell: '<path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/>',
  bellOff: '<path d="M13.73 21a2 2 0 0 1-3.46 0"/><path d="M18.63 13A17.89 17.89 0 0 1 18 8"/><path d="M6.26 6.26A5.86 5.86 0 0 0 6 8c0 7-3 9-3 9h14"/><path d="M18 8a6 6 0 0 0-9.33-5"/><line x1="1" y1="1" x2="23" y2="23"/>',
  popout: '<path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/>',
  flask: '<path d="M10 2v7.31"/><path d="M14 2v7.31"/><path d="M8.5 2h7"/><path d="M14 9.3a6.5 6.5 0 1 1-4 0"/><path d="M5.52 16h12.96"/>',
  xCircle: '<circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/>',
};

function reqIcon(name, size = 15) {
  if (LOCAL_ICONS[name]) {
    return `<svg viewBox="0 0 24 24" width="${size}" height="${size}" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${LOCAL_ICONS[name]}</svg>`;
  }
  return createIcon(name, size);
}

const BOT_ROLE_META = {
  owner: { label: '群主', cls: 'badge-success', short: '群主' },
  admin: { label: '群管', cls: 'badge-accent', short: '群管' },
  member: { label: '普通成员', cls: 'badge-amber', short: '普通成员' },
  unknown: { label: '未知身份', cls: 'badge-muted', short: '未知' },
};

const MODE_OPTIONS = [
  {
    id: 'manual',
    label: '仅手动审批',
    short: '手动审批',
    desc: '所有申请进入待处理队列，由管理员在控制台或盯盘窗决定。',
  },
  {
    id: 'keyword',
    label: '关键词自动通过',
    short: '关键词放行',
    desc: '验证消息命中放行词即自动同意；黑名单拒绝词优先级更高。',
  },
  {
    id: 'approve_all',
    label: '自动全部通过',
    short: '全部通过',
    desc: '自动同意所有加群申请（仍会执行黑名单拒绝词与等级门槛）。',
  },
  {
    id: 'reject_all',
    label: '自动全部拒绝',
    short: '全部拒绝',
    desc: '自动拒绝所有新的加群申请，并附带默认拒绝理由。',
  },
];
const MODE_MAP = Object.fromEntries(MODE_OPTIONS.map((m) => [m.id, m]));

const INVITE_OPTIONS = [
  { id: 'manual', label: '手动审批' },
  { id: 'approve', label: '自动同意' },
  { id: 'reject', label: '自动拒绝' },
];

const DECISION_SOURCE_LABELS = {
  auto_keyword: '自动·关键词',
  auto_approve_keyword: '自动·关键词',
  auto_all: '自动·全部',
  auto_approve_all: '自动·全部',
  auto_approve: '自动·全部',
  auto_invite_approve: '自动·同意邀请',
  auto_invite_reject: '自动·拒绝邀请',
  auto_reject_keyword: '自动·拒绝关键词',
  auto_reject_unmatched: '自动·未命中关键词',
  auto_reject_level: '自动·等级不足',
  auto_reject_all: '自动·全部拒绝',
  auto_reject: '自动拒绝',
  manual: '人工审批',
  webui: '人工审批',
  webui_manual: '人工审批',
  webui_batch: '人工批量审批',
  batch: '人工批量审批',
  bot_tool: '机器人对话审批',
  bot_chat: '机器人对话审批',
  agent: '机器人对话审批',
  chat: '机器人对话审批',
  qq_tool: '机器人对话审批',
  other_admin: '群内其他管理员已处理',
  external: '群内其他管理员已处理',
  qq_client: '群内其他管理员已处理',
  napcat_sync: 'NapCat 同步',
  sync: 'NapCat 同步',
};

const QUICK_REJECT_REASONS = [
  '未通过入群验证',
  '请按要求填写正确暗号',
  '暂不接受入群申请',
  '账号存在风险/疑似广告',
];

const DEFAULT_RULE = {
  mode: 'manual',
  approve_keywords: [],
  reject_keywords: [],
  reject_unmatched: false,
  default_reject_reason: '未通过入群验证',
  min_qq_level: 0,
  handle_invite: 'manual',
  auto_delay_secs: 1.0,
  notify_admin_dm: false,
  welcome_message: '',
};

const SCOPED_CSS = `
.req-view { display: flex; flex-direction: column; gap: 20px; }
.req-view.is-compact { gap: 12px; }
.req-view.is-compact .tab-btn { padding: 0 9px; font-size: 12px; height: 26px; gap: 4px; }
.req-view.is-compact .req-card-name { max-width: 170px; }

.req-live-strip {
  display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap;
  padding: 10px 14px; border-radius: var(--radius);
  background: var(--bg-elevated); border: 1px solid var(--border);
}
.req-live-left { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; min-width: 0; }
.req-live-right { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.req-sse-indicator {
  display: inline-flex; align-items: center; gap: 8px;
  font-size: 13px; font-weight: 500; color: var(--text);
}
.req-dot-live {
  position: relative; width: 8px; height: 8px; border-radius: 50%;
  background: var(--success); display: inline-block; flex-shrink: 0;
}
.req-dot-live::after {
  content: ''; position: absolute; inset: -3px; border-radius: 50%;
  border: 1.5px solid var(--success); opacity: 0;
  animation: req-ping 2.2s cubic-bezier(0, 0, 0.2, 1) infinite;
}
@keyframes req-ping {
  0% { transform: scale(0.85); opacity: 0.7; }
  75%, 100% { transform: scale(1.9); opacity: 0; }
}
.req-toggle-pill {
  display: inline-flex; align-items: center; gap: 5px;
  height: 28px; padding: 0 10px; border-radius: 999px;
  border: 1px solid var(--border); background: var(--surface);
  color: var(--text-secondary); font-size: 12px; font-weight: 500;
  transition: border-color 0.15s ease, background-color 0.15s ease, color 0.15s ease;
}
.req-toggle-pill:hover { border-color: var(--border-strong); color: var(--text); }
.req-toggle-pill.is-on {
  background: var(--accent-soft); border-color: transparent; color: var(--accent);
}

.req-toolbar {
  display: flex; flex-direction: column; gap: 10px;
}
.req-toolbar-row {
  display: flex; align-items: center; justify-content: space-between;
  gap: 10px; flex-wrap: wrap;
}
.req-toolbar-filters {
  display: flex; align-items: center; gap: 8px; flex-wrap: wrap; flex: 1; min-width: 0;
}
.req-toolbar-actions {
  display: flex; align-items: center; gap: 6px; flex-wrap: wrap; flex-shrink: 0;
}
.req-tab-count {
  display: inline-flex; align-items: center; justify-content: center;
  min-width: 18px; height: 17px; padding: 0 5px; border-radius: 999px;
  font-size: 11px; font-weight: 600; font-variant-numeric: tabular-nums;
  background: var(--surface-2); color: var(--text-secondary);
}
.tab-btn.active .req-tab-count { background: var(--surface-2); color: var(--text); }
.req-tab-count.is-pending { background: var(--amber-soft); color: var(--amber); }
.req-tab-count.is-danger { background: var(--danger-soft); color: var(--danger); }

.req-list { display: flex; flex-direction: column; gap: 12px; }
.req-view.is-compact .req-list { gap: 10px; }

.req-card {
  background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius);
  border-left: 3px solid var(--border-strong);
  padding: 16px 18px; display: flex; flex-direction: column; gap: 12px;
  transition: border-color 0.18s ease, box-shadow 0.25s ease, background-color 0.25s ease;
}
.req-card:hover { border-color: var(--border-strong); }
.req-card.status-pending { border-left-color: var(--amber); }
.req-card.status-approved { border-left-color: var(--success); }
.req-card.status-rejected { border-left-color: var(--danger); }
.req-card.status-failed { border-left-color: var(--danger); }
.req-card.is-new {
  box-shadow: 0 0 0 2px var(--accent-glow);
  animation: req-card-flash 0.55s var(--ease);
}
@keyframes req-card-flash {
  0% { opacity: 0.4; transform: translateY(-6px); }
  100% { opacity: 1; transform: none; }
}
.req-view.is-compact .req-card { padding: 12px 14px; gap: 10px; }

.req-card-head {
  display: flex; align-items: flex-start; justify-content: space-between; gap: 12px;
}
.req-card-identity {
  display: flex; align-items: flex-start; gap: 12px; min-width: 0; flex: 1;
}
.req-card-info { display: flex; flex-direction: column; gap: 4px; min-width: 0; flex: 1; }
.req-card-userline {
  display: flex; align-items: center; gap: 6px; flex-wrap: wrap; min-width: 0;
}
.req-card-name {
  font-size: 14.5px; font-weight: 600; color: var(--text);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 240px;
}
.req-copy-btn {
  background: none; border: none; padding: 0 2px; border-radius: 4px;
  color: var(--text-muted); font-family: var(--font-mono); font-size: 12px;
  cursor: pointer; transition: color 0.15s ease, background-color 0.15s ease;
}
.req-copy-btn:hover { color: var(--accent); background: var(--accent-soft); }

.req-card-groupline {
  display: flex; align-items: center; gap: 6px; flex-wrap: wrap;
  font-size: 12.5px; color: var(--text-secondary);
}
.req-card-meta-right {
  display: flex; flex-direction: column; align-items: flex-end; gap: 5px; flex-shrink: 0;
}
.req-card-time { font-size: 12px; color: var(--text-muted); white-space: nowrap; }

.req-comment-box {
  padding: 10px 13px; border-radius: var(--radius-sm);
  background: var(--bg-elevated); border: 1px solid var(--border-subtle);
  border-left: 3px solid var(--accent);
  font-size: 13.5px; line-height: 1.6; color: var(--text);
  white-space: pre-wrap; word-break: break-word;
}
.req-comment-box.is-empty {
  border-left-color: var(--border); color: var(--text-muted); font-style: italic; font-size: 13px;
}
.req-comment-label {
  font-size: 11px; font-weight: 600; letter-spacing: 0.02em;
  color: var(--text-muted); margin-bottom: 3px; text-transform: uppercase;
}
.req-error-box {
  display: flex; align-items: flex-start; gap: 8px;
  padding: 8px 12px; border-radius: var(--radius-sm);
  background: var(--danger-soft); color: var(--danger); font-size: 12.5px; line-height: 1.5;
}
.req-warn-note {
  display: flex; align-items: center; gap: 6px;
  padding: 7px 11px; border-radius: var(--radius-sm);
  background: var(--amber-soft); color: var(--amber); font-size: 12px; line-height: 1.45;
}

.req-card-foot {
  display: flex; align-items: center; justify-content: space-between;
  gap: 10px; flex-wrap: wrap; padding-top: 8px; border-top: 1px solid var(--border-subtle);
}
.req-card-decision {
  display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  font-size: 12.5px; color: var(--text-secondary); min-width: 0;
}
.req-card-btns {
  display: flex; align-items: center; gap: 8px; flex-shrink: 0; margin-left: auto;
}

.req-mode-grid {
  display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; width: 100%;
}
@media (max-width: 1020px) { .req-mode-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
@media (max-width: 580px) { .req-mode-grid { grid-template-columns: minmax(0, 1fr); } }
.req-mode-card {
  text-align: left; padding: 12px 13px; border-radius: 10px;
  border: 1px solid var(--border); background: var(--surface);
  display: flex; flex-direction: column; gap: 4px; color: var(--text);
  transition: border-color 0.15s ease, box-shadow 0.15s ease, background-color 0.15s ease;
}
.req-mode-card:hover { border-color: var(--border-strong); }
.req-mode-card.is-active {
  border-color: var(--accent); background: var(--accent-soft);
  box-shadow: 0 0 0 1px var(--accent);
}
.req-mode-title { font-size: 13.5px; font-weight: 600; display: flex; align-items: center; justify-content: space-between; gap: 6px; }
.req-mode-desc { font-size: 12px; color: var(--text-muted); line-height: 1.45; }

.req-chip-approve { background: var(--success-soft); color: var(--success); }
.req-chip-reject { background: var(--danger-soft); color: var(--danger); }

.req-test-grid {
  display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px;
}
@media (max-width: 780px) { .req-test-grid { grid-template-columns: minmax(0, 1fr); } }
.req-test-result {
  padding: 14px 16px; border-radius: 10px; border: 1px solid var(--border);
  background: var(--bg-elevated); display: flex; flex-direction: column; gap: 8px;
}
.req-test-result.act-approve { background: var(--success-soft); border-color: transparent; }
.req-test-result.act-reject { background: var(--danger-soft); border-color: transparent; }
.req-test-result.act-manual { background: var(--amber-soft); border-color: transparent; }

.req-reason-chips { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
.req-reason-chip {
  height: 26px; padding: 0 10px; border-radius: 999px;
  border: 1px solid var(--border); background: var(--bg-elevated);
  color: var(--text-secondary); font-size: 12px; cursor: pointer;
  transition: border-color 0.15s ease, color 0.15s ease;
}
.req-reason-chip:hover { border-color: var(--accent); color: var(--accent); }

.req-compact-summary {
  display: flex; align-items: center; justify-content: space-between; gap: 8px; flex-wrap: wrap;
  padding: 8px 12px; border-radius: 10px;
  background: var(--bg-elevated); border: 1px solid var(--border-subtle);
  font-size: 12.5px; color: var(--text-secondary);
}

@media (max-width: 640px) {
  .req-card-head { flex-direction: column; gap: 8px; }
  .req-card-meta-right { flex-direction: row; align-items: center; width: 100%; justify-content: space-between; }
}
`;

function ensureScopedStyles() {
  if (document.getElementById('req-view-styles')) return;
  const style = document.createElement('style');
  style.id = 'req-view-styles';
  style.textContent = SCOPED_CSS;
  document.head.appendChild(style);
}

function safeCssEscape(val) {
  const s = String(val ?? '');
  if (typeof CSS !== 'undefined' && typeof CSS.escape === 'function') {
    return CSS.escape(s);
  }
  return s.replace(/([^\w-])/g, '\\$1');
}

function deepClone(obj) {
  return JSON.parse(JSON.stringify(obj ?? {}));
}

function stableJson(obj) {
  if (Array.isArray(obj)) return `[${obj.map(stableJson).join(',')}]`;
  if (obj && typeof obj === 'object') {
    const keys = Object.keys(obj).sort();
    return `{${keys.map((k) => `${JSON.stringify(k)}:${stableJson(obj[k])}`).join(',')}}`;
  }
  return JSON.stringify(obj);
}

function normalizeStringList(arr) {
  if (!Array.isArray(arr)) return [];
  const seen = new Set();
  const out = [];
  for (const item of arr) {
    const s = String(item ?? '').trim();
    if (!s || seen.has(s)) continue;
    seen.add(s);
    out.push(s);
  }
  return out;
}

function clampNumber(val, min, max, fallback) {
  const n = Number(val);
  if (!Number.isFinite(n)) return fallback;
  return Math.min(max, Math.max(min, n));
}

function normalizeRule(raw, fallback = DEFAULT_RULE) {
  const src = raw && typeof raw === 'object' ? raw : {};
  const fb = fallback || DEFAULT_RULE;
  const mode = MODE_MAP[src.mode] ? src.mode : (MODE_MAP[fb.mode] ? fb.mode : 'manual');
  const handleInvite = ['manual', 'approve', 'reject'].includes(src.handle_invite)
    ? src.handle_invite
    : (['manual', 'approve', 'reject'].includes(fb.handle_invite) ? fb.handle_invite : 'manual');
  return {
    mode,
    approve_keywords: normalizeStringList(src.approve_keywords ?? fb.approve_keywords),
    reject_keywords: normalizeStringList(src.reject_keywords ?? fb.reject_keywords),
    reject_unmatched: Boolean(src.reject_unmatched ?? fb.reject_unmatched ?? false),
    default_reject_reason: String(src.default_reject_reason ?? fb.default_reject_reason ?? DEFAULT_RULE.default_reject_reason),
    min_qq_level: Math.round(clampNumber(src.min_qq_level ?? fb.min_qq_level ?? 0, 0, 200, 0)),
    handle_invite: handleInvite,
    auto_delay_secs: Number(clampNumber(src.auto_delay_secs ?? fb.auto_delay_secs ?? 1.0, 0, 30, 1.0).toFixed(1)),
    notify_admin_dm: Boolean(src.notify_admin_dm ?? fb.notify_admin_dm ?? false),
    welcome_message: String(src.welcome_message ?? fb.welcome_message ?? ''),
  };
}

function normalizePolicy(raw) {
  const src = raw && typeof raw === 'object' ? raw : {};
  const globalRule = normalizeRule(src.global, DEFAULT_RULE);
  const groupsOut = {};
  if (src.groups && typeof src.groups === 'object') {
    for (const [gid, gRule] of Object.entries(src.groups)) {
      const key = String(gid || '').trim();
      if (!key || !gRule || typeof gRule !== 'object') continue;
      groupsOut[key] = normalizeRule(gRule, globalRule);
    }
  }
  return {
    version: Number(src.version || 1),
    updated_at: String(src.updated_at || ''),
    enabled: src.enabled !== undefined ? Boolean(src.enabled) : true,
    global: globalRule,
    groups: groupsOut,
  };
}

function policyPayload(policy) {
  return {
    enabled: Boolean(policy.enabled),
    global: normalizeRule(policy.global, DEFAULT_RULE),
    groups: Object.fromEntries(
      Object.entries(policy.groups || {}).map(([gid, r]) => [gid, normalizeRule(r, policy.global)]),
    ),
  };
}

function renderQqAvatar(userId, label, sizeClass = '') {
  const digits = String(userId || '').replace(/\D/g, '');
  const s = String(label || '').trim();
  const initial = escapeHtml(s ? s.slice(0, 1).toUpperCase() : 'Q');
  const cls = sizeClass ? `avatar ${sizeClass}` : 'avatar';
  if (!digits) {
    return `<span class="${cls}" aria-hidden="true"><span>${initial}</span></span>`;
  }
  const url = `https://q1.qlogo.cn/g?b=qq&nk=${digits}&s=100`;
  return `
    <span class="${cls}" aria-hidden="true" style="position:relative;overflow:hidden;">
      <span>${initial}</span>
      <img src="${escapeHtml(url)}" alt="" loading="lazy" referrerpolicy="no-referrer" data-avatar-img="1" style="position:absolute;inset:0;width:100%;height:100%;object-fit:cover;" />
    </span>
  `;
}

function formatDecisionSource(src) {
  const key = String(src || '').trim();
  if (!key) return '';
  return DECISION_SOURCE_LABELS[key] || DECISION_SOURCE_LABELS[key.toLowerCase()] || key;
}

function formatExactTime(item, field = 'created') {
  const iso = field === 'handled' ? item.handled_iso : item.created_iso;
  const ts = field === 'handled' ? item.handled_at : item.created_at;
  if (iso) {
    return formatTime(iso);
  }
  return ts ? formatTime(ts) : '--';
}

function readBoolStorage(key, defaultVal) {
  try {
    const raw = localStorage.getItem(key);
    if (raw === null) return defaultVal;
    return raw === '1' || raw === 'true';
  } catch (_) {
    return defaultVal;
  }
}

function writeBoolStorage(key, val) {
  try {
    localStorage.setItem(key, val ? '1' : '0');
  } catch (_) {}
}

// ---------- Gentle Synthesized 2-Tone Chime (Web Audio API, zero files) ----------
let sharedAudioCtx = null;

function playNotificationChime() {
  try {
    const AudioCtx = window.AudioContext || window.webkitAudioContext;
    if (!AudioCtx) return;
    if (!sharedAudioCtx) {
      sharedAudioCtx = new AudioCtx();
    }
    if (sharedAudioCtx.state === 'suspended') {
      sharedAudioCtx.resume().catch(() => {});
    }
    const now = sharedAudioCtx.currentTime;
    const notes = [
      { freq: 880, start: 0, dur: 0.22, peak: 0.11 },     // A5
      { freq: 1174.66, start: 0.11, dur: 0.32, peak: 0.1 }, // D6
    ];
    for (const n of notes) {
      const osc = sharedAudioCtx.createOscillator();
      const gain = sharedAudioCtx.createGain();
      osc.type = 'sine';
      osc.frequency.setValueAtTime(n.freq, now + n.start);
      gain.gain.setValueAtTime(0.0001, now + n.start);
      gain.gain.linearRampToValueAtTime(n.peak, now + n.start + 0.018);
      gain.gain.exponentialRampToValueAtTime(0.0001, now + n.start + n.dur);
      osc.connect(gain);
      gain.connect(sharedAudioCtx.destination);
      osc.start(now + n.start);
      osc.stop(now + n.start + n.dur + 0.02);
    }
  } catch (_) {
    // Ignore audio context errors (e.g. autoplay blocked before first gesture)
  }
}

export function openMonitorPopup() {
  const win = window.open(
    '/join-monitor',
    'hajimi-join-monitor',
    'width=540,height=780,resizable=yes,scrollbars=yes',
  );
  if (win) {
    try { win.focus(); } catch (_) {}
  } else {
    showToast('弹窗被浏览器拦截，请允许本站弹出窗口或直接访问 /join-monitor', 'warning');
  }
  return win;
}

export async function mount(container, ctx = {}) {
  ensureScopedStyles();

  const isStandalone = Boolean(ctx?.standalone);
  const isCompact = isStandalone || String(ctx?.params?.monitor || '') === '1';

  const validTopTabs = new Set(['monitor', 'rules']);
  let activeTab = !isCompact && validTopTabs.has(ctx?.params?.tab) ? ctx.params.tab : 'monitor';

  const validStatuses = new Set(['pending', 'all', 'approved', 'rejected', 'failed']);
  let filterStatus = validStatuses.has(ctx?.params?.status) ? ctx.params.status : 'pending';
  let filterGroupId = String(ctx?.params?.group_id || '');
  let filterSubType = ['all', 'add', 'invite'].includes(ctx?.params?.sub_type) ? ctx.params.sub_type : 'all';
  let filterQuery = String(ctx?.params?.q || '');

  // Runtime state
  let items = [];
  let counts = { pending: 0, approved: 0, rejected: 0, failed: 0, total: 0 };
  let groups = [];
  let basePolicy = normalizePolicy({});
  let draftPolicy = normalizePolicy({});
  let napcatOnline = true;
  let wsGuardRunning = true;
  let latestSeq = 0;

  // Connection & preferences
  let sseState = 'connecting'; // 'connecting' | 'connected' | 'reconnecting'
  let eventSource = null;
  let reconnectTimer = null;
  let reconnectDelayMs = 1500;
  let searchDebounceTimer = null;
  let relTimeTimer = null;
  let isComposingSearch = false;

  let soundEnabled = readBoolStorage(STORAGE_SOUND_KEY, true);
  let notifyEnabled = readBoolStorage(STORAGE_NOTIFY_KEY, false) &&
    (typeof window !== 'undefined' && 'Notification' in window && Notification.permission === 'granted');

  // Rules tab state
  let selectedRuleGid = '';
  let ruleGroupQuery = '';
  let testerGroupId = '';
  let testerSubType = 'add';
  let testerQqLevel = '';
  let testerComment = '';
  let testerResult = null;
  let testingRule = false;

  // Action busy flags
  let initialLoading = true;
  let loadError = '';
  let syncing = false;
  let batchBusy = false;
  let savingPolicy = false;
  const busyIds = new Set();
  const notifiedIds = new Set();
  const newFlashIds = new Set();
  const activeModalClosers = new Set();

  let destroyed = false;

  // Capture-phase CSP-safe avatar fallback handler
  const onAvatarError = (e) => {
    const target = e.target;
    if (target && target.tagName === 'IMG' && target.dataset && target.dataset.avatarImg === '1') {
      target.remove();
    }
  };
  container.addEventListener('error', onAvatarError, true);

  function notifyHostStatus() {
    if (typeof ctx?.onStatusChange === 'function') {
      ctx.onStatusChange({
        sseState,
        counts: { ...counts },
        pendingCount: Number(counts.pending || 0),
        napcatOnline,
        wsGuardRunning,
        policyEnabled: Boolean(basePolicy.enabled),
        soundEnabled,
        notifyEnabled,
        syncing,
      });
    }
  }

  function isPolicyDirty() {
    return stableJson(policyPayload(basePolicy)) !== stableJson(policyPayload(draftPolicy));
  }

  function getGroupInfo(gid) {
    const key = String(gid || '');
    return groups.find((g) => String(g.group_id) === key) || null;
  }

  function getDefaultRejectReasonForGroup(gid) {
    const key = String(gid || '');
    const gRule = basePolicy.groups?.[key];
    const reason = gRule?.default_reject_reason || basePolicy.global?.default_reject_reason || DEFAULT_RULE.default_reject_reason;
    return String(reason || DEFAULT_RULE.default_reject_reason);
  }

  function itemMatchesFilters(item) {
    if (!item) return false;
    if (filterStatus !== 'all' && item.status !== filterStatus) return false;
    if (filterGroupId && String(item.group_id) !== filterGroupId) return false;
    if (filterSubType !== 'all' && item.sub_type !== filterSubType) return false;
    const q = filterQuery.trim().toLowerCase();
    if (q) {
      const hay = [
        item.user_id,
        item.user_name,
        item.group_id,
        item.group_name,
        item.invitor_id,
        item.invitor_name,
        item.comment,
        item.reject_reason,
      ].join(' ').toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  }

  function upsertItem(rawReq) {
    if (!rawReq || rawReq.id === undefined || rawReq.id === null) return;
    const idx = items.findIndex((it) => String(it.id) === String(rawReq.id));
    if (idx >= 0) {
      items[idx] = { ...items[idx], ...rawReq };
    } else {
      items.unshift(rawReq);
    }
    items.sort((a, b) => {
      const ta = Number(a.created_at || 0);
      const tb = Number(b.created_at || 0);
      if (tb !== ta) return tb - ta;
      return Number(b.id || 0) - Number(a.id || 0);
    });
    if (items.length > 200) items.length = 200;
  }

  function getVisibleItems() {
    return items.filter(itemMatchesFilters);
  }

  // ---------- Header actions (full mode only) ----------
  function renderHeaderActions() {
    if (isCompact || typeof ctx?.setActions !== 'function') return;
    const actionsEl = ctx.setActions(`
      <button type="button" class="btn btn-secondary" id="req-popout-btn" title="弹出独立的入群申请监控小窗口">
        ${reqIcon('popout', 14)}弹出独立监控窗
      </button>
      <button type="button" class="btn btn-primary" id="req-sync-btn" ${syncing ? 'disabled' : ''}>
        <span class="${syncing ? 'spin' : ''}" style="display:inline-flex;">${reqIcon('refresh', 14)}</span>${syncing ? '同步中…' : '同步 NapCat'}
      </button>
    `);
    actionsEl?.querySelector('#req-popout-btn')?.addEventListener('click', openMonitorPopup);
    actionsEl?.querySelector('#req-sync-btn')?.addEventListener('click', () => syncNapcat());
  }

  // ---------- Sound & Desktop Notification ----------
  function toggleSound() {
    soundEnabled = !soundEnabled;
    writeBoolStorage(STORAGE_SOUND_KEY, soundEnabled);
    if (soundEnabled) {
      playNotificationChime();
      showToast('已开启新申请声音提醒', 'success');
    } else {
      showToast('已关闭声音提醒', 'info');
    }
    renderLiveStrip();
    notifyHostStatus();
    return soundEnabled;
  }

  async function toggleNotify() {
    if (typeof window === 'undefined' || !('Notification' in window)) {
      showToast('当前浏览器不支持桌面通知 API', 'warning');
      return false;
    }
    if (notifyEnabled) {
      notifyEnabled = false;
      writeBoolStorage(STORAGE_NOTIFY_KEY, false);
      showToast('已关闭桌面通知', 'info');
      renderLiveStrip();
      notifyHostStatus();
      return false;
    }

    if (Notification.permission === 'denied') {
      showToast('浏览器已禁止桌面通知，请在地址栏站点权限中手动允许通知', 'warning');
      notifyEnabled = false;
      writeBoolStorage(STORAGE_NOTIFY_KEY, false);
      renderLiveStrip();
      notifyHostStatus();
      return false;
    }

    let perm = Notification.permission;
    if (perm === 'default') {
      try {
        perm = await Notification.requestPermission();
      } catch (_) {
        perm = 'denied';
      }
    }
    if (destroyed) return false;

    if (perm === 'granted') {
      notifyEnabled = true;
      writeBoolStorage(STORAGE_NOTIFY_KEY, true);
      showToast('已开启桌面通知，收到新入群申请时会弹窗提醒', 'success');
    } else {
      notifyEnabled = false;
      writeBoolStorage(STORAGE_NOTIFY_KEY, false);
      showToast('未获得桌面通知权限', 'warning');
    }
    renderLiveStrip();
    notifyHostStatus();
    return notifyEnabled;
  }

  function triggerNewRequestAlerts(req) {
    if (!req || req.status !== 'pending') return;
    const key = String(req.id ?? req.flag ?? '');
    if (!key || notifiedIds.has(key)) return;
    notifiedIds.add(key);

    newFlashIds.add(key);
    window.setTimeout(() => {
      newFlashIds.delete(key);
      const card = container.querySelector(`[data-req-card="${safeCssEscape(key)}"]`);
      card?.classList.remove('is-new');
    }, 3600);

    if (soundEnabled) {
      playNotificationChime();
    }

    if (notifyEnabled && typeof window !== 'undefined' && 'Notification' in window && Notification.permission === 'granted') {
      try {
        const prefix = req.sub_type === 'invite' ? '邀请机器人入群' : '新入群申请';
        const gLabel = req.group_name || `群 ${req.group_id}`;
        const uLabel = req.user_name ? `${req.user_name} (${req.user_id})` : `QQ ${req.user_id}`;
        const commentText = String(req.comment || '').trim() || '无验证消息';
        const n = new Notification(`${prefix} · ${gLabel}`, {
          body: `${uLabel}\n${commentText.slice(0, 140)}`,
          tag: `hajimi-group-req-${key}`,
        });
        n.onclick = () => {
          try { window.focus(); } catch (_) {}
          n.close();
        };
      } catch (_) {}
    }

    if (!isCompact && activeTab !== 'monitor') {
      showToast(`收到新入群申请：${req.user_name || req.user_id} → ${req.group_name || req.group_id}`, 'info');
    }
  }

  // ---------- Reject Reason Modal ----------
  function openRejectModal({ title, defaultReason = '', subtitle = '', confirmText = '确认拒绝' }) {
    return new Promise((resolve) => {
      const root = document.getElementById('modal-root') || document.body;
      const prevFocus = document.activeElement;

      const backdrop = document.createElement('div');
      backdrop.className = 'modal-backdrop';
      backdrop.setAttribute('role', 'dialog');
      backdrop.setAttribute('aria-modal', 'true');
      backdrop.setAttribute('aria-labelledby', 'req-reject-modal-title');

      backdrop.innerHTML = `
        <div class="modal-dialog">
          <div class="modal-header">
            <h3 class="modal-title" id="req-reject-modal-title">${escapeHtml(title || '拒绝入群申请')}</h3>
            <button type="button" class="btn btn-ghost btn-icon btn-sm" data-m-cancel aria-label="关闭">
              ${reqIcon('x', 16)}
            </button>
          </div>
          <div class="modal-body" style="white-space:normal;">
            ${subtitle ? `<div class="text-sm text-secondary mb-12">${escapeHtml(subtitle)}</div>` : ''}
            <div class="field">
              <label class="field-label" for="req-reject-reason-input">拒绝理由（可选，将展示给申请人）</label>
              <input type="text" class="input" id="req-reject-reason-input" value="${escapeHtml(defaultReason)}" placeholder="输入拒绝理由，留空则不附带理由" maxlength="120" autocomplete="off" />
            </div>
            <div class="req-reason-chips">
              ${QUICK_REJECT_REASONS.map((r) => `
                <button type="button" class="req-reason-chip" data-quick-reason="${escapeHtml(r)}">${escapeHtml(r)}</button>
              `).join('')}
            </div>
          </div>
          <div class="modal-footer">
            <button type="button" class="btn btn-secondary" data-m-cancel>取消</button>
            <button type="button" class="btn btn-danger" data-m-confirm>${escapeHtml(confirmText)}</button>
          </div>
        </div>
      `;

      let settled = false;
      const finish = (val) => {
        if (settled) return;
        settled = true;
        activeModalClosers.delete(closeOnDestroy);
        document.removeEventListener('keydown', onKeyDown);
        backdrop.remove();
        if (prevFocus && typeof prevFocus.focus === 'function') {
          try { prevFocus.focus(); } catch (_) {}
        }
        resolve(val);
      };
      const closeOnDestroy = () => finish(null);
      activeModalClosers.add(closeOnDestroy);

      const inputEl = backdrop.querySelector('#req-reject-reason-input');

      const onKeyDown = (e) => {
        if (e.key === 'Escape') {
          e.preventDefault();
          finish(null);
        } else if (e.key === 'Enter' && document.activeElement === inputEl && !e.isComposing) {
          e.preventDefault();
          finish(String(inputEl?.value ?? '').trim());
        }
      };

      backdrop.addEventListener('click', (e) => {
        if (e.target === backdrop) finish(null);
      });
      backdrop.querySelectorAll('[data-m-cancel]').forEach((b) => {
        b.addEventListener('click', () => finish(null));
      });
      backdrop.querySelector('[data-m-confirm]')?.addEventListener('click', () => {
        finish(String(inputEl?.value ?? '').trim());
      });
      backdrop.querySelectorAll('[data-quick-reason]').forEach((chip) => {
        chip.addEventListener('click', () => {
          if (inputEl) {
            inputEl.value = chip.dataset.quickReason || '';
            inputEl.focus();
          }
        });
      });

      document.addEventListener('keydown', onKeyDown);
      root.appendChild(backdrop);
      if (inputEl) {
        inputEl.focus();
        inputEl.select();
      }
    });
  }

  // ---------- API Operations ----------
  async function fetchList({ sync = false, silent = false, ruleUpdated = false } = {}) {
    const qs = new URLSearchParams();
    qs.set('status', filterStatus);
    if (filterGroupId) qs.set('group_id', filterGroupId);
    if (filterSubType && filterSubType !== 'all') qs.set('sub_type', filterSubType);
    if (filterQuery.trim()) qs.set('q', filterQuery.trim());
    qs.set('limit', '100');
    if (sync) qs.set('sync', '1');

    try {
      const data = await apiGet(`/api/group-requests?${qs.toString()}`);
      if (destroyed) return null;
      const wasInitial = initialLoading;
      const wasDirty = isPolicyDirty();
      loadError = '';
      initialLoading = false;
      if (data && typeof data === 'object') {
        if (Array.isArray(data.items)) {
          items = data.items;
          for (const it of items) {
            if (it && (it.id !== undefined || it.flag)) {
              notifiedIds.add(String(it.id ?? it.flag));
            }
          }
        }
        if (data.counts && typeof data.counts === 'object') {
          counts = {
            pending: Number(data.counts.pending || 0),
            approved: Number(data.counts.approved || 0),
            rejected: Number(data.counts.rejected || 0),
            failed: Number(data.counts.failed || 0),
            total: Number(data.counts.total || 0),
          };
        }
        if (Array.isArray(data.groups)) {
          groups = data.groups;
        }
        if (data.policy && typeof data.policy === 'object') {
          const nextNorm = normalizePolicy(data.policy);
          basePolicy = nextNorm;
          if (!wasDirty) {
            draftPolicy = deepClone(nextNorm);
          }
        }
        if (typeof data.napcat_online === 'boolean') napcatOnline = data.napcat_online;
        if (typeof data.ws_guard_running === 'boolean') wsGuardRunning = data.ws_guard_running;
        if (Number.isFinite(Number(data.latest_seq))) {
          latestSeq = Math.max(latestSeq, Number(data.latest_seq));
        }
      }
      renderLiveStrip();
      renderOfflineBanners();
      renderTopTabs();
      if (activeTab === 'monitor' || isCompact) {
        renderKpiStrip();
        updateGroupFilterOptions();
        updateStatusTabCounts();
        updateBatchButtons();
        renderRequestList();
      } else if (activeTab === 'rules') {
        if (wasInitial || (ruleUpdated && !wasDirty)) {
          renderRulesBody();
        } else {
          renderRulesGroupList();
          populateTesterGroups();
        }
      }
      notifyHostStatus();
      return data;
    } catch (err) {
      if (destroyed) return null;
      initialLoading = false;
      loadError = err?.message || String(err);
      if (!silent) showToast(`加载入群申请失败：${loadError}`, 'error');
      renderRequestList();
      return null;
    }
  }

  async function syncNapcat() {
    if (syncing) return;
    syncing = true;
    renderHeaderActions();
    notifyHostStatus();
    try {
      const res = await apiPost('/api/group-requests/sync', {});
      if (destroyed) return;
      if (res && typeof res === 'object') {
        if (res.counts) {
          counts = {
            pending: Number(res.counts.pending || 0),
            approved: Number(res.counts.approved || 0),
            rejected: Number(res.counts.rejected || 0),
            failed: Number(res.counts.failed || 0),
            total: Number(res.counts.total || 0),
          };
        }
        const added = res.sync?.synced_new ?? res.sync?.inserted ?? res.sync?.added ?? res.sync?.new_count;
        const total = res.sync?.total_fetched ?? res.sync?.fetched ?? res.sync?.total;
        const detail = added !== undefined
          ? `新增 ${added} 条`
          : total !== undefined
            ? `拉取 ${total} 条`
            : '已完成';
        showToast(`已与 NapCat 同步（${detail}）`, 'success');
      } else {
        showToast('已与 NapCat 完成同步', 'success');
      }
      await fetchList({ sync: false, silent: true });
      ctx?.refreshGlobalStatus?.({ silent: true, force: true });
    } catch (err) {
      if (!destroyed) showToast(`同步 NapCat 失败：${err.message}`, 'error');
    } finally {
      syncing = false;
      if (!destroyed) {
        renderHeaderActions();
        notifyHostStatus();
      }
    }
  }

  async function handleSingleDecision(reqId, action) {
    const idKey = String(reqId);
    if (busyIds.has(idKey)) return;
    const item = items.find((it) => String(it.id) === idKey);
    if (!item) return;

    let reason = '';
    if (action === 'reject') {
      const defaultReason = getDefaultRejectReasonForGroup(item.group_id);
      const userLabel = item.user_name ? `${item.user_name} (${item.user_id})` : `QQ ${item.user_id}`;
      const groupLabel = item.group_name || `群 ${item.group_id}`;
      const resReason = await openRejectModal({
        title: `拒绝 ${item.user_name || item.user_id} 的申请`,
        subtitle: `目标群聊：${groupLabel}（#${item.group_id}） · 申请人：${userLabel}`,
        defaultReason,
        confirmText: '确认拒绝',
      });
      if (resReason === null || destroyed) return;
      reason = resReason;
    }

    busyIds.add(idKey);
    renderRequestList();
    try {
      const url = action === 'approve'
        ? `/api/group-requests/${encodeURIComponent(idKey)}/approve`
        : `/api/group-requests/${encodeURIComponent(idKey)}/reject`;
      const body = action === 'reject' ? { reason } : {};
      const res = await apiPost(url, body);
      if (destroyed) return;
      if (res?.request) {
        upsertItem(res.request);
      } else {
        await fetchList({ silent: true });
      }
      if (res?.counts) {
        counts = {
          pending: Number(res.counts.pending || 0),
          approved: Number(res.counts.approved || 0),
          rejected: Number(res.counts.rejected || 0),
          failed: Number(res.counts.failed || 0),
          total: Number(res.counts.total || 0),
        };
      }
      showToast(
        action === 'approve'
          ? `已同意 ${item.user_name || item.user_id} 加入 ${item.group_name || item.group_id}`
          : `已拒绝 ${item.user_name || item.user_id} 的申请`,
        'success',
      );
      renderTopTabs();
      renderKpiStrip();
      updateStatusTabCounts();
      updateBatchButtons();
      renderRequestList();
      notifyHostStatus();
      ctx?.refreshGlobalStatus?.({ silent: true, force: true });
    } catch (err) {
      if (!destroyed) {
        showToast(`${action === 'approve' ? '同意' : '拒绝'}失败：${err.message}`, 'error');
        await fetchList({ silent: true });
      }
    } finally {
      busyIds.delete(idKey);
      if (!destroyed) renderRequestList();
    }
  }

  async function handleBatchApprove() {
    if (batchBusy) return;
    const pendingItems = getVisibleItems().filter((it) => it.status === 'pending');
    if (!pendingItems.length) {
      showToast('当前筛选条件下没有待处理申请', 'info');
      return;
    }
    const nonAdminCount = pendingItems.filter((it) => it.bot_role === 'member').length;
    const extraHint = nonAdminCount > 0
      ? `\n注意：其中 ${nonAdminCount} 条所在群机器人仅为普通成员，可能会执行失败。`
      : '';
    const ok = await confirmModal({
      title: `一键全部同意 ${pendingItems.length} 条待处理申请？`,
      message: `将按顺序向 NapCat 提交同意操作。${extraHint}`,
      confirmText: `全部同意 (${pendingItems.length})`,
    });
    if (!ok || destroyed) return;

    batchBusy = true;
    updateBatchButtons();
    try {
      const res = await apiPost('/api/group-requests/batch', {
        ids: pendingItems.map((it) => it.id),
        action: 'approve',
        reason: '',
      });
      if (destroyed) return;
      if (res?.counts) {
        counts = {
          pending: Number(res.counts.pending || 0),
          approved: Number(res.counts.approved || 0),
          rejected: Number(res.counts.rejected || 0),
          failed: Number(res.counts.failed || 0),
          total: Number(res.counts.total || 0),
        };
      }
      const succ = Number(res?.succeeded ?? 0);
      const fail = Number(res?.failed ?? 0);
      showToast(
        fail > 0
          ? `批量处理完成：成功 ${succ} 条，失败 ${fail} 条`
          : `已批量同意 ${succ || pendingItems.length} 条申请`,
        fail > 0 ? 'warning' : 'success',
      );
      await fetchList({ silent: true });
      ctx?.refreshGlobalStatus?.({ silent: true, force: true });
    } catch (err) {
      if (!destroyed) showToast(`批量同意失败：${err.message}`, 'error');
    } finally {
      batchBusy = false;
      if (!destroyed) updateBatchButtons();
    }
  }

  async function handleClearProcessed() {
    if (batchBusy) return;
    const processedCount = (counts.approved || 0) + (counts.rejected || 0) + (counts.failed || 0);
    if (processedCount <= 0) {
      showToast('当前没有已处理的历史记录需要清理', 'info');
      return;
    }
    const ok = await confirmModal({
      title: '清理已处理的入群记录？',
      message: `将删除全部已同意、已拒绝及失败的历史记录（约 ${processedCount} 条），待处理记录会保留。`,
      confirmText: '确认清理',
      danger: true,
    });
    if (!ok || destroyed) return;

    batchBusy = true;
    updateBatchButtons();
    try {
      const res = await apiPost('/api/group-requests/clear', { before_days: 0 });
      if (destroyed) return;
      const deleted = Number(res?.deleted ?? 0);
      if (res?.counts) {
        counts = {
          pending: Number(res.counts.pending || 0),
          approved: Number(res.counts.approved || 0),
          rejected: Number(res.counts.rejected || 0),
          failed: Number(res.counts.failed || 0),
          total: Number(res.counts.total || 0),
        };
      }
      showToast(`已清理 ${deleted} 条历史记录`, 'success');
      await fetchList({ silent: true });
      ctx?.refreshGlobalStatus?.({ silent: true, force: true });
    } catch (err) {
      if (!destroyed) showToast(`清理失败：${err.message}`, 'error');
    } finally {
      batchBusy = false;
      if (!destroyed) updateBatchButtons();
    }
  }

  async function savePolicy() {
    if (savingPolicy) return;
    // Commit any active chip editor input before saving (e.g. when pressing Cmd/Ctrl+S)
    const activeEl = document.activeElement;
    if (activeEl && activeEl.dataset && activeEl.dataset.chipInput && activeEl.value.trim()) {
      activeEl.blur();
    }
    savingPolicy = true;
    renderStickyBar();
    try {
      const payload = policyPayload(draftPolicy);
      const res = await apiPut('/api/group-requests/policy', payload);
      if (destroyed) return;
      const updatedPolicy = normalizePolicy(res?.policy || res || payload);
      basePolicy = updatedPolicy;
      draftPolicy = deepClone(updatedPolicy);
      if (Array.isArray(res?.groups)) {
        groups = res.groups;
      }
      showToast('自动审批规则已保存并立即热生效', 'success');
      renderTopTabs();
      renderRulesBody();
      renderStickyBar();
      notifyHostStatus();
    } catch (err) {
      if (!destroyed) showToast(`保存规则失败：${err.message}`, 'error');
    } finally {
      savingPolicy = false;
      if (!destroyed) renderStickyBar();
    }
  }

  async function runRuleTest() {
    if (testingRule) return;
    testingRule = true;
    renderTesterResult();
    try {
      const lvlRaw = String(testerQqLevel || '').trim();
      const qqLevel = lvlRaw !== '' && Number.isFinite(Number(lvlRaw)) ? Number(lvlRaw) : null;
      const res = await apiPost('/api/group-requests/policy/test', {
        group_id: testerGroupId || '',
        comment: testerComment,
        sub_type: testerSubType,
        qq_level: qqLevel,
        policy: draftPolicy,
      });
      if (destroyed) return;
      testerResult = res || { action: 'manual', reason: '无返回结果' };
    } catch (err) {
      if (!destroyed) {
        testerResult = { error: err.message };
        showToast(`模拟判定失败：${err.message}`, 'error');
      }
    } finally {
      testingRule = false;
      if (!destroyed) renderTesterResult();
    }
  }

  // ---------- SSE Live Stream ----------
  function closeStream() {
    if (eventSource) {
      try { eventSource.close(); } catch (_) {}
      eventSource = null;
    }
    if (reconnectTimer) {
      window.clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
  }

  function connectStream() {
    closeStream();
    if (destroyed) return;

    sseState = sseState === 'reconnecting' ? 'reconnecting' : 'connecting';
    renderLiveStrip();
    notifyHostStatus();

    const url = `/api/group-requests/stream?since=${encodeURIComponent(latestSeq)}`;
    const es = new EventSource(url);
    eventSource = es;

    es.onopen = () => {
      if (destroyed || eventSource !== es) return;
      sseState = 'connected';
      reconnectDelayMs = 1500;
      renderLiveStrip();
      notifyHostStatus();
    };

    es.addEventListener('snapshot', (ev) => {
      if (destroyed || eventSource !== es) return;
      let data = null;
      try { data = JSON.parse(ev.data); } catch (_) { return; }
      if (!data || typeof data !== 'object') return;
      sseState = 'connected';
      if (data.counts && typeof data.counts === 'object') {
        counts = {
          pending: Number(data.counts.pending || 0),
          approved: Number(data.counts.approved || 0),
          rejected: Number(data.counts.rejected || 0),
          failed: Number(data.counts.failed || 0),
          total: Number(data.counts.total || 0),
        };
      }
      if (Number.isFinite(Number(data.latest_seq))) {
        latestSeq = Math.max(latestSeq, Number(data.latest_seq));
      }
      const prevOnline = napcatOnline;
      if (typeof data.napcat_online === 'boolean') napcatOnline = data.napcat_online;
      if (typeof data.ws_guard_running === 'boolean') wsGuardRunning = data.ws_guard_running;
      if (typeof data.policy_enabled === 'boolean') {
        basePolicy.enabled = data.policy_enabled;
        if (!isPolicyDirty()) draftPolicy.enabled = data.policy_enabled;
      }
      renderLiveStrip();
      renderOfflineBanners();
      renderTopTabs();
      renderKpiStrip();
      updateStatusTabCounts();
      updateBatchButtons();
      if (prevOnline !== napcatOnline && (activeTab === 'monitor' || isCompact)) {
        renderRequestList();
      }
      notifyHostStatus();
    });

    es.addEventListener('request_event', (ev) => {
      if (destroyed || eventSource !== es) return;
      let data = null;
      try { data = JSON.parse(ev.data); } catch (_) { return; }
      if (!data || typeof data !== 'object') return;

      if (Number.isFinite(Number(data.seq))) {
        latestSeq = Math.max(latestSeq, Number(data.seq));
      }
      if (data.counts && typeof data.counts === 'object') {
        counts = {
          pending: Number(data.counts.pending || 0),
          approved: Number(data.counts.approved || 0),
          rejected: Number(data.counts.rejected || 0),
          failed: Number(data.counts.failed || 0),
          total: Number(data.counts.total || 0),
        };
      }

      const evType = String(data.event_type || '');
      if (data.request && typeof data.request === 'object') {
        upsertItem(data.request);
        if (evType === 'new') {
          triggerNewRequestAlerts(data.request);
        }
      }

      if (evType === 'synced' || evType === 'rule_updated') {
        fetchList({ silent: true, ruleUpdated: evType === 'rule_updated' });
        return;
      }

      renderTopTabs();
      if (activeTab === 'monitor' || isCompact) {
        renderKpiStrip();
        updateStatusTabCounts();
        updateBatchButtons();
        renderRequestList();
      }
      notifyHostStatus();
      ctx?.refreshGlobalStatus?.({ silent: true, force: true });
    });

    es.onerror = () => {
      if (destroyed || eventSource !== es) return;
      closeStream();
      sseState = 'reconnecting';
      renderLiveStrip();
      notifyHostStatus();
      const delay = reconnectDelayMs;
      reconnectDelayMs = Math.min(10000, Math.round(reconnectDelayMs * 1.6));
      reconnectTimer = window.setTimeout(() => {
        if (!destroyed) connectStream();
      }, delay);
    };
  }

  // ---------- Render Shell ----------
  container.innerHTML = `
    <div class="req-view${isCompact ? ' is-compact' : ''}">
      ${!isStandalone ? '<div id="req-live-strip-slot"></div>' : ''}
      <div id="req-alerts-slot"></div>
      ${!isCompact ? '<div class="page-tabs-row" id="req-top-tabs"></div>' : ''}
      <div id="req-tab-body"></div>
      ${!isCompact ? '<div id="req-sticky-root"></div>' : ''}
    </div>
  `;

  function renderLiveStrip() {
    const slot = container.querySelector('#req-live-strip-slot');
    if (!slot) return;

    const statusHtml = sseState === 'connected'
      ? `<span class="req-sse-indicator"><span class="req-dot-live"></span>实时盯盘中</span>`
      : sseState === 'reconnecting'
        ? `<span class="req-sse-indicator text-amber"><span class="status-dot warn"></span>SSE 重连中…</span>`
        : `<span class="req-sse-indicator text-muted"><span class="status-dot"></span>正在连接实时流…</span>`;

    const modeLabel = basePolicy.enabled
      ? (MODE_MAP[basePolicy.global?.mode]?.short || '手动审批')
      : '引擎已停用';

    slot.innerHTML = `
      <div class="req-live-strip">
        <div class="req-live-left">
          ${statusHtml}
          <span class="badge badge-sm ${basePolicy.enabled && basePolicy.global?.mode !== 'manual' ? 'badge-success' : 'badge-muted'}">
            ${reqIcon('zap', 11)}自动审批：${escapeHtml(modeLabel)}
          </span>
          ${isCompact && !isStandalone ? `
            <button type="button" class="btn btn-ghost btn-xs" id="req-exit-compact-btn">
              返回完整视图
            </button>
          ` : ''}
        </div>
        <div class="req-live-right">
          <button type="button" class="req-toggle-pill ${soundEnabled ? 'is-on' : ''}" id="req-sound-toggle" title="${soundEnabled ? '点击关闭声音提醒' : '点击开启声音提醒'}">
            ${reqIcon(soundEnabled ? 'volume2' : 'volumeX', 13)}声音提醒
          </button>
          <button type="button" class="req-toggle-pill ${notifyEnabled ? 'is-on' : ''}" id="req-notify-toggle" title="${notifyEnabled ? '点击关闭桌面通知' : '点击开启桌面通知'}">
            ${reqIcon(notifyEnabled ? 'bell' : 'bellOff', 13)}桌面通知
          </button>
        </div>
      </div>
    `;

    slot.querySelector('#req-sound-toggle')?.addEventListener('click', () => toggleSound());
    slot.querySelector('#req-notify-toggle')?.addEventListener('click', () => toggleNotify());
    slot.querySelector('#req-exit-compact-btn')?.addEventListener('click', () => {
      ctx?.navigate?.('requests');
    });
  }

  function renderOfflineBanners() {
    const slot = container.querySelector('#req-alerts-slot');
    if (!slot) return;
    const banners = [];
    if (!napcatOnline) {
      banners.push(`
        <div class="alert-banner alert-warning">
          <div class="row-sm" style="min-width:0;">
            <span class="text-amber">${reqIcon('alert', 16)}</span>
            <span><strong>NapCat 未在线</strong>：当前无法向 QQ 同步申请或执行同意/拒绝操作。</span>
          </div>
          ${!isStandalone ? `<button type="button" class="btn btn-secondary btn-xs" data-nav-target="napcat">前往 QQ 连接</button>` : ''}
        </div>
      `);
    }
    if (!wsGuardRunning) {
      banners.push(`
        <div class="alert-banner alert-danger">
          <div class="row-sm" style="min-width:0;">
            <span class="text-danger">${reqIcon('alert', 16)}</span>
            <span><strong>ws-guard 未运行</strong>：新的入群申请可能无法被实时捕获，自动审批引擎暂停工作。</span>
          </div>
          ${!isStandalone ? `<button type="button" class="btn btn-secondary btn-xs" data-nav-target="overview">查看服务总览</button>` : ''}
        </div>
      `);
    }
    slot.innerHTML = banners.length ? `<div class="stack-sm">${banners.join('')}</div>` : '';
    slot.querySelectorAll('[data-nav-target]').forEach((btn) => {
      btn.addEventListener('click', () => ctx?.navigate?.(btn.dataset.navTarget));
    });
  }

  function renderTopTabs() {
    const bar = container.querySelector('#req-top-tabs');
    if (!bar) return;

    const overrideGroupsCount = Object.keys(draftPolicy.groups || {}).length;
    bar.innerHTML = `
      <div class="page-tabs">
        <button type="button" class="page-tab ${activeTab === 'monitor' ? 'active' : ''}" data-rtab="monitor">
          实时申请监控
          ${counts.pending > 0 ? `<span class="req-tab-count is-pending">${counts.pending}</span>` : `<span class="count">${counts.total}</span>`}
        </button>
        <button type="button" class="page-tab ${activeTab === 'rules' ? 'active' : ''}" data-rtab="rules">
          自动审批规则
          ${overrideGroupsCount > 0 ? `<span class="count">${overrideGroupsCount} 群独立</span>` : ''}
          ${isPolicyDirty() ? '<span class="dirty-dot" title="有未保存改动"></span>' : ''}
        </button>
      </div>
      <div class="metric-strip hide-xs" style="padding-bottom:12px;">
        <span class="metric-chip">待处理 <strong class="${counts.pending > 0 ? 'text-amber' : ''}">${counts.pending}</strong></span>
        <span class="metric-sep"></span>
        <span class="metric-chip">已通过 <strong class="text-success">${counts.approved}</strong></span>
        <span class="metric-sep"></span>
        <span class="metric-chip">已拒绝 <strong>${counts.rejected}</strong></span>
      </div>
    `;

    bar.querySelectorAll('[data-rtab]').forEach((btn) => {
      btn.addEventListener('click', () => {
        const next = btn.dataset.rtab;
        if (next === activeTab) return;
        activeTab = next;
        renderTopTabs();
        renderActiveTab();
        renderStickyBar();
      });
    });
  }

  // ---------- Tab 1: Monitor View ----------
  function renderMonitorTab(body) {
    body.innerHTML = `
      <div class="stack" style="gap:${isCompact ? '12px' : '18px'};">
        ${isCompact
          ? '<div id="req-compact-summary-slot"></div>'
          : '<div id="req-kpi-slot"></div>'}

        <div class="req-toolbar">
          <div class="req-toolbar-row">
            <div class="tabs" id="req-status-tabs">
              <button type="button" class="tab-btn ${filterStatus === 'pending' ? 'active' : ''}" data-status="pending">
                待处理 <span class="req-tab-count" data-scount="pending">0</span>
              </button>
              <button type="button" class="tab-btn ${filterStatus === 'all' ? 'active' : ''}" data-status="all">
                全部 <span class="req-tab-count" data-scount="all">0</span>
              </button>
              <button type="button" class="tab-btn ${filterStatus === 'approved' ? 'active' : ''}" data-status="approved">
                已同意 <span class="req-tab-count" data-scount="approved">0</span>
              </button>
              <button type="button" class="tab-btn ${filterStatus === 'rejected' ? 'active' : ''}" data-status="rejected">
                已拒绝 <span class="req-tab-count" data-scount="rejected">0</span>
              </button>
              <button type="button" class="tab-btn ${filterStatus === 'failed' ? 'active' : ''}" data-status="failed">
                失败 <span class="req-tab-count" data-scount="failed">0</span>
              </button>
            </div>

            <div class="req-toolbar-actions" id="req-batch-actions">
              <button type="button" class="btn btn-primary btn-sm" id="req-batch-approve-btn" style="display:none;">
                ${reqIcon('check', 14)}一键全部同意
              </button>
              ${!isCompact ? `
                <button type="button" class="btn btn-ghost btn-sm" id="req-clear-btn">
                  ${reqIcon('trash', 14)}清理已处理
                </button>
              ` : ''}
            </div>
          </div>

          <div class="req-toolbar-row">
            <div class="req-toolbar-filters">
              <select class="select select-sm" id="req-group-select" style="width:${isCompact ? '160px' : '220px'};max-width:100%;" aria-label="按群聊筛选">
                <option value="">全部群聊</option>
              </select>
              <select class="select select-sm" id="req-subtype-select" style="width:${isCompact ? '110px' : '128px'};" aria-label="按申请类型筛选">
                <option value="all"${filterSubType === 'all' ? ' selected' : ''}>全部类型</option>
                <option value="add"${filterSubType === 'add' ? ' selected' : ''}>申请加群</option>
                <option value="invite"${filterSubType === 'invite' ? ' selected' : ''}>邀请入群</option>
              </select>
              <div class="search-box" style="flex:1;min-width:${isCompact ? '150px' : '220px'};max-width:${isCompact ? 'none' : '320px'};">
                <span class="search-icon">${reqIcon('search', 14)}</span>
                <input type="text" id="req-search-input" value="${escapeHtml(filterQuery)}" placeholder="搜索 QQ、昵称、群名或验证消息" autocomplete="off" />
                <button type="button" class="search-clear" id="req-search-clear" aria-label="清空搜索" style="display:${filterQuery ? 'inline-flex' : 'none'};">
                  ${reqIcon('x', 13)}
                </button>
              </div>
            </div>
          </div>
        </div>

        <div id="req-list-slot"></div>
      </div>
    `;

    body.querySelectorAll('#req-status-tabs [data-status]').forEach((btn) => {
      btn.addEventListener('click', () => {
        const next = btn.dataset.status;
        if (next === filterStatus) return;
        filterStatus = next;
        body.querySelectorAll('#req-status-tabs [data-status]').forEach((b) => {
          b.classList.toggle('active', b.dataset.status === filterStatus);
        });
        updateBatchButtons();
        fetchList({ silent: true });
      });
    });

    body.querySelector('#req-group-select')?.addEventListener('change', (e) => {
      filterGroupId = e.target.value;
      fetchList({ silent: true });
    });

    body.querySelector('#req-subtype-select')?.addEventListener('change', (e) => {
      filterSubType = e.target.value;
      fetchList({ silent: true });
    });

    const searchInput = body.querySelector('#req-search-input');
    const clearBtn = body.querySelector('#req-search-clear');
    const scheduleSearch = () => {
      if (searchDebounceTimer) window.clearTimeout(searchDebounceTimer);
      renderRequestList();
      searchDebounceTimer = window.setTimeout(() => {
        if (!destroyed) fetchList({ silent: true });
      }, 260);
    };

    searchInput?.addEventListener('compositionstart', () => {
      isComposingSearch = true;
    });
    searchInput?.addEventListener('compositionend', (e) => {
      isComposingSearch = false;
      filterQuery = e.target.value;
      if (clearBtn) clearBtn.style.display = filterQuery ? 'inline-flex' : 'none';
      scheduleSearch();
    });
    searchInput?.addEventListener('input', (e) => {
      filterQuery = e.target.value;
      if (clearBtn) clearBtn.style.display = filterQuery ? 'inline-flex' : 'none';
      if (!isComposingSearch) scheduleSearch();
    });
    clearBtn?.addEventListener('click', () => {
      filterQuery = '';
      if (searchInput) {
        searchInput.value = '';
        searchInput.focus();
      }
      clearBtn.style.display = 'none';
      fetchList({ silent: true });
    });

    body.querySelector('#req-batch-approve-btn')?.addEventListener('click', handleBatchApprove);
    body.querySelector('#req-clear-btn')?.addEventListener('click', handleClearProcessed);

    renderKpiStrip();
    updateGroupFilterOptions();
    updateStatusTabCounts();
    updateBatchButtons();
    renderRequestList();
  }

  function renderKpiStrip() {
    if (isCompact) {
      const compactSlot = container.querySelector('#req-compact-summary-slot');
      if (!compactSlot) return;
      const modeName = basePolicy.enabled
        ? (MODE_MAP[basePolicy.global?.mode]?.short || '手动审批')
        : '已停用';
      compactSlot.innerHTML = `
        <div class="req-compact-summary">
          <div class="row-sm wrap">
            <span>待处理 <strong class="tabular ${counts.pending > 0 ? 'text-amber' : ''}">${counts.pending}</strong></span>
            <span class="text-muted">·</span>
            <span>已同意 <strong class="tabular text-success">${counts.approved}</strong></span>
            <span class="text-muted">·</span>
            <span>已拒绝 <strong class="tabular">${counts.rejected}</strong></span>
            ${counts.failed > 0 ? `<span class="text-muted">·</span><span>失败 <strong class="tabular text-danger">${counts.failed}</strong></span>` : ''}
          </div>
          <span class="badge badge-sm ${basePolicy.enabled && basePolicy.global?.mode !== 'manual' ? 'badge-success' : 'badge-muted'}">
            ${escapeHtml(modeName)}
          </span>
        </div>
      `;
      return;
    }

    const slot = container.querySelector('#req-kpi-slot');
    if (!slot) return;

    const overrideCount = Object.keys(basePolicy.groups || {}).length;
    const modeInfo = MODE_MAP[basePolicy.global?.mode] || MODE_OPTIONS[0];
    const autoSummary = !basePolicy.enabled
      ? '已停用'
      : modeInfo.short;
    const autoSub = !basePolicy.enabled
      ? '全部申请将保留为待处理'
      : overrideCount > 0
        ? `全局 ${modeInfo.label} · ${overrideCount} 个群独立规则`
        : `全局生效 · ${groups.length} 个群聊`;

    slot.innerHTML = `
      <div class="kpi-strip">
        <div class="kpi clickable" data-kpi-status="pending" title="点击筛选待处理申请">
          <div class="kpi-label">
            <span>待处理</span>
            ${counts.pending > 0 ? '<span class="badge badge-sm badge-amber">需处理</span>' : '<span class="badge badge-sm badge-muted">清空</span>'}
          </div>
          <div class="kpi-value ${counts.pending > 0 ? 'text-amber' : ''}">${counts.pending}</div>
          <div class="kpi-sub">${counts.pending > 0 ? '点击快速查看队列' : '实时监听中，暂无积压'}</div>
        </div>
        <div class="kpi clickable" data-kpi-status="approved" title="点击筛选已同意记录">
          <div class="kpi-label"><span>已同意</span><span class="text-success">${reqIcon('userCheck', 14)}</span></div>
          <div class="kpi-value">${counts.approved}</div>
          <div class="kpi-sub">含人工与自动审批通过</div>
        </div>
        <div class="kpi clickable" data-kpi-status="rejected" title="点击筛选已拒绝记录">
          <div class="kpi-label">
            <span>已拒绝</span>
            ${counts.failed > 0 ? `<span class="badge badge-sm badge-danger">${counts.failed} 失败</span>` : `<span class="text-muted">${reqIcon('userX', 14)}</span>`}
          </div>
          <div class="kpi-value">${counts.rejected}</div>
          <div class="kpi-sub">${counts.failed > 0 ? `另有 ${counts.failed} 条处理失败待重试` : `累计记录 ${counts.total} 条`}</div>
        </div>
        <div class="kpi clickable" data-kpi-rules="1" title="点击配置自动审批规则">
          <div class="kpi-label">
            <span>自动审批状态</span>
            <span class="badge badge-sm ${basePolicy.enabled ? 'badge-success' : 'badge-muted'}">${basePolicy.enabled ? '运行中' : '已暂停'}</span>
          </div>
          <div class="kpi-value" style="font-size:22px;padding-top:4px;">${escapeHtml(autoSummary)}</div>
          <div class="kpi-sub">${escapeHtml(autoSub)}</div>
        </div>
      </div>
    `;

    slot.querySelectorAll('[data-kpi-status]').forEach((el) => {
      el.addEventListener('click', () => {
        const next = el.dataset.kpiStatus;
        if (!validStatuses.has(next)) return;
        filterStatus = next;
        container.querySelectorAll('#req-status-tabs [data-status]').forEach((b) => {
          b.classList.toggle('active', b.dataset.status === filterStatus);
        });
        updateBatchButtons();
        fetchList({ silent: true });
      });
    });

    slot.querySelector('[data-kpi-rules]')?.addEventListener('click', () => {
      activeTab = 'rules';
      renderTopTabs();
      renderActiveTab();
      renderStickyBar();
    });
  }

  function updateStatusTabCounts() {
    const map = {
      pending: counts.pending,
      all: counts.total,
      approved: counts.approved,
      rejected: counts.rejected,
      failed: counts.failed,
    };
    container.querySelectorAll('#req-status-tabs [data-scount]').forEach((el) => {
      const k = el.dataset.scount;
      const val = Number(map[k] || 0);
      el.textContent = String(val);
      el.classList.toggle('is-pending', k === 'pending' && val > 0);
      el.classList.toggle('is-danger', k === 'failed' && val > 0);
    });
  }

  function updateGroupFilterOptions() {
    const sel = container.querySelector('#req-group-select');
    if (!sel) return;

    const merged = new Map();
    for (const g of groups) {
      const gid = String(g.group_id || '');
      if (!gid) continue;
      merged.set(gid, {
        group_id: gid,
        group_name: g.group_name || `群 ${gid}`,
        bot_role: g.bot_role || 'unknown',
        pending_count: Number(g.pending_count || 0),
      });
    }
    for (const it of items) {
      const gid = String(it.group_id || '');
      if (!gid || merged.has(gid)) continue;
      merged.set(gid, {
        group_id: gid,
        group_name: it.group_name || `群 ${gid}`,
        bot_role: it.bot_role || 'unknown',
        pending_count: 0,
      });
    }

    const sorted = Array.from(merged.values()).sort((a, b) => {
      if (b.pending_count !== a.pending_count) return b.pending_count - a.pending_count;
      return a.group_name.localeCompare(b.group_name, 'zh-CN');
    });

    const opts = ['<option value="">全部群聊</option>'];
    for (const g of sorted) {
      const roleShort = (BOT_ROLE_META[g.bot_role] || BOT_ROLE_META.unknown).short;
      const pendingSuffix = g.pending_count > 0 ? ` (${g.pending_count})` : '';
      const label = `${g.group_name} [${roleShort}]${pendingSuffix}`;
      const selected = filterGroupId === g.group_id ? ' selected' : '';
      opts.push(`<option value="${escapeHtml(g.group_id)}"${selected}>${escapeHtml(label)}</option>`);
    }
    sel.innerHTML = opts.join('');
    sel.value = filterGroupId;
  }

  function updateBatchButtons() {
    const approveBtn = container.querySelector('#req-batch-approve-btn');
    const clearBtn = container.querySelector('#req-clear-btn');
    const visiblePending = getVisibleItems().filter((it) => it.status === 'pending').length;
    if (approveBtn) {
      const show = visiblePending > 0 && (filterStatus === 'pending' || filterStatus === 'all');
      approveBtn.style.display = show ? 'inline-flex' : 'none';
      approveBtn.disabled = batchBusy || !napcatOnline;
      approveBtn.innerHTML = `${reqIcon('check', 14)}${batchBusy ? '处理中…' : `一键全部同意 (${visiblePending})`}`;
    }
    if (clearBtn) {
      const processed = (counts.approved || 0) + (counts.rejected || 0) + (counts.failed || 0);
      clearBtn.disabled = batchBusy || processed <= 0;
    }
  }

  function renderRequestCard(item) {
    const idStr = String(item.id);
    const isBusy = busyIds.has(idStr);
    const isNew = newFlashIds.has(idStr);
    const roleMeta = BOT_ROLE_META[item.bot_role] || null;
    const isMemberOnly = item.bot_role === 'member';
    const isInvite = item.sub_type === 'invite';
    const userDisplay = item.user_name || `QQ ${item.user_id || '未知'}`;
    const groupDisplay = item.group_name || `群 ${item.group_id || '未知'}`;
    const commentText = String(item.comment || '').trim();
    const relTime = formatRelativeTime(item.created_at || item.created_iso);
    const exactTime = formatExactTime(item, 'created');
    const qqLevel = Number(item.qq_level);
    const hasLevel = item.qq_level !== null && item.qq_level !== undefined && Number.isFinite(qqLevel) && qqLevel > 0;

    let statusBadgeHtml = '';
    if (item.status === 'pending') {
      statusBadgeHtml = '<span class="badge badge-sm badge-amber">待处理</span>';
    } else if (item.status === 'approved') {
      statusBadgeHtml = '<span class="badge badge-sm badge-success">已同意</span>';
    } else if (item.status === 'rejected') {
      statusBadgeHtml = '<span class="badge badge-sm badge-danger">已拒绝</span>';
    } else if (item.status === 'failed') {
      statusBadgeHtml = '<span class="badge badge-sm badge-danger">处理失败</span>';
    }

    let footerHtml = '';
    if (item.status === 'pending' || item.status === 'failed') {
      const isFailed = item.status === 'failed';
      footerHtml = `
        <div class="req-card-foot">
          <div class="req-card-decision">
            ${item.source ? `<span class="text-xs text-muted">来源：${escapeHtml(item.source === 'ws' ? '实时推送' : item.source === 'sync' ? 'NapCat 同步' : item.source)}</span>` : ''}
            ${item.flag ? `<span class="text-xs text-muted mono" title="请求 Flag">flag:${escapeHtml(String(item.flag).slice(-8))}</span>` : ''}
          </div>
          <div class="req-card-btns">
            <button type="button" class="btn btn-secondary btn-sm" data-act="reject" data-id="${escapeHtml(idStr)}" ${isBusy || !napcatOnline ? 'disabled' : ''}>
              ${reqIcon('x', 13)}${isFailed ? '重试拒绝' : '拒绝'}
            </button>
            <button type="button" class="btn btn-primary btn-sm" data-act="approve" data-id="${escapeHtml(idStr)}" ${isBusy || !napcatOnline ? 'disabled' : ''}>
              ${isBusy ? `<span class="spin">${reqIcon('refresh', 13)}</span>处理中` : `${reqIcon('check', 13)}${isFailed ? '重试同意' : '同意'}`}
            </button>
          </div>
        </div>
      `;
    } else {
      const srcLabel = formatDecisionSource(item.decision_source);
      const handledStr = item.handled_at || item.handled_iso ? formatExactTime(item, 'handled') : '';
      footerHtml = `
        <div class="req-card-foot">
          <div class="req-card-decision">
            ${statusBadgeHtml}
            ${srcLabel ? `<span class="badge badge-sm badge-muted">${escapeHtml(srcLabel)}</span>` : ''}
            ${item.operator_id ? `<span class="text-xs text-muted mono">操作人 ${escapeHtml(item.operator_id)}</span>` : ''}
            ${item.status === 'rejected' && item.reject_reason ? `<span class="text-xs text-secondary">理由：${escapeHtml(item.reject_reason)}</span>` : ''}
          </div>
          ${handledStr ? `<span class="text-xs text-muted tabular" title="处理时间">${escapeHtml(handledStr)}</span>` : ''}
        </div>
      `;
    }

    return `
      <article class="req-card status-${escapeHtml(item.status || 'pending')}${isNew ? ' is-new' : ''}" data-req-card="${escapeHtml(idStr)}">
        <div class="req-card-head">
          <div class="req-card-identity">
            ${renderQqAvatar(item.user_id, userDisplay, isCompact ? '' : 'avatar-lg')}
            <div class="req-card-info">
              <div class="req-card-userline">
                <span class="req-card-name" title="${escapeHtml(userDisplay)}">${escapeHtml(userDisplay)}</span>
                ${item.user_id ? `<button type="button" class="req-copy-btn" data-copy="${escapeHtml(item.user_id)}" title="点击复制 QQ 号">${escapeHtml(item.user_id)}</button>` : ''}
                ${hasLevel ? `<span class="badge badge-sm badge-muted tabular">Lv.${qqLevel}</span>` : ''}
                ${item.is_ignored ? '<span class="badge badge-sm badge-danger" title="QQ 系统标记为过滤/风险申请">已过滤/风险</span>' : ''}
              </div>
              <div class="req-card-groupline">
                <span class="font-semibold">${escapeHtml(groupDisplay)}</span>
                ${item.group_id ? `<button type="button" class="req-copy-btn" data-copy="${escapeHtml(item.group_id)}" title="点击复制群号">#${escapeHtml(item.group_id)}</button>` : ''}
                ${roleMeta ? `<span class="badge badge-sm ${roleMeta.cls}"${isMemberOnly ? ' title="机器人非群管，无法代批"' : ''}>${escapeHtml(roleMeta.label)}</span>` : ''}
              </div>
              ${isInvite && item.invitor_id ? `
                <div class="text-xs text-muted">
                  邀请人：${escapeHtml(item.invitor_name || item.invitor_id)}
                  <button type="button" class="req-copy-btn" data-copy="${escapeHtml(item.invitor_id)}" title="点击复制邀请人 QQ">${escapeHtml(item.invitor_id)}</button>
                </div>
              ` : ''}
            </div>
          </div>

          <div class="req-card-meta-right">
            <div class="row-sm">
              <span class="badge badge-sm ${isInvite ? 'badge-accent' : 'badge-info'}">${isInvite ? '邀请机器人入群' : '申请加群'}</span>
              ${item.status === 'pending' || item.status === 'failed' ? statusBadgeHtml : ''}
            </div>
            <span class="req-card-time tabular" data-rel-ts="${escapeHtml(String(item.created_at || item.created_iso || ''))}" title="${escapeHtml(exactTime)}">${escapeHtml(relTime)}</span>
          </div>
        </div>

        <div class="req-comment-box${commentText ? '' : ' is-empty'}">
          ${commentText ? `<div class="req-comment-label">验证消息</div>${escapeHtml(commentText)}` : '无验证消息'}
        </div>

        ${isMemberOnly && item.status === 'pending' ? `
          <div class="req-warn-note">
            ${reqIcon('alert', 14)}
            <span>机器人在此群为普通成员，无权审批入群请求，请由群主或群管在 QQ 处理。</span>
          </div>
        ` : ''}

        ${item.status === 'failed' && item.error_msg ? `
          <div class="req-error-box">
            <span style="margin-top:1px;">${reqIcon('xCircle', 14)}</span>
            <span>处理失败：${escapeHtml(item.error_msg)}</span>
          </div>
        ` : ''}

        ${footerHtml}
      </article>
    `;
  }

  function renderRequestList() {
    const slot = container.querySelector('#req-list-slot');
    if (!slot) return;

    if (initialLoading) {
      slot.innerHTML = `
        <div class="stack-sm">
          <div class="loading-skeleton" style="height:126px;"></div>
          <div class="loading-skeleton" style="height:126px;"></div>
        </div>
      `;
      return;
    }

    if (loadError && items.length === 0) {
      slot.innerHTML = `
        <div class="card">
          <div class="empty-state">
            <div class="empty-state-icon">${reqIcon('alert', 20)}</div>
            <div class="empty-state-title">加载入群申请失败</div>
            <div class="text-sm text-muted">${escapeHtml(loadError)}</div>
            <button type="button" class="btn btn-primary btn-sm mt-8" id="req-retry-btn">${reqIcon('refresh', 14)}重试</button>
          </div>
        </div>
      `;
      slot.querySelector('#req-retry-btn')?.addEventListener('click', () => fetchList({ silent: false }));
      return;
    }

    const visible = getVisibleItems();
    updateBatchButtons();

    if (!visible.length) {
      const hasFilters = Boolean(filterGroupId || filterSubType !== 'all' || filterQuery.trim());
      const emptyTitle = filterStatus === 'pending' && !hasFilters
        ? '暂无待处理入群申请'
        : '没有符合条件的入群申请记录';
      const emptyDesc = filterStatus === 'pending' && !hasFilters
        ? '实时流正在监听中，有新申请时将第一时间自动推送。'
        : '可以调整筛选条件、清空搜索或从 NapCat 手动同步历史记录。';

      slot.innerHTML = `
        <div class="card">
          <div class="empty-state" style="padding:${isCompact ? '36px 18px' : '52px 24px'};">
            <div class="empty-state-icon">${reqIcon(filterStatus === 'pending' ? 'userCheck' : 'userPlus', 20)}</div>
            <div class="empty-state-title">${escapeHtml(emptyTitle)}</div>
            <div class="text-sm text-muted" style="max-width:380px;">${escapeHtml(emptyDesc)}</div>
            <div class="row-sm wrap mt-8" style="justify-content:center;">
              ${filterStatus === 'pending' && counts.total > 0 ? `
                <button type="button" class="btn btn-secondary btn-sm" id="req-empty-show-all">查看全部记录 (${counts.total})</button>
              ` : ''}
              ${hasFilters ? `
                <button type="button" class="btn btn-secondary btn-sm" id="req-empty-reset-filter">重置筛选</button>
              ` : ''}
              <button type="button" class="btn btn-ghost btn-sm" id="req-empty-sync">${reqIcon('refresh', 13)}从 NapCat 同步</button>
            </div>
          </div>
        </div>
      `;

      slot.querySelector('#req-empty-show-all')?.addEventListener('click', () => {
        filterStatus = 'all';
        container.querySelectorAll('#req-status-tabs [data-status]').forEach((b) => {
          b.classList.toggle('active', b.dataset.status === 'all');
        });
        fetchList({ silent: true });
      });
      slot.querySelector('#req-empty-reset-filter')?.addEventListener('click', () => {
        filterGroupId = '';
        filterSubType = 'all';
        filterQuery = '';
        const gSel = container.querySelector('#req-group-select');
        const stSel = container.querySelector('#req-subtype-select');
        const sIn = container.querySelector('#req-search-input');
        const sClr = container.querySelector('#req-search-clear');
        if (gSel) gSel.value = '';
        if (stSel) stSel.value = 'all';
        if (sIn) sIn.value = '';
        if (sClr) sClr.style.display = 'none';
        fetchList({ silent: true });
      });
      slot.querySelector('#req-empty-sync')?.addEventListener('click', () => syncNapcat());
      return;
    }

    slot.innerHTML = `<div class="req-list">${visible.map(renderRequestCard).join('')}</div>`;

    slot.querySelectorAll('[data-copy]').forEach((btn) => {
      btn.addEventListener('click', () => {
        copyText(btn.dataset.copy, `已复制 ${btn.dataset.copy}`);
      });
    });

    slot.querySelectorAll('[data-act]').forEach((btn) => {
      btn.addEventListener('click', () => {
        handleSingleDecision(btn.dataset.id, btn.dataset.act);
      });
    });
  }

  // ---------- Tab 2: Auto-Approval Rules ----------
  function renderChipEditorHtml(id, keywords, variantClass, placeholder) {
    const list = Array.isArray(keywords) ? keywords : [];
    return `
      <div class="chip-editor" data-chip-box="${escapeHtml(id)}">
        ${list.map((kw, idx) => `
          <span class="chip ${variantClass}">
            <span>${escapeHtml(kw)}</span>
            <button type="button" class="chip-remove" data-chip-del="${idx}" aria-label="移除 ${escapeHtml(kw)}">×</button>
          </span>
        `).join('')}
        <input type="text" data-chip-input="${escapeHtml(id)}" placeholder="${escapeHtml(placeholder)}" autocomplete="off" />
      </div>
    `;
  }

  function bindChipEditor(rootEl, id, getList, setList, variantClass, placeholder) {
    const renderChips = (refocus = false) => {
      const box = rootEl.querySelector(`[data-chip-box="${safeCssEscape(id)}"]`);
      if (!box) return;
      const list = getList();
      box.innerHTML = `
        ${list.map((kw, idx) => `
          <span class="chip ${variantClass}">
            <span>${escapeHtml(kw)}</span>
            <button type="button" class="chip-remove" data-chip-del="${idx}" aria-label="移除 ${escapeHtml(kw)}">×</button>
          </span>
        `).join('')}
        <input type="text" data-chip-input="${escapeHtml(id)}" placeholder="${escapeHtml(placeholder)}" autocomplete="off" />
      `;
      wireBox(box);
      if (refocus) {
        box.querySelector('input')?.focus();
      }
    };

    const addTokens = (raw) => {
      const parts = String(raw || '').split(/[,，\n]/).map((s) => s.trim()).filter(Boolean);
      if (!parts.length) return false;
      const cur = [...getList()];
      let changed = false;
      for (const p of parts) {
        if (!cur.includes(p)) {
          cur.push(p);
          changed = true;
        }
      }
      if (changed) {
        setList(cur);
        onDraftChanged();
      }
      return changed;
    };

    const wireBox = (box) => {
      const inp = box.querySelector('input');
      box.addEventListener('click', (e) => {
        if (e.target === box) inp?.focus();
      });
      box.querySelectorAll('[data-chip-del]').forEach((btn) => {
        btn.addEventListener('click', (e) => {
          e.stopPropagation();
          const idx = Number(btn.dataset.chipDel);
          const cur = [...getList()];
          if (idx >= 0 && idx < cur.length) {
            cur.splice(idx, 1);
            setList(cur);
            onDraftChanged();
            renderChips(true);
          }
        });
      });
      inp?.addEventListener('keydown', (e) => {
        if (e.isComposing) return;
        if (e.key === 'Enter' || e.key === ',' || e.key === '，') {
          e.preventDefault();
          const val = inp.value.trim();
          if (val) {
            addTokens(val);
            renderChips(true);
          }
        } else if (e.key === 'Backspace' && inp.value === '') {
          const cur = [...getList()];
          if (cur.length > 0) {
            cur.pop();
            setList(cur);
            onDraftChanged();
            renderChips(true);
          }
        }
      });
      inp?.addEventListener('blur', () => {
        const val = inp.value.trim();
        if (val) {
          addTokens(val);
          renderChips(false);
        }
      });
    };

    const initialBox = rootEl.querySelector(`[data-chip-box="${safeCssEscape(id)}"]`);
    if (initialBox) wireBox(initialBox);
  }

  function onDraftChanged() {
    renderTopTabs();
    renderStickyBar();
    updateTesterDirtyHint();
  }

  function renderRuleFormHtml(rule, prefix) {
    return `
      <div class="setting-list">
        <div class="setting-row setting-row-stack">
          <div class="setting-info">
            <div class="setting-label">审批模式</div>
            <div class="setting-desc">决定新加群申请的默认自动处理策略。</div>
          </div>
          <div class="req-mode-grid">
            ${MODE_OPTIONS.map((m) => `
              <button type="button" class="req-mode-card ${rule.mode === m.id ? 'is-active' : ''}" data-rule-mode="${escapeHtml(m.id)}" data-prefix="${escapeHtml(prefix)}">
                <div class="req-mode-title">
                  <span>${escapeHtml(m.label)}</span>
                  ${rule.mode === m.id ? `<span class="text-accent">${reqIcon('check', 14)}</span>` : ''}
                </div>
                <span class="req-mode-desc">${escapeHtml(m.desc)}</span>
              </button>
            `).join('')}
          </div>
        </div>

        <div class="setting-row setting-row-stack">
          <div class="setting-info">
            <div class="setting-title-line">
              <span class="setting-label">放行关键词</span>
              <span class="badge badge-sm badge-success">自动通过</span>
            </div>
            <div class="setting-desc">验证消息包含任意一个关键词即视为命中（不区分大小写），按回车或逗号添加。</div>
          </div>
          ${renderChipEditorHtml(`${prefix}-approve-kw`, rule.approve_keywords, 'req-chip-approve', '输入暗号/关键词后按回车添加…')}
        </div>

        <div class="setting-row setting-row-stack">
          <div class="setting-info">
            <div class="setting-title-line">
              <span class="setting-label">拒绝关键词</span>
              <span class="badge badge-sm badge-danger">更高优先级</span>
            </div>
            <div class="setting-desc">优先级高于放行关键词与「自动全部通过」；验证消息包含任意词将直接拒绝。</div>
          </div>
          ${renderChipEditorHtml(`${prefix}-reject-kw`, rule.reject_keywords, 'req-chip-reject', '输入广告词/违禁词后按回车添加…')}
        </div>

        <div class="setting-row">
          <div class="setting-info">
            <div class="setting-label">未命中关键词时自动拒绝</div>
            <div class="setting-desc">仅在「关键词自动通过」模式下生效；关闭时未命中申请会留在待处理队列供人工审核。</div>
          </div>
          <div class="setting-control">
            <label class="switch">
              <input type="checkbox" data-rule-field="reject_unmatched" ${rule.reject_unmatched ? 'checked' : ''} />
              <span class="switch-track"></span>
            </label>
          </div>
        </div>

        <div class="setting-row">
          <div class="setting-info">
            <div class="setting-label">默认拒绝理由</div>
            <div class="setting-desc">自动拒绝或手动快捷拒绝时默认附带的说明文案。</div>
          </div>
          <div class="setting-control" style="flex:1;max-width:340px;">
            <input type="text" class="input" data-rule-field="default_reject_reason" value="${escapeHtml(rule.default_reject_reason)}" placeholder="未通过入群验证" maxlength="120" />
          </div>
        </div>

        <div class="setting-row">
          <div class="setting-info">
            <div class="setting-label">最低 QQ 等级要求</div>
            <div class="setting-desc">设为 0 表示不限制；低于该等级的申请会被自动拒绝。</div>
          </div>
          <div class="setting-control">
            <div class="row-sm">
              <input type="number" class="input" data-rule-field="min_qq_level" value="${Number(rule.min_qq_level || 0)}" min="0" max="200" step="1" style="width:96px;text-align:right;" />
              <span class="unit-label">级</span>
            </div>
          </div>
        </div>

        <div class="setting-row">
          <div class="setting-info">
            <div class="setting-label">邀请机器人入群处理</div>
            <div class="setting-desc">收到拉机器人进群邀请时的处理策略。</div>
          </div>
          <div class="setting-control">
            <div class="segment-group">
              ${INVITE_OPTIONS.map((opt) => `
                <button type="button" class="segment-btn ${rule.handle_invite === opt.id ? 'active' : ''}" data-rule-invite="${escapeHtml(opt.id)}">${escapeHtml(opt.label)}</button>
              `).join('')}
            </div>
          </div>
        </div>

        <div class="setting-row">
          <div class="setting-info">
            <div class="setting-label">自动审批延迟</div>
            <div class="setting-desc">命中自动策略后等待若干秒再执行，模拟真人节奏（0 ~ 30 秒）。</div>
          </div>
          <div class="setting-control">
            <div class="range-control">
              <input type="range" data-rule-range="auto_delay_secs" min="0" max="30" step="0.5" value="${Number(rule.auto_delay_secs ?? 1)}" />
              <input type="number" class="input" data-rule-field="auto_delay_secs" min="0" max="30" step="0.5" value="${Number(rule.auto_delay_secs ?? 1)}" />
              <span class="unit-label">秒</span>
            </div>
          </div>
        </div>

        <div class="setting-row">
          <div class="setting-info">
            <div class="setting-label">新申请私聊通知管理员</div>
            <div class="setting-desc">收到新入群申请时，由机器人私聊提醒配置的管理员 QQ。</div>
          </div>
          <div class="setting-control">
            <label class="switch">
              <input type="checkbox" data-rule-field="notify_admin_dm" ${rule.notify_admin_dm ? 'checked' : ''} />
              <span class="switch-track"></span>
            </label>
          </div>
        </div>

        <div class="setting-row setting-row-stack">
          <div class="setting-info">
            <div class="setting-label">入群欢迎语（可选）</div>
            <div class="setting-desc">审批通过后在群内发送欢迎消息；留空则不发送。支持 <code class="mono">{user}</code>（昵称）和 <code class="mono">{qq}</code>（QQ 号）占位符。</div>
          </div>
          <textarea class="textarea" rows="2" data-rule-field="welcome_message" placeholder="例如：欢迎 {user} 加入本群！请先查看群公告～">${escapeHtml(rule.welcome_message)}</textarea>
        </div>
      </div>
    `;
  }

  function bindRuleForm(rootEl, getRule, prefix, onModeRerender) {
    rootEl.querySelectorAll(`[data-rule-mode][data-prefix="${safeCssEscape(prefix)}"]`).forEach((btn) => {
      btn.addEventListener('click', () => {
        getRule().mode = btn.dataset.ruleMode;
        onDraftChanged();
        onModeRerender();
      });
    });

    bindChipEditor(
      rootEl,
      `${prefix}-approve-kw`,
      () => getRule().approve_keywords,
      (next) => { getRule().approve_keywords = next; },
      'req-chip-approve',
      '输入暗号/关键词后按回车添加…',
    );

    bindChipEditor(
      rootEl,
      `${prefix}-reject-kw`,
      () => getRule().reject_keywords,
      (next) => { getRule().reject_keywords = next; },
      'req-chip-reject',
      '输入广告词/违禁词后按回车添加…',
    );

    rootEl.querySelectorAll('[data-rule-invite]').forEach((btn) => {
      btn.addEventListener('click', () => {
        getRule().handle_invite = btn.dataset.ruleInvite;
        rootEl.querySelectorAll('[data-rule-invite]').forEach((b) => {
          b.classList.toggle('active', b.dataset.ruleInvite === getRule().handle_invite);
        });
        onDraftChanged();
      });
    });

    const delayRange = rootEl.querySelector('[data-rule-range="auto_delay_secs"]');
    const delayInput = rootEl.querySelector('[data-rule-field="auto_delay_secs"]');
    delayRange?.addEventListener('input', () => {
      const val = clampNumber(delayRange.value, 0, 30, 1);
      getRule().auto_delay_secs = Number(val.toFixed(1));
      if (delayInput) delayInput.value = String(getRule().auto_delay_secs);
      onDraftChanged();
    });

    rootEl.querySelectorAll('[data-rule-field]').forEach((el) => {
      const field = el.dataset.ruleField;
      if (el.type === 'checkbox') {
        el.addEventListener('change', () => {
          getRule()[field] = Boolean(el.checked);
          onDraftChanged();
        });
      } else if (el.type === 'number') {
        el.addEventListener('input', () => {
          if (field === 'min_qq_level') {
            getRule().min_qq_level = Math.round(clampNumber(el.value, 0, 200, 0));
          } else if (field === 'auto_delay_secs') {
            const val = clampNumber(el.value, 0, 30, 1);
            getRule().auto_delay_secs = Number(val.toFixed(1));
            if (delayRange) delayRange.value = String(getRule().auto_delay_secs);
          }
          onDraftChanged();
        });
      } else {
        el.addEventListener('input', () => {
          getRule()[field] = el.value;
          onDraftChanged();
        });
      }
    });
  }

  function renderRulesTab(body) {
    if (!selectedRuleGid) {
      const firstGroup = groups[0]?.group_id || Object.keys(draftPolicy.groups || {})[0] || '';
      selectedRuleGid = String(firstGroup);
    }

    body.innerHTML = `
      <div class="stack-lg">
        <!-- Master Switch -->
        <div class="card">
          <div class="setting-row">
            <div class="setting-info">
              <div class="setting-title-line">
                <span class="font-semibold" style="font-size:15px;">启用自动审批引擎 · 热生效，无需重启</span>
                <span class="badge badge-sm ${draftPolicy.enabled ? 'badge-success' : 'badge-muted'}" id="req-master-badge">
                  ${draftPolicy.enabled ? '已启用' : '已停用'}
                </span>
              </div>
              <div class="setting-desc">
                关闭总开关后，所有群的自动规则都会暂时休眠，新的加群申请将全部保留在「待处理」队列中。
                ${basePolicy.updated_at ? `上次更新：${escapeHtml(formatTime(basePolicy.updated_at))}` : ''}
              </div>
            </div>
            <div class="setting-control" style="min-width:auto;">
              <label class="switch">
                <input type="checkbox" id="req-policy-master-switch" ${draftPolicy.enabled ? 'checked' : ''} />
                <span class="switch-track"></span>
              </label>
            </div>
          </div>
        </div>

        <!-- Global Default Rules -->
        <section class="section">
          <div class="section-head">
            <div>
              <h2 class="section-title">全局默认规则</h2>
              <p class="section-desc">所有未单独配置独立规则的群聊都会继承这套默认策略。</p>
            </div>
          </div>
          <div class="card" id="req-global-rule-card"></div>
        </section>

        <!-- Per-Group Overrides -->
        <section class="section">
          <div class="section-head">
            <div>
              <h2 class="section-title">按群独立规则覆写</h2>
              <p class="section-desc">为特定群聊单独设置审批模式、入群暗号关键词与欢迎语。</p>
            </div>
          </div>
          <div class="split-layout" style="min-height:460px;">
            <aside class="list-pane" style="max-height:680px;">
              <div class="pane-header">
                <div class="search-box">
                  <span class="search-icon">${reqIcon('search', 13)}</span>
                  <input type="text" id="req-rule-grp-search" value="${escapeHtml(ruleGroupQuery)}" placeholder="搜索群名或群号" autocomplete="off" />
                </div>
                <div class="row-sm">
                  <input type="text" class="input input-sm mono flex-1" id="req-rule-add-gid" placeholder="输入群号添加" />
                  <button type="button" class="btn btn-secondary btn-sm" id="req-rule-add-btn">${reqIcon('plus', 14)}添加</button>
                </div>
              </div>
              <div class="pane-scroll" id="req-rule-grp-list"></div>
            </aside>
            <section class="detail-pane" id="req-rule-grp-detail"></section>
          </div>
        </section>

        <!-- Live Rule Tester -->
        <section class="section">
          <div class="section-head">
            <div>
              <h2 class="section-title">规则模拟判定</h2>
              <p class="section-desc">输入模拟验证消息，立即验证当前生效规则会如何处理。</p>
            </div>
          </div>
          <div class="card">
            <div class="card-body stack">
              <div id="req-tester-dirty-hint"></div>
              <div class="req-test-grid">
                <div class="field">
                  <label class="field-label" for="req-test-group">目标群聊</label>
                  <select class="select" id="req-test-group"></select>
                </div>
                <div class="field">
                  <label class="field-label" for="req-test-subtype">申请类型</label>
                  <select class="select" id="req-test-subtype">
                    <option value="add"${testerSubType === 'add' ? ' selected' : ''}>申请加群 (add)</option>
                    <option value="invite"${testerSubType === 'invite' ? ' selected' : ''}>邀请机器人入群 (invite)</option>
                  </select>
                </div>
                <div class="field">
                  <label class="field-label" for="req-test-level">申请人 QQ 等级（可选）</label>
                  <input type="number" class="input" id="req-test-level" value="${escapeHtml(testerQqLevel)}" min="0" max="200" placeholder="留空表示不提供" />
                </div>
              </div>
              <div class="field">
                <label class="field-label" for="req-test-comment">验证消息内容</label>
                <textarea class="textarea" id="req-test-comment" rows="2" placeholder="例如：问题：入群暗号&#10;答案：哈基米">${escapeHtml(testerComment)}</textarea>
              </div>
              <div class="row-between wrap">
                <span class="text-xs text-muted">只做规则演练，不会产生任何实际审批记录或 QQ 请求。</span>
                <button type="button" class="btn btn-primary btn-sm" id="req-test-run-btn">
                  ${reqIcon('flask', 14)}模拟判定
                </button>
              </div>
              <div id="req-test-result-slot"></div>
            </div>
          </div>
        </section>
      </div>
    `;

    body.querySelector('#req-policy-master-switch')?.addEventListener('change', (e) => {
      draftPolicy.enabled = Boolean(e.target.checked);
      const badge = body.querySelector('#req-master-badge');
      if (badge) {
        badge.textContent = draftPolicy.enabled ? '已启用' : '已停用';
        badge.className = `badge badge-sm ${draftPolicy.enabled ? 'badge-success' : 'badge-muted'}`;
      }
      onDraftChanged();
    });

    body.querySelector('#req-rule-grp-search')?.addEventListener('input', (e) => {
      ruleGroupQuery = e.target.value;
      renderRulesGroupList();
    });

    body.querySelector('#req-rule-add-btn')?.addEventListener('click', () => {
      const inp = body.querySelector('#req-rule-add-gid');
      const gid = String(inp?.value || '').trim();
      if (!/^\d{5,15}$/.test(gid)) {
        showToast('请输入 5~15 位纯数字 QQ 群号', 'warning');
        return;
      }
      if (!getGroupInfo(gid)) {
        groups.unshift({
          group_id: gid,
          group_name: `QQ 群 ${gid}`,
          member_count: 0,
          bot_role: 'unknown',
          pending_count: 0,
        });
      }
      if (!draftPolicy.groups[gid]) {
        draftPolicy.groups[gid] = normalizeRule(draftPolicy.global, DEFAULT_RULE);
      }
      if (inp) inp.value = '';
      selectedRuleGid = gid;
      onDraftChanged();
      renderRulesGroupList();
      renderRulesGroupDetail();
      populateTesterGroups();
    });

    body.querySelector('#req-test-group')?.addEventListener('change', (e) => {
      testerGroupId = e.target.value;
    });
    body.querySelector('#req-test-subtype')?.addEventListener('change', (e) => {
      testerSubType = e.target.value;
    });
    body.querySelector('#req-test-level')?.addEventListener('input', (e) => {
      testerQqLevel = e.target.value;
    });
    body.querySelector('#req-test-comment')?.addEventListener('input', (e) => {
      testerComment = e.target.value;
    });
    body.querySelector('#req-test-run-btn')?.addEventListener('click', runRuleTest);

    renderGlobalRuleCard();
    renderRulesGroupList();
    renderRulesGroupDetail();
    populateTesterGroups();
    updateTesterDirtyHint();
    renderTesterResult();
  }

  function renderGlobalRuleCard() {
    const card = container.querySelector('#req-global-rule-card');
    if (!card) return;
    card.innerHTML = renderRuleFormHtml(draftPolicy.global, 'global');
    bindRuleForm(card, () => draftPolicy.global, 'global', () => {
      renderGlobalRuleCard();
      renderRulesGroupDetail();
    });
  }

  function getMergedRuleGroups() {
    const map = new Map();
    for (const g of groups) {
      const gid = String(g.group_id || '');
      if (!gid) continue;
      map.set(gid, {
        group_id: gid,
        group_name: g.group_name || `QQ 群 ${gid}`,
        member_count: Number(g.member_count || 0),
        bot_role: g.bot_role || 'unknown',
        pending_count: Number(g.pending_count || 0),
      });
    }
    for (const gid of Object.keys(draftPolicy.groups || {})) {
      if (!map.has(gid)) {
        map.set(gid, {
          group_id: gid,
          group_name: `QQ 群 ${gid}`,
          member_count: 0,
          bot_role: 'unknown',
          pending_count: 0,
        });
      }
    }
    return Array.from(map.values()).sort((a, b) => {
      const aOv = Object.prototype.hasOwnProperty.call(draftPolicy.groups || {}, a.group_id) ? 1 : 0;
      const bOv = Object.prototype.hasOwnProperty.call(draftPolicy.groups || {}, b.group_id) ? 1 : 0;
      if (bOv !== aOv) return bOv - aOv;
      return a.group_name.localeCompare(b.group_name, 'zh-CN');
    });
  }

  function renderRulesGroupList() {
    const listEl = container.querySelector('#req-rule-grp-list');
    if (!listEl) return;
    const q = ruleGroupQuery.trim().toLowerCase();
    const all = getMergedRuleGroups().filter((g) => {
      if (!q) return true;
      return `${g.group_name} ${g.group_id}`.toLowerCase().includes(q);
    });

    if (!all.length) {
      listEl.innerHTML = '<div class="empty-state"><div class="text-sm text-muted">暂无匹配的群聊</div></div>';
      return;
    }

    listEl.innerHTML = all.map((g) => {
      const isSelected = g.group_id === selectedRuleGid;
      const hasOverride = Object.prototype.hasOwnProperty.call(draftPolicy.groups || {}, g.group_id);
      const role = BOT_ROLE_META[g.bot_role] || BOT_ROLE_META.unknown;
      const modeShort = hasOverride ? (MODE_MAP[draftPolicy.groups[g.group_id]?.mode]?.short || '') : '';
      return `
        <button type="button" class="list-item${isSelected ? ' active' : ''}" data-rgrp="${escapeHtml(g.group_id)}">
          <div class="flex-1" style="min-width:0;">
            <div class="row-between" style="gap:6px;">
              <span class="font-semibold truncate">${escapeHtml(g.group_name)}</span>
              ${hasOverride
                ? `<span class="badge badge-sm badge-accent" style="flex-shrink:0;">独立 · ${escapeHtml(modeShort)}</span>`
                : '<span class="text-xs text-muted" style="flex-shrink:0;">继承全局</span>'}
            </div>
            <div class="row-between mt-4 text-xs text-muted">
              <span class="mono">${escapeHtml(g.group_id)}${g.member_count ? ` · ${g.member_count}人` : ''}</span>
              <span class="row-sm">
                ${g.pending_count > 0 ? `<span class="badge badge-sm badge-amber">${g.pending_count} 待批</span>` : ''}
                <span class="badge badge-sm ${role.cls}">${escapeHtml(role.label)}</span>
              </span>
            </div>
          </div>
        </button>
      `;
    }).join('');

    listEl.querySelectorAll('[data-rgrp]').forEach((btn) => {
      btn.addEventListener('click', () => {
        selectedRuleGid = btn.dataset.rgrp;
        renderRulesGroupList();
        renderRulesGroupDetail();
      });
    });
  }

  function renderRulesGroupDetail() {
    const pane = container.querySelector('#req-rule-grp-detail');
    if (!pane) return;

    const all = getMergedRuleGroups();
    const grp = all.find((g) => g.group_id === selectedRuleGid) || null;
    if (!grp) {
      pane.innerHTML = `
        <div class="empty-state" style="height:100%;">
          <div class="empty-state-icon">${reqIcon('users', 20)}</div>
          <div class="empty-state-title">选择左侧群聊配置独立规则</div>
          <div class="text-sm text-muted">也可以直接在左侧输入 QQ 群号提前预设规则。</div>
        </div>
      `;
      return;
    }

    const gid = grp.group_id;
    const hasOverride = Object.prototype.hasOwnProperty.call(draftPolicy.groups || {}, gid);
    const role = BOT_ROLE_META[grp.bot_role] || BOT_ROLE_META.unknown;

    pane.innerHTML = `
      <div class="pane-header">
        <div class="row-between wrap">
          <div class="row-sm wrap" style="min-width:0;">
            <span class="font-semibold" style="font-size:15px;">${escapeHtml(grp.group_name)}</span>
            <span class="mono text-xs text-muted">#${escapeHtml(gid)}</span>
            <span class="badge badge-sm ${role.cls}">${escapeHtml(role.label)}</span>
          </div>
          <label class="switch switch-sm">
            <input type="checkbox" id="req-grp-override-switch" ${hasOverride ? 'checked' : ''} />
            <span class="switch-track"></span>
            <span class="text-sm font-semibold">${hasOverride ? '独立规则' : '继承全局'}</span>
          </label>
        </div>
        ${grp.bot_role === 'member' ? `
          <div class="req-warn-note">
            ${reqIcon('alert', 14)}
            <span>机器人在此群当前为普通成员，无法代批入群申请（赋予群管权限后规则将自动生效）。</span>
          </div>
        ` : ''}
      </div>
      <div class="pane-scroll" id="req-grp-rule-body" style="padding:0;"></div>
    `;

    pane.querySelector('#req-grp-override-switch')?.addEventListener('change', (e) => {
      if (e.target.checked) {
        draftPolicy.groups[gid] = normalizeRule(draftPolicy.global, DEFAULT_RULE);
      } else {
        delete draftPolicy.groups[gid];
      }
      onDraftChanged();
      renderRulesGroupList();
      renderRulesGroupDetail();
    });

    const bodyEl = pane.querySelector('#req-grp-rule-body');
    if (!bodyEl) return;

    if (!hasOverride) {
      const g = draftPolicy.global;
      const modeName = MODE_MAP[g.mode]?.label || g.mode;
      bodyEl.innerHTML = `
        <div class="card-body stack">
          <div class="alert-banner">
            <div class="stack-xs">
              <span class="font-semibold text-sm">当前继承全局默认规则</span>
              <span class="text-xs text-muted">修改全局规则时本群会同步生效。如需为本群单独设定暗号或欢迎语，请开启右上角「独立规则」。</span>
            </div>
            <button type="button" class="btn btn-secondary btn-sm" id="req-enable-grp-ov-btn">切换为独立规则</button>
          </div>
          <div class="kv-list">
            <div class="kv"><span>当前生效模式</span><strong>${escapeHtml(modeName)}</strong></div>
            <div class="kv"><span>放行关键词</span><strong>${g.approve_keywords.length ? escapeHtml(g.approve_keywords.join(' / ')) : '未设置'}</strong></div>
            <div class="kv"><span>拒绝关键词</span><strong>${g.reject_keywords.length ? escapeHtml(g.reject_keywords.join(' / ')) : '未设置'}</strong></div>
            <div class="kv"><span>最低 QQ 等级</span><strong>${g.min_qq_level > 0 ? `Lv.${g.min_qq_level}` : '不限'}</strong></div>
            <div class="kv"><span>自动审批延迟</span><strong>${g.auto_delay_secs} 秒</strong></div>
            <div class="kv"><span>入群欢迎语</span><strong>${g.welcome_message ? escapeHtml(g.welcome_message) : '未开启'}</strong></div>
          </div>
        </div>
      `;
      bodyEl.querySelector('#req-enable-grp-ov-btn')?.addEventListener('click', () => {
        draftPolicy.groups[gid] = normalizeRule(draftPolicy.global, DEFAULT_RULE);
        onDraftChanged();
        renderRulesGroupList();
        renderRulesGroupDetail();
      });
      return;
    }

    bodyEl.innerHTML = renderRuleFormHtml(draftPolicy.groups[gid], `grp-${gid}`);
    bindRuleForm(bodyEl, () => draftPolicy.groups[gid], `grp-${gid}`, () => {
      renderRulesGroupList();
      renderRulesGroupDetail();
    });
  }

  function populateTesterGroups() {
    const sel = container.querySelector('#req-test-group');
    if (!sel) return;
    const opts = ['<option value="">全局默认（不指定群）</option>'];
    for (const g of getMergedRuleGroups()) {
      const hasOv = Object.prototype.hasOwnProperty.call(basePolicy.groups || {}, g.group_id);
      const label = `${g.group_name} (#${g.group_id})${hasOv ? ' · 独立规则' : ''}`;
      const selected = testerGroupId === g.group_id ? ' selected' : '';
      opts.push(`<option value="${escapeHtml(g.group_id)}"${selected}>${escapeHtml(label)}</option>`);
    }
    sel.innerHTML = opts.join('');
    sel.value = testerGroupId;
  }

  function updateTesterDirtyHint() {
    const slot = container.querySelector('#req-tester-dirty-hint');
    if (!slot) return;
    if (!isPolicyDirty()) {
      slot.innerHTML = '';
      return;
    }
    slot.innerHTML = `
      <div class="alert-banner alert-warning" style="padding:8px 12px;">
        <span class="text-xs">你有未保存的规则改动；模拟判定会基于服务端<strong>当前已保存</strong>的规则执行，请先保存后再测新规则。</span>
      </div>
    `;
  }

  function renderTesterResult() {
    const slot = container.querySelector('#req-test-result-slot');
    const btn = container.querySelector('#req-test-run-btn');
    if (btn) {
      btn.disabled = testingRule;
      btn.innerHTML = testingRule
        ? `<span class="spin">${reqIcon('refresh', 14)}</span>判定中…`
        : `${reqIcon('flask', 14)}模拟判定`;
    }
    if (!slot) return;
    if (!testerResult) {
      slot.innerHTML = '';
      return;
    }
    if (testerResult.error) {
      slot.innerHTML = `<div class="req-error-box">${reqIcon('alert', 14)}<span>${escapeHtml(testerResult.error)}</span></div>`;
      return;
    }

    const act = String(testerResult.action || 'manual');
    const actMeta = act === 'approve'
      ? { label: '自动同意', cls: 'act-approve', badge: 'badge-success' }
      : act === 'reject'
        ? { label: '自动拒绝', cls: 'act-reject', badge: 'badge-danger' }
        : { label: '转人工审批', cls: 'act-manual', badge: 'badge-amber' };

    slot.innerHTML = `
      <div class="req-test-result ${actMeta.cls}">
        <div class="row-between wrap">
          <div class="row-sm wrap">
            <span class="badge ${actMeta.badge}">${actMeta.label}</span>
            ${testerResult.source ? `<span class="text-sm font-semibold">${escapeHtml(formatDecisionSource(testerResult.source))}</span>` : ''}
            ${testerResult.matched_keyword ? `<span class="badge badge-sm badge-outline mono">命中词：${escapeHtml(testerResult.matched_keyword)}</span>` : ''}
          </div>
          ${Number(testerResult.delay_secs) > 0 && act !== 'manual' ? `<span class="text-xs text-muted tabular">延迟 ${Number(testerResult.delay_secs)}s 后执行</span>` : ''}
        </div>
        ${testerResult.reason ? `<div class="text-sm text-secondary">说明：${escapeHtml(testerResult.reason)}</div>` : ''}
      </div>
    `;
  }

  function renderRulesBody() {
    const body = container.querySelector('#req-tab-body');
    if (!body || activeTab !== 'rules') return;
    renderRulesTab(body);
  }

  function renderStickyBar() {
    const root = container.querySelector('#req-sticky-root');
    if (!root) return;
    if (activeTab !== 'rules' || !isPolicyDirty()) {
      root.innerHTML = '';
      return;
    }

    root.innerHTML = `
      <div class="sticky-action-bar">
        <div class="row-between wrap">
          <div class="row-sm wrap">
            <span class="dirty-dot"></span>
            <span class="font-semibold text-sm">自动审批规则有未保存的改动</span>
            <span class="text-xs text-muted">保存后立即热生效，无需重启服务</span>
          </div>
          <div class="row-sm">
            <button type="button" class="btn btn-ghost btn-sm" id="req-policy-revert-btn" ${savingPolicy ? 'disabled' : ''}>放弃修改</button>
            <button type="button" class="btn btn-primary btn-sm" id="req-policy-save-btn" ${savingPolicy ? 'disabled' : ''}>
              ${savingPolicy ? '保存中…' : `保存并生效 <span style="opacity:0.65;">${saveShortcutLabel()}</span>`}
            </button>
          </div>
        </div>
      </div>
    `;

    root.querySelector('#req-policy-revert-btn')?.addEventListener('click', () => {
      draftPolicy = deepClone(basePolicy);
      renderTopTabs();
      renderRulesBody();
      renderStickyBar();
    });
    root.querySelector('#req-policy-save-btn')?.addEventListener('click', () => savePolicy());
  }

  function renderActiveTab() {
    const body = container.querySelector('#req-tab-body');
    if (!body) return;
    if (isCompact || activeTab === 'monitor') {
      renderMonitorTab(body);
    } else {
      renderRulesTab(body);
    }
  }

  // ---------- Global Keyboard & Storage Sync Listeners ----------
  const onKeyDown = (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 's') {
      if (!isCompact && activeTab === 'rules' && isPolicyDirty()) {
        e.preventDefault();
        savePolicy();
      }
    }
  };
  document.addEventListener('keydown', onKeyDown);

  const onStorage = (e) => {
    if (e.key === STORAGE_SOUND_KEY) {
      soundEnabled = readBoolStorage(STORAGE_SOUND_KEY, true);
      renderLiveStrip();
      notifyHostStatus();
    } else if (e.key === STORAGE_NOTIFY_KEY) {
      notifyEnabled = readBoolStorage(STORAGE_NOTIFY_KEY, false) &&
        (typeof window !== 'undefined' && 'Notification' in window && Notification.permission === 'granted');
      renderLiveStrip();
      notifyHostStatus();
    }
  };
  window.addEventListener('storage', onStorage);

  // Refresh relative timestamps ("刚刚" -> "1 分钟前") cleanly every 30s without touching DOM inputs
  relTimeTimer = window.setInterval(() => {
    if (destroyed || document.hidden) return;
    container.querySelectorAll('[data-rel-ts]').forEach((el) => {
      const raw = el.getAttribute('data-rel-ts');
      if (raw) el.textContent = formatRelativeTime(raw);
    });
  }, 30000);

  // ---------- Initial Boot ----------
  renderHeaderActions();
  renderLiveStrip();
  renderTopTabs();
  renderActiveTab();
  renderStickyBar();
  notifyHostStatus();

  await fetchList({ sync: false, silent: true });
  if (!destroyed) {
    connectStream();
  }

  return {
    cleanup() {
      destroyed = true;
      closeStream();
      if (searchDebounceTimer) window.clearTimeout(searchDebounceTimer);
      if (relTimeTimer) window.clearInterval(relTimeTimer);
      document.removeEventListener('keydown', onKeyDown);
      window.removeEventListener('storage', onStorage);
      container.removeEventListener('error', onAvatarError, true);
      for (const closeFn of Array.from(activeModalClosers)) {
        try { closeFn(); } catch (_) {}
      }
      activeModalClosers.clear();
    },
    async refresh() {
      await fetchList({ sync: false, silent: false });
    },
    syncNapcat,
    toggleSound,
    toggleNotify,
    openPopout: openMonitorPopup,
  };
}
