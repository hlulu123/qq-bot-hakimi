// ==========================================================================
// 哈基米 Console — Shared API Client, Formatting, Icons, Markdown & UI Helpers
// ==========================================================================

async function requestJson(method, path, body, options = {}) {
  const init = {
    method,
    headers: {
      Accept: 'application/json',
      ...(options.headers || {}),
    },
    signal: options.signal,
  };

  if (body !== undefined && method !== 'GET' && method !== 'HEAD') {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(body);
  }

  let res;
  try {
    res = await fetch(path, init);
  } catch (err) {
    throw new Error(`网络请求失败 (${path}): ${err && err.message ? err.message : String(err)}`);
  }

  const text = await res.text();
  let json = null;
  if (text) {
    try {
      json = JSON.parse(text);
    } catch (_) {
      if (!res.ok) {
        throw new Error(`HTTP ${res.status}: ${text.slice(0, 180)}`);
      }
      throw new Error(`服务端返回了非 JSON 响应 (${path})`);
    }
  }

  if (!json) {
    if (!res.ok) {
      throw new Error(`HTTP ${res.status} (${path})`);
    }
    return null;
  }

  if (Object.prototype.hasOwnProperty.call(json, 'ok')) {
    if (!json.ok) {
      throw new Error(json.error || `请求失败 (HTTP ${res.status})`);
    }
    return json.data;
  }

  if (!res.ok) {
    throw new Error(json.error || json.message || `HTTP ${res.status}`);
  }

  return json;
}

export function apiGet(path, options) {
  return requestJson('GET', path, undefined, options);
}

export function apiPost(path, body = {}, options) {
  return requestJson('POST', path, body, options);
}

export function apiPut(path, body = {}, options) {
  return requestJson('PUT', path, body, options);
}

export function apiDelete(path, body, options) {
  return requestJson('DELETE', path, body, options);
}

export function escapeHtml(str) {
  if (str === null || str === undefined) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

export async function copyText(text, successMsg = '已复制到剪贴板') {
  const val = String(text ?? '');
  if (!val) {
    showToast('没有可复制的内容', 'warning');
    return false;
  }
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(val);
    } else {
      const ta = document.createElement('textarea');
      ta.value = val;
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      document.execCommand('copy');
      ta.remove();
    }
    if (successMsg) showToast(successMsg, 'success');
    return true;
  } catch (err) {
    showToast(`复制失败: ${err && err.message ? err.message : String(err)}`, 'error');
    return false;
  }
}

function parseTimestamp(ts) {
  if (ts === null || ts === undefined || ts === '') return null;
  if (ts instanceof Date) return Number.isNaN(ts.getTime()) ? null : ts;

  if (typeof ts === 'number') {
    if (!Number.isFinite(ts) || ts <= 0) return null;
    const ms = ts < 1e11 ? ts * 1000 : ts;
    const d = new Date(ms);
    return Number.isNaN(d.getTime()) ? null : d;
  }

  const raw = String(ts).trim();
  if (!raw) return null;

  if (/^\d+(\.\d+)?$/.test(raw)) {
    const num = Number(raw);
    if (!Number.isFinite(num) || num <= 0) return null;
    const ms = num < 1e11 ? num * 1000 : num;
    const d = new Date(ms);
    return Number.isNaN(d.getTime()) ? null : d;
  }

  let normalized = raw;
  if (/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}/.test(normalized)) {
    normalized = normalized.replace(' ', 'T');
  }
  const d = new Date(normalized);
  return Number.isNaN(d.getTime()) ? null : d;
}

function pad2(n) {
  return String(n).padStart(2, '0');
}

export function formatTime(ts) {
  if (ts === null || ts === undefined || ts === '') return '--';
  if (typeof ts === 'string' && /^\d{2}:\d{2}(:\d{2})?$/.test(ts.trim())) {
    return ts.trim();
  }
  const d = parseTimestamp(ts);
  if (!d) return String(ts);

  const now = new Date();
  const hh = pad2(d.getHours());
  const mm = pad2(d.getMinutes());
  const ss = pad2(d.getSeconds());

  const sameDay =
    d.getFullYear() === now.getFullYear() &&
    d.getMonth() === now.getMonth() &&
    d.getDate() === now.getDate();

  if (sameDay) {
    return `${hh}:${mm}:${ss}`;
  }
  const mo = pad2(d.getMonth() + 1);
  const da = pad2(d.getDate());
  if (d.getFullYear() === now.getFullYear()) {
    return `${mo}-${da} ${hh}:${mm}:${ss}`;
  }
  return `${d.getFullYear()}-${mo}-${da} ${hh}:${mm}`;
}

