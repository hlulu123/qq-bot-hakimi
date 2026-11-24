import {
  apiGet,
  apiPost,
  apiPut,
  confirmModal,
  copyText as sharedCopyText,
  createIcon,
  escapeHtml,
  formatDuration,
  formatTime,
  safeLocalHttpUrl,
  showToast,
} from '../api.js';

const HINT_DISMISS_KEY = 'hajimi_napcat_hint_dismissed';
const MAX_TELEMETRY_POINTS = 60;
const MAX_RT_LOGS = 500;

const LOCAL_ICONS = {
  maximize: '<polyline points="15 3 21 3 21 9"/><polyline points="9 21 3 21 3 15"/><line x1="21" y1="3" x2="14" y2="10"/><line x1="3" y1="21" x2="10" y2="14"/>',
  minimize: '<polyline points="4 14 10 14 10 20"/><polyline points="20 10 14 10 14 4"/><line x1="14" y1="10" x2="21" y2="3"/><line x1="3" y1="21" x2="10" y2="14"/>',
  arrowDown: '<line x1="12" y1="5" x2="12" y2="19"/><polyline points="19 12 12 19 5 12"/>',
  cpu: '<rect x="4" y="4" width="16" height="16" rx="2"/><rect x="9" y="9" width="6" height="6"/><line x1="9" y1="1" x2="9" y2="4"/><line x1="15" y1="1" x2="15" y2="4"/><line x1="9" y1="20" x2="9" y2="23"/><line x1="15" y1="20" x2="15" y2="23"/><line x1="20" y1="9" x2="23" y2="9"/><line x1="20" y1="14" x2="23" y2="14"/><line x1="1" y1="9" x2="4" y2="9"/><line x1="1" y1="14" x2="4" y2="14"/>',
  network: '<circle cx="12" cy="5" r="3"/><circle cx="5" cy="19" r="3"/><circle cx="19" cy="19" r="3"/><line x1="10.5" y1="7.5" x2="6.5" y2="16.5"/><line x1="13.5" y1="7.5" x2="17.5" y2="16.5"/>',
  qr: '<rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/><rect x="14" y="14" width="3" height="3"/><line x1="21" y1="14" x2="21" y2="21"/><line x1="14" y1="21" x2="21" y2="21"/>',
  plug: '<path d="M12 22v-5"/><path d="M9 8V2"/><path d="M15 8V2"/><path d="M18 8v5a4 4 0 0 1-4 4h-4a4 4 0 0 1-4-4V8Z"/>',
  pause: '<rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/>',
};

const ADAPTER_TYPES = [
  { key: 'httpServers', label: 'HTTP 服务器', short: 'HTTP Server', kind: 'server', badge: 'badge-violet' },
  { key: 'httpSseServers', label: 'HTTP SSE 服务器', short: 'HTTP SSE', kind: 'server', badge: 'badge-info' },
  { key: 'httpClients', label: 'HTTP 客户端', short: 'HTTP Client', kind: 'client', badge: 'badge-amber' },
  { key: 'websocketServers', label: 'WS 服务器', short: 'WS Server', kind: 'ws-server', badge: 'badge-success' },
  { key: 'websocketClients', label: 'WS 客户端', short: 'WS Client', kind: 'ws-client', badge: 'badge-amber' },
];
const ADAPTER_TYPE_MAP = Object.fromEntries(ADAPTER_TYPES.map((t) => [t.key, t]));

const DEBUG_PRESETS = [
  { action: 'get_login_info', label: '登录账号', params: {} },
  { action: 'get_status', label: '运行状态', params: {} },
  { action: 'get_version_info', label: '版本信息', params: {} },
  { action: 'get_group_list', label: '群聊列表', params: {} },
  { action: 'get_friend_list', label: '好友列表', params: {} },
  { action: 'get_online_clients', label: '在线设备', params: {} },
  { action: 'get_group_info', label: '群详情', params: { group_id: 0, no_cache: false } },
  { action: 'get_group_member_list', label: '群成员', params: { group_id: 0 } },
  { action: 'get_stranger_info', label: '陌生人信息', params: { user_id: 0 } },
  { action: 'get_group_msg_history', label: '群历史消息', params: { group_id: 0, count: 10 } },
];

function icon(name, size = 14) {
  if (LOCAL_ICONS[name]) {
    return `<svg viewBox="0 0 24 24" width="${size}" height="${size}" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${LOCAL_ICONS[name]}</svg>`;
  }
  return createIcon(name, size);
}

