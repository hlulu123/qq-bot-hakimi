import { createIcon, escapeHtml } from './api.js';
import { mount as mountRequests } from './views/requests.js';

const THEME_KEY = 'hajimi_theme';

const MONITOR_ICONS = {
  volume2: '<polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><path d="M15.54 8.46a5 5 0 0 1 0 7.07"/><path d="M19.07 4.93a10 10 0 0 1 0 14.14"/>',
  volumeX: '<polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><line x1="23" y1="9" x2="17" y2="15"/><line x1="17" y1="9" x2="23" y2="15"/>',
  bell: '<path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/>',
  bellOff: '<path d="M13.73 21a2 2 0 0 1-3.46 0"/><path d="M18.63 13A17.89 17.89 0 0 1 18 8"/><path d="M6.26 6.26A5.86 5.86 0 0 0 6 8c0 7-3 9-3 9h14"/><path d="M18 8a6 6 0 0 0-9.33-5"/><line x1="1" y1="1" x2="23" y2="23"/>',
};

function monitorIcon(name, size = 15) {
  if (MONITOR_ICONS[name]) {
    return `<svg viewBox="0 0 24 24" width="${size}" height="${size}" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${MONITOR_ICONS[name]}</svg>`;
  }
  return createIcon(name, size);
}

// ---------- Theme Sync (immediately on load + live cross-window storage event) ----------
function resolveInitialTheme() {
  try {
    const saved = localStorage.getItem(THEME_KEY);
    if (saved === 'light' || saved === 'dark') return saved;
  } catch (_) {}
  try {
    if (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) {
      return 'dark';
    }
  } catch (_) {}
  return 'light';
}

function getCurrentTheme() {
  return document.documentElement.getAttribute('data-theme') === 'dark' ? 'dark' : 'light';
}

function applyTheme(theme, persist = false) {
  const safe = theme === 'dark' ? 'dark' : 'light';
  document.documentElement.setAttribute('data-theme', safe);
  if (persist) {
    try {
      localStorage.setItem(THEME_KEY, safe);
    } catch (_) {}
  }
  const themeBtn = document.getElementById('jm-theme-btn');
  if (themeBtn) {
    themeBtn.innerHTML = safe === 'dark' ? createIcon('sun', 15) : createIcon('moon', 15);
    themeBtn.title = safe === 'dark' ? '切换到浅色模式' : '切换到深色模式';
  }
}

// Apply theme as early as possible before first render
applyTheme(resolveInitialTheme(), false);

window.addEventListener('storage', (e) => {
  if (e.key === THEME_KEY && (e.newValue === 'light' || e.newValue === 'dark')) {
    applyTheme(e.newValue, false);
  }
});