export function formatRelativeTime(ts) {
  if (ts === null || ts === undefined || ts === '') return '--';
  const d = parseTimestamp(ts);
  if (!d) return String(ts);

  const diffSec = Math.floor((Date.now() - d.getTime()) / 1000);
  if (diffSec < 0) return formatTime(d);
  if (diffSec < 45) return '刚刚';
  if (diffSec < 3600) return `${Math.floor(diffSec / 60)} 分钟前`;
  if (diffSec < 86400) return `${Math.floor(diffSec / 3600)} 小时前`;

  const now = new Date();
  const yesterday = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1);
  if (
    d.getFullYear() === yesterday.getFullYear() &&
    d.getMonth() === yesterday.getMonth() &&
    d.getDate() === yesterday.getDate()
  ) {
    return `昨天 ${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
  }

  const days = Math.floor(diffSec / 86400);
  if (days < 30) return `${days} 天前`;
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
}

export function formatDuration(sec) {
  if (sec === null || sec === undefined || sec === '') return '--';
  const n = Number(sec);
  if (!Number.isFinite(n) || n < 0) return '--';

  if (n < 60) {
    if (n === 0) return '0s';
    if (n < 10) return `${Number(n.toFixed(1))}s`;
    if (Math.abs(n - Math.round(n)) > 0.05) return `${n.toFixed(1)}s`;
    return `${Math.round(n)}s`;
  }

  const total = Math.floor(n);
  const days = Math.floor(total / 86400);
  const hours = Math.floor((total % 86400) / 3600);
  const mins = Math.floor((total % 3600) / 60);
  const secs = total % 60;

  if (days > 0) {
    return hours > 0 ? `${days}d ${hours}h` : `${days}d`;
  }
  if (hours > 0) {
    return mins > 0 ? `${hours}h ${mins}m` : `${hours}h`;
  }
  return secs > 0 ? `${mins}m ${secs}s` : `${mins}m`;
}

export function formatBytes(bytes) {
  if (bytes === null || bytes === undefined || bytes === '') return '--';
  const n = Number(bytes);
  if (!Number.isFinite(n) || n < 0) return '--';
  if (n < 1024) return `${Math.round(n)} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let val = n / 1024;
  let idx = 0;
  while (val >= 1024 && idx < units.length - 1) {
    val /= 1024;
    idx += 1;
  }
  const fixed = val >= 100 ? val.toFixed(0) : val.toFixed(1);
  return `${fixed} ${units[idx]}`;
}

export function formatTokens(tokens) {
  if (tokens === null || tokens === undefined || tokens === '') return '0';
  const n = Number(tokens);
  if (!Number.isFinite(n) || n < 0) return '0';
  if (n < 1000) return String(Math.round(n));
  if (n < 1e6) {
    const k = n / 1e3;
    return `${k >= 100 ? k.toFixed(0) : k.toFixed(1)}K`;
  }
  if (n < 1e9) {
    const m = n / 1e6;
    if (m >= 100) return `${m.toFixed(1)}M`;
    if (m >= 10) return `${m.toFixed(1)}M`;
    return `${m.toFixed(2)}M`;
  }
  const b = n / 1e9;
  return `${b >= 100 ? b.toFixed(1) : b.toFixed(2)}B`;
}

export function formatTokensExact(tokens) {
  const n = Number(tokens || 0);
  if (!Number.isFinite(n) || n < 0) return '0';
  return Math.round(n).toLocaleString();
}

export function isMacPlatform() {
  try {
    const p =
      (navigator.userAgentData && navigator.userAgentData.platform) ||
      navigator.platform ||
      navigator.userAgent ||
      '';
    return /mac|iphone|ipad|ipod/i.test(String(p));
  } catch (_) {
    return false;
  }
}

export function saveShortcutLabel() {
  return isMacPlatform() ? '⌘S' : 'Ctrl+S';
}