function stripAnsi(text) {
  return String(text || '').replace(/\x1b\[[0-9;?]*[ -/]*[@-~]/g, '');
}

function safeSvgMarkup(svgStr) {
  const s = String(svgStr || '').trim();
  if (!s.startsWith('<svg ') || /<script|on\w+\s*=/i.test(s)) return '';
  return s;
}

function extractPortLabel(url, fallbackPort) {
  try {
    const u = new URL(String(url || ''));
    if (u.port) return `:${u.port}`;
  } catch (_) {}
  return `:${fallbackPort}`;
}

function isContainerRunning(info) {
  if (!info || typeof info !== 'object') return false;
  if (info.online === true || info.webui?.reachable === true || info.webui_api?.ok === true || info.status?.ok === true) return true;
  const ds = String(info.docker_status || '').toLowerCase().trim();
  if (['running', 'up', 'healthy', 'restarting'].includes(ds)) return true;
  const cs = String(info.container_status || '').toLowerCase().trim();
  return cs.startsWith('up ') || cs === 'up' || cs === 'running';
}

function isDockerAvailable(info) {
  if (!info || typeof info !== 'object') return false;
  return info.mode === 'docker' || info.container?.available === true;
}

function isNativeMode(info) {
  if (!info || typeof info !== 'object') return false;
  if (info.mode === 'native') return true;
  return info.container?.available === false && (info.webui?.reachable === true || info.online === true || info.status?.ok === true);
}

function safeAvatarUrl(rawUrl) {
  const s = String(rawUrl || '').trim();
  if (!s) return '';
  if (/^https?:\/\//i.test(s) || /^data:image\/(png|jpe?g|gif|webp|avif);base64,/i.test(s)) {
    return s;
  }
  return '';
}

function renderAvatar(src, label, sizeClass = '') {
  const cls = sizeClass ? `avatar ${sizeClass}` : 'avatar';
  const s = String(label || '').trim();
  const fallback = escapeHtml(s ? s.slice(0, 1).toUpperCase() : 'Q');
  const safeSrc = safeAvatarUrl(src);
  if (!safeSrc) return `<span class="${cls}" aria-hidden="true"><span>${fallback}</span></span>`;
  return `
    <span class="${cls}" aria-hidden="true" style="position:relative;overflow:hidden;">
      <span>${fallback}</span>
      <img src="${escapeHtml(safeSrc)}" alt="" loading="lazy" referrerpolicy="no-referrer" data-avatar-img="1" style="position:absolute;inset:0;width:100%;height:100%;object-fit:cover;" />
    </span>
  `;
}

function formatMemMb(mb) {
  const n = Number(mb);
  if (!Number.isFinite(n) || n < 0) return '--';
  return n >= 1024 ? `${(n / 1024).toFixed(2)} GB` : `${n.toFixed(1)} MB`;
}

function formatLoginPhase(phase) {
  const map = {
    ready: '已就绪',
    idle: '等待登录',
    qrcode_waiting: '等待扫码',
    qrcode_scanned: '已扫码 · 请在手机 QQ 确认',
    initializing: '正在初始化协议核心',
    reconnecting: '正在重连',
    offline: '已掉线',
  };
  return map[String(phase || '').toLowerCase()] || (phase || '--');
}

function maskToken(token) {
  if (!token) return '未设置';
  if (token.length <= 6) return '••••••';
  return `${token.slice(0, 3)}${'•'.repeat(Math.min(10, token.length - 5))}${token.slice(-2)}`;
}

async function copyText(text, successMsg = '已复制到剪贴板') {
  if (typeof sharedCopyText === 'function') {
    return sharedCopyText(text, successMsg);
  }
  showToast('复制功能不可用', 'error');
  return false;
}

function hasQrGlyphs(line) {
  return /[█▀▄▓▒░■□]{3,}/.test(line);
}

function renderDualSparkline(seriesA, seriesB, { maxY = 100, unit = '%', labelA = '系统', labelB = 'QQ' } = {}) {
  const W = 320;
  const H = 86;
  const padL = 6;
  const padR = 6;
  const padT = 8;
  const padB = 14;
  const plotW = W - padL - padR;
  const plotH = H - padT - padB;
  const n = Math.max(seriesA.length, seriesB.length);
  if (n === 0) {
    return `
      <svg class="napcat-chart-svg" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
        <line x1="${padL}" y1="${padT + plotH}" x2="${W - padR}" y2="${padT + plotH}" stroke="currentColor" stroke-opacity="0.14" stroke-dasharray="3 3" />
        <text x="${W / 2}" y="${H / 2}" text-anchor="middle" fill="currentColor" fill-opacity="0.45" font-size="11">等待实时采样数据…</text>
      </svg>
    `;
  }

  const peak = Math.max(
    maxY || 1,
    ...seriesA.map((v) => (Number.isFinite(v) ? v : 0)),
    ...seriesB.map((v) => (Number.isFinite(v) ? v : 0)),
  );
  const ceilY = peak <= 5 ? 5 : peak <= 10 ? 10 : peak <= 25 ? 25 : peak <= 50 ? 50 : Math.ceil(peak / 10) * 10;

  const toPoints = (arr) => {
    const len = arr.length;
    if (len === 0) return [];
    const denom = Math.max(MAX_TELEMETRY_POINTS - 1, len - 1, 1);
    const startOffset = MAX_TELEMETRY_POINTS - len;
    return arr.map((val, i) => {
      const clamped = Math.max(0, Number.isFinite(val) ? val : 0);
      const x = padL + ((startOffset + i) / denom) * plotW;
      const y = padT + plotH - (Math.min(clamped, ceilY) / ceilY) * plotH;
      return [Number(x.toFixed(1)), Number(y.toFixed(1))];
    });
  };

  const ptsA = toPoints(seriesA);
  const ptsB = toPoints(seriesB);
  const polyA = ptsA.map((p) => p.join(',')).join(' ');
  const polyB = ptsB.map((p) => p.join(',')).join(' ');
  const areaA = ptsA.length >= 2 ? `M ${ptsA[0][0]},${padT + plotH} L ${ptsA.map((p) => p.join(',')).join(' L ')} L ${ptsA[ptsA.length - 1][0]},${padT + plotH} Z` : '';
  const areaB = ptsB.length >= 2 ? `M ${ptsB[0][0]},${padT + plotH} L ${ptsB.map((p) => p.join(',')).join(' L ')} L ${ptsB[ptsB.length - 1][0]},${padT + plotH} Z` : '';
  const lastA = ptsA[ptsA.length - 1];
  const lastB = ptsB[ptsB.length - 1];

  return `
    <svg class="napcat-chart-svg" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" aria-label="${escapeHtml(labelA)} / ${escapeHtml(labelB)} 趋势图">
      <line x1="${padL}" y1="${padT}" x2="${W - padR}" y2="${padT}" stroke="currentColor" stroke-opacity="0.1" stroke-dasharray="2 3" />
      <line x1="${padL}" y1="${padT + plotH / 2}" x2="${W - padR}" y2="${padT + plotH / 2}" stroke="currentColor" stroke-opacity="0.1" stroke-dasharray="2 3" />
      <line x1="${padL}" y1="${padT + plotH}" x2="${W - padR}" y2="${padT + plotH}" stroke="currentColor" stroke-opacity="0.18" />
      ${areaA ? `<path d="${areaA}" fill="var(--accent)" fill-opacity="0.13" />` : ''}
      ${areaB ? `<path d="${areaB}" fill="var(--success)" fill-opacity="0.12" />` : ''}
      ${polyA ? `<polyline fill="none" stroke="var(--accent)" stroke-width="1.8" stroke-linejoin="round" stroke-linecap="round" points="${polyA}" />` : ''}
      ${polyB ? `<polyline fill="none" stroke="var(--success)" stroke-width="1.8" stroke-linejoin="round" stroke-linecap="round" points="${polyB}" />` : ''}
      ${lastA ? `<circle cx="${lastA[0]}" cy="${lastA[1]}" r="2.8" fill="var(--accent)" />` : ''}
      ${lastB ? `<circle cx="${lastB[0]}" cy="${lastB[1]}" r="2.8" fill="var(--success)" />` : ''}
      <text x="${padL + 2}" y="${padT + 8}" fill="currentColor" fill-opacity="0.45" font-size="9" font-family="monospace">${ceilY}${escapeHtml(unit)}</text>
      <text x="${padL + 2}" y="${H - 2}" fill="currentColor" fill-opacity="0.45" font-size="9" font-family="monospace">0${escapeHtml(unit)}</text>
      <text x="${W - padR}" y="${H - 2}" text-anchor="end" fill="currentColor" fill-opacity="0.45" font-size="9" font-family="monospace">最近 ${n} 次采样</text>
    </svg>
  `;
}

export async function mount(container, ctx) {
  let destroyed = false;
  let logPollTimer = null;
  let loginPollTimer = null;
  let iframeLoadFallbackTimer = null;
  let sysEventSource = null;
  let logEventSource = null;
  let activeModalCleanup = null;

  let hintDismissed = false;
  try {
    hintDismissed = sessionStorage.getItem(HINT_DISMISS_KEY) === '1';
  } catch (_) {}

  const validTabs = ['overview', 'network', 'logs', 'contacts', 'debug', 'webui'];
  const initialTab = validTabs.includes(ctx?.params?.tab) ? ctx.params.tab : 'overview';

  const ui = {
    tab: initialTab,
    info: null,
    infoLoading: true,
    infoError: '',
    fullscreen: false,
    containerBusy: false,
    processRestarting: false,
    iframeSrcMounted: '',
    iframeLoaded: false,

    qrData: null,
    qrLoading: false,
    qrRefreshing: false,
    showQrPanelWhenOnline: false,
    autoLoginSaving: false,
    quickLoginBusyUin: '',

    sysStatus: null,
    sysConnected: false,
    sysError: '',
    sysHistory: { cpuSys: [], cpuQq: [], memSysPct: [], memQqPct: [] },

    adapterFilter: 'all',
    revealedAdapterTokens: new Set(),
    ob11Saving: false,
    coreSaving: false,
    pluginBusyId: '',

    logMode: ctx?.params?.mode === 'docker' ? 'docker' : ctx?.params?.mode === 'files' ? 'files' : 'realtime',
    rtConnected: false,
    rtPaused: false,
    rtError: '',
    rtLevel: 'ALL',
    rtSearch: '',
    rtLogs: [],
    logsLoaded: false,
    logsLoading: false,
    logsError: '',
    logsLines: [],
    logsRaw: '',
    hasQrHint: false,
    logsLineLimit: 180,
    logsSearch: '',
    logsAutoRefresh: false,
    logsQrMode: false,
    logsUpdatedAt: null,
    logFiles: [],
    logFilesLoaded: false,
    selectedLogFile: '',
    logFileContent: '',
    logFileLines: [],
    logFileLoading: false,
    logFileSearch: '',

    contactsFilter: 'all',
    contactsSearch: '',
    whitelist: { enabled: false, group_ids: [] },
    wsGuardNeedsRestart: false,
    whitelistSaving: false,
    wsGuardRestarting: false,
    showManualAddGroup: false,

    debugAction: 'get_login_info',
    debugParamsText: '{}',
    debugRunning: false,
    debugResult: null,
    debugSchemas: [],
    debugSchemasLoaded: false,
    debugSchemaFilter: '',
  };

  const onAvatarError = (e) => {
    const target = e.target;
    if (target && target.tagName === 'IMG' && target.dataset && target.dataset.avatarImg === '1') {
      target.style.display = 'none';
    }
  };
  container.addEventListener('error', onAvatarError, true);

  const onKeyDown = (e) => {
    if (e.key === 'Escape') {
      if (activeModalCleanup) {
        e.preventDefault();
        activeModalCleanup();
        return;
      }
      if (ui.fullscreen) {
        e.preventDefault();
        setFullscreen(false);
      }
    }
  };
  document.addEventListener('keydown', onKeyDown);

  container.innerHTML = `
    <div class="napcat-view" id="napcat-root">
      <section class="napcat-control-bar" id="napcat-header-bar"></section>

      <div class="napcat-subtabs" id="napcat-subtabs-row">
        <div class="page-tabs" id="napcat-tabs" role="tablist" aria-label="NapCat 控制台视图">
          <button type="button" class="page-tab" role="tab" data-tab="overview">概览</button>
          <button type="button" class="page-tab" role="tab" data-tab="network" id="napcat-tab-network">网络配置</button>
          <button type="button" class="page-tab" role="tab" data-tab="logs">
            <span>日志与扫码</span>
            <span class="napcat-tab-dot" id="napcat-qr-tab-dot" style="display:none;" title="检测到扫码登录提示"></span>
          </button>
          <button type="button" class="page-tab" role="tab" data-tab="contacts" id="napcat-tab-contacts">群聊与好友</button>
          <button type="button" class="page-tab" role="tab" data-tab="debug">接口调试</button>
          <button type="button" class="page-tab" role="tab" data-tab="webui">原生面板</button>
        </div>
      </div>

      <section id="napcat-pane-overview"></section>
      <section id="napcat-pane-network" style="display:none;"></section>
      <section id="napcat-pane-logs" style="display:none;"></section>
      <section id="napcat-pane-contacts" style="display:none;"></section>
      <section id="napcat-pane-debug" style="display:none;"></section>
      <section id="napcat-pane-webui" style="display:none;"></section>
    </div>
  `;

  const headerBarEl = container.querySelector('#napcat-header-bar');
  const tabsEl = container.querySelector('#napcat-tabs');
  const quickActionsEl = ctx.setActions('') || document.createElement('div');
  const panes = {
    overview: container.querySelector('#napcat-pane-overview'),
    network: container.querySelector('#napcat-pane-network'),
    logs: container.querySelector('#napcat-pane-logs'),
    contacts: container.querySelector('#napcat-pane-contacts'),
    debug: container.querySelector('#napcat-pane-debug'),
    webui: container.querySelector('#napcat-pane-webui'),
  };

  function getWebuiMeta() {
    const w = ui.info?.webui || {};
    const port = w.port || ui.info?.ports?.webui || 6099;
    const rawMasked = String(w.token_masked || (w.token ? maskToken(String(w.token)) : ''));
    const hasToken = Boolean(w.has_token ?? (rawMasked || w.token));
    return {
      port,
      url: safeLocalHttpUrl(w.url || ui.info?.webui_url, port, '/webui/'),
      hasToken,
      tokenMasked: rawMasked,
      disabled: Boolean(w.disabled),
      authenticated: Boolean(w.api_authenticated || ui.info?.webui_api?.authenticated),
      require2fa: Boolean(w.require_2fa || ui.info?.webui_api?.require_2fa),
      apiError: String(w.api_error || ui.info?.webui_api?.error || ''),
    };
  }

  function setFullscreen(next) {
    const shouldEnable = Boolean(next);
    if (shouldEnable && ui.tab !== 'webui') switchTab('webui');
    ui.fullscreen = shouldEnable;
    const shell = panes.webui.querySelector('#napcat-frame-shell');
    if (shell) shell.classList.toggle('napcat-fullscreen', ui.fullscreen);
    document.body.style.overflow = ui.fullscreen ? 'hidden' : '';
    renderQuickActions();
    updateTokenDisplays();
  }

  function updateTokenDisplays() {
    const { hasToken, tokenMasked } = getWebuiMeta();
    const displayText = !hasToken ? '未检测到' : (tokenMasked || '已配置（已脱敏）');
    container.querySelectorAll('[data-token-display]').forEach((el) => { el.textContent = displayText; });
  }

  // ---- Header Bar / Tabs / Quick Actions ----

  function renderHeaderBar() {
    const info = ui.info;
    if (!info && ui.infoLoading) {
      headerBarEl.innerHTML = `
        <div class="row flex-1">
          <div class="loading-skeleton" style="width:36px;height:36px;border-radius:10px;"></div>
          <div class="stack" style="gap:6px;flex:1;max-width:360px;">
            <div class="loading-skeleton" style="height:14px;width:62%;"></div>
            <div class="loading-skeleton" style="height:11px;width:88%;"></div>
          </div>
        </div>
        <div class="loading-skeleton" style="height:30px;width:210px;border-radius:999px;"></div>
      `;
      return;
    }

    const running = isContainerRunning(info);
    const nativeMode = isNativeMode(info);
    const dockerAvail = isDockerAvailable(info);
    const online = Boolean(info?.online);
    const good = info?.good !== false;
    const uid = String(info?.user_id || '').trim();
    const nickname = String(info?.nickname || '').trim() || (uid ? `QQ ${uid}` : '未登录 QQ');
    const qqAvatar = info?.login_info?.avatar_url || (uid ? `https://q1.qlogo.cn/g?b=qq&nk=${encodeURIComponent(uid)}&s=100` : '');

    let statusPillHtml = '';
    if (online) {
      statusPillHtml = `<span class="row-sm text-success" title="${good ? 'QQ 协议端在线且状态正常' : 'QQ 在线（good=false）'}"><span class="status-dot online"></span>${good ? '在线' : '在线（降级）'}</span>`;
    } else if (running) {
      const phaseText = formatLoginPhase(info?.login_state?.login_phase);
      statusPillHtml = `<span class="row-sm text-amber"><span class="status-dot warn"></span>待登录${phaseText && phaseText !== '--' ? ` · ${escapeHtml(phaseText)}` : ''}</span>`;
    } else {
      statusPillHtml = `<span class="row-sm text-danger"><span class="status-dot offline"></span>离线</span>`;
    }

    const napcatVer = info?.versions?.napcat || '';
    const qqVer = info?.versions?.qq || '';
    const containerStatus = info?.container_status || info?.docker_status || (ui.infoError ? '连接失败' : 'unknown');
    const runtimeBadgeHtml = nativeMode
      ? '<span>本机原生</span>'
      : dockerAvail
        ? `<span class="mono ${running ? '' : 'text-danger'}" title="${escapeHtml(info?.image || '')}">Docker · ${escapeHtml(containerStatus)}</span>`
        : '<span>未检测到 NapCat</span>';

    const httpUrl = info?.endpoints?.http_url || info?.http_url || `http://127.0.0.1:${info?.ports?.http || 3000}`;
    const wsUrl = info?.endpoints?.ws_url || info?.ws_url || `ws://127.0.0.1:${info?.ports?.ws || 3001}`;
    const httpPort = extractPortLabel(httpUrl, info?.ports?.http || 3000);
    const wsPort = extractPortLabel(wsUrl, info?.ports?.ws || 3001);

    const wMeta = getWebuiMeta();
    let directPillHtml = '';
    if (wMeta.authenticated) {
      directPillHtml = `<span class="napcat-direct-pill" title="后端已自动使用本地 webui.json 凭据免登直连 NapCat 全部管理接口与实时监控流">${icon('checkCircle', 15)}<span>已免登直连 NapCat</span></span>`;
    } else if (wMeta.require2fa) {
      directPillHtml = `<span class="napcat-direct-pill is-warn" title="${escapeHtml(wMeta.apiError)}">${icon('alert', 13)}<span>NapCat 开启了 2FA（本地降级模式）</span></span>`;
    } else if (running) {
      directPillHtml = `<span class="napcat-direct-pill is-warn" title="${escapeHtml(wMeta.apiError || '')}">${icon('alert', 13)}<span>仅 OneBot 直连</span></span>`;
    } else {
      directPillHtml = `<span class="napcat-direct-pill is-off"><span class="status-dot offline"></span><span>NapCat 未连接</span></span>`;
    }

    headerBarEl.innerHTML = `
      <div class="napcat-bar-left">
        <div class="napcat-identity">
          ${renderAvatar(qqAvatar, nickname)}
          <div class="napcat-identity-text">
            <div class="napcat-identity-name">
              <span>${escapeHtml(nickname)}</span>
              ${uid ? `<button type="button" class="napcat-copy-id" data-copy-endpoint="${escapeHtml(uid)}" title="复制 QQ 号">${escapeHtml(uid)}</button>` : ''}
            </div>
            <div class="napcat-identity-meta">
              ${statusPillHtml}
              <span>·</span>
              ${runtimeBadgeHtml}
              ${napcatVer ? `<span>·</span><span>NapCat v${escapeHtml(napcatVer)}</span>` : ''}
              ${qqVer ? `<span>·</span><span>QQ ${escapeHtml(qqVer)}</span>` : ''}
              <button type="button" class="napcat-endpoint-chip" data-copy-endpoint="${escapeHtml(httpUrl)}" title="复制 OneBot HTTP 地址：${escapeHtml(httpUrl)}">${escapeHtml(httpPort)} HTTP</button>
              <button type="button" class="napcat-endpoint-chip" data-copy-endpoint="${escapeHtml(wsUrl)}" title="复制 OneBot WS 地址：${escapeHtml(wsUrl)}">${escapeHtml(wsPort)} WS</button>
            </div>
          </div>
        </div>
      </div>
      <div class="napcat-bar-right">${directPillHtml}</div>
    `;

    headerBarEl.querySelectorAll('[data-copy-endpoint]').forEach((btn) => {
      btn.addEventListener('click', () => copyText(btn.dataset.copyEndpoint || '', `已复制: ${btn.dataset.copyEndpoint}`));
    });
  }

  function renderTabsAndCounts() {
    const groups = Array.isArray(ui.info?.groups) ? ui.info.groups : [];
    const friends = Array.isArray(ui.info?.friends) ? ui.info.friends : [];
    const groupCount = typeof ui.info?.group_count === 'number' ? ui.info.group_count : groups.length;
    const friendCount = typeof ui.info?.friend_count === 'number' ? ui.info.friend_count : friends.length;

    const contactsTab = tabsEl.querySelector('#napcat-tab-contacts');
    if (contactsTab) {
      contactsTab.innerHTML = `群聊与好友 <span class="count">${groupCount + friendCount}</span>`;
      contactsTab.title = `群聊 ${groupCount} 个 · 好友 ${friendCount} 位`;
    }

    const netSummary = ui.info?.ob11_config?.summary;
    const netTab = tabsEl.querySelector('#napcat-tab-network');
    if (netTab && netSummary && typeof netSummary.total === 'number') {
      netTab.innerHTML = `网络配置 <span class="count">${netSummary.enabled}/${netSummary.total}</span>`;
    }

    const qrDot = tabsEl.querySelector('#napcat-qr-tab-dot');
    if (qrDot) qrDot.style.display = ui.hasQrHint ? 'inline-block' : 'none';

    tabsEl.querySelectorAll('.page-tab').forEach((btn) => {
      const isActive = btn.dataset.tab === ui.tab;
      btn.classList.toggle('active', isActive);
      btn.setAttribute('aria-selected', isActive ? 'true' : 'false');
    });
  }

  function renderQuickActions() {
    if (destroyed) return;
    const running = isContainerRunning(ui.info);
    const nativeMode = isNativeMode(ui.info);
    const { authenticated } = getWebuiMeta();
    const busy = ui.containerBusy || ui.processRestarting;

    const containerCtrlHtml = nativeMode
      ? authenticated
        ? `<button type="button" class="btn btn-secondary" id="napcat-proc-restart-btn" ${busy ? 'disabled' : ''}>${icon('rotate', 15)}${ui.processRestarting ? '重启中…' : '重启 NapCat'}</button>`
        : ''
      : running
        ? `<button type="button" class="btn btn-secondary" id="napcat-restart-btn" ${busy ? 'disabled' : ''}>${icon('rotate', 15)}${ui.containerBusy ? '处理中…' : '重启容器'}</button>`
        : `<button type="button" class="btn btn-primary" id="napcat-start-btn" ${busy ? 'disabled' : ''}>${icon('play', 14)}${ui.containerBusy ? '启动中…' : '启动容器'}</button>`;

    const fullscreenHtml = ui.tab === 'webui'
      ? `<button type="button" class="btn btn-ghost" id="napcat-fullscreen-btn">${ui.fullscreen ? icon('minimize', 15) : icon('maximize', 15)}${ui.fullscreen ? '退出全屏' : '全屏'}</button>`
      : '';

    quickActionsEl.innerHTML = `
      ${fullscreenHtml}
      ${containerCtrlHtml}
    `;

    quickActionsEl.querySelector('#napcat-restart-btn')?.addEventListener('click', () => handleContainerAction('restart'));
    quickActionsEl.querySelector('#napcat-start-btn')?.addEventListener('click', () => handleContainerAction('start'));
    quickActionsEl.querySelector('#napcat-proc-restart-btn')?.addEventListener('click', handleProcessRestart);
    quickActionsEl.querySelector('#napcat-fullscreen-btn')?.addEventListener('click', () => setFullscreen(!ui.fullscreen));
  }

  // ---- Live SSE: System Telemetry ----

  function stopSysStream() {
    if (sysEventSource) {
      try { sysEventSource.close(); } catch (_) {}
      sysEventSource = null;
    }
    ui.sysConnected = false;
  }

  function pushSysSample(sample) {
    if (!sample || typeof sample !== 'object') return;
    ui.sysStatus = sample;
    ui.sysError = '';
    const push = (arr, v) => {
      arr.push(Number.isFinite(v) ? v : 0);
      if (arr.length > MAX_TELEMETRY_POINTS) arr.shift();
    };
    push(ui.sysHistory.cpuSys, Number(sample?.cpu?.system_pct ?? 0));
    push(ui.sysHistory.cpuQq, Number(sample?.cpu?.qq_pct ?? 0));
    push(ui.sysHistory.memSysPct, Number(sample?.memory?.system_pct ?? 0));
    push(ui.sysHistory.memQqPct, Number(sample?.memory?.qq_pct ?? 0));
  }

  function syncSysStream() {
    const shouldRun = !destroyed && ui.tab === 'overview' && isContainerRunning(ui.info) && getWebuiMeta().authenticated;
    if (!shouldRun) {
      stopSysStream();
      return;
    }
    if (sysEventSource) return;

    try {
      const es = new EventSource('/api/napcat/sys-stream');
      sysEventSource = es;
      es.onopen = () => {
        if (destroyed || sysEventSource !== es) return;
        ui.sysConnected = true;
        ui.sysError = '';
        updateTelemetryDOM();
      };
      es.onmessage = (ev) => {
        if (destroyed || sysEventSource !== es) return;
        try {
          const data = JSON.parse(ev.data);
          ui.sysConnected = true;
          pushSysSample(data);
          updateTelemetryDOM();
        } catch (_) {}
      };
      es.addEventListener('status_error', (ev) => {
        if (destroyed || sysEventSource !== es) return;
        try { ui.sysError = String(JSON.parse(ev.data)?.error || '监控流暂不可用'); } catch (_) { ui.sysError = '监控流暂不可用'; }
        ui.sysConnected = false;
        updateTelemetryDOM();
      });
      es.onerror = () => {
        if (destroyed || sysEventSource !== es) return;
        ui.sysConnected = false;
        updateTelemetryDOM();
      };
    } catch (err) {
      ui.sysError = err?.message || String(err);
    }
  }

  function updateTelemetryDOM() {
    const slot = panes.overview.querySelector('#napcat-telemetry-slot');
    if (!slot) return;

    const st = ui.sysStatus;
    const cpu = st?.cpu || {};
    const mem = st?.memory || {};
    const arch = st?.arch || ui.info?.versions?.platform || '';
    const cpuSys = Number(cpu.system_pct ?? 0);
    const cpuQq = Number(cpu.qq_pct ?? 0);
    const memTotal = Number(mem.total_mb ?? 0);
    const memSysMb = Number(mem.system_mb ?? 0);
    const memQqMb = Number(mem.qq_mb ?? 0);
    const memSysPct = Number(mem.system_pct ?? 0);
    const memQqPct = Number(mem.qq_pct ?? 0);

    const statusBadge = ui.sysConnected
      ? `<span class="badge badge-success"><span class="status-dot online pulse"></span>实时监控中</span>`
      : ui.sysError
        ? `<span class="badge badge-warning" title="${escapeHtml(ui.sysError)}">重连中…</span>`
        : `<span class="badge badge-muted">等待采样…</span>`;

    const cpuBarCls = cpuSys >= 85 ? 'is-danger' : cpuSys >= 60 ? 'is-warn' : '';
    const memBarCls = memSysPct >= 85 ? 'is-danger' : memSysPct >= 70 ? 'is-warn' : '';

    slot.innerHTML = `
      <div class="card">
        <div class="card-header">
          <div>
            <h3 class="card-title">${icon('cpu', 15)} 实时系统与 QQ 资源监控</h3>
            <p class="card-subtitle">直连 NapCat 遥测流（与 NapCat 首页一致），实时追踪 CPU 与内存占用</p>
          </div>
          <div class="row wrap">
            ${arch ? `<span class="badge badge-muted mono" title="内核与运行架构">${escapeHtml(arch)}</span>` : ''}
            ${statusBadge}
          </div>
        </div>
        <div class="card-body">
          <div class="napcat-telemetry-grid">
            <div class="napcat-metric-box">
              <div class="row-between wrap">
                <strong style="font-size:13px;">CPU 占用</strong>
                <div class="row text-xs text-muted mono">
                  ${cpu.cores ? `<span>${escapeHtml(String(cpu.cores))} 核</span>` : ''}
                  ${cpu.speed_ghz ? `<span>· ${escapeHtml(String(cpu.speed_ghz))} GHz</span>` : ''}
                  ${cpu.model && cpu.model !== 'unknown' ? `<span class="truncate" style="max-width:150px;" title="${escapeHtml(cpu.model)}">· ${escapeHtml(cpu.model)}</span>` : ''}
                </div>
              </div>
              <div class="napcat-metric-nums">
                <div class="napcat-metric-stat">
                  <div class="napcat-metric-stat-label"><span class="napcat-legend-dot sys"></span>系统总 CPU</div>
                  <div class="napcat-metric-stat-val">${st ? `${cpuSys.toFixed(2)}%` : '--'}</div>
                  <div class="napcat-progress"><div class="napcat-progress-fill ${cpuBarCls}" style="width:${Math.min(100, cpuSys)}%;"></div></div>
                </div>
                <div class="napcat-metric-stat">
                  <div class="napcat-metric-stat-label"><span class="napcat-legend-dot qq"></span>QQ 主线程 CPU</div>
                  <div class="napcat-metric-stat-val">${st ? `${cpuQq.toFixed(2)}%` : '--'}</div>
                  <div class="napcat-progress"><div class="napcat-progress-fill is-qq" style="width:${Math.min(100, cpuQq)}%;"></div></div>
                </div>
              </div>
              <div class="napcat-chart-wrap">
                ${renderDualSparkline(ui.sysHistory.cpuSys, ui.sysHistory.cpuQq, { maxY: 10, unit: '%', labelA: '系统 CPU', labelB: 'QQ CPU' })}
              </div>
            </div>

            <div class="napcat-metric-box">
              <div class="row-between wrap">
                <strong style="font-size:13px;">内存占用</strong>
                <div class="row text-xs text-muted mono"><span>总内存 ${memTotal > 0 ? escapeHtml(formatMemMb(memTotal)) : '--'}</span></div>
              </div>
              <div class="napcat-metric-nums">
                <div class="napcat-metric-stat">
                  <div class="napcat-metric-stat-label"><span class="napcat-legend-dot sys"></span>系统已用</div>
                  <div class="napcat-metric-stat-val">${st ? escapeHtml(formatMemMb(memSysMb)) : '--'}</div>
                  <div class="text-xs text-muted mono">${st ? `${memSysPct.toFixed(1)}%` : '--'}</div>
                  <div class="napcat-progress"><div class="napcat-progress-fill ${memBarCls}" style="width:${Math.min(100, memSysPct)}%;"></div></div>
                </div>
                <div class="napcat-metric-stat">
                  <div class="napcat-metric-stat-label"><span class="napcat-legend-dot qq"></span>QQ 进程占用</div>
                  <div class="napcat-metric-stat-val">${st ? escapeHtml(formatMemMb(memQqMb)) : '--'}</div>
                  <div class="text-xs text-muted mono">${st ? `${memQqPct.toFixed(2)}%` : '--'}</div>
                  <div class="napcat-progress"><div class="napcat-progress-fill is-qq" style="width:${Math.min(100, memQqPct * 4)}%;"></div></div>
                </div>
              </div>
              <div class="napcat-chart-wrap">
                ${renderDualSparkline(ui.sysHistory.memSysPct, ui.sysHistory.memQqPct, { maxY: 20, unit: '%', labelA: '系统内存', labelB: 'QQ 内存' })}
              </div>
            </div>
          </div>
        </div>
      </div>
    `;
  }

  // ---- QR Code & Login Polling ----

  function stopLoginPoll() {
    if (loginPollTimer) {
      window.clearInterval(loginPollTimer);
      loginPollTimer = null;
    }
  }

  async function fetchQrState({ silent = true } = {}) {
    if (destroyed) return;
    ui.qrLoading = true;
    try {
      const data = await apiGet('/api/napcat/qrcode');
      if (destroyed) return;
      ui.qrData = data || null;
      if (data?.is_login && !ui.info?.online) {
        stopLoginPoll();
        showToast(`QQ ${data.nick || data.uin || ''} 已登录成功！`, 'success');
        await refreshAll({ reloadIframe: false, notify: false, force: true });
        return;
      }
      renderOverviewPane();
    } catch (err) {
      if (!silent) showToast(`获取登录二维码失败: ${err?.message || String(err)}`, 'error');
    } finally {
      ui.qrLoading = false;
    }
  }

  function syncLoginPoll() {
    const needLogin = isContainerRunning(ui.info) && !ui.info?.online && getWebuiMeta().authenticated;
    if (!needLogin || ui.tab !== 'overview' || destroyed) {
      stopLoginPoll();
      return;
    }
    if (!ui.qrData && !ui.qrLoading) fetchQrState({ silent: true });
    if (!loginPollTimer) {
      loginPollTimer = window.setInterval(() => {
        if (!destroyed && !document.hidden && ui.tab === 'overview') fetchQrState({ silent: true });
      }, 3000);
    }
  }

  // ---- TAB 1: Overview & Telemetry ----

  function renderOverviewPane() {
    const el = panes.overview;
    const info = ui.info;

    if (!info && ui.infoLoading) {
      el.innerHTML = `
        <div class="stack">
          <div class="napcat-kpi-grid">
            <div class="loading-skeleton" style="height:92px;border-radius:12px;"></div>
            <div class="loading-skeleton" style="height:92px;border-radius:12px;"></div>
            <div class="loading-skeleton" style="height:92px;border-radius:12px;"></div>
            <div class="loading-skeleton" style="height:92px;border-radius:12px;"></div>
          </div>
          <div class="loading-skeleton" style="height:260px;border-radius:14px;"></div>
        </div>
      `;
      return;
    }

    const running = isContainerRunning(info);
    const dockerAvail = isDockerAvailable(info);
    const nativeMode = isNativeMode(info);
    const online = Boolean(info?.online);
    const wMeta = getWebuiMeta();

    if (!running) {
      const containerStatus = info?.container_status || info?.docker_status || (ui.infoError ? '连接失败' : 'stopped');
      el.innerHTML = `
        <div class="card">
          <div class="napcat-offline-card" style="min-height:320px;">
            <div class="empty-state-icon" style="color:var(--danger);">${icon('server', 24)}</div>
            <div class="empty-state-title" style="font-size:16px;color:var(--text);">NapCat 服务当前未运行</div>
            <div class="text-sm text-secondary" style="max-width:500px;">
              ${dockerAvail
                ? `Docker 容器状态：<span class="mono">${escapeHtml(containerStatus)}</span>。点击下方启动容器后，本面板将自动免登直连 NapCat 显示登录二维码、资源监控与网络配置。`
                : `未检测到正在运行的 NapCat 服务（端口 <span class="mono">:${escapeHtml(String(wMeta.port))}</span>）。请启动 NapCat 容器或本机 NapCat 进程后点击刷新。`}
            </div>
            <div class="row mt-12 wrap" style="justify-content:center;">
              ${dockerAvail ? `<button type="button" class="btn btn-primary" id="napcat-ov-start-btn" ${ui.containerBusy ? 'disabled' : ''}>${icon('play', 15)} ${ui.containerBusy ? '正在启动容器…' : '启动 NapCat 容器'}</button>` : ''}
              <button type="button" class="btn btn-secondary" id="napcat-ov-retry-btn">${icon('refresh', 14)} 重新探测</button>
              <button type="button" class="btn btn-secondary" id="napcat-ov-logs-btn">${icon('terminal', 14)} 查看容器日志</button>
            </div>
          </div>
        </div>
      `;
      el.querySelector('#napcat-ov-start-btn')?.addEventListener('click', () => handleContainerAction('start'));
      el.querySelector('#napcat-ov-retry-btn')?.addEventListener('click', () => refreshAll({ notify: false, force: true }));
      el.querySelector('#napcat-ov-logs-btn')?.addEventListener('click', () => { ui.logMode = 'docker'; switchTab('logs'); });
      return;
    }

    const ls = ui.qrData || info?.login_state || {};
    const versions = info?.versions || {};
    const ob11 = info?.ob11_config || {};
    const ob11Summary = ob11.summary || { total: 0, enabled: 0, by_type: {} };
    const ob11Net = ob11.config?.network || {};
    const wc = info?.webui_config || {};
    const groups = Array.isArray(info?.groups) ? info.groups : [];
    const friends = Array.isArray(info?.friends) ? info.friends : [];
    const wlEnabled = Boolean(ui.whitelist?.enabled);
    const wlCount = Array.isArray(ui.whitelist?.group_ids) ? ui.whitelist.group_ids.length : 0;

    let loginCardHtml = '';
    if (!online || ui.showQrPanelWhenOnline) {
      const qrSvg = safeSvgMarkup(ls.qrcode_svg);
      const quickList = Array.isArray(ls.quick_login_list) ? ls.quick_login_list : [];
      const phaseLabel = formatLoginPhase(ls.login_phase);
      loginCardHtml = `
        <div class="card">
          <div class="card-header">
            <div>
              <h3 class="card-title">${icon('qr', 15)} ${online ? 'QQ 登录信息与扫码区' : 'QQ 扫码 / 快捷登录（免登 NapCat 直接操作）'}</h3>
              <p class="card-subtitle">${online ? '当前 QQ 已在线，如需更换账号可在停止后重新扫码' : '使用机器人 QQ 手机端扫描下方二维码，或点击右侧历史账号一键快捷登录，登录成功后页面自动刷新'}</p>
            </div>
            <div class="row wrap">
              <span class="badge ${ls.qr_login_accepted ? 'badge-info' : 'badge-warning'}">${escapeHtml(phaseLabel)}</span>
              ${!online
                ? `<button type="button" class="btn btn-primary btn-sm" id="napcat-refresh-qr-btn" ${ui.qrRefreshing ? 'disabled' : ''}>${icon('refresh', 13)} ${ui.qrRefreshing ? '刷新中…' : '刷新二维码'}</button>`
                : `<button type="button" class="btn btn-ghost btn-sm" id="napcat-hide-qr-btn">收起</button>`}
            </div>
          </div>
          <div class="card-body">
            ${ls.login_error ? `<div class="alert-banner alert-danger mb-12">${icon('alert', 15)} <span>登录提示：${escapeHtml(ls.login_error)}</span></div>` : ''}
            <div class="napcat-login-grid">
              <div class="napcat-qr-box">
                <div class="napcat-qr-svg-wrap">
                  ${qrSvg || `<div class="text-xs text-muted" style="text-align:center;padding:12px;">${online ? '当前账号已在线，无需扫码' : ui.qrLoading ? '正在拉取最新登录二维码…' : '暂未获取到二维码，请点击「刷新二维码」'}</div>`}
                </div>
                <div class="text-xs text-muted" style="text-align:center;">${online ? '登录态正常' : '每 3 秒自动检测扫码结果'}</div>
              </div>
              <div class="stack" style="gap:12px;">
                <div>
                  <div class="text-sm" style="font-weight:600;margin-bottom:6px;">快捷登录历史账号</div>
                  ${quickList.length
                    ? `<div class="napcat-quick-list">
                         ${quickList.map((acc) => {
                           const uin = String(acc.uin || '');
                           const nick = String(acc.nick || '') || `QQ ${uin}`;
                           const av = acc.face_url || (uin ? `https://q1.qlogo.cn/g?b=qq&nk=${encodeURIComponent(uin)}&s=100` : '');
                           const busy = ui.quickLoginBusyUin === uin;
                           return `
                             <div class="napcat-quick-item">
                               ${renderAvatar(av, nick, 'avatar-sm')}
                               <div class="flex-1" style="min-width:0;">
                                 <div class="truncate" style="font-size:13px;font-weight:600;">${escapeHtml(nick)}</div>
                                 <div class="text-xs text-muted mono">${escapeHtml(uin)}</div>
                               </div>
                               <button type="button" class="btn btn-primary btn-sm" data-quick-login="${escapeHtml(uin)}" ${busy || online ? 'disabled' : ''}>${busy ? '登录中…' : '快捷登录'}</button>
                             </div>`;
                         }).join('')}
                       </div>`
                    : `<div class="text-xs text-muted">暂无保存的快捷登录历史账号（扫码成功一次后将自动记录，也可在下方配置自动登录 QQ）。</div>`}
                </div>
                <div class="row wrap">
                  <button type="button" class="btn btn-secondary btn-sm" id="napcat-qr-goto-logs">${icon('terminal', 13)} 查看终端扫码日志</button>
                </div>
              </div>
            </div>
          </div>
        </div>
      `;
    }

    const uptimeLabel = info?.uptime_seconds != null ? formatDuration(info.uptime_seconds) : nativeMode ? '本机原生运行' : info?.container_status || '--';
    const allAdapters = [];
    for (const t of ADAPTER_TYPES) {
      const list = Array.isArray(ob11Net[t.key]) ? ob11Net[t.key] : [];
      list.forEach((item, idx) => allAdapters.push({ typeMeta: t, item, idx }));
    }

    const adaptersMiniHtml = allAdapters.length
      ? `<div class="napcat-kv-list">
           ${allAdapters.map(({ typeMeta, item }) => {
             const ep = item.url || `${item.host || '0.0.0.0'}:${item.port ?? '--'}`;
             return `
               <div class="napcat-kv-row">
                 <div class="row" style="min-width:0;gap:8px;">
                   <span class="status-dot ${item.enable ? 'online' : 'offline'}"></span>
                   <strong class="truncate" style="font-size:12.5px;">${escapeHtml(item.name || typeMeta.short)}</strong>
                   <span class="badge ${typeMeta.badge}">${escapeHtml(typeMeta.short)}</span>
                 </div>
                 <div class="row" style="gap:6px;flex-shrink:0;">
                   <span class="mono text-xs text-secondary">${escapeHtml(ep)}</span>
                   <span class="badge badge-muted mono">${escapeHtml(item.messagePostFormat || 'array')}</span>
                 </div>
               </div>`;
           }).join('')}
         </div>`
      : `<div class="text-xs text-muted" style="padding:10px 0;">未读取到 OneBot 网络适配器配置</div>`;

    const autoLoginVal = ls.auto_login_qq || '';
    const onlineClients = Array.isArray(info?.online_clients) ? info.online_clients : [];
    const coreCfg = info?.napcat_config?.config || {};

    el.innerHTML = `
      <div class="stack">
        ${loginCardHtml}

        <div class="napcat-kpi-grid">
          <div class="napcat-kpi-card">
            <div class="napcat-kpi-label"><span>QQ 协议账号</span><span class="badge ${online ? 'badge-success' : 'badge-warning'}">${online ? '已就绪' : '待登录'}</span></div>
            <div class="napcat-kpi-value" title="${escapeHtml(info?.nickname || '')}">${escapeHtml(info?.nickname || (info?.user_id ? `QQ ${info.user_id}` : '未登录'))}</div>
            <div class="napcat-kpi-sub">
              <span class="mono">${escapeHtml(info?.user_id || '--')}</span>
              ${ls.uid ? `<button type="button" class="napcat-copy-id" data-copy-val="${escapeHtml(ls.uid)}" title="点击复制 UID">· 复制 UID</button>` : ''}
            </div>
          </div>

          <div class="napcat-kpi-card">
            <div class="napcat-kpi-label"><span>版本与运行环境</span><span class="badge badge-violet mono">${escapeHtml(versions.protocol || 'v11')}</span></div>
            <div class="napcat-kpi-value mono">${versions.napcat ? `v${escapeHtml(versions.napcat)}` : '--'}</div>
            <div class="napcat-kpi-sub mono"><span>QQ ${escapeHtml(versions.qq || '--')}</span><span>· ${escapeHtml(uptimeLabel)}</span></div>
          </div>

          <div class="napcat-kpi-card clickable" data-jump-tab="network" title="点击管理 OneBot 11 网络适配器与 NapCat 配置">
            <div class="napcat-kpi-label"><span>OneBot 网络服务</span><span style="color:var(--accent);">${icon('network', 14)}</span></div>
            <div class="napcat-kpi-value mono">${ob11Summary.enabled} / ${ob11Summary.total} <span class="text-xs text-muted" style="font-weight:400;">已启用</span></div>
            <div class="napcat-kpi-sub">
              <span>HTTP ${(ob11Summary.by_type?.httpServers?.enabled || 0)}</span>
              <span>· WS ${(ob11Summary.by_type?.websocketServers?.enabled || 0)}</span>
              <span>· 客户端 ${((ob11Summary.by_type?.httpClients?.enabled || 0) + (ob11Summary.by_type?.websocketClients?.enabled || 0))}</span>
            </div>
          </div>

          <div class="napcat-kpi-card clickable" data-jump-tab="contacts" title="点击查看 QQ 群聊、好友与群聊白名单">
            <div class="napcat-kpi-label"><span>QQ 群聊与好友</span><span style="color:var(--amber);">${icon('users', 14)}</span></div>
            <div class="napcat-kpi-value mono">${groups.length} 群 · ${friends.length} 友</div>
            <div class="napcat-kpi-sub"><span>${wlEnabled ? `白名单生效中 (${wlCount} 群)` : '群聊白名单未启（允许全部）'}</span></div>
          </div>
        </div>

        <div id="napcat-telemetry-slot"></div>

        <div class="napcat-two-col">
          <div class="card">
            <div class="card-header">
              <div>
                <h3 class="card-title">${icon('network', 15)} OneBot 11 网络通道</h3>
                <p class="card-subtitle">当前 NapCat 对外暴露的 HTTP / WebSocket 服务与客户端</p>
              </div>
              <button type="button" class="btn btn-secondary btn-sm" data-jump-tab="network">管理配置 ${icon('chevronRight', 13)}</button>
            </div>
            <div class="card-body">${adaptersMiniHtml}</div>
          </div>

          <div class="card">
            <div class="card-header">
              <div>
                <h3 class="card-title">${icon('sliders', 15)} 账号与运行环境详情</h3>
                <p class="card-subtitle">自动登录账号、日志模式与 NapCat 安全状态</p>
              </div>
              ${online ? `<button type="button" class="btn btn-ghost btn-sm" id="napcat-toggle-qr-btn">${icon('qr', 13)} ${ui.showQrPanelWhenOnline ? '隐藏登录区' : '登录/扫码信息'}</button>` : ''}
            </div>
            <div class="card-body stack" style="gap:10px;">
              <form class="row wrap" id="napcat-autologin-form" style="gap:8px;">
                <label class="text-xs text-secondary" for="napcat-autologin-input" style="white-space:nowrap;font-weight:600;">自动登录 QQ</label>
                <input type="text" class="input" id="napcat-autologin-input" value="${escapeHtml(autoLoginVal)}" placeholder="留空则跟随容器 NAPCAT_QUICK_ACCOUNT" inputmode="numeric" style="flex:1;min-width:160px;height:32px;font-size:12.5px;" ${wMeta.authenticated ? '' : 'disabled'} />
                ${info?.user_id && autoLoginVal !== info.user_id && wMeta.authenticated ? `<button type="button" class="btn btn-ghost btn-sm" id="napcat-autologin-fill">当前 QQ</button>` : ''}
                <button type="submit" class="btn btn-secondary btn-sm" ${ui.autoLoginSaving || !wMeta.authenticated ? 'disabled' : ''}>${icon('save', 12)} ${ui.autoLoginSaving ? '保存中…' : '保存'}</button>
              </form>

              <div class="napcat-kv-list">
                <div class="napcat-kv-row"><span class="napcat-kv-key">运行方式 / 镜像</span><span class="napcat-kv-val mono">${nativeMode ? '本机原生进程' : escapeHtml(info?.image || 'Docker (napcat)')}</span></div>
                <div class="napcat-kv-row"><span class="napcat-kv-key">控制台 / 文件日志</span><span class="napcat-kv-val">控制台 ${coreCfg.consoleLog !== false ? `开 (${escapeHtml(coreCfg.consoleLogLevel || 'info')})` : '关'} · 文件 ${coreCfg.fileLog ? `开 (${escapeHtml(coreCfg.fileLogLevel || 'debug')})` : '关'}</span></div>
                <div class="napcat-kv-row"><span class="napcat-kv-key">NapCat WebUI 监听</span><span class="napcat-kv-val mono">${escapeHtml(wc.host || '::')}:${escapeHtml(String(wc.port || wMeta.port))} · 访问控制 ${escapeHtml(wc.access_control_mode || 'none')} · 2FA ${wc.enable_2fa ? '已开启' : '未开启'}</span></div>
                <div class="napcat-kv-row"><span class="napcat-kv-key">同账号其他在线端</span><span class="napcat-kv-val">${onlineClients.length ? onlineClients.map((c) => escapeHtml(c.device_name || c.device_kind || '设备')).join('、') : '无其他在线客户端'}</span></div>
              </div>

              <div class="row wrap" style="gap:8px;padding-top:2px;">
                <button type="button" class="btn btn-secondary btn-sm" data-jump-tab="logs">${icon('terminal', 13)} 实时日志</button>
                <button type="button" class="btn btn-secondary btn-sm" data-jump-tab="debug">${icon('zap', 13)} 接口调试</button>
                ${wMeta.authenticated ? `<button type="button" class="btn btn-ghost btn-sm" id="napcat-ov-soft-restart" ${ui.processRestarting ? 'disabled' : ''}>${icon('rotate', 13)} 软重启 NapCat</button>` : ''}
              </div>
            </div>
          </div>
        </div>
      </div>
    `;

    updateTelemetryDOM();

    el.querySelectorAll('[data-jump-tab]').forEach((node) => node.addEventListener('click', () => switchTab(node.dataset.jumpTab)));
    el.querySelectorAll('[data-copy-val]').forEach((btn) => btn.addEventListener('click', (e) => { e.stopPropagation(); copyText(btn.dataset.copyVal, '已复制 UID'); }));
    el.querySelector('#napcat-refresh-qr-btn')?.addEventListener('click', handleRefreshQr);
    el.querySelector('#napcat-hide-qr-btn')?.addEventListener('click', () => { ui.showQrPanelWhenOnline = false; renderOverviewPane(); });
    el.querySelector('#napcat-toggle-qr-btn')?.addEventListener('click', async () => {
      ui.showQrPanelWhenOnline = !ui.showQrPanelWhenOnline;
      renderOverviewPane();
      if (ui.showQrPanelWhenOnline) await fetchQrState({ silent: true });
    });
    el.querySelector('#napcat-qr-goto-logs')?.addEventListener('click', () => { ui.logMode = 'docker'; switchTab('logs'); });
    el.querySelectorAll('[data-quick-login]').forEach((btn) => btn.addEventListener('click', () => handleQuickLogin(btn.dataset.quickLogin)));
    el.querySelector('#napcat-autologin-fill')?.addEventListener('click', () => {
      const inp = el.querySelector('#napcat-autologin-input');
      if (inp && info?.user_id) inp.value = String(info.user_id);
    });
    el.querySelector('#napcat-autologin-form')?.addEventListener('submit', async (e) => {
      e.preventDefault();
      const val = String(el.querySelector('#napcat-autologin-input')?.value || '').trim();
      if (val && !/^\d{5,15}$/.test(val)) { showToast('自动登录 QQ 号需为纯数字或留空', 'warning'); return; }
      ui.autoLoginSaving = true;
      renderOverviewPane();
      try {
        const res = await apiPost('/api/napcat/login/auto-account', { uin: val });
        if (destroyed) return;
        showToast(res?.message || '自动登录 QQ 已更新', 'success');
        if (ui.info?.login_state) ui.info.login_state.auto_login_qq = val;
        if (ui.qrData) ui.qrData.auto_login_qq = val;
      } catch (err) {
        showToast(`保存失败: ${err?.message || String(err)}`, 'error');
      } finally {
        ui.autoLoginSaving = false;
        if (!destroyed) renderOverviewPane();
      }
    });
    el.querySelector('#napcat-ov-soft-restart')?.addEventListener('click', handleProcessRestart);
  }

  async function handleRefreshQr() {
    if (ui.qrRefreshing) return;
    ui.qrRefreshing = true;
    renderOverviewPane();
    try {
      const data = await apiPost('/api/napcat/login/refresh-qr', {});
      if (destroyed) return;
      ui.qrData = data || ui.qrData;
      showToast('二维码已刷新，请用手机 QQ 扫码', 'success');
    } catch (err) {
      if (!destroyed) showToast(`刷新二维码失败: ${err?.message || String(err)}`, 'error');
    } finally {
      ui.qrRefreshing = false;
      if (!destroyed) renderOverviewPane();
    }
  }

  async function handleQuickLogin(uin) {
    if (!uin || ui.quickLoginBusyUin) return;
    ui.quickLoginBusyUin = String(uin);
    renderOverviewPane();
    try {
      const res = await apiPost('/api/napcat/login/quick', { uin: String(uin) });
      if (destroyed) return;
      showToast(res?.message || `已触发 QQ ${uin} 快捷登录`, 'success');
      window.setTimeout(() => { if (!destroyed) refreshAll({ notify: false, force: true }); }, 2200);
    } catch (err) {
      if (!destroyed) showToast(`快捷登录失败: ${err?.message || String(err)}`, 'error');
    } finally {
      ui.quickLoginBusyUin = '';
      if (!destroyed) renderOverviewPane();
    }
  }

  async function handleProcessRestart() {
    if (ui.processRestarting) return;
    const ok = await confirmModal({
      title: '软重启 NapCat 进程',
      message: '将通知 NapCat 内部重新启动协议端，期间 QQ 连接会短暂中断几秒，确定继续吗？',
      confirmText: '确认重启',
      cancelText: '取消',
      danger: true,
    });
    if (!ok || destroyed) return;
    ui.processRestarting = true;
    renderQuickActions();
    try {
      const res = await apiPost('/api/napcat/process/restart', {});
      if (destroyed) return;
      showToast(res?.message || '已发送 NapCat 重启指令', 'success');
      await new Promise((r) => window.setTimeout(r, 2000));
      if (!destroyed) await refreshAll({ notify: false, force: true });
    } catch (err) {
      if (!destroyed) showToast(`软重启失败: ${err?.message || String(err)}`, 'error');
    } finally {
      ui.processRestarting = false;
      if (!destroyed) { renderQuickActions(); if (ui.tab === 'overview') renderOverviewPane(); }
    }
  }

  // ---- TAB 2: Network Config + Core Config + Plugins ----

  function deepClone(obj) {
    return JSON.parse(JSON.stringify(obj || {}));
  }

  function defaultAdapterForType(typeKey) {
    if (typeKey === 'httpServers') return { name: 'http-server', enable: true, host: '0.0.0.0', port: 3000, enableCors: true, enableWebsocket: false, messagePostFormat: 'array', token: '', debug: false };
    if (typeKey === 'httpSseServers') return { name: 'http-sse-server', enable: true, host: '0.0.0.0', port: 3005, enableCors: true, enableWebsocket: false, messagePostFormat: 'array', token: '', debug: false, reportSelfMessage: false };
    if (typeKey === 'httpClients') return { name: 'http-client', enable: true, url: 'http://127.0.0.1:8080', messagePostFormat: 'array', reportSelfMessage: false, token: '', debug: false };
    if (typeKey === 'websocketServers') return { name: 'ws-server', enable: true, host: '0.0.0.0', port: 3001, messagePostFormat: 'array', reportSelfMessage: false, token: '', enableForcePushEvent: true, debug: false, heartInterval: 30000 };
    return { name: 'ws-client', enable: true, url: 'ws://127.0.0.1:8080/ws', messagePostFormat: 'array', reportSelfMessage: false, reconnectInterval: 5000, token: '', debug: false, heartInterval: 30000 };
  }

  function isCriticalBotAdapter(typeKey, item) {
    const httpPort = Number(ui.info?.ports?.http || 3000);
    const wsPort = Number(ui.info?.ports?.ws || 3001);
    return (typeKey === 'httpServers' && Number(item?.port) === httpPort) || (typeKey === 'websocketServers' && Number(item?.port) === wsPort);
  }

  async function saveOb11Config(nextConfig, successMsg) {
    if (ui.ob11Saving) return false;
    ui.ob11Saving = true;
    renderNetworkPane();
    try {
      const res = await apiPut('/api/napcat/ob11-config', nextConfig);
      if (destroyed) return false;
      if (ui.info) ui.info.ob11_config = res;
      renderTabsAndCounts();
      showToast(successMsg || res?.message || 'OneBot 网络配置已保存', 'success');
      return true;
    } catch (err) {
      if (!destroyed) showToast(`保存网络配置失败: ${err?.message || String(err)}`, 'error');
      return false;
    } finally {
      ui.ob11Saving = false;
      if (!destroyed) renderNetworkPane();
    }
  }

  function openAdapterEditorModal({ mode, typeKey, index, initialItem }) {
    if (activeModalCleanup) activeModalCleanup();
    const typeMeta = ADAPTER_TYPE_MAP[typeKey] || ADAPTER_TYPES[0];
    const isClient = typeMeta.kind === 'client' || typeMeta.kind === 'ws-client';
    const isWs = typeMeta.kind === 'ws-server' || typeMeta.kind === 'ws-client';
    const isHttpSrv = typeKey === 'httpServers' || typeKey === 'httpSseServers';
    const item = initialItem ? deepClone(initialItem) : defaultAdapterForType(typeKey);

    const root = document.getElementById('modal-root') || document.body;
    const backdrop = document.createElement('div');
    backdrop.className = 'modal-backdrop';
    backdrop.setAttribute('role', 'dialog');
    backdrop.setAttribute('aria-modal', 'true');

    const close = () => { activeModalCleanup = null; backdrop.remove(); };
    activeModalCleanup = close;

    backdrop.innerHTML = `
      <div class="modal-dialog" style="max-width:540px;width:94vw;">
        <div class="modal-header">
          <h3 class="modal-title">${mode === 'add' ? '新增 OneBot 网络适配器' : `编辑适配器 · ${escapeHtml(typeMeta.label)}`}</h3>
          <button type="button" class="btn btn-ghost btn-icon btn-sm" data-adp-cancel aria-label="关闭">${icon('x', 16)}</button>
        </div>
        <form id="adp-editor-form">
          <div class="modal-body stack" style="gap:12px;max-height:70vh;overflow:auto;">
            <div class="napcat-form-grid">
              ${mode === 'add' ? `<div class="napcat-field"><label for="adp-type">适配器类型</label><select class="select" id="adp-type">${ADAPTER_TYPES.map((t) => `<option value="${t.key}" ${t.key === typeKey ? 'selected' : ''}>${escapeHtml(t.label)}</option>`).join('')}</select></div>` : ''}
              <div class="napcat-field"><label for="adp-name">名称</label><input class="input" id="adp-name" value="${escapeHtml(item.name || '')}" required /></div>
              <div class="napcat-field"><label for="adp-format">消息格式</label><select class="select" id="adp-format"><option value="array" ${item.messagePostFormat !== 'string' ? 'selected' : ''}>array（消息段数组，推荐）</option><option value="string" ${item.messagePostFormat === 'string' ? 'selected' : ''}>string（CQ 码字符串）</option></select></div>
              ${isClient
                ? `<div class="napcat-field" style="grid-column:1 / -1;"><label for="adp-url">目标 URL</label><input class="input mono" id="adp-url" value="${escapeHtml(item.url || '')}" required /></div>`
                : `<div class="napcat-field"><label for="adp-host">监听 Host</label><input class="input mono" id="adp-host" value="${escapeHtml(item.host || '0.0.0.0')}" required /></div>
                   <div class="napcat-field"><label for="adp-port">监听端口</label><input class="input mono" id="adp-port" type="number" min="1" max="65535" value="${escapeHtml(String(item.port ?? 3000))}" required /></div>`}
              <div class="napcat-field" style="grid-column:1 / -1;"><label for="adp-token">鉴权 Token</label><input class="input mono" id="adp-token" value="${escapeHtml(item.token || '')}" placeholder="建议与 bot.env 中 NAPCAT_TOKEN 保持一致" /></div>
              ${isWs ? `<div class="napcat-field"><label for="adp-heart">心跳间隔 (ms)</label><input class="input mono" id="adp-heart" type="number" min="1000" step="1000" value="${escapeHtml(String(item.heartInterval ?? 30000))}" /></div>` : ''}
              ${typeKey === 'websocketClients' ? `<div class="napcat-field"><label for="adp-reconnect">重连间隔 (ms)</label><input class="input mono" id="adp-reconnect" type="number" min="1000" step="1000" value="${escapeHtml(String(item.reconnectInterval ?? 5000))}" /></div>` : ''}
            </div>
            <div class="napcat-form-grid">
              <label class="napcat-switch-card"><span class="text-sm">启用此适配器</span><span class="switch"><input type="checkbox" id="adp-enable" ${item.enable !== false ? 'checked' : ''} /><span class="switch-track"></span></span></label>
              <label class="napcat-switch-card"><span class="text-sm">开启 Debug 日志</span><span class="switch"><input type="checkbox" id="adp-debug" ${item.debug ? 'checked' : ''} /><span class="switch-track"></span></span></label>
              ${isHttpSrv
                ? `<label class="napcat-switch-card"><span class="text-sm">启用 CORS</span><span class="switch"><input type="checkbox" id="adp-cors" ${item.enableCors !== false ? 'checked' : ''} /><span class="switch-track"></span></span></label>
                   <label class="napcat-switch-card"><span class="text-sm">同端口启用 WS</span><span class="switch"><input type="checkbox" id="adp-enable-ws" ${item.enableWebsocket ? 'checked' : ''} /><span class="switch-track"></span></span></label>`
                : `<label class="napcat-switch-card"><span class="text-sm">上报自身消息</span><span class="switch"><input type="checkbox" id="adp-report-self" ${item.reportSelfMessage ? 'checked' : ''} /><span class="switch-track"></span></span></label>`}
              ${typeKey === 'websocketServers' ? `<label class="napcat-switch-card"><span class="text-sm">强制推送事件</span><span class="switch"><input type="checkbox" id="adp-force-push" ${item.enableForcePushEvent !== false ? 'checked' : ''} /><span class="switch-track"></span></span></label>` : ''}
            </div>
          </div>
          <div class="modal-footer">
            <button type="button" class="btn btn-secondary" data-adp-cancel>取消</button>
            <button type="submit" class="btn btn-primary">${icon('save', 13)} 保存并应用</button>
          </div>
        </form>
      </div>
    `;

    root.appendChild(backdrop);
    backdrop.querySelectorAll('[data-adp-cancel]').forEach((b) => b.addEventListener('click', close));
    backdrop.addEventListener('click', (e) => { if (e.target === backdrop) close(); });
    if (mode === 'add') {
      backdrop.querySelector('#adp-type')?.addEventListener('change', (e) => {
        const nextType = e.target.value;
        close();
        openAdapterEditorModal({ mode: 'add', typeKey: nextType, index: -1, initialItem: null });
      });
    }
    backdrop.querySelector('#adp-editor-form')?.addEventListener('submit', async (e) => {
      e.preventDefault();
      const updated = deepClone(item);
      updated.name = String(backdrop.querySelector('#adp-name')?.value || '').trim() || typeMeta.short;
      updated.messagePostFormat = backdrop.querySelector('#adp-format')?.value === 'string' ? 'string' : 'array';
      updated.token = String(backdrop.querySelector('#adp-token')?.value || '');
      updated.enable = Boolean(backdrop.querySelector('#adp-enable')?.checked);
      updated.debug = Boolean(backdrop.querySelector('#adp-debug')?.checked);
      if (isClient) {
        updated.url = String(backdrop.querySelector('#adp-url')?.value || '').trim();
        if (!updated.url) { showToast('请输入目标 URL', 'warning'); return; }
      } else {
        updated.host = String(backdrop.querySelector('#adp-host')?.value || '0.0.0.0').trim();
        const p = Number(backdrop.querySelector('#adp-port')?.value);
        if (!Number.isInteger(p) || p < 1 || p > 65535) { showToast('端口必须是 1~65535 的整数', 'warning'); return; }
        updated.port = p;
      }
      if (isHttpSrv) {
        updated.enableCors = Boolean(backdrop.querySelector('#adp-cors')?.checked);
        updated.enableWebsocket = Boolean(backdrop.querySelector('#adp-enable-ws')?.checked);
      } else {
        updated.reportSelfMessage = Boolean(backdrop.querySelector('#adp-report-self')?.checked);
      }
      if (isWs) updated.heartInterval = Math.max(1000, Number(backdrop.querySelector('#adp-heart')?.value) || 30000);
      if (typeKey === 'websocketServers') updated.enableForcePushEvent = Boolean(backdrop.querySelector('#adp-force-push')?.checked);
      if (typeKey === 'websocketClients') updated.reconnectInterval = Math.max(1000, Number(backdrop.querySelector('#adp-reconnect')?.value) || 5000);

      const fullCfg = deepClone(ui.info?.ob11_config?.config || { network: {} });
      if (!fullCfg.network || typeof fullCfg.network !== 'object') fullCfg.network = {};
      if (!Array.isArray(fullCfg.network[typeKey])) fullCfg.network[typeKey] = [];
      if (mode === 'add') fullCfg.network[typeKey].push(updated);
      else if (index >= 0 && index < fullCfg.network[typeKey].length) fullCfg.network[typeKey][index] = updated;
      close();
      await saveOb11Config(fullCfg, mode === 'add' ? `已新增 ${updated.name}` : `已更新 ${updated.name}`);
    });
  }

  function renderNetworkPane() {
    const el = panes.network;
    const ob11Wrap = ui.info?.ob11_config || {};
    const ob11Cfg = ob11Wrap.config || {};
    const ob11Net = ob11Cfg.network || {};
    const coreCfg = ui.info?.napcat_config?.config || {};
    const pluginsWrap = ui.info?.plugins || {};
    const pluginItems = Array.isArray(pluginsWrap.items) ? pluginsWrap.items : [];
    const canEditApi = getWebuiMeta().authenticated;
    const canEditOb11 = canEditApi && Boolean(ui.info?.online);

    const adapters = [];
    for (const t of ADAPTER_TYPES) {
      const list = Array.isArray(ob11Net[t.key]) ? ob11Net[t.key] : [];
      list.forEach((item, idx) => adapters.push({ typeKey: t.key, typeMeta: t, item, idx }));
    }
    const filteredAdapters = ui.adapterFilter === 'all' ? adapters : adapters.filter((a) => a.typeKey === ui.adapterFilter);
    const timeoutCfg = ob11Cfg.timeout || {};
    const logLevels = ['debug', 'info', 'warn', 'error', 'fatal', 'off'];

    el.innerHTML = `
      <div class="stack">
        ${ob11Wrap.source === 'disk' ? `<div class="alert-banner alert-warning">${icon('alert', 15)}<span>当前显示的是本地配置文件（<span class="mono">${escapeHtml(ob11Wrap.file || 'onebot11.json')}</span>）只读视图；NapCat 在线且免登直连后可直接在线编辑并热生效。</span></div>` : ''}

        <div class="card">
          <div class="card-header">
            <div>
              <h3 class="card-title">${icon('network', 15)} OneBot 11 网络配置</h3>
              <p class="card-subtitle">管理 HTTP / SSE / WebSocket 服务端与客户端（对应 NapCat「网络配置」页面，保存即热生效）</p>
            </div>
            <button type="button" class="btn btn-primary btn-sm" id="napcat-adp-add-btn" ${!canEditOb11 || ui.ob11Saving ? 'disabled' : ''}>${icon('plus', 13)} 新增适配器</button>
          </div>
          <div class="card-body stack" style="gap:12px;">
            <div class="filter-pills">
              <button type="button" class="pill-btn ${ui.adapterFilter === 'all' ? 'active' : ''}" data-adp-filter="all">全部 (${adapters.length})</button>
              ${ADAPTER_TYPES.map((t) => `<button type="button" class="pill-btn ${ui.adapterFilter === t.key ? 'active' : ''}" data-adp-filter="${t.key}">${escapeHtml(t.short)} (${(ob11Net[t.key] || []).length})</button>`).join('')}
            </div>

            ${filteredAdapters.length
              ? `<div class="napcat-adapter-grid">
                   ${filteredAdapters.map(({ typeKey, typeMeta, item, idx }) => {
                     const uidKey = `${typeKey}:${idx}`;
                     const on = Boolean(item.enable);
                     const ep = item.url || `${item.host || '0.0.0.0'}:${item.port ?? '--'}`;
                     const tok = String(item.token || '');
                     const revealed = ui.revealedAdapterTokens.has(uidKey);
                     const critical = isCriticalBotAdapter(typeKey, item);
                     return `
                       <div class="napcat-adapter-card${on ? '' : ' is-disabled'}">
                         <div class="napcat-adapter-head">
                           <div style="min-width:0;">
                             <div class="row" style="gap:7px;">
                               <span class="status-dot ${on ? 'online' : 'offline'}"></span>
                               <strong class="truncate" style="font-size:13.5px;">${escapeHtml(item.name || typeMeta.short)}</strong>
                               <span class="badge ${typeMeta.badge}">${escapeHtml(typeMeta.short)}</span>
                               ${critical ? `<span class="badge badge-info">核心通道</span>` : ''}
                             </div>
                             <div class="text-xs text-secondary mono mt-4">${escapeHtml(ep)}</div>
                           </div>
                           <label class="switch"><input type="checkbox" data-adp-toggle="${escapeHtml(uidKey)}" ${on ? 'checked' : ''} ${ui.ob11Saving || !canEditOb11 ? 'disabled' : ''} /><span class="switch-track"></span></label>
                         </div>
                         <div class="napcat-adapter-meta">
                           <span class="badge badge-muted mono">格式: ${escapeHtml(item.messagePostFormat || 'array')}</span>
                           ${item.heartInterval ? `<span class="badge badge-muted mono">心跳: ${escapeHtml(String(item.heartInterval))}ms</span>` : ''}
                           ${item.reconnectInterval ? `<span class="badge badge-muted mono">重连: ${escapeHtml(String(item.reconnectInterval))}ms</span>` : ''}
                           ${item.enableCors ? `<span class="badge badge-muted">CORS</span>` : ''}
                           ${item.enableForcePushEvent ? `<span class="badge badge-muted">强制推送</span>` : ''}
                           ${item.reportSelfMessage ? `<span class="badge badge-muted">上报自身</span>` : ''}
                           ${item.debug ? `<span class="badge badge-warning">Debug</span>` : ''}
                         </div>
                         <div class="napcat-adapter-token-row">
                           <span class="text-muted">Token:</span>
                           <span class="mono flex-1 truncate" style="color:var(--text);">${escapeHtml(revealed ? (tok || '未设置') : maskToken(tok))}</span>
                           <button type="button" class="btn btn-ghost btn-icon btn-sm" data-adp-reveal="${escapeHtml(uidKey)}" ${tok ? '' : 'disabled'}>${revealed ? icon('eyeOff', 12) : icon('eye', 12)}</button>
                           <button type="button" class="btn btn-ghost btn-icon btn-sm" data-adp-copy-tok="${escapeHtml(uidKey)}" ${tok ? '' : 'disabled'}>${icon('copy', 12)}</button>
                         </div>
                         <div class="row-between">
                           <button type="button" class="btn btn-secondary btn-sm" data-adp-edit="${escapeHtml(uidKey)}" ${!canEditOb11 || ui.ob11Saving ? 'disabled' : ''}>${icon('edit', 12)} 编辑</button>
                           <button type="button" class="btn btn-ghost btn-sm" style="color:var(--danger);" data-adp-del="${escapeHtml(uidKey)}" ${!canEditOb11 || ui.ob11Saving ? 'disabled' : ''}>${icon('trash', 12)} 删除</button>
                         </div>
                       </div>`;
                   }).join('')}
                 </div>`
              : `<div class="empty-state" style="padding:26px 14px;"><div class="empty-state-title">当前分类下暂无网络适配器</div></div>`}
          </div>
        </div>

        <div class="napcat-two-col">
          <div class="card">
            <div class="card-header"><div><h3 class="card-title">${icon('sliders', 15)} OneBot 全局选项</h3><p class="card-subtitle">媒体代理、音乐签名与上传下载超时策略</p></div></div>
            <form class="card-body stack" id="napcat-ob11-global-form" style="gap:12px;">
              <div class="napcat-form-grid">
                <div class="napcat-field" style="grid-column:1 / -1;"><label for="ob11-music-url">音乐卡片签名 URL (musicSignUrl)</label><input class="input mono" id="ob11-music-url" value="${escapeHtml(ob11Cfg.musicSignUrl || '')}" ${!canEditOb11 ? 'disabled' : ''} /></div>
                <div class="napcat-field" style="grid-column:1 / -1;"><label for="ob11-img-proxy">图片下载代理 (imageDownloadProxy)</label><input class="input mono" id="ob11-img-proxy" value="${escapeHtml(ob11Cfg.imageDownloadProxy || '')}" ${!canEditOb11 ? 'disabled' : ''} /></div>
                <label class="napcat-switch-card"><span class="text-sm">本地文件转 URL</span><span class="switch"><input type="checkbox" id="ob11-local-url" ${ob11Cfg.enableLocalFile2Url ? 'checked' : ''} ${!canEditOb11 ? 'disabled' : ''} /><span class="switch-track"></span></span></label>
                <label class="napcat-switch-card"><span class="text-sm">解析合并转发</span><span class="switch"><input type="checkbox" id="ob11-parse-mult" ${ob11Cfg.parseMultMsg ? 'checked' : ''} ${!canEditOb11 ? 'disabled' : ''} /><span class="switch-track"></span></span></label>
                <div class="napcat-field"><label for="ob11-base-to">基础超时 (ms)</label><input class="input mono" id="ob11-base-to" type="number" min="1000" value="${escapeHtml(String(timeoutCfg.baseTimeout ?? 10000))}" ${!canEditOb11 ? 'disabled' : ''} /></div>
                <div class="napcat-field"><label for="ob11-max-to">最大超时 (ms)</label><input class="input mono" id="ob11-max-to" type="number" min="1000" value="${escapeHtml(String(timeoutCfg.maxTimeout ?? 1800000))}" ${!canEditOb11 ? 'disabled' : ''} /></div>
                <div class="napcat-field"><label for="ob11-up-kb">上传速率基准 (KB/s)</label><input class="input mono" id="ob11-up-kb" type="number" min="1" value="${escapeHtml(String(timeoutCfg.uploadSpeedKBps ?? 256))}" ${!canEditOb11 ? 'disabled' : ''} /></div>
                <div class="napcat-field"><label for="ob11-dn-kb">下载速率基准 (KB/s)</label><input class="input mono" id="ob11-dn-kb" type="number" min="1" value="${escapeHtml(String(timeoutCfg.downloadSpeedKBps ?? 256))}" ${!canEditOb11 ? 'disabled' : ''} /></div>
              </div>
              <div class="row" style="justify-content:flex-end;"><button type="submit" class="btn btn-primary btn-sm" ${!canEditOb11 || ui.ob11Saving ? 'disabled' : ''}>${icon('save', 13)} ${ui.ob11Saving ? '保存中…' : '保存 OneBot 全局选项'}</button></div>
            </form>
          </div>

          <div class="card">
            <div class="card-header"><div><h3 class="card-title">${icon('shield', 15)} NapCat 核心与日志配置</h3><p class="card-subtitle">对应 NapCat「系统配置」，调整日志输出、时间同步与协议包设置</p></div></div>
            <form class="card-body stack" id="napcat-core-cfg-form" style="gap:12px;">
              <div class="napcat-form-grid">
                <label class="napcat-switch-card"><span class="text-sm">控制台日志 (consoleLog)</span><span class="switch"><input type="checkbox" id="core-console-log" ${coreCfg.consoleLog !== false ? 'checked' : ''} ${!canEditApi ? 'disabled' : ''} /><span class="switch-track"></span></span></label>
                <div class="napcat-field"><label for="core-console-lvl">控制台日志级别</label><select class="select" id="core-console-lvl" ${!canEditApi ? 'disabled' : ''}>${logLevels.map((lv) => `<option value="${lv}" ${(coreCfg.consoleLogLevel || 'info') === lv ? 'selected' : ''}>${lv}</option>`).join('')}</select></div>
                <label class="napcat-switch-card"><span class="text-sm">写入日志文件 (fileLog)</span><span class="switch"><input type="checkbox" id="core-file-log" ${coreCfg.fileLog ? 'checked' : ''} ${!canEditApi ? 'disabled' : ''} /><span class="switch-track"></span></span></label>
                <div class="napcat-field"><label for="core-file-lvl">文件日志级别</label><select class="select" id="core-file-lvl" ${!canEditApi ? 'disabled' : ''}>${logLevels.map((lv) => `<option value="${lv}" ${(coreCfg.fileLogLevel || 'debug') === lv ? 'selected' : ''}>${lv}</option>`).join('')}</select></div>
                <label class="napcat-switch-card"><span class="text-sm">自动校准时间 (autoTimeSync)</span><span class="switch"><input type="checkbox" id="core-time-sync" ${coreCfg.autoTimeSync !== false ? 'checked' : ''} ${!canEditApi ? 'disabled' : ''} /><span class="switch-track"></span></span></label>
                <div class="napcat-field"><label for="core-o3-mode">o3HookMode</label><input class="input mono" id="core-o3-mode" type="number" value="${escapeHtml(String(coreCfg.o3HookMode ?? 0))}" ${!canEditApi ? 'disabled' : ''} /></div>
                <div class="napcat-field"><label for="core-pkt-backend">Packet 后端</label><input class="input mono" id="core-pkt-backend" value="${escapeHtml(coreCfg.packetBackend || 'auto')}" ${!canEditApi ? 'disabled' : ''} /></div>
                <div class="napcat-field"><label for="core-pkt-server">Packet 服务器</label><input class="input mono" id="core-pkt-server" value="${escapeHtml(coreCfg.packetServer || '')}" ${!canEditApi ? 'disabled' : ''} /></div>
              </div>
              <div class="row" style="justify-content:flex-end;"><button type="submit" class="btn btn-primary btn-sm" ${!canEditApi || ui.coreSaving ? 'disabled' : ''}>${icon('save', 13)} ${ui.coreSaving ? '保存中…' : '保存核心配置'}</button></div>
            </form>
          </div>
        </div>

        <div class="card">
          <div class="card-header"><div><h3 class="card-title">${icon('plug', 15)} NapCat 插件列表 <span class="badge badge-muted">${pluginItems.length}</span></h3><p class="card-subtitle">查看已安装的 NapCat 插件并直接启停</p></div></div>
          <div class="card-body">
            ${pluginItems.length
              ? `<div class="napcat-entity-list">${pluginItems.map((p) => {
                  const pid = String(p.id || p.name || '');
                  const active = p.status === 'active' || p.enable === true;
                  return `<div class="napcat-entity-card"><div class="napcat-entity-info"><div class="napcat-entity-name">${escapeHtml(p.name || pid)}${p.version ? `<span class="badge badge-muted mono">v${escapeHtml(String(p.version))}</span>` : ''}</div><div class="napcat-entity-sub"><span class="badge ${active ? 'badge-success' : 'badge-muted'}">${escapeHtml(String(p.status || (active ? 'active' : 'disabled')))}</span>${p.author ? `<span>作者: ${escapeHtml(String(p.author))}</span>` : ''}</div></div><label class="switch"><input type="checkbox" data-plugin-toggle="${escapeHtml(pid)}" ${active ? 'checked' : ''} ${ui.pluginBusyId === pid || !canEditApi ? 'disabled' : ''} /><span class="switch-track"></span></label></div>`;
                }).join('')}</div>`
              : `<div class="text-xs text-muted">${pluginsWrap.plugin_manager_not_found ? '当前 NapCat 实例未启用插件管理器或未安装任何扩展插件。' : '当前尚未安装任何 NapCat 插件。'}</div>`}
          </div>
        </div>
      </div>
    `;

    el.querySelectorAll('[data-adp-filter]').forEach((btn) => btn.addEventListener('click', () => { ui.adapterFilter = btn.dataset.adpFilter || 'all'; renderNetworkPane(); }));
    el.querySelector('#napcat-adp-add-btn')?.addEventListener('click', () => openAdapterEditorModal({ mode: 'add', typeKey: ui.adapterFilter !== 'all' ? ui.adapterFilter : 'httpServers', index: -1, initialItem: null }));

    const findAdapter = (uidKey) => {
      const [typeKey, idxStr] = String(uidKey || '').split(':');
      const idx = Number(idxStr);
      return { typeKey, idx, item: (Array.isArray(ob11Net[typeKey]) ? ob11Net[typeKey] : [])[idx] };
    };

    el.querySelectorAll('[data-adp-reveal]').forEach((btn) => btn.addEventListener('click', () => {
      const k = btn.dataset.adpReveal;
      if (ui.revealedAdapterTokens.has(k)) ui.revealedAdapterTokens.delete(k);
      else ui.revealedAdapterTokens.add(k);
      renderNetworkPane();
    }));
    el.querySelectorAll('[data-adp-copy-tok]').forEach((btn) => btn.addEventListener('click', () => copyText(findAdapter(btn.dataset.adpCopyTok).item?.token || '', '已复制适配器 Token')));
    el.querySelectorAll('[data-adp-edit]').forEach((btn) => btn.addEventListener('click', () => {
      const { typeKey, idx, item } = findAdapter(btn.dataset.adpEdit);
      if (item) openAdapterEditorModal({ mode: 'edit', typeKey, index: idx, initialItem: item });
    }));
    el.querySelectorAll('[data-adp-toggle]').forEach((chk) => chk.addEventListener('change', async () => {
      const { typeKey, idx, item } = findAdapter(chk.dataset.adpToggle);
      if (!item) return;
      const nextEnable = Boolean(chk.checked);
      if (!nextEnable && isCriticalBotAdapter(typeKey, item)) {
        const ok = await confirmModal({ title: '停用核心机器人通道？', message: `适配器「${item.name || typeKey}」正在为哈基米提供 OneBot 通信（:${item.port}），停用后机器人将无法收发消息，确定吗？`, confirmText: '确认停用', cancelText: '取消', danger: true });
        if (!ok || destroyed) { renderNetworkPane(); return; }
      }
      const nextCfg = deepClone(ob11Cfg);
      nextCfg.network[typeKey][idx].enable = nextEnable;
      await saveOb11Config(nextCfg, `${item.name || typeKey} 已${nextEnable ? '启用' : '停用'}`);
    }));
    el.querySelectorAll('[data-adp-del]').forEach((btn) => btn.addEventListener('click', async () => {
      const { typeKey, idx, item } = findAdapter(btn.dataset.adpDel);
      if (!item) return;
      const ok = await confirmModal({ title: `删除适配器「${item.name || typeKey}」？`, message: isCriticalBotAdapter(typeKey, item) ? `警告：该适配器为核心通信端口（:${item.port}），删除后 ws-guard / cc-connect 将断开！确定继续吗？` : '删除后该网络通道将立即停止监听，确定继续吗？', confirmText: '确认删除', cancelText: '取消', danger: true });
      if (!ok || destroyed) return;
      const nextCfg = deepClone(ob11Cfg);
      nextCfg.network[typeKey].splice(idx, 1);
      await saveOb11Config(nextCfg, `已删除适配器 ${item.name || typeKey}`);
    }));
    el.querySelector('#napcat-ob11-global-form')?.addEventListener('submit', async (e) => {
      e.preventDefault();
      const nextCfg = deepClone(ob11Cfg);
      nextCfg.musicSignUrl = String(el.querySelector('#ob11-music-url')?.value || '').trim();
      nextCfg.imageDownloadProxy = String(el.querySelector('#ob11-img-proxy')?.value || '').trim();
      nextCfg.enableLocalFile2Url = Boolean(el.querySelector('#ob11-local-url')?.checked);
      nextCfg.parseMultMsg = Boolean(el.querySelector('#ob11-parse-mult')?.checked);
      nextCfg.timeout = {
        baseTimeout: Math.max(1000, Number(el.querySelector('#ob11-base-to')?.value) || 10000),
        maxTimeout: Math.max(1000, Number(el.querySelector('#ob11-max-to')?.value) || 1800000),
        uploadSpeedKBps: Math.max(1, Number(el.querySelector('#ob11-up-kb')?.value) || 256),
        downloadSpeedKBps: Math.max(1, Number(el.querySelector('#ob11-dn-kb')?.value) || 256),
      };
      await saveOb11Config(nextCfg, 'OneBot 全局选项已保存');
    });
    el.querySelector('#napcat-core-cfg-form')?.addEventListener('submit', async (e) => {
      e.preventDefault();
      if (ui.coreSaving) return;
      ui.coreSaving = true;
      renderNetworkPane();
      try {
        const res = await apiPut('/api/napcat/core-config', {
          consoleLog: Boolean(el.querySelector('#core-console-log')?.checked),
          consoleLogLevel: String(el.querySelector('#core-console-lvl')?.value || 'info'),
          fileLog: Boolean(el.querySelector('#core-file-log')?.checked),
          fileLogLevel: String(el.querySelector('#core-file-lvl')?.value || 'debug'),
          autoTimeSync: Boolean(el.querySelector('#core-time-sync')?.checked),
          o3HookMode: Number(el.querySelector('#core-o3-mode')?.value) || 0,
          packetBackend: String(el.querySelector('#core-pkt-backend')?.value || 'auto').trim(),
          packetServer: String(el.querySelector('#core-pkt-server')?.value || '').trim(),
        });
        if (destroyed) return;
        if (ui.info) ui.info.napcat_config = res;
        showToast(res?.message || 'NapCat 核心配置已保存', 'success');
      } catch (err) {
        if (!destroyed) showToast(`保存核心配置失败: ${err?.message || String(err)}`, 'error');
      } finally {
        ui.coreSaving = false;
        if (!destroyed) renderNetworkPane();
      }
    });
    el.querySelectorAll('[data-plugin-toggle]').forEach((chk) => chk.addEventListener('change', async () => {
      const pid = chk.dataset.pluginToggle;
      ui.pluginBusyId = pid;
      renderNetworkPane();
      try {
        const res = await apiPost('/api/napcat/plugins/status', { id: pid, enable: Boolean(chk.checked) });
        if (destroyed) return;
        if (res?.plugins && ui.info) ui.info.plugins = res.plugins;
        showToast(res?.message || '插件状态已更新', 'success');
      } catch (err) {
        if (!destroyed) showToast(`切换插件失败: ${err?.message || String(err)}`, 'error');
      } finally {
        ui.pluginBusyId = '';
        if (!destroyed) renderNetworkPane();
      }
    }));
  }

  // ---- TAB 3: Unified Logs ----

  function stopRtLogStream() {
    if (logEventSource) {
      try { logEventSource.close(); } catch (_) {}
      logEventSource = null;
    }
    ui.rtConnected = false;
  }

  function syncRtLogStream() {
    const shouldRun = !destroyed && ui.tab === 'logs' && ui.logMode === 'realtime' && !ui.rtPaused && getWebuiMeta().authenticated;
    if (!shouldRun) { stopRtLogStream(); updateRtLogBadge(); return; }
    if (logEventSource) return;

    try {
      const es = new EventSource('/api/napcat/log-stream');
      logEventSource = es;
      es.onopen = () => { if (!destroyed && logEventSource === es) { ui.rtConnected = true; ui.rtError = ''; updateRtLogBadge(); } };
      es.onmessage = (ev) => {
        if (destroyed || logEventSource !== es || ui.rtPaused) return;
        try {
          ui.rtConnected = true;
          ui.rtLogs.push(JSON.parse(ev.data));
          if (ui.rtLogs.length > MAX_RT_LOGS) ui.rtLogs.shift();
          renderRtLogConsole();
          updateRtLogBadge();
        } catch (_) {}
      };
      es.addEventListener('log_error', (ev) => {
        if (destroyed || logEventSource !== es) return;
        try { ui.rtError = String(JSON.parse(ev.data)?.error || '实时日志流连接异常'); } catch (_) { ui.rtError = '实时日志流连接异常'; }
        ui.rtConnected = false;
        updateRtLogBadge();
      });
      es.onerror = () => { if (!destroyed && logEventSource === es) { ui.rtConnected = false; updateRtLogBadge(); } };
    } catch (err) {
      ui.rtError = err?.message || String(err);
    }
  }

  function updateRtLogBadge() {
    const badge = panes.logs.querySelector('#napcat-rt-status-badge');
    if (!badge) return;
    if (ui.rtPaused) { badge.className = 'badge badge-warning'; badge.textContent = '已暂停'; }
    else if (ui.rtConnected) { badge.className = 'badge badge-success'; badge.innerHTML = '<span class="status-dot online pulse"></span>SSE 实时推送中'; }
    else if (ui.rtError) { badge.className = 'badge badge-danger'; badge.textContent = '连接异常'; badge.title = ui.rtError; }
    else { badge.className = 'badge badge-muted'; badge.textContent = '连接中…'; }
  }

  function renderRtLogConsole({ scrollBottom = false } = {}) {
    const box = panes.logs.querySelector('#napcat-rt-console');
    const meta = panes.logs.querySelector('#napcat-rt-meta');
    if (!box) return;
    const q = ui.rtSearch.trim().toLowerCase();
    const filtered = ui.rtLogs.filter((entry) => (ui.rtLevel === 'ALL' || entry.level === ui.rtLevel) && (!q || String(entry.text || '').toLowerCase().includes(q)));
    if (meta) meta.textContent = q || ui.rtLevel !== 'ALL' ? `匹配 ${filtered.length} / 缓冲 ${ui.rtLogs.length} 条` : `实时缓冲 ${ui.rtLogs.length} 条`;
    if (!filtered.length) {
      box.innerHTML = `<div class="text-muted">${!getWebuiMeta().authenticated ? '未直连 NapCat WebUI API，请切换到「容器日志」。' : ui.rtLogs.length ? '没有符合筛选条件的日志' : '已订阅 NapCat 实时日志流，有新事件时将立即显示（也可以切换到「容器日志」回看历史输出）…'}</div>`;
      return;
    }
    const wasNearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 64;
    box.innerHTML = filtered.map((e) => {
      const cls = e.level === 'ERROR' || e.level === 'FATAL' ? 'napcat-log-row is-error' : e.level === 'WARN' ? 'napcat-log-row is-warn' : 'napcat-log-row';
      return `<div class="${cls}"><span class="text-muted">[${escapeHtml(e.ts || '--')}]</span> <span class="badge badge-muted" style="font-size:10px;padding:0 5px;">${escapeHtml(e.level || 'INFO')}</span> ${escapeHtml(e.text || '')}</div>`;
    }).join('');
    if (scrollBottom || wasNearBottom) box.scrollTop = box.scrollHeight;
  }

  async function loadLogFilesList() {
    if (destroyed) return;
    try {
      const res = await apiGet('/api/napcat/log-files');
      if (destroyed) return;
      ui.logFiles = Array.isArray(res?.files) ? res.files : [];
      ui.logFilesLoaded = true;
      if (!ui.selectedLogFile && ui.logFiles.length) ui.selectedLogFile = ui.logFiles[ui.logFiles.length - 1];
      renderLogsPane();
      if (ui.selectedLogFile) await loadSingleLogFile(ui.selectedLogFile);
    } catch (err) {
      if (!destroyed) showToast(`获取历史日志列表失败: ${err?.message || String(err)}`, 'error');
    }
  }

  async function loadSingleLogFile(logId) {
    if (!logId || destroyed) return;
    ui.selectedLogFile = logId;
    ui.logFileLoading = true;
    renderLogFileContent();
    try {
      const res = await apiGet(`/api/napcat/log-file?id=${encodeURIComponent(logId)}`);
      if (destroyed) return;
      ui.logFileLines = Array.isArray(res?.lines) ? res.lines : [];
      ui.logFileContent = String(res?.text || '');
    } catch (err) {
      if (!destroyed) { ui.logFileLines = []; ui.logFileContent = ''; showToast(`读取日志文件失败: ${err?.message || String(err)}`, 'error'); }
    } finally {
      ui.logFileLoading = false;
      if (!destroyed) renderLogFileContent();
    }
  }

  function renderLogFileContent() {
    const box = panes.logs.querySelector('#napcat-file-console');
    const meta = panes.logs.querySelector('#napcat-file-meta');
    if (!box) return;
    if (ui.logFileLoading) { box.innerHTML = '<div class="text-muted">正在加载日志文件内容…</div>'; return; }
    if (!ui.logFiles.length) {
      const fileLogOn = Boolean(ui.info?.napcat_config?.config?.fileLog);
      box.innerHTML = `<div class="empty-state" style="padding:32px 16px;"><div class="empty-state-title">暂无历史日志文件</div><div class="text-xs text-muted">${fileLogOn ? 'NapCat 尚未生成历史日志文件，稍后刷新查看。' : '当前 NapCat「写入日志文件 (fileLog)」处于关闭状态，开启后才会持久化日志文件。'}</div>${!fileLogOn && getWebuiMeta().authenticated ? `<button type="button" class="btn btn-primary btn-sm mt-8" id="napcat-enable-filelog-btn">立即开启文件日志 (fileLog)</button>` : ''}</div>`;
      box.querySelector('#napcat-enable-filelog-btn')?.addEventListener('click', async () => {
        try {
          const res = await apiPut('/api/napcat/core-config', { fileLog: true });
          if (ui.info) ui.info.napcat_config = res;
          showToast('已开启 NapCat 文件日志', 'success');
          loadLogFilesList();
        } catch (err) { showToast(`开启失败: ${err?.message || String(err)}`, 'error'); }
      });
      if (meta) meta.textContent = '0 个文件';
      return;
    }
    const q = ui.logFileSearch.trim().toLowerCase();
    const lines = q ? ui.logFileLines.filter((l) => l.toLowerCase().includes(q)) : ui.logFileLines;
    if (meta) meta.textContent = q ? `匹配 ${lines.length} / ${ui.logFileLines.length} 行` : `${ui.selectedLogFile} · 共 ${ui.logFileLines.length} 行`;
    box.innerHTML = lines.length ? lines.map((l) => `<div class="napcat-log-row${/\b(error|err|fatal)\b/i.test(l) ? ' is-error' : /\bwarn/i.test(l) ? ' is-warn' : ''}">${escapeHtml(l || ' ')}</div>`).join('') : '<div class="text-muted">没有匹配的日志行</div>';
  }

  function renderLogsPane() {
    const el = panes.logs;
    const mode = ui.logMode;
    const wAuth = getWebuiMeta().authenticated;

    let bodyHtml = '';
    if (mode === 'realtime') {
      bodyHtml = `
        <div class="row-between wrap" style="gap:10px;">
          <div class="row wrap">
            <span class="badge badge-muted" id="napcat-rt-status-badge">连接中…</span>
            <div class="filter-pills">${['ALL', 'INFO', 'WARN', 'ERROR', 'DEBUG'].map((lv) => `<button type="button" class="pill-btn ${ui.rtLevel === lv ? 'active' : ''}" data-rt-lvl="${lv}">${lv}</button>`).join('')}</div>
          </div>
          <div class="row wrap">
            <button type="button" class="btn btn-secondary btn-sm" id="napcat-rt-pause-btn" ${wAuth ? '' : 'disabled'}>${ui.rtPaused ? `${icon('play', 12)} 恢复` : `${icon('pause', 12)} 暂停`}</button>
            <button type="button" class="btn btn-secondary btn-sm" id="napcat-rt-clear-btn">清空</button>
            <button type="button" class="btn btn-secondary btn-sm" id="napcat-rt-copy-btn">${icon('copy', 12)} 复制</button>
          </div>
        </div>
        <div class="row-between wrap">
          <div class="search-box" style="max-width:360px;">${icon('search', 14)}<input type="search" id="napcat-rt-search" value="${escapeHtml(ui.rtSearch)}" placeholder="筛选实时日志关键词…" /></div>
          <div class="row text-xs text-muted"><span id="napcat-rt-meta">准备就绪</span><button type="button" class="btn btn-ghost btn-sm" id="napcat-rt-bottom-btn">${icon('arrowDown', 13)} 到底部</button></div>
        </div>
        <div class="napcat-log-console" id="napcat-rt-console" role="log" aria-live="polite"></div>
      `;
    } else if (mode === 'docker') {
      bodyHtml = `
        <div class="row-between wrap" style="gap:8px;">
          <div class="search-box" style="max-width:340px;">${icon('search', 14)}<input type="search" id="napcat-log-search" value="${escapeHtml(ui.logsSearch)}" placeholder="筛选日志关键词（如 QR / login / error / ws）…" /></div>
          <div class="row wrap">
            <select class="select" id="napcat-log-limit" style="width:auto;height:31px;font-size:12px;"><option value="100" ${ui.logsLineLimit === 100 ? 'selected' : ''}>最近 100 行</option><option value="180" ${ui.logsLineLimit === 180 ? 'selected' : ''}>最近 180 行</option><option value="350" ${ui.logsLineLimit === 350 ? 'selected' : ''}>最近 350 行</option></select>
            <label class="switch text-xs"><input type="checkbox" id="napcat-log-auto" ${ui.logsAutoRefresh ? 'checked' : ''} /><span class="switch-track"></span><span>自动刷新 (5s)</span></label>
            <button type="button" class="pill-btn ${ui.logsQrMode ? 'active' : ''}" id="napcat-log-qr-toggle">扫码紧凑排版</button>
            <button type="button" class="btn btn-secondary btn-sm" id="napcat-log-refresh-btn">${icon('refresh', 13)} 刷新</button>
            <button type="button" class="btn btn-secondary btn-sm" id="napcat-log-copy-btn">${icon('copy', 13)} 复制</button>
          </div>
        </div>
        <div id="napcat-qr-banner" style="display:none;"></div>
        <div class="row-between text-xs text-muted"><span id="napcat-log-meta">等待加载…</span><button type="button" class="btn btn-ghost btn-sm" id="napcat-log-bottom-btn">${icon('arrowDown', 13)} 到底部</button></div>
        <div class="napcat-log-console${ui.logsQrMode ? ' qr-mode' : ''}" id="napcat-log-console" role="log" aria-live="polite"></div>
      `;
    } else {
      bodyHtml = `
        <div class="row-between wrap" style="gap:8px;">
          <div class="row wrap">
            <select class="select mono" id="napcat-file-select" style="width:auto;min-width:220px;height:32px;font-size:12px;" ${ui.logFiles.length ? '' : 'disabled'}>${ui.logFiles.length ? ui.logFiles.map((f) => `<option value="${escapeHtml(f)}" ${f === ui.selectedLogFile ? 'selected' : ''}>${escapeHtml(f)}</option>`).join('') : '<option value="">暂无历史日志文件</option>'}</select>
            <button type="button" class="btn btn-secondary btn-sm" id="napcat-file-refresh-btn">${icon('refresh', 13)} 刷新列表</button>
            <button type="button" class="btn btn-secondary btn-sm" id="napcat-file-copy-btn" ${ui.logFileContent ? '' : 'disabled'}>${icon('copy', 13)} 复制内容</button>
          </div>
          <div class="search-box" style="max-width:300px;">${icon('search', 14)}<input type="search" id="napcat-file-search" value="${escapeHtml(ui.logFileSearch)}" placeholder="搜索当前日志文件…" /></div>
        </div>
        <div class="row-between text-xs text-muted"><span id="napcat-file-meta"></span></div>
        <div class="napcat-log-console" id="napcat-file-console" role="log"></div>
      `;
    }

    el.innerHTML = `
      <div class="card">
        <div class="card-header">
          <div>
            <h3 class="card-title">${icon('terminal', 15)} 猫猫日志与容器输出</h3>
            <p class="card-subtitle">支持 NapCat 原生实时日志流（Docker / 本机原生通用）、Docker 扫码日志与历史日志文件</p>
          </div>
          <div class="row wrap">
            <div class="napcat-log-mode-bar" role="tablist">
              <button type="button" class="napcat-log-mode-btn ${mode === 'realtime' ? 'active' : ''}" data-log-mode="realtime">实时日志</button>
              <button type="button" class="napcat-log-mode-btn ${mode === 'docker' ? 'active' : ''}" data-log-mode="docker">容器日志</button>
              <button type="button" class="napcat-log-mode-btn ${mode === 'files' ? 'active' : ''}" data-log-mode="files">历史文件 <span class="text-muted">${ui.logFiles.length}</span></button>
            </div>
            <button type="button" class="btn btn-ghost btn-sm" id="napcat-goto-all-logs" title="在全局日志中心聚合查看">全局日志 ${icon('chevronRight', 12)}</button>
          </div>
        </div>
        <div class="card-body stack" style="gap:10px;">${bodyHtml}</div>
      </div>
    `;

    el.querySelector('#napcat-goto-all-logs')?.addEventListener('click', () => ctx.navigate('logs', { source: 'napcat' }));
    el.querySelectorAll('[data-log-mode]').forEach((btn) => btn.addEventListener('click', () => {
      const nextMode = btn.dataset.logMode || 'realtime';
      if (nextMode === ui.logMode) return;
      ui.logMode = nextMode;
      renderLogsPane();
      syncRtLogStream();
      syncLogAutoPoll();
      if (ui.logMode === 'docker' && !ui.logsLoaded) loadLogs({ scrollBottom: true });
      else if (ui.logMode === 'files' && !ui.logFilesLoaded) loadLogFilesList();
    }));

    if (mode === 'realtime') {
      updateRtLogBadge();
      renderRtLogConsole({ scrollBottom: true });
      el.querySelectorAll('[data-rt-lvl]').forEach((btn) => btn.addEventListener('click', () => {
        ui.rtLevel = btn.dataset.rtLvl || 'ALL';
        el.querySelectorAll('[data-rt-lvl]').forEach((b) => b.classList.toggle('active', b.dataset.rtLvl === ui.rtLevel));
        renderRtLogConsole({ scrollBottom: false });
      }));
      el.querySelector('#napcat-rt-pause-btn')?.addEventListener('click', () => { ui.rtPaused = !ui.rtPaused; renderLogsPane(); syncRtLogStream(); });
      el.querySelector('#napcat-rt-clear-btn')?.addEventListener('click', () => { ui.rtLogs = []; renderRtLogConsole(); });
      el.querySelector('#napcat-rt-copy-btn')?.addEventListener('click', () => copyText(ui.rtLogs.map((x) => `[${x.ts}] [${x.level}] ${x.text}`).join('\n'), '已复制 NapCat 实时日志'));
      el.querySelector('#napcat-rt-search')?.addEventListener('input', (e) => { ui.rtSearch = e.target.value || ''; renderRtLogConsole({ scrollBottom: false }); });
      el.querySelector('#napcat-rt-bottom-btn')?.addEventListener('click', () => { const b = el.querySelector('#napcat-rt-console'); if (b) b.scrollTop = b.scrollHeight; });
    } else if (mode === 'docker') {
      renderDockerLogsContent({ scrollBottom: false });
      el.querySelector('#napcat-log-limit')?.addEventListener('change', (e) => { ui.logsLineLimit = Number(e.target.value) || 180; loadLogs({ scrollBottom: true }); });
      el.querySelector('#napcat-log-auto')?.addEventListener('change', (e) => { ui.logsAutoRefresh = Boolean(e.target.checked); syncLogAutoPoll(); });
      el.querySelector('#napcat-log-qr-toggle')?.addEventListener('click', (e) => {
        ui.logsQrMode = !ui.logsQrMode;
        e.currentTarget.classList.toggle('active', ui.logsQrMode);
        el.querySelector('#napcat-log-console')?.classList.toggle('qr-mode', ui.logsQrMode);
      });
      el.querySelector('#napcat-log-refresh-btn')?.addEventListener('click', () => loadLogs({ scrollBottom: true, notify: true }));
      el.querySelector('#napcat-log-copy-btn')?.addEventListener('click', () => copyText(stripAnsi(ui.logsRaw || ui.logsLines.join('\n')), '已复制 NapCat 容器日志'));
      el.querySelector('#napcat-log-search')?.addEventListener('input', (e) => { ui.logsSearch = e.target.value || ''; renderDockerLogsContent({ scrollBottom: false }); });
      el.querySelector('#napcat-log-bottom-btn')?.addEventListener('click', () => { const b = el.querySelector('#napcat-log-console'); if (b) b.scrollTop = b.scrollHeight; });
    } else {
      renderLogFileContent();
      el.querySelector('#napcat-file-select')?.addEventListener('change', (e) => loadSingleLogFile(e.target.value));
      el.querySelector('#napcat-file-refresh-btn')?.addEventListener('click', loadLogFilesList);
      el.querySelector('#napcat-file-copy-btn')?.addEventListener('click', () => copyText(ui.logFileContent, '已复制历史日志内容'));
      el.querySelector('#napcat-file-search')?.addEventListener('input', (e) => { ui.logFileSearch = e.target.value || ''; renderLogFileContent(); });
    }
  }

  function renderDockerLogsContent({ scrollBottom = false } = {}) {
    const box = panes.logs.querySelector('#napcat-log-console');
    const metaEl = panes.logs.querySelector('#napcat-log-meta');
    const qrBannerEl = panes.logs.querySelector('#napcat-qr-banner');
    if (!box) return;

    if (qrBannerEl) {
      if (ui.hasQrHint) {
        qrBannerEl.style.display = '';
        qrBannerEl.innerHTML = `<div class="alert-banner alert-warning"><span style="color:var(--amber);display:inline-flex;">${icon('alert', 15)}</span><div class="flex-1"><strong>检测到扫码登录提示</strong>：建议回到「概览」直接扫描高清二维码，或开启「扫码紧凑排版」。</div><button type="button" class="btn btn-secondary btn-sm" id="napcat-log-goto-qr">前往高清扫码</button></div>`;
        qrBannerEl.querySelector('#napcat-log-goto-qr')?.addEventListener('click', () => switchTab('overview'));
      } else { qrBannerEl.style.display = 'none'; qrBannerEl.innerHTML = ''; }
    }

    if (ui.logsLoading && !ui.logsLoaded) { box.innerHTML = '<div class="text-muted">正在读取 NapCat 容器日志…</div>'; return; }
    if (ui.logsError) {
      box.innerHTML = isNativeMode(ui.info)
        ? `<div class="text-muted" style="padding:12px 4px;">当前为本机原生 NapCat 模式，无 Docker 容器日志。请切换到上方的「实时日志」。</div>`
        : `<div class="napcat-log-row is-error">加载日志失败：${escapeHtml(ui.logsError)}</div>`;
      if (metaEl) metaEl.textContent = isNativeMode(ui.info) ? '本机原生模式' : '加载失败';
      return;
    }

    const rawLines = ui.logsLines.length ? ui.logsLines : ui.logsRaw ? String(ui.logsRaw).split(/\r?\n/) : [];
    const cleaned = rawLines.map((l) => stripAnsi(l));
    const q = ui.logsSearch.trim().toLowerCase();
    const filtered = q ? cleaned.filter((l) => l.toLowerCase().includes(q)) : cleaned;

    if (metaEl) {
      const updated = ui.logsUpdatedAt ? ` · 更新于 ${formatTime(ui.logsUpdatedAt)}` : '';
      metaEl.textContent = q ? `匹配 ${filtered.length} / ${cleaned.length} 行${updated}` : `共 ${cleaned.length} 行${updated}`;
    }
    if (!filtered.length) { box.innerHTML = `<div class="text-muted">${q ? '没有匹配该关键词的日志行' : '暂无容器日志输出'}</div>`; return; }

    const wasNearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 64;
    box.innerHTML = filtered.map((line) => {
      const cls = hasQrGlyphs(line) ? 'napcat-log-row is-qr' : /\b(error|err|fatal|exception|fail|failed)\b/i.test(line) ? 'napcat-log-row is-error' : /\b(warn|warning)\b/i.test(line) ? 'napcat-log-row is-warn' : 'napcat-log-row';
      return `<div class="${cls}">${escapeHtml(line || ' ')}</div>`;
    }).join('');
    if (scrollBottom || wasNearBottom) box.scrollTop = box.scrollHeight;
  }

  function syncLogAutoPoll() {
    if (logPollTimer) { window.clearInterval(logPollTimer); logPollTimer = null; }
    if (ui.logsAutoRefresh && ui.tab === 'logs' && ui.logMode === 'docker' && !destroyed) {
      logPollTimer = window.setInterval(() => {
        if (!destroyed && !document.hidden && ui.tab === 'logs' && ui.logMode === 'docker') loadLogs({ scrollBottom: false, silent: true });
      }, 5000);
    }
  }

  async function loadLogs({ scrollBottom = false, silent = false, notify = false } = {}) {
    if (destroyed) return;
    ui.logsLoading = true;
    ui.logsError = '';
    if (!ui.logsLoaded) renderDockerLogsContent();
    try {
      const data = await apiGet(`/api/napcat/logs?lines=${encodeURIComponent(String(ui.logsLineLimit))}`);
      if (destroyed) return;
      ui.logsLines = Array.isArray(data?.lines) ? data.lines : [];
      ui.logsRaw = typeof data?.raw === 'string' ? data.raw : '';
      ui.hasQrHint = Boolean(data?.has_qr_hint);
      if (data?.ok === false && ui.logsLines.length === 0 && data?.error) ui.logsError = String(data.error);
      if (ui.hasQrHint && !ui.logsLoaded) ui.logsQrMode = true;
      ui.logsLoaded = true;
      ui.logsUpdatedAt = Date.now();
      renderTabsAndCounts();
      renderDockerLogsContent({ scrollBottom });
      if (notify) showToast('容器日志已更新', 'success');
    } catch (err) {
      if (destroyed) return;
      ui.logsError = err?.message || String(err);
      renderDockerLogsContent();
      if (!silent) showToast(`读取容器日志失败: ${ui.logsError}`, 'error');
    } finally {
      ui.logsLoading = false;
    }
  }

  // ---- TAB 4: Contacts & Group Whitelist ----

  function renderContactsShell() {
    panes.contacts.innerHTML = `
      <div class="stack">
        <div id="napcat-contacts-banner"></div>
        <div class="card">
          <div class="card-body row-between wrap" style="padding:12px 16px;">
            <div class="filter-pills" id="napcat-contacts-filter-pills">
              <button type="button" class="pill-btn" data-cfilter="all">全部 (0)</button>
              <button type="button" class="pill-btn" data-cfilter="groups">群聊 (0)</button>
              <button type="button" class="pill-btn" data-cfilter="friends">好友 (0)</button>
            </div>
            <div class="row wrap" style="flex:1;justify-content:flex-end;min-width:220px;">
              <div class="search-box" style="max-width:340px;">${icon('search', 14)}<input type="search" id="napcat-contacts-search" value="${escapeHtml(ui.contactsSearch)}" placeholder="搜索群名、群号、好友备注/昵称或 QQ 号…" /></div>
              <button type="button" class="btn btn-secondary btn-sm" id="napcat-contacts-refresh">${icon('refresh', 13)} 同步通讯录</button>
            </div>
          </div>
        </div>
        <div id="napcat-contacts-results"></div>
      </div>
    `;
    panes.contacts.querySelectorAll('[data-cfilter]').forEach((btn) => btn.addEventListener('click', () => { ui.contactsFilter = btn.dataset.cfilter || 'all'; renderContactsResults(); }));
    panes.contacts.querySelector('#napcat-contacts-search')?.addEventListener('input', (e) => { ui.contactsSearch = e.target.value || ''; renderContactsResults(); });
    panes.contacts.querySelector('#napcat-contacts-refresh')?.addEventListener('click', () => refreshAll({ reloadIframe: false, notify: true, force: true }));
  }

  function renderContactsResults() {
    if (!panes.contacts.querySelector('#napcat-contacts-results')) renderContactsShell();
    const groups = Array.isArray(ui.info?.groups) ? ui.info.groups : [];
    const friends = Array.isArray(ui.info?.friends) ? ui.info.friends : [];
    const running = isContainerRunning(ui.info);
    const online = Boolean(ui.info?.online);

    const setPill = (sel, text, active) => {
      const p = panes.contacts.querySelector(sel);
      if (p) { p.textContent = text; p.classList.toggle('active', active); }
    };
    setPill('[data-cfilter="all"]', `全部 (${groups.length + friends.length})`, ui.contactsFilter === 'all');
    setPill('[data-cfilter="groups"]', `群聊 (${groups.length})`, ui.contactsFilter === 'groups');
    setPill('[data-cfilter="friends"]', `好友 (${friends.length})`, ui.contactsFilter === 'friends');

    const bannerEl = panes.contacts.querySelector('#napcat-contacts-banner');
    if (bannerEl) {
      if (!online && !groups.length && !friends.length) {
        bannerEl.innerHTML = `<div class="alert-banner alert-warning"><span style="color:var(--amber);display:inline-flex;">${icon('alert', 15)}</span><div class="flex-1">${running ? 'NapCat 正在运行，但 QQ 尚未完成登录，暂时无法读取群聊与好友通讯录。' : 'NapCat 当前处于离线状态，启动 NapCat 并登录 QQ 后可浏览全部群聊与好友。'}</div><button type="button" class="btn btn-secondary btn-sm" id="napcat-contacts-goto-overview">前往扫码登录</button></div>`;
        bannerEl.querySelector('#napcat-contacts-goto-overview')?.addEventListener('click', () => switchTab('overview'));
      } else { bannerEl.innerHTML = ''; }
    }

    const q = ui.contactsSearch.trim().toLowerCase();
    const filteredGroups = q ? groups.filter((g) => String(g.group_id || '').toLowerCase().includes(q) || String(g.group_name || '').toLowerCase().includes(q)) : groups;
    const filteredFriends = q ? friends.filter((f) => String(f.user_id || '').toLowerCase().includes(q) || String(f.nickname || '').toLowerCase().includes(q) || String(f.remark || '').toLowerCase().includes(q)) : friends;

    const showGroups = ui.contactsFilter === 'all' || ui.contactsFilter === 'groups';
    const showFriends = ui.contactsFilter === 'all' || ui.contactsFilter === 'friends';
    const wlEnabled = Boolean(ui.whitelist?.enabled);
    const wlIds = Array.isArray(ui.whitelist?.group_ids) ? ui.whitelist.group_ids.map((x) => String(x)) : [];
    const wlSet = new Set(wlIds);
    const joinedSet = new Set(groups.map((g) => String(g.group_id || '')).filter(Boolean));
    const allowedJoined = wlEnabled ? groups.filter((g) => wlSet.has(String(g.group_id || ''))).length : groups.length;
    const extraWlIds = wlIds.filter((gid) => !joinedSet.has(gid));

    const resultsEl = panes.contacts.querySelector('#napcat-contacts-results');
    if (!resultsEl) return;

    const renderGroupCards = () => !filteredGroups.length
      ? `<div class="empty-state" style="padding:28px 16px;"><div class="empty-state-title">${q ? '没有匹配的群聊' : '暂无群聊数据'}</div></div>`
      : `<div class="napcat-entity-list">${filteredGroups.map((g) => {
          const gid = String(g.group_id || '');
          const gname = String(g.group_name || `群 ${gid}`);
          const av = gid ? `https://p.qlogo.cn/gh/${encodeURIComponent(gid)}/${encodeURIComponent(gid)}/100` : '';
          const memText = typeof g.member_count === 'number' ? (g.max_member_count > 0 ? `${g.member_count} / ${g.max_member_count} 人` : `${g.member_count} 人`) : '';
          const isAllowed = !wlEnabled || wlSet.has(gid);
          return `
            <div class="napcat-entity-card${isAllowed ? '' : ' is-muted'}">
              ${renderAvatar(av, gname)}
              <div class="napcat-entity-info">
                <div class="napcat-entity-name" title="${escapeHtml(gname)}">${escapeHtml(gname)}</div>
                <div class="napcat-entity-sub">
                  <button type="button" class="napcat-copy-id" data-copy-id="${escapeHtml(gid)}">#${escapeHtml(gid)}</button>
                  ${memText ? `<span class="badge badge-muted">${escapeHtml(memText)}</span>` : ''}
                  ${wlEnabled ? (isAllowed ? `<span class="badge badge-success">白名单放行</span>` : `<span class="badge badge-warning">已静音拦截</span>`) : ''}
                </div>
              </div>
              <div class="napcat-entity-actions">
                <label class="napcat-wl-group-toggle">
                  <span class="napcat-wl-toggle-label ${isAllowed ? 'on' : 'off'}">${isAllowed ? '允许' : '静音'}</span>
                  <span class="switch"><input type="checkbox" data-toggle-group-wl="${escapeHtml(gid)}" ${isAllowed ? 'checked' : ''} ${ui.whitelistSaving ? 'disabled' : ''} /><span class="switch-track"></span></span>
                </label>
                <button type="button" class="btn btn-secondary btn-sm" data-open-group-session="${escapeHtml(gid)}">${icon('chat', 12)} 会话</button>
              </div>
            </div>`;
        }).join('')}</div>`;

    const renderFriendCards = () => !filteredFriends.length
      ? `<div class="empty-state" style="padding:28px 16px;"><div class="empty-state-title">${q ? '没有匹配的好友' : '暂无好友数据'}</div></div>`
      : `<div class="napcat-entity-list">${filteredFriends.map((f) => {
          const uid = String(f.user_id || '');
          const remark = String(f.remark || '').trim();
          const nick = String(f.nickname || '').trim();
          const displayName = remark || nick || `QQ ${uid}`;
          const subNick = remark && nick && remark !== nick ? nick : '';
          const av = uid ? `https://q1.qlogo.cn/g?b=qq&nk=${encodeURIComponent(uid)}&s=100` : '';
          return `
            <div class="napcat-entity-card">
              ${renderAvatar(av, displayName)}
              <div class="napcat-entity-info">
                <div class="napcat-entity-name" title="${escapeHtml(displayName)}">${escapeHtml(displayName)}</div>
                <div class="napcat-entity-sub">
                  <button type="button" class="napcat-copy-id" data-copy-id="${escapeHtml(uid)}">${escapeHtml(uid)}</button>
                  ${subNick ? `<span class="truncate">· ${escapeHtml(subNick)}</span>` : ''}
                </div>
              </div>
              <div class="napcat-entity-actions">
                <button type="button" class="btn btn-secondary btn-sm" data-open-user-memory="${escapeHtml(uid)}">${icon('brain', 12)} 记忆</button>
                <button type="button" class="btn btn-ghost btn-sm" data-open-dm-session="${escapeHtml(uid)}">${icon('chat', 12)} 私聊</button>
              </div>
            </div>`;
        }).join('')}</div>`;

    const wlBar = `
      <div class="napcat-wl-toolbar">
        <div class="napcat-wl-left">
          <label class="napcat-wl-master">
            <span class="switch"><input type="checkbox" id="napcat-wl-master-toggle" ${wlEnabled ? 'checked' : ''} ${ui.whitelistSaving ? 'disabled' : ''} /><span class="switch-track"></span></span>
            <span class="napcat-wl-master-text">聊群白名单开关</span>
          </label>
          <span class="badge ${wlEnabled ? (allowedJoined > 0 ? 'badge-success' : 'badge-warning') : 'badge-muted'}">${wlEnabled ? `白名单生效中 · 允许 ${allowedJoined} / ${groups.length} 群` : `白名单未启 · 允许全部 ${groups.length} 个群`}</span>
        </div>
        <div class="napcat-wl-actions">
          ${wlEnabled
            ? `<button type="button" class="btn btn-ghost btn-sm" id="napcat-wl-allow-all" ${ui.whitelistSaving ? 'disabled' : ''}>全部允许</button>
               <button type="button" class="btn btn-ghost btn-sm" id="napcat-wl-mute-all" ${ui.whitelistSaving ? 'disabled' : ''}>全部静音</button>
               <button type="button" class="btn btn-secondary btn-sm" id="napcat-wl-add-btn" ${ui.whitelistSaving ? 'disabled' : ''}>${icon('plus', 12)} 手动加群号</button>`
            : `<span class="text-xs text-muted">关闭任意群开关也会自动开启白名单</span>`}
        </div>
      </div>
      ${ui.wsGuardNeedsRestart && wlEnabled ? `<div class="napcat-wl-stale-banner"><span style="color:var(--amber);display:inline-flex;">${icon('alert', 14)}</span><span class="flex-1">ws-guard 正在运行旧代码，需无感重启一次即可启用白名单热更新。</span><button type="button" class="btn btn-amber btn-sm" id="napcat-wl-restart-guard" ${ui.wsGuardRestarting ? 'disabled' : ''}>${icon('rotate', 12)} ${ui.wsGuardRestarting ? '重启中...' : '立即重启 ws-guard'}</button></div>` : wlEnabled ? `<div class="napcat-wl-live-hint"><span class="status-dot online" style="width:6px;height:6px;"></span><span>热更新已就绪 · 切换任意群开关 1 秒内实时生效（私聊不受影响）</span></div>` : ''}
      ${ui.showManualAddGroup ? `<form class="napcat-wl-add-form" id="napcat-wl-add-form"><input type="text" class="input" id="napcat-wl-add-input" placeholder="输入纯数字 QQ 群号" inputmode="numeric" /><button type="submit" class="btn btn-primary btn-sm">加入白名单</button><button type="button" class="btn btn-ghost btn-sm" id="napcat-wl-add-cancel">取消</button></form>` : ''}
      ${wlEnabled && extraWlIds.length ? `<div class="napcat-wl-extra"><span class="text-xs text-muted">未在当前列表中的白名单群号：</span>${extraWlIds.map((gid) => `<span class="chip"><span class="mono">#${escapeHtml(gid)}</span><button type="button" class="chip-remove" data-remove-extra-wl="${escapeHtml(gid)}">×</button></span>`).join('')}</div>` : ''}
    `;

    resultsEl.innerHTML = `
      <div class="napcat-dir-columns${ui.contactsFilter !== 'all' ? ' single-col' : ''}">
        ${showGroups ? `<div class="card"><div class="card-header"><h3 class="card-title">${icon('users', 15)} QQ 群聊 <span class="badge badge-violet">${filteredGroups.length}</span></h3><span class="text-xs text-muted">逐群开关聊天白名单或查看会话</span></div><div class="card-body">${wlBar}${renderGroupCards()}</div></div>` : ''}
        ${showFriends ? `<div class="card"><div class="card-header"><h3 class="card-title">${icon('cat', 15)} QQ 好友 <span class="badge badge-amber">${filteredFriends.length}</span></h3><span class="text-xs text-muted">一键跳转长期记忆或私聊记录</span></div><div class="card-body">${renderFriendCards()}</div></div>` : ''}
      </div>
    `;

    resultsEl.querySelector('#napcat-wl-master-toggle')?.addEventListener('change', (e) => {
      const nextEnabled = Boolean(e.target.checked);
      const nextIds = nextEnabled && wlIds.length === 0 && groups.length > 0 ? groups.map((g) => String(g.group_id || '')).filter(Boolean) : [...wlIds];
      saveWhitelist(nextEnabled, nextIds, nextEnabled ? `已开启群聊白名单（放行 ${nextIds.length} 群）` : '已关闭群聊白名单');
    });
    resultsEl.querySelectorAll('[data-toggle-group-wl]').forEach((input) => input.addEventListener('change', () => {
      const gid = String(input.dataset.toggleGroupWl || '');
      if (!gid) return;
      const wantAllow = Boolean(input.checked);
      if (!wlEnabled && !wantAllow) {
        const nextSet = new Set(groups.map((g) => String(g.group_id || '')).filter(Boolean));
        nextSet.delete(gid);
        saveWhitelist(true, Array.from(nextSet), `已自动开启白名单并静音群 #${gid}`);
        return;
      }
      const nextSet = new Set(wlIds);
      if (wantAllow) nextSet.add(gid); else nextSet.delete(gid);
      saveWhitelist(wlEnabled, Array.from(nextSet), wantAllow ? `已允许群 #${gid} 聊天` : `已静音群 #${gid}`);
    }));
    resultsEl.querySelector('#napcat-wl-allow-all')?.addEventListener('click', () => saveWhitelist(true, Array.from(new Set([...wlIds, ...groups.map((g) => String(g.group_id || '')).filter(Boolean)])), '已放行全部群聊'));
    resultsEl.querySelector('#napcat-wl-mute-all')?.addEventListener('click', () => saveWhitelist(true, [], '已清空群聊白名单（所有群静音）'));
    resultsEl.querySelector('#napcat-wl-add-btn')?.addEventListener('click', () => { ui.showManualAddGroup = !ui.showManualAddGroup; renderContactsResults(); if (ui.showManualAddGroup) resultsEl.querySelector('#napcat-wl-add-input')?.focus(); });
    resultsEl.querySelector('#napcat-wl-add-cancel')?.addEventListener('click', () => { ui.showManualAddGroup = false; renderContactsResults(); });
    resultsEl.querySelector('#napcat-wl-add-form')?.addEventListener('submit', (e) => {
      e.preventDefault();
      const raw = String(resultsEl.querySelector('#napcat-wl-add-input')?.value || '').trim();
      if (!/^\d{5,15}$/.test(raw)) { showToast('请输入有效的纯数字 QQ 群号（5~15 位）', 'warning'); return; }
      ui.showManualAddGroup = false;
      saveWhitelist(true, Array.from(new Set([...wlIds, raw])), `已将群 #${raw} 加入白名单`);
    });
    resultsEl.querySelectorAll('[data-remove-extra-wl]').forEach((b) => b.addEventListener('click', () => saveWhitelist(true, wlIds.filter((x) => x !== b.dataset.removeExtraWl), `已移出群号 #${b.dataset.removeExtraWl}`)));
    resultsEl.querySelector('#napcat-wl-restart-guard')?.addEventListener('click', restartWsGuardForWhitelist);
    resultsEl.querySelectorAll('[data-copy-id]').forEach((b) => b.addEventListener('click', () => copyText(b.dataset.copyId || '', `已复制号码: ${b.dataset.copyId}`)));
    resultsEl.querySelectorAll('[data-open-group-session]').forEach((b) => b.addEventListener('click', () => ctx.navigate('sessions', { sessionKey: `qq:g:${b.dataset.openGroupSession}` })));
    resultsEl.querySelectorAll('[data-open-user-memory]').forEach((b) => b.addEventListener('click', () => ctx.navigate('memory', { userId: b.dataset.openUserMemory })));
    resultsEl.querySelectorAll('[data-open-dm-session]').forEach((b) => b.addEventListener('click', () => ctx.navigate('sessions', { sessionKey: `qq:${b.dataset.openDmSession}` })));
  }

  // ---- TAB 5: OneBot v11 API Debugger ----

  async function loadDebugSchemas() {
    if (ui.debugSchemasLoaded || destroyed) return;
    try {
      const res = await apiGet('/api/napcat/debug/schemas');
      if (destroyed) return;
      ui.debugSchemas = Array.isArray(res?.actions) ? res.actions : [];
      ui.debugSchemasLoaded = true;
      if (ui.tab === 'debug') renderDebugPane();
    } catch (_) { ui.debugSchemasLoaded = true; }
  }

  function renderDebugPane() {
    const el = panes.debug;
    const firstGroupId = ui.info?.groups?.[0]?.group_id ? Number(ui.info.groups[0].group_id) : 0;
    const firstFriendId = ui.info?.friends?.[0]?.user_id ? Number(ui.info.friends[0].user_id) : Number(ui.info?.user_id || 0);
    const sf = ui.debugSchemaFilter.trim().toLowerCase();
    const matched = sf ? ui.debugSchemas.filter((a) => a.action.toLowerCase().includes(sf) || String(a.description || '').toLowerCase().includes(sf)) : ui.debugSchemas;
    const currentSchema = ui.debugSchemas.find((a) => a.action === ui.debugAction);
    const res = ui.debugResult;

    el.innerHTML = `
      <div class="napcat-debug-grid">
        <div class="card">
          <div class="card-header"><div><h3 class="card-title">${icon('zap', 15)} OneBot v11 接口调试</h3><p class="card-subtitle">免登直接向 NapCat 发送 OneBot HTTP / Debug 指令，查看原始返回</p></div></div>
          <div class="card-body stack" style="gap:12px;">
            <div>
              <div class="text-xs text-muted mb-6">常用接口快捷选择</div>
              <div class="napcat-preset-chips">${DEBUG_PRESETS.map((p) => `<button type="button" class="pill-btn ${ui.debugAction === p.action ? 'active' : ''}" data-dbg-preset="${escapeHtml(p.action)}">${escapeHtml(p.label)}</button>`).join('')}</div>
            </div>
            ${ui.debugSchemas.length ? `
              <div class="napcat-field">
                <label for="dbg-schema-search">全部 NapCat 接口检索 (${ui.debugSchemas.length} 个)</label>
                <div class="row" style="gap:6px;">
                  <input class="input" id="dbg-schema-search" value="${escapeHtml(ui.debugSchemaFilter)}" placeholder="搜索接口名或中文说明…" style="flex:1;height:32px;font-size:12.5px;" />
                  <select class="select mono" id="dbg-schema-select" style="flex:1.2;min-width:0;height:32px;font-size:12px;">
                    <option value="">— 选择接口 (${matched.length}) —</option>
                    ${matched.slice(0, 120).map((a) => `<option value="${escapeHtml(a.action)}" ${a.action === ui.debugAction ? 'selected' : ''}>${escapeHtml(a.action)}${a.description ? ` · ${escapeHtml(a.description.slice(0, 28))}` : ''}</option>`).join('')}
                  </select>
                </div>
              </div>` : ''}
            <form class="stack" id="dbg-call-form" style="gap:10px;">
              <div class="napcat-field">
                <label for="dbg-action">接口动作名 (Action)</label>
                <input class="input mono" id="dbg-action" value="${escapeHtml(ui.debugAction)}" required />
                ${currentSchema?.description ? `<div class="text-xs text-muted">${escapeHtml(currentSchema.description)}</div>` : ''}
              </div>
              <div class="napcat-field">
                <div class="row-between"><label for="dbg-params">请求参数 (JSON)</label><button type="button" class="btn btn-ghost btn-sm" id="dbg-format-json" style="height:24px;font-size:11px;">格式化 JSON</button></div>
                <textarea class="textarea mono" id="dbg-params" rows="7" style="font-size:12px;line-height:1.5;">${escapeHtml(ui.debugParamsText)}</textarea>
                ${currentSchema?.params?.length ? `<div class="text-xs text-muted">参数字段：${currentSchema.params.slice(0, 8).map((p) => `<span class="mono">${escapeHtml(p.name)}${p.required ? '*' : ''}</span>`).join('、')}</div>` : ''}
              </div>
              <div class="row-between">
                <span class="text-xs text-muted">快捷键：⌘/Ctrl + Enter 发送</span>
                <button type="submit" class="btn btn-primary btn-sm" ${ui.debugRunning ? 'disabled' : ''}>${icon('send', 13)} ${ui.debugRunning ? '请求中…' : '发送请求'}</button>
              </div>
            </form>
          </div>
        </div>

        <div class="card">
          <div class="card-header">
            <div class="row wrap">
              <h3 class="card-title">${icon('terminal', 15)} 响应结果</h3>
              ${res ? `<span class="badge ${res.ok ? 'badge-success' : 'badge-danger'}">${res.ok ? 'OK' : 'FAILED'} · ${escapeHtml(String(res.http_status || '--'))}</span><span class="badge badge-muted mono">${escapeHtml(String(res.elapsed_ms ?? '--'))} ms</span><span class="badge badge-muted mono">${escapeHtml(res.via || '')}</span>` : ''}
            </div>
            <button type="button" class="btn btn-secondary btn-sm" id="dbg-copy-res" ${res ? '' : 'disabled'}>${icon('copy', 12)} 复制响应</button>
          </div>
          <div class="card-body">
            ${res
              ? `${res.error ? `<div class="alert-banner alert-danger mb-10">${icon('alert', 14)} <span>${escapeHtml(res.error)}</span></div>` : ''}<pre class="napcat-json-pre">${escapeHtml(JSON.stringify(res.response !== undefined && res.response !== null ? res.response : res, null, 2))}</pre>`
              : `<div class="empty-state" style="padding:48px 16px;"><div class="empty-state-icon">${icon('zap', 20)}</div><div class="empty-state-title">尚未发送测试请求</div><div class="text-xs text-muted">选择左侧常用接口或自定义 Action，点击「发送请求」即可实时查看 NapCat 返回结果</div></div>`}
          </div>
        </div>
      </div>
    `;

    el.querySelectorAll('[data-dbg-preset]').forEach((btn) => btn.addEventListener('click', () => {
      const preset = DEBUG_PRESETS.find((p) => p.action === btn.dataset.dbgPreset);
      if (!preset) return;
      ui.debugAction = preset.action;
      const params = deepClone(preset.params);
      if ('group_id' in params && firstGroupId) params.group_id = firstGroupId;
      if ('user_id' in params && firstFriendId) params.user_id = firstFriendId;
      ui.debugParamsText = JSON.stringify(params, null, 2);
      renderDebugPane();
    }));
    el.querySelector('#dbg-schema-search')?.addEventListener('input', (e) => {
      ui.debugSchemaFilter = e.target.value || '';
      const sel = el.querySelector('#dbg-schema-select');
      if (!sel) return;
      const q = ui.debugSchemaFilter.trim().toLowerCase();
      const list = q ? ui.debugSchemas.filter((a) => a.action.toLowerCase().includes(q) || String(a.description || '').toLowerCase().includes(q)) : ui.debugSchemas;
      sel.innerHTML = `<option value="">— 选择接口 (${list.length}) —</option>` + list.slice(0, 120).map((a) => `<option value="${escapeHtml(a.action)}" ${a.action === ui.debugAction ? 'selected' : ''}>${escapeHtml(a.action)}${a.description ? ` · ${escapeHtml(a.description.slice(0, 28))}` : ''}</option>`).join('');
    });
    el.querySelector('#dbg-schema-select')?.addEventListener('change', (e) => {
      const act = e.target.value;
      if (!act) return;
      ui.debugAction = act;
      const schema = ui.debugSchemas.find((a) => a.action === act);
      const tpl = {};
      if (schema?.params) {
        for (const p of schema.params) {
          if (!p.required) continue;
          if (p.name === 'group_id' && firstGroupId) tpl[p.name] = firstGroupId;
          else if (p.name === 'user_id' && firstFriendId) tpl[p.name] = firstFriendId;
          else if (p.type === 'number' || p.type === 'integer') tpl[p.name] = 0;
          else if (p.type === 'boolean') tpl[p.name] = false;
          else if (p.type === 'array') tpl[p.name] = [];
          else if (p.type === 'object') tpl[p.name] = {};
          else tpl[p.name] = '';
        }
      }
      ui.debugParamsText = JSON.stringify(tpl, null, 2);
      renderDebugPane();
    });
    el.querySelector('#dbg-action')?.addEventListener('input', (e) => { ui.debugAction = e.target.value; });
    const paramsTa = el.querySelector('#dbg-params');
    paramsTa?.addEventListener('input', (e) => { ui.debugParamsText = e.target.value; });
    paramsTa?.addEventListener('keydown', (e) => { if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') { e.preventDefault(); runDebugCall(); } });
    el.querySelector('#dbg-format-json')?.addEventListener('click', () => {
      try {
        ui.debugParamsText = JSON.stringify(ui.debugParamsText.trim() ? JSON.parse(ui.debugParamsText) : {}, null, 2);
        if (paramsTa) paramsTa.value = ui.debugParamsText;
      } catch (err) { showToast(`JSON 格式有误: ${err.message}`, 'warning'); }
    });
    el.querySelector('#dbg-call-form')?.addEventListener('submit', (e) => { e.preventDefault(); runDebugCall(); });
    el.querySelector('#dbg-copy-res')?.addEventListener('click', () => {
      if (!ui.debugResult) return;
      copyText(JSON.stringify(ui.debugResult.response !== undefined && ui.debugResult.response !== null ? ui.debugResult.response : ui.debugResult, null, 2), '已复制接口响应 JSON');
    });
  }

  async function runDebugCall() {
    if (ui.debugRunning) return;
    const action = String(ui.debugAction || '').trim();
    if (!action) { showToast('请输入 OneBot 接口名', 'warning'); return; }
    let params = {};
    try {
      params = ui.debugParamsText.trim() ? JSON.parse(ui.debugParamsText) : {};
      if (!params || typeof params !== 'object' || Array.isArray(params)) throw new Error('params 必须是 JSON 对象');
    } catch (err) { showToast(`请求参数 JSON 无效: ${err.message}`, 'warning'); return; }

    ui.debugRunning = true;
    renderDebugPane();
    try {
      const res = await apiPost('/api/napcat/debug/call', { action, params });
      if (destroyed) return;
      ui.debugResult = res;
      if (res?.ok) showToast(`${action} 调用成功 (${res.elapsed_ms} ms)`, 'success');
      else showToast(`${action} 返回错误: ${res?.error || 'failed'}`, 'warning');
    } catch (err) {
      if (!destroyed) { ui.debugResult = { ok: false, error: err?.message || String(err), response: null }; showToast(`调用失败: ${err?.message || String(err)}`, 'error'); }
    } finally {
      ui.debugRunning = false;
      if (!destroyed) renderDebugPane();
    }
  }

  // ---- TAB 6: Optional Lazy-Loaded Native NapCat Iframe ----

  function reloadIframe() {
    const iframe = panes.webui.querySelector('#napcat-webui-iframe');
    const loadingMask = panes.webui.querySelector('#napcat-iframe-loading');
    const { url } = getWebuiMeta();
    if (!iframe) return;
    if (loadingMask) loadingMask.classList.remove('hidden');
    ui.iframeLoaded = false;
    try { const u = new URL(url); u.searchParams.set('_t', String(Date.now())); iframe.src = u.toString(); } catch (_) { iframe.src = url; }
    if (iframeLoadFallbackTimer) window.clearTimeout(iframeLoadFallbackTimer);
    iframeLoadFallbackTimer = window.setTimeout(() => { if (!destroyed && loadingMask) loadingMask.classList.add('hidden'); }, 4000);
  }

  function renderWebuiPane({ forceRebuild = false } = {}) {
    if (ui.tab !== 'webui' && !ui.iframeSrcMounted) return;
    const running = isContainerRunning(ui.info);
    const { port, url, hasToken, tokenMasked, disabled } = getWebuiMeta();

    if (!running || disabled) {
      ui.iframeSrcMounted = '';
      if (ui.fullscreen) setFullscreen(false);
      panes.webui.innerHTML = `<div class="napcat-frame-shell"><div class="napcat-offline-card"><div class="empty-state-icon">${icon('server', 24)}</div><div class="empty-state-title">${disabled ? 'NapCat 原生 WebUI 已禁用' : 'NapCat 服务当前未运行'}</div></div></div>`;
      return;
    }

    const existingIframe = panes.webui.querySelector('#napcat-webui-iframe');
    if (existingIframe && ui.iframeSrcMounted === url && !forceRebuild) {
      const shell = panes.webui.querySelector('#napcat-frame-shell');
      if (shell) shell.classList.toggle('napcat-fullscreen', ui.fullscreen);
      updateTokenDisplays();
      return;
    }

    ui.iframeSrcMounted = url;
    ui.iframeLoaded = false;
    const tokenDisplay = !hasToken ? '未检测到' : (tokenMasked || '已配置（已脱敏）');

    panes.webui.innerHTML = `
      <div class="napcat-frame-shell${ui.fullscreen ? ' napcat-fullscreen' : ''}" id="napcat-frame-shell">
        <div class="napcat-fs-bar">
          <div class="row wrap">
            <strong style="font-size:14px;">NapCat 原生面板</strong>
            <div class="napcat-token-capsule" title="为保障本机安全，接口仅返回脱敏后的 Token；如需手动登录原生面板，请直接查看本地 napcat/config/webui.json">
              <span class="napcat-token-label">Token</span>
              <span class="napcat-token-value" data-token-display>${escapeHtml(tokenDisplay)}</span>
            </div>
          </div>
          <div class="row wrap">
            <button type="button" class="btn btn-secondary btn-sm" id="napcat-fs-reload-btn">${icon('refresh', 13)} 重载</button>
            <a class="btn btn-secondary btn-sm" href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer">${icon('external', 13)} :${escapeHtml(String(port))}</a>
            <button type="button" class="btn btn-primary btn-sm" id="napcat-fs-exit-btn">${icon('minimize', 13)} 退出全屏 (Esc)</button>
          </div>
        </div>
        <div class="napcat-hint-strip${hintDismissed ? ' hidden' : ''}" id="napcat-hint-strip">
          <div class="row" style="min-width:0;gap:7px;"><span style="color:var(--accent);display:inline-flex;">${icon('info', 14)}</span><span>常规监控、扫码、网络配置与日志均已在前几个标签页免登展示；如需手动登录原生面板，请从本地 <code class="mono">napcat/config/webui.json</code> 读取 Token。</span></div>
          <div class="row" style="flex-shrink:0;">
            ${hasToken ? `<span class="badge badge-sm mono" title="为防止明文泄露，WebUI 接口已脱敏">${escapeHtml(tokenDisplay)}</span>` : ''}
            <a class="btn btn-ghost btn-sm" href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer">${icon('external', 12)} 新标签 :${escapeHtml(String(port))}</a>
            <button type="button" class="btn btn-ghost btn-sm" id="napcat-hint-reload-btn">${icon('refresh', 12)} 重载</button>
            <button type="button" class="btn btn-ghost btn-icon btn-sm" id="napcat-hint-dismiss-btn">${icon('x', 13)}</button>
          </div>
        </div>
        <div class="napcat-iframe-wrap">
          <div class="napcat-iframe-loading" id="napcat-iframe-loading"><span class="spin" style="color:var(--accent);display:inline-flex;">${icon('refresh', 20)}</span><span>正在加载 NapCat 原生面板 (${escapeHtml(url)})…</span></div>
          <iframe id="napcat-webui-iframe" class="napcat-iframe" src="${escapeHtml(url)}" title="NapCat WebUI" allow="clipboard-read; clipboard-write"></iframe>
        </div>
      </div>
    `;

    const iframe = panes.webui.querySelector('#napcat-webui-iframe');
    const loadingMask = panes.webui.querySelector('#napcat-iframe-loading');
    iframe?.addEventListener('load', () => {
      ui.iframeLoaded = true;
      if (loadingMask) loadingMask.classList.add('hidden');
      if (iframeLoadFallbackTimer) { window.clearTimeout(iframeLoadFallbackTimer); iframeLoadFallbackTimer = null; }
    });
    if (iframeLoadFallbackTimer) window.clearTimeout(iframeLoadFallbackTimer);
    iframeLoadFallbackTimer = window.setTimeout(() => { if (!destroyed && loadingMask) loadingMask.classList.add('hidden'); }, 4000);

    panes.webui.querySelector('#napcat-fs-reload-btn')?.addEventListener('click', reloadIframe);
    panes.webui.querySelector('#napcat-fs-exit-btn')?.addEventListener('click', () => setFullscreen(false));
    panes.webui.querySelector('#napcat-hint-reload-btn')?.addEventListener('click', reloadIframe);
    panes.webui.querySelector('#napcat-hint-dismiss-btn')?.addEventListener('click', () => {
      hintDismissed = true;
      try { sessionStorage.setItem(HINT_DISMISS_KEY, '1'); } catch (_) {}
      panes.webui.querySelector('#napcat-hint-strip')?.classList.add('hidden');
    });
  }

  // ---- Tab Switching & Shared Actions ----

  function switchTab(nextTab) {
    if (!validTabs.includes(nextTab)) return;
    if (nextTab !== 'webui' && ui.fullscreen) setFullscreen(false);
    ui.tab = nextTab;
    renderTabsAndCounts();
    renderQuickActions();
    for (const [k, node] of Object.entries(panes)) node.style.display = ui.tab === k ? '' : 'none';

    if (ui.tab === 'overview') renderOverviewPane();
    else if (ui.tab === 'network') renderNetworkPane();
    else if (ui.tab === 'logs') {
      if (!getWebuiMeta().authenticated && ui.logMode === 'realtime' && isDockerAvailable(ui.info)) ui.logMode = 'docker';
      renderLogsPane();
      if (ui.logMode === 'docker' && !ui.logsLoaded) loadLogs({ scrollBottom: true });
      else if (ui.logMode === 'files' && !ui.logFilesLoaded) loadLogFilesList();
    } else if (ui.tab === 'contacts') renderContactsResults();
    else if (ui.tab === 'debug') { renderDebugPane(); if (!ui.debugSchemasLoaded) loadDebugSchemas(); }
    else if (ui.tab === 'webui') renderWebuiPane();

    syncSysStream();
    syncLoginPoll();
    syncRtLogStream();
    syncLogAutoPoll();
  }

  tabsEl.querySelectorAll('.page-tab').forEach((btn) => btn.addEventListener('click', () => switchTab(btn.dataset.tab)));

  async function handleContainerAction(action) {
    if (ui.containerBusy) return;
    const verb = { start: '启动', stop: '停止', restart: '重启' }[action] || action;
    if (action === 'restart' || action === 'stop') {
      const ok = await confirmModal({
        title: `${verb} NapCat 容器`,
        message: action === 'restart' ? '重启 NapCat 容器会短暂断开 QQ 协议端连接与 OneBot WS 通道，确定继续吗？' : '停止 NapCat 容器后机器人将离线，确定继续吗？',
        confirmText: `确认${verb}`,
        cancelText: '取消',
        danger: true,
      });
      if (!ok || destroyed) return;
    }
    ui.containerBusy = true;
    stopSysStream();
    stopRtLogStream();
    renderQuickActions();
    try {
      const res = await apiPost(`/api/napcat/container/${action}`, {});
      if (destroyed) return;
      showToast(res?.message || `NapCat 容器${verb}指令已执行`, 'success');
      await new Promise((r) => window.setTimeout(r, action === 'stop' ? 700 : 1800));
      if (destroyed) return;
      await refreshAll({ reloadIframe: action !== 'stop', notify: false, force: true });
      ctx.refreshGlobalStatus?.({ silent: true });
    } catch (err) {
      if (!destroyed) showToast(`${verb} NapCat 容器失败: ${err?.message || String(err)}`, 'error');
    } finally {
      if (!destroyed) { ui.containerBusy = false; renderQuickActions(); if (ui.tab === 'overview') renderOverviewPane(); }
    }
  }

  async function saveWhitelist(enabled, groupIds, successMsg) {
    if (ui.whitelistSaving) return;
    ui.whitelistSaving = true;
    renderContactsResults();
    try {
      const res = await apiPut('/api/napcat/whitelist', { enabled: Boolean(enabled), group_ids: groupIds, restart_ws_guard: false });
      if (destroyed) return;
      ui.whitelist = res?.whitelist || { enabled: Boolean(enabled), group_ids: groupIds };
      if (typeof res?.ws_guard_needs_restart === 'boolean') ui.wsGuardNeedsRestart = res.ws_guard_needs_restart;
      showToast(successMsg || '群聊白名单已保存', 'success');
    } catch (err) {
      if (!destroyed) showToast(`保存群聊白名单失败: ${err?.message || String(err)}`, 'error');
    } finally {
      if (!destroyed) { ui.whitelistSaving = false; renderContactsResults(); }
    }
  }

  async function restartWsGuardForWhitelist() {
    if (ui.wsGuardRestarting) return;
    ui.wsGuardRestarting = true;
    renderContactsResults();
    try {
      const res = await apiPost('/api/control/restart-ws-guard', {});
      if (destroyed) return;
      showToast(res?.message || '已触发 ws-guard 无感重启，约 3 秒后就绪', 'success');
      await new Promise((r) => window.setTimeout(r, 3800));
      if (destroyed) return;
      await loadInfo({ silent: true, force: true });
      if (!destroyed) { renderContactsResults(); ctx.refreshGlobalStatus?.({ silent: true }); }
    } catch (err) {
      if (!destroyed) showToast(`重启 ws-guard 失败: ${err?.message || String(err)}`, 'error');
    } finally {
      if (!destroyed) { ui.wsGuardRestarting = false; renderContactsResults(); }
    }
  }

  async function loadInfo({ silent = false, force = false } = {}) {
    ui.infoLoading = true;
    ui.infoError = '';
    try {
      const data = await apiGet(force ? '/api/napcat/info?refresh=1' : '/api/napcat/info');
      if (destroyed) return null;
      ui.info = data || {};
      if (ui.info.sys_status && !ui.sysStatus) pushSysSample(ui.info.sys_status);
      if (Array.isArray(ui.info.log_files)) ui.logFiles = ui.info.log_files;
      if (ui.info.group_whitelist && typeof ui.info.group_whitelist === 'object') {
        ui.whitelist = {
          enabled: Boolean(ui.info.group_whitelist.enabled),
          group_ids: Array.isArray(ui.info.group_whitelist.group_ids) ? ui.info.group_whitelist.group_ids.map((x) => String(x)) : [],
        };
      }
      if (typeof ui.info.ws_guard_needs_restart === 'boolean') ui.wsGuardNeedsRestart = ui.info.ws_guard_needs_restart;
      return ui.info;
    } catch (err) {
      if (destroyed) return null;
      ui.infoError = err?.message || String(err);
      if (!silent) showToast(`获取 NapCat 信息失败: ${ui.infoError}`, 'error');
      return null;
    } finally {
      ui.infoLoading = false;
    }
  }

  async function refreshAll({ reloadIframe: shouldReload = false, notify = false, force = false } = {}) {
    const hadIframe = Boolean(panes.webui.querySelector('#napcat-webui-iframe'));
    await loadInfo({ silent: false, force });
    if (destroyed) return;
    renderHeaderBar();
    renderTabsAndCounts();
    renderQuickActions();
    if (ui.tab === 'overview') renderOverviewPane();
    else if (ui.tab === 'network') renderNetworkPane();
    else if (ui.tab === 'logs') {
      if (ui.logMode === 'docker') await loadLogs({ scrollBottom: false, silent: true });
      else if (ui.logMode === 'files') await loadLogFilesList();
      else renderLogsPane();
    } else if (ui.tab === 'contacts') renderContactsResults();
    else if (ui.tab === 'debug') renderDebugPane();
    else if (ui.tab === 'webui') {
      renderWebuiPane({ forceRebuild: false });
      if (shouldReload && hadIframe && isContainerRunning(ui.info)) reloadIframe();
    }
    syncSysStream();
    syncLoginPoll();
    syncRtLogStream();
    ctx.refreshGlobalStatus?.({ silent: true });
    if (notify) showToast('NapCat 状态已刷新', 'success');
  }

  renderHeaderBar();
  renderTabsAndCounts();
  renderQuickActions();
  renderOverviewPane();

  await loadInfo({ silent: false });
  if (destroyed) return { cleanup() {} };

  renderHeaderBar();
  renderTabsAndCounts();
  renderQuickActions();
  switchTab(ui.tab);

  return {
    cleanup() {
      destroyed = true;
      stopSysStream();
      stopRtLogStream();
      stopLoginPoll();
      if (activeModalCleanup) { activeModalCleanup(); activeModalCleanup = null; }
      if (logPollTimer) { window.clearInterval(logPollTimer); logPollTimer = null; }
      if (iframeLoadFallbackTimer) { window.clearTimeout(iframeLoadFallbackTimer); iframeLoadFallbackTimer = null; }
      document.removeEventListener('keydown', onKeyDown);
      container.removeEventListener('error', onAvatarError, true);
      if (ui.fullscreen) document.body.style.overflow = '';
    },
    refresh() {
      return refreshAll({ reloadIframe: false, notify: false, force: true });
    },
  };
}
