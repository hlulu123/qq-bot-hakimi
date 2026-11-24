import {
  apiGet,
  createIcon,
  escapeHtml,
  extractUrlPort,
  formatDuration,
  formatTokens,
  safeLocalHttpUrl,
  showToast,
} from './api.js';
import { maybeShowWelcome } from './welcome.js';

const DEFAULT_ROUTE = 'overview';

const ROUTES = [
  {
    id: 'overview',
    label: '总览',
    title: '总览',
    subtitle: '机器人运行状态、服务链路与关键指标一览。',
    icon: 'dashboard',
    group: '运行',
  },
  {
    id: 'tokens',
    label: 'Token 用量',
    title: 'Token 用量',
    subtitle: '按群聊与私聊统计的模型消耗、趋势与明细。',
    icon: 'barChart',
    group: '运行',
  },
  {
    id: 'sessions',
    label: '会话',
    title: '会话',
    subtitle: '查看 cc-connect 会话与 NapCat 原始聊天记录。',
    icon: 'chat',
    group: '运行',
  },
  {
    id: 'requests',
    label: '入群监控',
    title: '入群监控',
    subtitle: '实时盯盘加群申请、一键审批与自动通过规则。',
    icon: 'userPlus',
    group: '运行',
  },
  {
    id: 'logs',
    label: '日志',
    title: '实时日志',
    subtitle: '聚合 cc-connect、ws-guard 与 NapCat 的运行日志。',
    icon: 'terminal',
    group: '运行',
  },
  {
    id: 'settings',
    label: '行为配置',
    title: '行为配置',
    subtitle: '场景预设、行为参数与 bot.env / config.toml。',
    icon: 'sliders',
    group: '配置',
  },
  {
    id: 'plugins',
    label: '插件',
    title: '插件',
    subtitle: '按需启用功能插件，热生效，可直接降低 Token 消耗。',
    icon: 'puzzle',
    group: '配置',
  },
  {
    id: 'permissions',
    label: '权限与沙盒',
    title: '权限与沙盒',
    subtitle: 'QQ 能力分级授权、按群覆写与工作区文件浏览。',
    icon: 'shield',
    group: '配置',
  },
  {
    id: 'napcat',
    label: 'QQ 连接',
    title: 'QQ 连接',
    subtitle: 'NapCat 登录、网络配置、运行监控与通讯录。',
    icon: 'server',
    group: '配置',
  },
  {
    id: 'persona',
    label: '人设',
    title: '人设与指令',
    subtitle: '在线编辑 workspace/GEMINI.md，并同步到聊天沙盒。',
    icon: 'sparkles',
    group: '人格与记忆',
  },
  {
    id: 'memory',
    label: '长期记忆',
    title: '长期记忆',
    subtitle: '群友画像、私聊隔离、群共享记忆与冲突裁决。',
    icon: 'brain',
    group: '人格与记忆',
  },
];

const ROUTE_ALIASES = {
  workbench: { route: 'settings', defaultParams: {} },
  config: { route: 'settings', defaultParams: { tab: 'system' } },
  workspace: { route: 'permissions', defaultParams: { tab: 'sandbox' } },
};

const ROUTE_MAP = Object.fromEntries(ROUTES.map((r) => [r.id, r]));

const state = {
  overview: null,
  lastUpdated: 0,
  route: DEFAULT_ROUTE,
  params: {},
};

let activeCleanup = null;
let activeRefresh = null;
let mountSeq = 0;
let pollTimer = null;

// ---------------------------------------------------------------------------
// Routing
// ---------------------------------------------------------------------------
function resolveRouteAndParams(rawRoute, rawParams = {}) {
  const key = String(rawRoute || '').trim();
  const params = { ...(rawParams || {}) };
  if (ROUTE_MAP[key]) return { route: key, params };
  const alias = ROUTE_ALIASES[key];
  if (alias) return { route: alias.route, params: { ...alias.defaultParams, ...params } };
  return { route: DEFAULT_ROUTE, params };
}

function buildHash(route, params = {}) {
  const sp = new URLSearchParams();
  Object.entries(params || {}).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== '') sp.set(k, String(v));
  });
  const qs = sp.toString();
  return qs ? `#${route}?${qs}` : `#${route}`;
}