export function extractUrlPort(url, fallback) {
  try {
    const u = new URL(String(url || ''));
    if (u.port) {
      const p = Number(u.port);
      if (Number.isFinite(p) && p > 0) return p;
    }
  } catch (_) {}
  return fallback;
}

export function safeLocalHttpUrl(rawUrl, fallbackPort = 6099, fallbackPath = '/webui/') {
  const port = Number(fallbackPort) > 0 ? Number(fallbackPort) : 6099;
  const fallback = `http://127.0.0.1:${port}${fallbackPath}`;
  const trimmed = String(rawUrl || '').trim();
  if (!trimmed) return fallback;
  if (/^https?:\/\/(127\.0\.0\.1|localhost|\[::1\])(:\d+)?(\/.*)?$/i.test(trimmed)) {
    return trimmed;
  }
  return fallback;
}

// ---------- Inline Crisp SVG Icon Library ----------
const ICONS = {
  dashboard: '<rect x="3" y="3" width="7" height="9" rx="1.5"/><rect x="14" y="3" width="7" height="5" rx="1.5"/><rect x="14" y="12" width="7" height="9" rx="1.5"/><rect x="3" y="16" width="7" height="5" rx="1.5"/>',
  chat: '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>',
  sparkles: '<path d="M12 3l1.9 4.9L19 9.8l-4.9 1.9L12 17l-2.1-5.3L5 9.8l5.1-1.9L12 3z"/><path d="M5 3v4M3 5h4M19 17v4M17 19h4"/>',
  brain: '<path d="M9.5 2A2.5 2.5 0 0 1 12 4.5v15a2.5 2.5 0 0 1-4.96.44 2.5 2.5 0 0 1-2.96-3.08 3 3 0 0 1-.34-5.58 2.5 2.5 0 0 1 1.32-4.24 2.5 2.5 0 0 1 4.44-2.54z"/><path d="M14.5 2A2.5 2.5 0 0 0 12 4.5v15a2.5 2.5 0 0 0 4.96.44 2.5 2.5 0 0 0 2.96-3.08 3 3 0 0 0 .34-5.58 2.5 2.5 0 0 0-1.32-4.24 2.5 2.5 0 0 0-4.44-2.54z"/>',
  sliders: '<line x1="4" y1="21" x2="4" y2="14"/><line x1="4" y1="10" x2="4" y2="3"/><line x1="12" y1="21" x2="12" y2="12"/><line x1="12" y1="8" x2="12" y2="3"/><line x1="20" y1="21" x2="20" y2="16"/><line x1="20" y1="12" x2="20" y2="3"/><line x1="1" y1="14" x2="7" y2="14"/><line x1="9" y1="8" x2="15" y2="8"/><line x1="17" y1="16" x2="23" y2="16"/>',
  terminal: '<polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/>',
  folder: '<path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>',
  shield: '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>',
  refresh: '<polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/>',
  play: '<polygon points="6 3 20 12 6 21 6 3"/>',
  pause: '<rect x="6" y="4" width="4" height="16" rx="1"/><rect x="14" y="4" width="4" height="16" rx="1"/>',
  stop: '<rect x="5" y="5" width="14" height="14" rx="2"/>',
  send: '<line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/>',
  search: '<circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>',
  check: '<polyline points="20 6 9 17 4 12"/>',
  checkCircle: '<path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/>',
  alert: '<path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>',
  copy: '<rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>',
  trash: '<polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6M14 11v6"/><path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/>',
  plus: '<line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/>',
  edit: '<path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/>',
  lock: '<rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>',
  eye: '<path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/>',
  eyeOff: '<path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/>',
  sun: '<circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/><line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/>',
  moon: '<path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/>',
  external: '<path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/>',
  diff: '<path d="M12 3v14M5 10h14M5 21h14"/>',
  repeat: '<polyline points="17 1 21 5 17 9"/><path d="M3 11V9a4 4 0 0 1 4-4h14"/><polyline points="7 23 3 19 7 15"/><path d="M21 13v2a4 4 0 0 1-4 4H3"/>',
  zap: '<polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/>',
  cat: '<path d="M4 10L6 4l4 3h4l4-3 2 6c1 2.4.8 5.2-.6 7.4C17.8 19.7 15.1 21 12 21s-5.8-1.3-7.4-3.6C3.2 15.2 3 12.4 4 10z"/><circle cx="9.5" cy="13.5" r="1"/><circle cx="14.5" cy="13.5" r="1"/><path d="M11 16.5l1 1 1-1"/>',
  users: '<path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>',
  user: '<path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/>',
  clock: '<circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>',
  activity: '<polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/>',
  server: '<rect x="2" y="2" width="20" height="8" rx="2" ry="2"/><rect x="2" y="14" width="20" height="8" rx="2" ry="2"/><line x1="6" y1="6" x2="6.01" y2="6"/><line x1="6" y1="18" x2="6.01" y2="18"/>',
  x: '<line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>',
  chevronRight: '<polyline points="9 18 15 12 9 6"/>',
  chevronDown: '<polyline points="6 9 12 15 18 9"/>',
  chevronUp: '<polyline points="18 15 12 9 6 15"/>',
  download: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/>',
  filter: '<polygon points="22 3 2 3 10 12.46 10 19 14 21 14 12.46 22 3"/>',
  rotate: '<path d="M21 12a9 9 0 1 1-2.64-6.36"/><polyline points="21 3 21 9 15 9"/>',
  rotateCcw: '<polyline points="1 4 1 10 7 10"/><path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10"/>',
  info: '<circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/>',
  save: '<path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><polyline points="17 21 17 13 7 13 7 21"/><polyline points="7 3 7 8 15 8"/>',
  file: '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/>',
  code: '<polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/>',
  maximize: '<polyline points="15 3 21 3 21 9"/><polyline points="9 21 3 21 3 15"/><line x1="21" y1="3" x2="14" y2="10"/><line x1="3" y1="21" x2="10" y2="14"/>',
  minimize: '<polyline points="4 14 10 14 10 20"/><polyline points="20 10 14 10 14 4"/><line x1="14" y1="10" x2="21" y2="3"/><line x1="3" y1="21" x2="10" y2="14"/>',
  power: '<path d="M18.36 6.64a9 9 0 1 1-12.73 0"/><line x1="12" y1="2" x2="12" y2="12"/>',
  history: '<path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><polyline points="3 3 3 8 8 8"/><polyline points="12 7 12 12 15 14"/>',
  layers: '<polygon points="12 2 2 7 12 12 22 7 12 2"/><polyline points="2 17 12 22 22 17"/><polyline points="2 12 12 17 22 12"/>',
  arrowDown: '<line x1="12" y1="5" x2="12" y2="19"/><polyline points="19 12 12 19 5 12"/>',
  puzzle: '<path d="M4 7h3a1 1 0 0 0 1-1V5a2 2 0 0 1 4 0v1a1 1 0 0 0 1 1h3a1 1 0 0 1 1 1v3a1 1 0 0 0 1 1h1a2 2 0 0 1 0 4h-1a1 1 0 0 0-1 1v3a1 1 0 0 1-1 1h-3a1 1 0 0 1-1-1v-1a2 2 0 0 0-4 0v1a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1v-3a1 1 0 0 1 1-1h1a2 2 0 0 0 0-4H4a1 1 0 0 1-1-1V8a1 1 0 0 1 1-1z"/>',
  wrench: '<path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/>',
  image: '<rect x="3" y="3" width="18" height="18" rx="2" ry="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21 15 16 10 5 21"/>',
  cpu: '<rect x="4" y="4" width="16" height="16" rx="2" ry="2"/><rect x="9" y="9" width="6" height="6"/><line x1="9" y1="1" x2="9" y2="4"/><line x1="15" y1="1" x2="15" y2="4"/><line x1="9" y1="20" x2="9" y2="23"/><line x1="15" y1="20" x2="15" y2="23"/><line x1="20" y1="9" x2="23" y2="9"/><line x1="20" y1="14" x2="23" y2="14"/><line x1="1" y1="9" x2="4" y2="9"/><line x1="1" y1="14" x2="4" y2="14"/>',
  barChart: '<line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/><line x1="6" y1="20" x2="6" y2="14"/><line x1="2" y1="20" x2="22" y2="20"/>',
  arrowLeft: '<line x1="19" y1="12" x2="5" y2="12"/><polyline points="12 19 5 12 12 5"/>',
  arrowRight: '<line x1="5" y1="12" x2="19" y2="12"/><polyline points="12 5 19 12 12 19"/>',
  arrowUpRight: '<line x1="7" y1="17" x2="17" y2="7"/><polyline points="7 7 17 7 17 17"/>',
  menu: '<line x1="4" y1="7" x2="20" y2="7"/><line x1="4" y1="12" x2="20" y2="12"/><line x1="4" y1="17" x2="20" y2="17"/>',
  more: '<circle cx="5" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/>',
  userPlus: '<path d="M16 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="8.5" cy="7" r="4"/><line x1="20" y1="8" x2="20" y2="14"/><line x1="23" y1="11" x2="17" y2="11"/>',
  bell: '<path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/>',
  bellOff: '<path d="M13.73 21a2 2 0 0 1-3.46 0"/><path d="M18.63 13A17.89 17.89 0 0 1 18 8"/><path d="M6.26 6.26A5.86 5.86 0 0 0 6 8c0 7-3 9-3 9h14"/><path d="M18 8a6 6 0 0 0-9.33-5"/><line x1="1" y1="1" x2="23" y2="23"/>',
  volume2: '<polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><path d="M19.07 4.93a10 10 0 0 1 0 14.14"/><path d="M15.54 8.46a5 5 0 0 1 0 7.07"/>',
  volumeX: '<polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><line x1="23" y1="9" x2="17" y2="15"/><line x1="17" y1="9" x2="23" y2="15"/>',
  popout: '<path d="M14 3h7v7"/><line x1="11" y1="13" x2="21" y2="3"/><path d="M21 14v5a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5"/><line x1="3" y1="9" x2="11" y2="9"/>',
};