// ---------- Bootstrap Standalone Monitor Window ----------
async function init() {
  const rootEl = document.getElementById('jm-root');
  const ssePillEl = document.getElementById('jm-sse-pill');
  const soundBtn = document.getElementById('jm-sound-btn');
  const notifyBtn = document.getElementById('jm-notify-btn');
  const syncBtn = document.getElementById('jm-sync-btn');
  const themeBtn = document.getElementById('jm-theme-btn');
  const consoleLink = document.getElementById('jm-console-link');
  const consoleIconSlot = document.getElementById('jm-console-icon');

  applyTheme(getCurrentTheme(), false);
  if (consoleIconSlot) consoleIconSlot.innerHTML = createIcon('external', 13);
  if (syncBtn) syncBtn.innerHTML = createIcon('refresh', 15);

  function renderHeaderState(st) {
    if (!st) return;

    const pending = Number(st.pendingCount || 0);
    document.title = pending > 0 ? `(${pending}) 入群盯盘 · 哈基米` : '入群盯盘 · 哈基米';

    if (ssePillEl) {
      if (st.sseState === 'connected') {
        ssePillEl.className = 'jm-sse-pill is-live';
        ssePillEl.innerHTML = '<span class="req-dot-live"></span>实时盯盘';
      } else if (st.sseState === 'reconnecting') {
        ssePillEl.className = 'jm-sse-pill is-warn';
        ssePillEl.innerHTML = '<span class="status-dot warn"></span>重连中…';
      } else {
        ssePillEl.className = 'jm-sse-pill';
        ssePillEl.innerHTML = '<span class="status-dot"></span>连接中…';
      }
    }

    if (soundBtn) {
      const on = Boolean(st.soundEnabled);
      soundBtn.classList.toggle('is-active', on);
      soundBtn.innerHTML = monitorIcon(on ? 'volume2' : 'volumeX', 15);
      soundBtn.title = on ? '声音提醒：已开启（点击静音）' : '声音提醒：已关闭（点击开启）';
      soundBtn.setAttribute('aria-pressed', String(on));
    }

    if (notifyBtn) {
      const on = Boolean(st.notifyEnabled);
      notifyBtn.classList.toggle('is-active', on);
      notifyBtn.innerHTML = monitorIcon(on ? 'bell' : 'bellOff', 15);
      notifyBtn.title = on ? '桌面通知：已开启（点击关闭）' : '桌面通知：已关闭（点击开启）';
      notifyBtn.setAttribute('aria-pressed', String(on));
    }

    if (syncBtn) {
      const busy = Boolean(st.syncing);
      syncBtn.disabled = busy;
      syncBtn.innerHTML = `<span class="${busy ? 'spin' : ''}" style="display:inline-flex;">${createIcon('refresh', 15)}</span>`;
      syncBtn.title = busy ? '正在从 NapCat 同步…' : '立即同步 NapCat 入群申请';
    }
  }

  // Parse optional URL query params (e.g. /join-monitor?group_id=10001&status=all)
  const sp = new URLSearchParams(window.location.search || '');
  const params = { monitor: '1' };
  for (const [k, v] of sp.entries()) {
    if (v !== '') params[k] = v;
  }

  let controller = null;

  themeBtn?.addEventListener('click', () => {
    applyTheme(getCurrentTheme() === 'dark' ? 'light' : 'dark', true);
  });

  consoleLink?.addEventListener('click', (e) => {
    try {
      if (window.opener && !window.opener.closed && window.opener.location.origin === window.location.origin) {
        e.preventDefault();
        window.opener.location.hash = '#requests';
        window.opener.focus();
      }
    } catch (_) {
      // Fallback to normal anchor target="_blank" navigation to /#requests
    }
  });

  if (!rootEl) return;

  try {
    controller = await mountRequests(rootEl, {
      standalone: true,
      params,
      setActions: () => null,
      refreshGlobalStatus: () => {},
      navigate: (route, navParams = {}) => {
        const qs = new URLSearchParams(navParams || {}).toString();
        const target = `/#${route}${qs ? `?${qs}` : ''}`;
        try {
          if (window.opener && !window.opener.closed && window.opener.location.origin === window.location.origin) {
            window.opener.location.hash = `#${route}${qs ? `?${qs}` : ''}`;
            window.opener.focus();
            return;
          }
        } catch (_) {}
        window.open(target, '_blank', 'noopener,noreferrer');
      },
      onStatusChange: renderHeaderState,
    });

    soundBtn?.addEventListener('click', () => controller?.toggleSound?.());
    notifyBtn?.addEventListener('click', () => controller?.toggleNotify?.());
    syncBtn?.addEventListener('click', () => controller?.syncNapcat?.());

    window.addEventListener('beforeunload', () => {
      try { controller?.cleanup?.(); } catch (_) {}
    });
  } catch (err) {
    console.error('[hajimi-join-monitor] mount failed:', err);
    rootEl.innerHTML = `
      <div class="card">
        <div class="empty-state">
          <div class="empty-state-icon">${createIcon('alert', 20)}</div>
          <div class="empty-state-title">盯盘窗口初始化失败</div>
          <div class="text-sm text-muted">${escapeHtml(err && err.message ? err.message : String(err))}</div>
          <button type="button" class="btn btn-primary btn-sm mt-8" id="jm-reload-btn">${createIcon('refresh', 14)}重新加载</button>
        </div>
      </div>
    `;
    rootEl.querySelector('#jm-reload-btn')?.addEventListener('click', () => window.location.reload());
  }
}

init();