function parseHash() {
  const raw = (window.location.hash || '').replace(/^#\/?/, '').trim();
  if (!raw) return { route: DEFAULT_ROUTE, params: {} };
  const qIdx = raw.indexOf('?');
  const routePart = (qIdx >= 0 ? raw.slice(0, qIdx) : raw).split('/')[0];
  const queryPart = qIdx >= 0 ? raw.slice(qIdx + 1) : '';
  const params = {};
  if (queryPart) {
    for (const [k, v] of new URLSearchParams(queryPart).entries()) params[k] = v;
  }
  const resolved = resolveRouteAndParams(routePart, params);
  if (ROUTE_ALIASES[routePart]) {
    try {
      window.history.replaceState(null, '', buildHash(resolved.route, resolved.params));
    } catch (_) {}
  }
  return resolved;
}

function navigate(route, params = {}) {
  const resolved = resolveRouteAndParams(route, params);
  const nextHash = buildHash(resolved.route, resolved.params);
  closeMobileSidebar();
  if (window.location.hash === nextHash) {
    mountRoute(resolved.route, resolved.params);
  } else {
    window.location.hash = nextHash;
  }
}

// ---------------------------------------------------------------------------
// Sidebar navigation
// ---------------------------------------------------------------------------
function renderSidebarNav() {
  const nav = document.getElementById('sidebar-nav');
  if (!nav) return;
  let html = '';
  let lastGroup = '';
  for (const r of ROUTES) {
    if (r.group !== lastGroup) {
      html += `<div class="nav-section-label">${escapeHtml(r.group)}</div>`;
      lastGroup = r.group;
    }
    html += `
      <a class="nav-item" href="#${r.id}" data-route="${r.id}">
        <span class="nav-icon">${createIcon(r.icon, 17)}</span>
        <span class="nav-label">${escapeHtml(r.label)}</span>
        <span class="nav-badge" data-nav-badge="${r.id}" style="display:none;"></span>
      </a>
    `;
  }
  nav.innerHTML = html;
  nav.querySelectorAll('.nav-item').forEach((el) => {
    el.addEventListener('click', (e) => {
      e.preventDefault();
      navigate(el.dataset.route);
    });
  });
}

function updateActiveNav(route) {
  document.querySelectorAll('#sidebar-nav .nav-item').forEach((el) => {
    el.classList.toggle('active', el.dataset.route === route);
  });
  const meta = ROUTE_MAP[route] || ROUTES[0];
  const eyebrowEl = document.getElementById('page-eyebrow');
  const titleEl = document.getElementById('topbar-title');
  const subEl = document.getElementById('topbar-subtitle');
  const mobileEl = document.getElementById('mobile-title');
  if (eyebrowEl) eyebrowEl.textContent = meta.group;
  if (titleEl) titleEl.textContent = meta.title || meta.label;
  if (subEl) subEl.textContent = meta.subtitle || '';
  if (mobileEl) mobileEl.textContent = meta.label;
  document.title = `${meta.label} · 哈基米 Console`;
}

function setNavBadge(routeId, text, variant = '', title = '') {
  const badge = document.querySelector(`[data-nav-badge="${routeId}"]`);
  if (!badge) return;
  if (text === null || text === undefined || text === '' || text === 0) {
    badge.style.display = 'none';
    badge.textContent = '';
    badge.className = 'nav-badge';
    badge.removeAttribute('title');
    return;
  }
  badge.style.display = 'inline-flex';
  badge.textContent = String(text);
  badge.className = variant ? `nav-badge ${variant}` : 'nav-badge';
  if (title) badge.title = title;
  else badge.removeAttribute('title');
}

function setNavDotBadge(routeId, status, title = '') {
  const badge = document.querySelector(`[data-nav-badge="${routeId}"]`);
  if (!badge) return;
  if (!status) {
    badge.style.display = 'none';
    badge.innerHTML = '';
    badge.className = 'nav-badge';
    badge.removeAttribute('title');
    return;
  }
  badge.style.display = 'inline-flex';
  badge.className = `nav-badge nav-badge-dot ${status}`;
  badge.innerHTML = `<span class="status-dot ${status}"></span>`;
  if (title) badge.title = title;
  else badge.removeAttribute('title');
}

// ---------------------------------------------------------------------------
// Global status (sidebar system card)
// ---------------------------------------------------------------------------
function isServiceUp(svc) {
  if (!svc || typeof svc !== 'object') return false;
  if (typeof svc.ok === 'boolean') return svc.ok;
  if (typeof svc.online === 'boolean') return svc.online;
  if (typeof svc.running === 'boolean') return svc.running;
  if (typeof svc.connected === 'boolean') return svc.connected;
  if (typeof svc.status === 'string') {
    return ['online', 'running', 'ok', 'healthy', 'up'].includes(svc.status.toLowerCase());
  }
  return false;
}

function getServiceState(svc) {
  if (!svc || typeof svc !== 'object') return 'offline';
  const statusStr = typeof svc.status === 'string' ? svc.status.toLowerCase() : '';
  if (statusStr === 'degraded') return 'warn';
  if (svc.ok === true || svc.online === true) return 'online';
  if (svc.running === true && (svc.ok === false || svc.online === false)) return 'warn';
  return isServiceUp(svc) ? 'online' : 'offline';
}

function applyGlobalStatusUI(overview) {
  if (!overview || typeof overview !== 'object') return;

  const services = overview.services || {};
  const napcat = services.napcat || overview.napcat || {};
  const wsGuard = services.ws_guard || services.wsguard || overview.ws_guard || {};
  const ccConnect = services.cc_connect || services.ccconnect || overview.cc_connect || {};

  const napcatState = getServiceState(napcat);
  const wsState = getServiceState(wsGuard);
  const ccState = getServiceState(ccConnect);
  const upCount = [napcatState, wsState, ccState].filter((s) => s === 'online').length;

  const napcatWebuiPort = napcat.webui_port || overview.ports?.napcat_webui || extractUrlPort(napcat.webui_url, 6099);
  const napcatWebuiUrl = safeLocalHttpUrl(napcat.webui_url, napcatWebuiPort, '/webui/');
  const wsPort = wsGuard.listen_port || overview.ports?.ws_guard || 3002;
  const ccPort = ccConnect.management_port || overview.ports?.cc_connect || 9820;

  const napcatLinkEl = document.getElementById('sidebar-napcat-link');
  if (napcatLinkEl) {
    napcatLinkEl.href = napcatWebuiUrl;
    napcatLinkEl.title = `新标签打开 NapCat WebUI（${napcatWebuiUrl}）`;
  }

  const rows = {
    napcat: {
      state: napcatState,
      text: napcatState === 'online' ? '在线' : napcatState === 'warn' ? '待登录' : '离线',
      tip: napcatState === 'online'
        ? `${napcat.nickname || 'QQ'}（${napcat.user_id || napcat.qq || '--'}）已登录`
        : napcatState === 'warn' ? 'NapCat 运行中，但 QQ 未登录' : 'NapCat 未连接',
    },
    ws_guard: {
      state: wsState,
      text: wsState === 'online' ? '运行中' : '未运行',
      tip: wsState === 'online' ? `PID ${wsGuard.pid || '--'} · 端口 ${wsPort}` : `端口 ${wsPort} 未监听`,
    },
    cc_connect: {
      state: ccState,
      text: ccState === 'online' ? formatDuration(ccConnect.uptime_seconds ?? ccConnect.uptime_sec) : ccState === 'warn' ? '就绪中' : '未运行',
      tip: ccState === 'online' ? `${ccConnect.version || ''} 已运行 ${formatDuration(ccConnect.uptime_seconds ?? ccConnect.uptime_sec)}` : `管理端口 ${ccPort}`,
    },
  };

  Object.entries(rows).forEach(([key, info]) => {
    const el = document.querySelector(`#global-status-pills [data-service="${key}"]`);
    if (!el) return;
    el.classList.remove('online', 'offline', 'warn');
    el.classList.add(info.state);
    el.title = info.tip;
    const stateEl = el.querySelector('.system-row-state');
    if (stateEl) stateEl.textContent = info.text;
  });

  const sysDot = document.getElementById('system-dot');
  const sysTitle = document.getElementById('system-title');
  const overall = upCount === 3 ? 'online' : upCount > 0 || napcatState === 'warn' || ccState === 'warn' ? 'warn' : 'offline';
  if (sysDot) sysDot.className = `status-dot ${overall}${overall === 'online' ? ' pulse' : ''}`;
  if (sysTitle) {
    sysTitle.textContent = overall === 'online' ? '全部服务运行正常' : overall === 'warn' ? `部分服务异常 · ${upCount}/3` : '机器人已停止';
  }

  const nickname = napcat.nickname || overview.bot?.nickname || '哈基米';
  const uin = napcat.user_id || napcat.qq || overview.bot?.qq || overview.config_summary?.bot_qq || '';
  const nameEl = document.getElementById('sidebar-bot-name');
  const uinEl = document.getElementById('sidebar-bot-uin');
  const avatarEl = document.getElementById('sidebar-bot-avatar');
  if (nameEl) nameEl.textContent = nickname;
  if (uinEl) uinEl.textContent = uin ? String(uin) : '未登录';
  if (avatarEl) {
    const initialChar = String(nickname).trim()[0] || '哈';
    const digits = String(uin || '').replace(/\D/g, '');
    if (digits && avatarEl.dataset.uin !== digits) {
      avatarEl.dataset.uin = digits;
      avatarEl.style.position = 'relative';
      avatarEl.innerHTML = '';
      const fallbackSpan = document.createElement('span');
      fallbackSpan.className = 'account-avatar-fallback';
      fallbackSpan.textContent = initialChar;
      const img = document.createElement('img');
      img.alt = '';
      img.style.position = 'absolute';
      img.style.inset = '0';
      img.addEventListener('load', () => {
        fallbackSpan.style.opacity = '0';
      });
      img.addEventListener('error', () => {
        img.remove();
        fallbackSpan.style.opacity = '1';
      });
      img.src = `https://q1.qlogo.cn/g?b=qq&nk=${digits}&s=100`;
      avatarEl.append(fallbackSpan, img);
    } else if (digits) {
      const fallbackSpan = avatarEl.querySelector('.account-avatar-fallback');
      if (fallbackSpan) fallbackSpan.textContent = initialChar;
    } else {
      avatarEl.textContent = initialChar;
      delete avatarEl.dataset.uin;
    }
  }

  // Quiet nav hints: only actionable items get color.
  if (napcatState !== 'online') {
    setNavDotBadge('napcat', napcatState, napcatState === 'warn' ? 'QQ 待登录' : 'NapCat 离线');
  } else {
    setNavDotBadge('napcat', '');
  }

  const liveSessions =
    overview.sessions_summary?.live_count ?? ccConnect.active_sessions ?? overview.counts?.live_sessions ?? 0;
  setNavBadge('sessions', liveSessions > 0 ? `${liveSessions}` : '', '', liveSessions > 0 ? `活跃会话 ${liveSessions} 个` : '');

  const reqPending = Number(overview?.group_requests_summary?.pending_count || 0);
  setNavBadge(
    'requests',
    reqPending > 0 ? String(reqPending) : '',
    'warn',
    reqPending > 0 ? `${reqPending} 条待审批入群申请` : '',
  );

  if (overview.config_health?.persona_in_sync === false) {
    setNavBadge('persona', '未同步', 'warn', '聊天沙盒中的 GEMINI.md 与线上人设不一致');
  } else {
    setNavBadge('persona', '');
  }

  const conflicts = overview.memory_summary?.conflict_count ?? overview.memory?.conflict_count ?? 0;
  setNavBadge('memory', conflicts > 0 ? `${conflicts}` : '', 'warn', conflicts > 0 ? `${conflicts} 组记忆冲突待裁决` : '');

  const configProblems = Array.isArray(overview.config_health?.problems) ? overview.config_health.problems : [];
  if (overview.config_health?.valid === false || configProblems.length > 0) {
    setNavBadge('settings', configProblems.length > 0 ? `${configProblems.length}` : '!', 'warn',
      configProblems.length > 0 ? `${configProblems.length} 项配置不一致` : '配置一致性异常');
  } else {
    setNavBadge('settings', '');
  }

  const ts = overview.token_summary;
  if (ts && typeof ts === 'object') {
    const todayTok = Number(ts.today_tokens ?? 0);
    const h24Tok = Number(ts.last_24h_tokens ?? 0);
    const showTok = todayTok > 0 ? todayTok : h24Tok;
    setNavBadge('tokens', showTok > 0 ? formatTokens(showTok) : '', '', showTok > 0 ? `今日 ${formatTokens(todayTok)} · 近 24h ${formatTokens(h24Tok)}` : '');
  } else {
    setNavBadge('tokens', '');
  }

  const ps = overview.plugins_summary;
  if (ps && typeof ps === 'object' && Number(ps.total_count) > 0) {
    setNavBadge('plugins', `${Number(ps.enabled_count ?? 0)}/${Number(ps.total_count ?? 0)}`, '', `已启用 ${ps.enabled_count}/${ps.total_count} 个插件`);
  } else {
    setNavBadge('plugins', '');
  }
}

async function refreshGlobalStatus({ silent = true, force = false } = {}) {
  try {
    const data = await apiGet(force ? '/api/overview?refresh=1' : '/api/overview');
    state.overview = data;
    state.lastUpdated = Date.now();
    applyGlobalStatusUI(data);
    return data;
  } catch (err) {
    if (!silent) showToast(`获取服务状态失败：${err.message}`, 'error');
    return null;
  }
}

// ---------------------------------------------------------------------------
// Page header actions — views place their primary buttons here
// ---------------------------------------------------------------------------
function getActionsEl() {
  return document.getElementById('page-actions');
}

function setActions(html = '') {
  const el = getActionsEl();
  if (!el) return null;
  el.innerHTML = html || '';
  return el;
}

// ---------------------------------------------------------------------------
// Mounting
// ---------------------------------------------------------------------------
function renderLoadingState(container) {
  container.innerHTML = `
    <div class="stack">
      <div class="loading-skeleton" style="height: 112px;"></div>
      <div class="grid-4">
        <div class="loading-skeleton" style="height: 96px;"></div>
        <div class="loading-skeleton" style="height: 96px;"></div>
        <div class="loading-skeleton" style="height: 96px;"></div>
        <div class="loading-skeleton" style="height: 96px;"></div>
      </div>
      <div class="loading-skeleton" style="height: 260px;"></div>
    </div>
  `;
}

async function mountRoute(route, params = {}) {
  const seq = ++mountSeq;
  state.route = route;
  state.params = params || {};
  updateActiveNav(route);

  if (typeof activeCleanup === 'function') {
    try {
      activeCleanup();
    } catch (err) {
      console.warn('[hajimi-ui] view cleanup error:', err);
    }
  }
  activeCleanup = null;
  activeRefresh = null;
  setActions('');

  const previousContainer = document.getElementById('app-content');
  if (!previousContainer) return;
  // A view that is still mounting may render after navigation.
  const container = previousContainer.cloneNode(false);
  container.classList.remove('is-entering');
  previousContainer.replaceWith(container);
  renderLoadingState(container);
  document.getElementById('main')?.scrollTo?.({ top: 0 });
  window.scrollTo({ top: 0 });

  try {
    const mod = await import(`./views/${route}.js`);
    if (seq !== mountSeq) return;
    if (!mod || typeof mod.mount !== 'function') {
      throw new Error(`视图模块 ${route}.js 未导出 mount(container, ctx)`);
    }

    container.innerHTML = '';
    container.classList.add('is-entering');
    window.setTimeout(() => container.classList.remove('is-entering'), 600);
    const ctx = {
      navigate,
      params: state.params,
      state,
      refreshGlobalStatus,
      setActions: (html) => (seq === mountSeq ? setActions(html) : null),
      get actionsEl() {
        return seq === mountSeq ? getActionsEl() : null;
      },
    };

    const result = await mod.mount(container, ctx);
    if (seq !== mountSeq) {
      if (typeof result === 'function') result();
      else if (result && typeof result.cleanup === 'function') result.cleanup();
      return;
    }
    if (typeof result === 'function') {
      activeCleanup = result;
    } else if (result && typeof result === 'object') {
      activeCleanup = typeof result.cleanup === 'function' ? result.cleanup : null;
      activeRefresh = typeof result.refresh === 'function' ? result.refresh : null;
    }
  } catch (err) {
    if (seq !== mountSeq) return;
    console.error(`[hajimi-ui] Failed to mount route "${route}":`, err);
    container.innerHTML = `
      <div class="card">
        <div class="empty-state">
          <div class="empty-state-icon">${createIcon('alert', 20)}</div>
          <div class="empty-state-title">页面加载失败</div>
          <div class="text-sm" style="max-width:520px;">${escapeHtml(err && err.message ? err.message : String(err))}</div>
          <div class="row mt-12">
            <button type="button" class="btn btn-primary btn-sm" id="retry-route-btn">${createIcon('refresh', 14)} 重试</button>
            <button type="button" class="btn btn-secondary btn-sm" id="back-home-btn">返回总览</button>
          </div>
        </div>
      </div>
    `;
    container.querySelector('#retry-route-btn')?.addEventListener('click', () => mountRoute(route, params));
    container.querySelector('#back-home-btn')?.addEventListener('click', () => navigate(DEFAULT_ROUTE));
  }
}

// ---------------------------------------------------------------------------
// Theme
// ---------------------------------------------------------------------------
function getCurrentTheme() {
  return document.documentElement.getAttribute('data-theme') === 'dark' ? 'dark' : 'light';
}

function applyTheme(theme, persist = true) {
  const safe = theme === 'dark' ? 'dark' : 'light';
  document.documentElement.setAttribute('data-theme', safe);
  if (persist) {
    try {
      localStorage.setItem('hajimi_theme', safe);
    } catch (_) {}
  }
  const btn = document.getElementById('theme-toggle-btn');
  if (btn) {
    btn.innerHTML = safe === 'dark' ? createIcon('sun', 16) : createIcon('moon', 16);
    btn.title = safe === 'dark' ? '切换到浅色模式' : '切换到深色模式';
  }
}

// ---------------------------------------------------------------------------
// Mobile sidebar
// ---------------------------------------------------------------------------
function openMobileSidebar() {
  document.getElementById('app-shell')?.classList.add('sidebar-open');
}

function closeMobileSidebar() {
  document.getElementById('app-shell')?.classList.remove('sidebar-open');
}

// ---------------------------------------------------------------------------
// Bootstrap
// ---------------------------------------------------------------------------
function init() {
  renderSidebarNav();
  applyTheme(getCurrentTheme(), false);

  const refreshIconSlot = document.getElementById('global-refresh-icon');
  if (refreshIconSlot) refreshIconSlot.innerHTML = createIcon('refresh', 15);
  const menuBtn = document.getElementById('mobile-menu-btn');
  if (menuBtn) menuBtn.innerHTML = createIcon('menu', 18);
  const napcatLink = document.getElementById('sidebar-napcat-link');
  if (napcatLink) napcatLink.innerHTML = createIcon('external', 15);

  document.getElementById('theme-toggle-btn')?.addEventListener('click', () => {
    applyTheme(getCurrentTheme() === 'dark' ? 'light' : 'dark');
  });

  menuBtn?.addEventListener('click', openMobileSidebar);
  document.getElementById('sidebar-backdrop')?.addEventListener('click', closeMobileSidebar);

  document.getElementById('sidebar-brand')?.addEventListener('click', (e) => {
    e.preventDefault();
    navigate(DEFAULT_ROUTE);
  });

  document.getElementById('system-card')?.addEventListener('click', () => {
    const napcatRow = document.querySelector('#global-status-pills [data-service="napcat"]');
    if (napcatRow && !napcatRow.classList.contains('online')) navigate('napcat');
    else navigate('overview');
  });

  const refreshBtn = document.getElementById('global-refresh-btn');
  refreshBtn?.addEventListener('click', async () => {
    const seq = mountSeq;
    const slot = document.getElementById('global-refresh-icon');
    slot?.classList.add('spin');
    refreshBtn.disabled = true;
    try {
      await refreshGlobalStatus({ silent: false, force: true });
      if (seq !== mountSeq) return;
      if (typeof activeRefresh === 'function') await activeRefresh();
      else await mountRoute(state.route, state.params);
    } finally {
      slot?.classList.remove('spin');
      refreshBtn.disabled = false;
    }
  });

  window.addEventListener('hashchange', () => {
    const { route, params } = parseHash();
    mountRoute(route, params);
  });

  refreshGlobalStatus({ silent: true });
  pollTimer = window.setInterval(() => {
    if (!document.hidden) refreshGlobalStatus({ silent: true });
  }, 15000);
  window.addEventListener('beforeunload', () => {
    if (pollTimer) window.clearInterval(pollTimer);
  });

  const initial = parseHash();
  mountRoute(initial.route, initial.params);
  maybeShowWelcome();
}

init();