ICONS['bar-chart'] = ICONS.barChart;
ICONS['arrow-left'] = ICONS.arrowLeft;
ICONS['arrow-right'] = ICONS.arrowRight;

ICONS['eye-off'] = ICONS.eyeOff;
ICONS['chevron-right'] = ICONS.chevronRight;
ICONS['chevron-down'] = ICONS.chevronDown;
ICONS['chevron-up'] = ICONS.chevronUp;
ICONS['rotate-ccw'] = ICONS.rotateCcw;
ICONS['check-circle'] = ICONS.checkCircle;
ICONS['arrow-down'] = ICONS.arrowDown;
ICONS['user-plus'] = ICONS.userPlus;
ICONS['bell-off'] = ICONS.bellOff;
ICONS['volume-2'] = ICONS.volume2;
ICONS['volume-x'] = ICONS.volumeX;

export function createIcon(name, size = 16) {
  const inner = ICONS[name] || ICONS.info;
  return `<svg viewBox="0 0 24 24" width="${size}" height="${size}" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${inner}</svg>`;
}

// ---------- Toast Notification Stack ----------
export function showToast(message, type = 'info') {
  let container = document.getElementById('toast-container');
  if (!container) {
    container = document.createElement('div');
    container.id = 'toast-container';
    container.className = 'toast-container';
    document.body.appendChild(container);
  }

  const safeType = ['info', 'success', 'warning', 'error'].includes(type) ? type : 'info';
  const iconMap = {
    info: createIcon('info', 16),
    success: createIcon('check', 16),
    warning: createIcon('alert', 16),
    error: createIcon('alert', 16),
  };
  const colorMap = {
    info: 'var(--accent)',
    success: 'var(--success)',
    warning: 'var(--amber)',
    error: 'var(--danger)',
  };

  const el = document.createElement('div');
  el.className = `toast toast-${safeType}`;
  el.setAttribute('role', 'status');
  el.innerHTML = `
    <span style="color:${colorMap[safeType]};margin-top:1px;display:inline-flex;">${iconMap[safeType]}</span>
    <span style="flex:1;word-break:break-word;">${escapeHtml(message)}</span>
    <button type="button" class="btn btn-ghost btn-icon btn-sm" aria-label="关闭通知" style="width:22px;height:22px;margin:-2px -4px 0 0;">
      ${createIcon('x', 13)}
    </button>
  `;

  let removed = false;
  const dismiss = () => {
    if (removed) return;
    removed = true;
    el.classList.add('toast-out');
    window.setTimeout(() => el.remove(), 200);
  };

  el.querySelector('button')?.addEventListener('click', dismiss);
  container.appendChild(el);

  const timeout = safeType === 'error' ? 5200 : safeType === 'warning' ? 4200 : 3200;
  window.setTimeout(dismiss, timeout);
}

// ---------- Accessible Confirmation Modal ----------
export function confirmModal({
  title = '请确认操作',
  message = '',
  confirmText = '确认',
  cancelText = '取消',
  danger = false,
} = {}) {
  return new Promise((resolve) => {
    const root = document.getElementById('modal-root') || document.body;
    const prevFocus = document.activeElement;

    const backdrop = document.createElement('div');
    backdrop.className = 'modal-backdrop';
    backdrop.setAttribute('role', 'dialog');
    backdrop.setAttribute('aria-modal', 'true');
    backdrop.setAttribute('aria-labelledby', 'confirm-modal-title');

    backdrop.innerHTML = `
      <div class="modal-dialog">
        <div class="modal-header">
          <h3 class="modal-title" id="confirm-modal-title">${escapeHtml(title)}</h3>
          <button type="button" class="btn btn-ghost btn-icon btn-sm" data-modal-cancel aria-label="关闭">
            ${createIcon('x', 16)}
          </button>
        </div>
        <div class="modal-body">${escapeHtml(message)}</div>
        <div class="modal-footer">
          <button type="button" class="btn btn-secondary" data-modal-cancel>${escapeHtml(cancelText)}</button>
          <button type="button" class="btn ${danger ? 'btn-danger' : 'btn-primary'}" data-modal-confirm>${escapeHtml(confirmText)}</button>
        </div>
      </div>
    `;

    let settled = false;
    const finish = (result) => {
      if (settled) return;
      settled = true;
      document.removeEventListener('keydown', onKeyDown);
      backdrop.remove();
      if (prevFocus && typeof prevFocus.focus === 'function') {
        try { prevFocus.focus(); } catch (_) {}
      }
      resolve(result);
    };

    const onKeyDown = (e) => {
      if (e.key === 'Escape') {
        e.preventDefault();
        finish(false);
      }
    };

    backdrop.addEventListener('click', (e) => {
      if (e.target === backdrop) finish(false);
    });
    backdrop.querySelectorAll('[data-modal-cancel]').forEach((btn) => {
      btn.addEventListener('click', () => finish(false));
    });
    const confirmBtn = backdrop.querySelector('[data-modal-confirm]');
    confirmBtn?.addEventListener('click', () => finish(true));

    document.addEventListener('keydown', onKeyDown);
    root.appendChild(backdrop);
    confirmBtn?.focus();
  });
}

// ---------- Safe HTML Markdown Renderer ----------
function sanitizeHref(url) {
  const trimmed = String(url || '').trim();
  if (/^(https?:\/\/|mailto:|\/|#)/i.test(trimmed)) {
    return trimmed;
  }
  return '#';
}

function renderInlineMarkdown(escapedLine) {
  const codeTokens = [];
  let text = escapedLine.replace(/`([^`]+)`/g, (_, code) => {
    const idx = codeTokens.length;
    codeTokens.push(`<code>${code}</code>`);
    return `\u0000IC${idx}\u0000`;
  });

  text = text.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (_, label, url) => {
    const safeUrl = sanitizeHref(url.replace(/&amp;/g, '&'));
    const external = /^https?:\/\//i.test(safeUrl)
      ? ' target="_blank" rel="noopener noreferrer"'
      : '';
    return `<a href="${escapeHtml(safeUrl)}"${external}>${label}</a>`;
  });

  text = text.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  text = text.replace(/__([^_]+)__/g, '<strong>$1</strong>');
  text = text.replace(/~~([^~]+)~~/g, '<del>$1</del>');
  text = text.replace(/(^|[^\*])\*([^\s*][^*]*?)\*(?!\*)/g, '$1<em>$2</em>');

  return text.replace(/\u0000IC(\d+)\u0000/g, (_, idx) => codeTokens[Number(idx)] || '');
}

export function renderMarkdown(md) {
  if (!md) return '';
  const escaped = escapeHtml(String(md)).replace(/\r\n?/g, '\n');

  const codeBlocks = [];
  const withPlaceholders = escaped.replace(/```([a-zA-Z0-9_-]*)\n([\s\S]*?)```/g, (_, lang, code) => {
    const idx = codeBlocks.length;
    const cls = lang ? ` class="lang-${lang}"` : '';
    codeBlocks.push(`<pre><code${cls}>${code.replace(/\n$/, '')}</code></pre>`);
    return `\n\u0000CB${idx}\u0000\n`;
  });

  const lines = withPlaceholders.split('\n');
  const out = [];
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];
    const trimmed = line.trim();

    if (!trimmed) {
      i += 1;
      continue;
    }

    const cbMatch = trimmed.match(/^\u0000CB(\d+)\u0000$/);
    if (cbMatch) {
      out.push(codeBlocks[Number(cbMatch[1])] || '');
      i += 1;
      continue;
    }

    if (/^(-{3,}|\*{3,}|_{3,})$/.test(trimmed)) {
      out.push('<hr />');
      i += 1;
      continue;
    }

    const headingMatch = trimmed.match(/^(#{1,4})\s+(.+)$/);
    if (headingMatch) {
      const level = headingMatch[1].length;
      out.push(`<h${level}>${renderInlineMarkdown(headingMatch[2])}</h${level}>`);
      i += 1;
      continue;
    }

    if (/^&gt;\s?/.test(trimmed)) {
      const quoteLines = [];
      while (i < lines.length && /^&gt;\s?/.test(lines[i].trim())) {
        quoteLines.push(lines[i].trim().replace(/^&gt;\s?/, ''));
        i += 1;
      }
      out.push(`<blockquote>${quoteLines.map((l) => renderInlineMarkdown(l)).join('<br />')}</blockquote>`);
      continue;
    }

    if (
      trimmed.includes('|') &&
      i + 1 < lines.length &&
      /^\s*\|?(\s*:?-{2,}:?\s*\|)+\s*:?-{2,}:?\s*\|?\s*$/.test(lines[i + 1])
    ) {
      const parseRow = (rowStr) =>
        rowStr
          .trim()
          .replace(/^\|/, '')
          .replace(/\|$/, '')
          .split('|')
          .map((c) => c.trim());

      const headers = parseRow(line);
      i += 2;
      const bodyRows = [];
      while (i < lines.length && lines[i].trim().includes('|') && lines[i].trim() !== '') {
        bodyRows.push(parseRow(lines[i]));
        i += 1;
      }
      const thead = `<thead><tr>${headers.map((h) => `<th>${renderInlineMarkdown(h)}</th>`).join('')}</tr></thead>`;
      const tbody = bodyRows.length
        ? `<tbody>${bodyRows
            .map((r) => `<tr>${headers.map((_, idx) => `<td>${renderInlineMarkdown(r[idx] || '')}</td>`).join('')}</tr>`)
            .join('')}</tbody>`
        : '';
      out.push(`<div class="table-wrap"><table>${thead}${tbody}</table></div>`);
      continue;
    }

    if (/^[-*+]\s+/.test(trimmed)) {
      const items = [];
      while (i < lines.length && /^[-*+]\s+/.test(lines[i].trim())) {
        items.push(`<li>${renderInlineMarkdown(lines[i].trim().replace(/^[-*+]\s+/, ''))}</li>`);
        i += 1;
      }
      out.push(`<ul>${items.join('')}</ul>`);
      continue;
    }

    if (/^\d+\.\s+/.test(trimmed)) {
      const items = [];
      while (i < lines.length && /^\d+\.\s+/.test(lines[i].trim())) {
        items.push(`<li>${renderInlineMarkdown(lines[i].trim().replace(/^\d+\.\s+/, ''))}</li>`);
        i += 1;
      }
      out.push(`<ol>${items.join('')}</ol>`);
      continue;
    }

    const paraLines = [];
    while (i < lines.length) {
      const cur = lines[i].trim();
      if (
        !cur ||
        /^\u0000CB\d+\u0000$/.test(cur) ||
        /^(#{1,4})\s+/.test(cur) ||
        /^(-{3,}|\*{3,}|_{3,})$/.test(cur) ||
        /^&gt;\s?/.test(cur) ||
        /^[-*+]\s+/.test(cur) ||
        /^\d+\.\s+/.test(cur)
      ) {
        break;
      }
      paraLines.push(renderInlineMarkdown(cur));
      i += 1;
    }
    if (paraLines.length) {
      out.push(`<p>${paraLines.join('<br />')}</p>`);
    }
  }

  return out.join('\n');
}
