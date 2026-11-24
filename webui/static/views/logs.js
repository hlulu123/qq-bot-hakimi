import {
  apiGet,
  createIcon,
  escapeHtml,
  showToast,
} from '../api.js';

const MAX_BUFFER = 600;
const ANSI_RE = /\x1b\[[0-9;?]*[a-zA-Z]/g;
const QR_ROW_RE = /[█▀▄▌▐░▒▓■□▪▫]{6,}/;

function highlightText(raw, query) {
  const safe = escapeHtml(raw);
  const q = String(query || '').trim();
  if (!q) return safe;
  const escapedQ = q.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  return safe.replace(new RegExp(escapedQ, 'gi'), (m) => `<mark class="log-mark">${m}</mark>`);
}

function normalizeEntry(raw) {
  if (!raw || typeof raw !== 'object') return null;
  const lvl = String(raw.level || 'INFO').toUpperCase();
  const level = ['INFO', 'WARN', 'ERROR', 'DEBUG'].includes(lvl) ? lvl : (lvl === 'WARNING' ? 'WARN' : 'INFO');
  const fields = raw.fields && typeof raw.fields === 'object' ? raw.fields : {};
  return {
    id: raw.id || `${raw.source || 'log'}-${raw.ts || ''}-${Math.random().toString(36).slice(2, 7)}`,
    source: String(raw.source || 'cc-connect'),
    time: String(raw.time || raw.ts || ''),
    level,
    msg: String(raw.msg || raw.raw || ''),
    fields,
    raw: String(raw.raw || raw.msg || ''),
  };
}

function parseDurationSecs(raw) {
  if (typeof raw === 'number') return Number.isFinite(raw) ? raw : NaN;
  const s = String(raw ?? '').trim();
  if (!s) return NaN;
  if (/^-?\d+(?:\.\d+)?$/.test(s)) return Number(s);
  const compact = s.replace(/\s+/g, '');
  if (!/^(?:\d+(?:\.\d+)?(?:ns|us|µs|μs|ms|secs?|mins?|hrs?|[smh]))+$/i.test(compact)) {
    return NaN;
  }
  const tokenRe = /(\d+(?:\.\d+)?)(ns|us|µs|μs|ms|secs?|mins?|hrs?|[smh])/gi;
  let total = 0;
  let matched = false;
  let m;
  while ((m = tokenRe.exec(compact)) !== null) {
    const val = Number(m[1]);
    const unit = m[2].toLowerCase();
    if (!Number.isFinite(val)) continue;
    matched = true;
    if (unit === 'h' || unit === 'hr' || unit === 'hrs') total += val * 3600;
    else if (unit === 'm' || unit === 'min' || unit === 'mins') total += val * 60;
    else if (unit === 's' || unit === 'sec' || unit === 'secs') total += val;
    else if (unit === 'ms') total += val / 1000;
    else if (unit === 'us' || unit === 'µs' || unit === 'μs') total += val / 1e6;
    else if (unit === 'ns') total += val / 1e9;
  }
  return matched ? total : NaN;
}

function matchesPreset(entry, preset) {
  if (preset === 'all') return true;
  const msg = `${entry.msg} ${entry.raw}`.toLowerCase();
  const f = entry.fields || {};
  if (preset === 'replied') {
    return f.silent === 'false' || f.silent === false || (msg.includes('turn complete') && f.silent !== 'true' && f.silent !== true);
  }
  if (preset === 'silent') {
    return f.silent === 'true' || f.silent === true || msg.includes('silent reply') || msg.includes('no_reply');
  }
  if (preset === 'repeat') {
    return msg.includes('repeat') || 'repeat' in f || msg.includes('复读');
  }
  if (preset === 'slow') {
    const secs = parseDurationSecs(f.turn_duration ?? f.elapsed ?? f.duration ?? '');
    if (Number.isFinite(secs) && secs >= 10) return true;
    return msg.includes('slow agent');
  }
  return true;
}

export async function mount(container, ctx) {
  const initSource = ['all', 'cc-connect', 'ws-guard', 'napcat'].includes(ctx.params?.source)
    ? ctx.params.source
    : 'all';
  let source = initSource;
  let level = 'ALL';
  let preset = 'all';
  let search = String(ctx.params?.q || '');
  let autoScroll = true;
  let paused = false;
  let qrCompact = false;

  let entries = [];
  let seenIds = new Set();
  let pauseQueue = [];
  let napcatLines = [];
  let napcatHasQr = false;
  let napcatError = '';

  let eventSource = null;
  let reconnectTimer = null;
  let napcatPollTimer = null;
  let destroyed = false;

  const actionsEl = ctx.setActions(`
    <button type="button" class="btn btn-secondary" id="logs-pause-btn">${createIcon('pause', 14)}暂停</button>
    <button type="button" class="btn btn-ghost" id="logs-clear-btn">${createIcon('trash', 14)}清空</button>
    <button type="button" class="btn btn-ghost" id="logs-export-btn">${createIcon('download', 14)}导出</button>
  `);

  container.innerHTML = `
    <div class="stack">
      <div class="toolbar-bar">
        <div class="tabs" id="logs-source-tabs">
          <button type="button" class="tab-btn ${source === 'all' ? 'active' : ''}" data-src="all">全部来源</button>
          <button type="button" class="tab-btn ${source === 'cc-connect' ? 'active' : ''}" data-src="cc-connect">cc-connect</button>
          <button type="button" class="tab-btn ${source === 'ws-guard' ? 'active' : ''}" data-src="ws-guard">ws-guard</button>
          <button type="button" class="tab-btn ${source === 'napcat' ? 'active' : ''}" data-src="napcat">NapCat 容器</button>
        </div>
        <div class="row" style="gap:14px;">
          <span class="row-sm text-sm text-secondary" id="logs-conn-badge"><span class="status-dot"></span>连接中…</span>
          <span class="text-sm text-muted tabular" id="logs-counter">0 条</span>
        </div>
      </div>

      <div class="toolbar-bar">
        <div class="row wrap" style="gap:8px;">
          <div class="filter-pills" id="logs-level-pills">
            <button type="button" class="pill-btn active" data-lvl="ALL">ALL</button>
            <button type="button" class="pill-btn" data-lvl="INFO">INFO</button>
            <button type="button" class="pill-btn" data-lvl="WARN">WARN</button>
            <button type="button" class="pill-btn" data-lvl="ERROR">ERROR</button>
            <button type="button" class="pill-btn" data-lvl="DEBUG">DEBUG</button>
          </div>
          <select class="select select-sm" id="logs-preset-select" style="width:auto;" title="按事件类型筛选">
            <option value="all">全部事件</option>
            <option value="replied">只看真实回复</option>
            <option value="silent">只看潜水</option>
            <option value="repeat">复读事件</option>
            <option value="slow">慢响应（&gt;10s）</option>
          </select>
          <button type="button" class="pill-btn" id="logs-qr-compact-btn" style="display:none;">二维码紧凑排版</button>
        </div>
        <div class="row wrap" style="gap:12px;">
          <label class="switch switch-sm text-sm text-secondary">
            <input type="checkbox" id="logs-autoscroll" checked />
            <span class="switch-track"></span>自动滚动
          </label>
          <div class="search-box" style="width:240px;">
            <span class="search-icon">${createIcon('search', 14)}</span>
            <input type="text" id="logs-search-input" value="${escapeHtml(search)}" placeholder="搜索关键字、群号或 QQ" autocomplete="off" />
          </div>
        </div>
      </div>

      <div class="log-console-wrap">
        <div class="log-console" id="logs-console"></div>
        <button type="button" class="btn btn-primary btn-sm log-jump-btn" id="logs-jump-bottom" style="display:none;">
          ${createIcon('arrowDown', 14)}回到底部
        </button>
      </div>
    </div>
  `;

  const consoleEl = container.querySelector('#logs-console');
  const connBadge = container.querySelector('#logs-conn-badge');
  const counterEl = container.querySelector('#logs-counter');
  const pauseBtn = actionsEl?.querySelector('#logs-pause-btn');
  const jumpBtn = container.querySelector('#logs-jump-bottom');
  const qrBtn = container.querySelector('#logs-qr-compact-btn');
  const levelPills = container.querySelector('#logs-level-pills');
  const presetSel = container.querySelector('#logs-preset-select');

  function setConnBadge(text, variant) {
    if (!connBadge) return;
    const dot = { 'badge-success': 'online', 'badge-amber': 'warn', 'badge-danger': 'offline' }[variant] || '';
    connBadge.innerHTML = `<span class="status-dot ${dot}"></span>${escapeHtml(text)}`;
  }

  function closeStreams() {
    if (eventSource) {
      try { eventSource.close(); } catch (_) {}
      eventSource = null;
    }
    if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
    if (napcatPollTimer) { clearInterval(napcatPollTimer); napcatPollTimer = null; }
  }

  function updateLevelCounts() {
    if (source === 'napcat') return;
    const counts = { ALL: entries.length, INFO: 0, WARN: 0, ERROR: 0, DEBUG: 0 };
    for (const e of entries) {
      if (counts[e.level] !== undefined) counts[e.level] += 1;
    }
    levelPills?.querySelectorAll('[data-lvl]').forEach((btn) => {
      const k = btn.dataset.lvl;
      btn.textContent = `${k} (${counts[k] || 0})`;
      btn.classList.toggle('active', k === level);
    });
  }

  function getFilteredEntries() {
    const q = search.trim().toLowerCase();
    return entries.filter((e) => {
      if (level !== 'ALL' && e.level !== level) return false;
      if (!matchesPreset(e, preset)) return false;
      if (q) {
        const hay = `${e.time} ${e.source} ${e.level} ${e.msg} ${JSON.stringify(e.fields)}`.toLowerCase();
        if (!hay.includes(q)) return false;
      }
      return true;
    });
  }

  function renderFieldsHtml(fields) {
    const pairs = Object.entries(fields || {}).filter(([, v]) => v !== '' && v !== null && v !== undefined);
    if (!pairs.length) return '';
    return `<span class="log-kvs">${pairs.map(([k, v]) => {
      const vs = String(v);
      let cls = 'log-kv';
      let extraAttr = '';
      if (k === 'silent' && vs === 'false') cls += ' hl-good';
      else if ((k === 'elapsed' || k === 'turn_duration' || k === 'duration') && parseDurationSecs(vs) >= 10) cls += ' hl-warn';
      else if (k === 'session' || k === 'group' || k === 'group_id') {
        cls += ' hl-link';
        extraAttr = `data-log-session="${escapeHtml(vs)}" title="点击跳转到会话"`;
      } else if (k === 'user_id' || k === 'sender_id') {
        cls += ' hl-link';
        extraAttr = `data-log-user="${escapeHtml(vs)}" title="点击查看用户记忆"`;
      }
      return `<span class="${cls}" ${extraAttr}>${escapeHtml(k)}=<strong>${highlightText(vs, search)}</strong></span>`;
    }).join('')}</span>`;
  }

  function renderConsole() {
    if (!consoleEl) return;
    const isNapcat = source === 'napcat';
    consoleEl.classList.toggle('qr-compact', isNapcat && qrCompact);
    if (levelPills) levelPills.style.display = isNapcat ? 'none' : 'inline-flex';
    if (presetSel) presetSel.style.display = isNapcat ? 'none' : '';
    if (qrBtn) {
      qrBtn.style.display = isNapcat ? 'inline-flex' : 'none';
      qrBtn.classList.toggle('active', qrCompact);
    }

    if (isNapcat) {
      if (napcatError) {
        consoleEl.innerHTML = `<div class="empty-state"><div class="empty-state-icon text-amber">${createIcon('alert', 20)}</div><div class="text-sm text-amber" style="max-width:520px;line-height:1.6;">${escapeHtml(napcatError)}</div></div>`;
        if (counterEl) counterEl.textContent = '0 行';
        return;
      }
      const q = search.trim().toLowerCase();
      const filtered = napcatLines.filter((line) => !q || line.toLowerCase().includes(q));
      if (counterEl) counterEl.textContent = `${filtered.length} / ${napcatLines.length} 行`;
      if (!filtered.length) {
        consoleEl.innerHTML = `<div class="empty-state"><div class="text-sm text-muted">暂无 NapCat 容器日志</div></div>`;
        return;
      }
      consoleEl.innerHTML = filtered.map((line) => {
        const lower = line.toLowerCase();
        const isQr = QR_ROW_RE.test(line);
        const cls = isQr
          ? 'qr-row'
          : (lower.includes('error') || lower.includes('fatal') ? 'lvl-error' : (lower.includes('warn') ? 'lvl-warn' : ''));
        return `<div class="log-line ${cls}"><span class="log-msg">${highlightText(line, search)}</span></div>`;
      }).join('');
    } else {
      updateLevelCounts();
      const visible = getFilteredEntries();
      if (counterEl) counterEl.textContent = `${visible.length} / ${entries.length} 条`;
      if (!visible.length) {
        consoleEl.innerHTML = `<div class="empty-state"><div class="text-sm text-muted">暂无匹配日志</div></div>`;
        return;
      }
      consoleEl.innerHTML = visible.map((e) => {
        const lvlCls = e.level === 'ERROR' ? 'lvl-error' : e.level === 'WARN' ? 'lvl-warn' : '';
        const srcCls = e.source === 'cc-connect' ? 'src-cc' : e.source === 'ws-guard' ? 'src-ws' : 'src-nc';
        const shortTime = e.time.length > 19 ? e.time.slice(11, 19) : e.time;
        return `
          <div class="log-line ${lvlCls}">
            <span class="log-ts">${escapeHtml(shortTime)}</span>
            <span class="log-src ${srcCls}">${escapeHtml(e.source)}</span>
            <span class="log-lvl ${e.level}">${e.level}</span>
            <span class="log-msg">${highlightText(e.msg, search)}</span>
            ${renderFieldsHtml(e.fields)}
          </div>
        `;
      }).join('');
    }

    if (autoScroll) {
      consoleEl.scrollTop = consoleEl.scrollHeight;
      if (jumpBtn) jumpBtn.style.display = 'none';
    }
  }

  function pushEntries(list) {
    let added = false;
    for (const raw of list) {
      const e = normalizeEntry(raw);
      if (!e || seenIds.has(e.id)) continue;
      seenIds.add(e.id);
      if (paused) {
        pauseQueue.push(e);
        if (pauseQueue.length > MAX_BUFFER) pauseQueue.shift();
      } else {
        entries.push(e);
        if (entries.length > MAX_BUFFER) {
          const dropped = entries.shift();
          if (dropped) seenIds.delete(dropped.id);
        }
        added = true;
      }
    }
    if (paused) {
      setConnBadge(`已暂停（+${pauseQueue.length}）`, 'badge-amber');
      if (pauseBtn) pauseBtn.innerHTML = `${createIcon('play', 14)}继续（+${pauseQueue.length}）`;
    } else if (added) {
      renderConsole();
    }
  }

  async function loadNapcatLogs() {
    if (paused) return;
    try {
      const data = await apiGet('/api/napcat/logs?lines=180');
      if (destroyed || source !== 'napcat') return;
      if (data && data.ok === false) {
        napcatLines = [];
        napcatError = String(data.error || '无法读取 NapCat 容器日志');
        setConnBadge('容器日志不可用', 'badge-amber');
        renderConsole();
        return;
      }
      napcatError = '';
      const rawText = String(
        data?.clean_text
        || data?.raw
        || (Array.isArray(data?.lines) ? data.lines.join('\n') : '')
        || data?.logs
        || data?.text
        || '',
      );
      napcatLines = rawText.replace(ANSI_RE, '').split('\n').filter((l) => l.length > 0);
      if (data?.has_qr_hint && !napcatHasQr) {
        napcatHasQr = true;
        qrCompact = true;
      }
      setConnBadge('容器日志 · 5s 轮询', 'badge-success');
      renderConsole();
    } catch (err) {
      if (destroyed || source !== 'napcat') return;
      napcatError = err.message;
      setConnBadge('拉取失败', 'badge-danger');
      renderConsole();
    }
  }

  async function connectSource() {
    closeStreams();
    entries = [];
    seenIds = new Set();
    pauseQueue = [];

    if (source === 'napcat') {
      setConnBadge('拉取中…', 'badge-muted');
      await loadNapcatLogs();
      napcatPollTimer = setInterval(() => {
        if (!destroyed && !document.hidden && source === 'napcat') loadNapcatLogs();
      }, 5000);
      return;
    }

    setConnBadge('连接中…', 'badge-muted');
    try {
      const tail = await apiGet(`/api/logs/tail?source=${encodeURIComponent(source)}&lines=200`);
      if (destroyed) return;
      const list = Array.isArray(tail?.entries) ? tail.entries : (Array.isArray(tail) ? tail : []);
      pushEntries(list);
      renderConsole();
    } catch (err) {
      if (!destroyed) showToast(`拉取日志失败: ${err.message}`, 'error');
    }

    if (destroyed) return;
    const startSSE = () => {
      if (destroyed || source === 'napcat') return;
      const es = new EventSource(`/api/logs/stream?source=${encodeURIComponent(source)}&initial=0`);
      eventSource = es;
      es.onopen = () => {
        if (!paused) setConnBadge('实时连接中', 'badge-success');
      };
      es.onmessage = (ev) => {
        if (!ev.data || ev.data === 'ping' || ev.data === ':ping') return;
        try {
          const obj = JSON.parse(ev.data);
          pushEntries([obj]);
        } catch (_) {}
      };
      es.onerror = () => {
        try { es.close(); } catch (_) {}
        if (destroyed || source === 'napcat') return;
        setConnBadge('重连中…', 'badge-amber');
        reconnectTimer = setTimeout(startSSE, 2500);
      };
    };
    startSSE();
  }

  // Event listeners
  container.querySelectorAll('#logs-source-tabs [data-src]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const next = btn.dataset.src;
      if (next === source) return;
      source = next;
      paused = false;
      if (pauseBtn) pauseBtn.innerHTML = `${createIcon('pause', 14)}暂停`;
      container.querySelectorAll('#logs-source-tabs [data-src]').forEach((b) => b.classList.toggle('active', b.dataset.src === source));
      connectSource();
    });
  });

  levelPills?.querySelectorAll('[data-lvl]').forEach((btn) => {
    btn.addEventListener('click', () => {
      level = btn.dataset.lvl;
      renderConsole();
    });
  });

  presetSel?.addEventListener('change', (e) => {
    preset = e.target.value;
    renderConsole();
  });

  qrBtn?.addEventListener('click', () => {
    qrCompact = !qrCompact;
    renderConsole();
  });

  container.querySelector('#logs-search-input')?.addEventListener('input', (e) => {
    search = e.target.value;
    renderConsole();
  });

  container.querySelector('#logs-autoscroll')?.addEventListener('change', (e) => {
    autoScroll = e.target.checked;
    if (autoScroll && consoleEl) {
      consoleEl.scrollTop = consoleEl.scrollHeight;
      if (jumpBtn) jumpBtn.style.display = 'none';
    }
  });

  pauseBtn?.addEventListener('click', () => {
    paused = !paused;
    if (!paused) {
      if (pauseQueue.length) {
        const queued = pauseQueue;
        pauseQueue = [];
        entries.push(...queued);
        if (entries.length > MAX_BUFFER) entries = entries.slice(-MAX_BUFFER);
      }
      pauseBtn.innerHTML = `${createIcon('pause', 14)}暂停`;
      setConnBadge(source === 'napcat' ? '容器日志 · 5s 轮询' : '实时连接中', 'badge-success');
      renderConsole();
    } else {
      pauseBtn.innerHTML = `${createIcon('play', 14)}继续`;
      setConnBadge('已暂停', 'badge-amber');
    }
  });

  actionsEl?.querySelector('#logs-clear-btn')?.addEventListener('click', () => {
    entries = [];
    seenIds.clear();
    pauseQueue = [];
    napcatLines = [];
    renderConsole();
  });

  actionsEl?.querySelector('#logs-export-btn')?.addEventListener('click', () => {
    const content = source === 'napcat'
      ? napcatLines.join('\n')
      : getFilteredEntries().map((e) => e.raw || `${e.time} [${e.source}] ${e.level} ${e.msg}`).join('\n');
    const blob = new Blob([content], { type: 'text/plain;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `hajimi-logs-${source}-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-')}.txt`;
    a.click();
    URL.revokeObjectURL(url);
    showToast('日志已导出', 'success');
  });

  consoleEl?.addEventListener('scroll', () => {
    const nearBottom = consoleEl.scrollHeight - consoleEl.scrollTop - consoleEl.clientHeight < 48;
    if (jumpBtn) jumpBtn.style.display = nearBottom ? 'none' : 'inline-flex';
  });

  consoleEl?.addEventListener('click', (e) => {
    const sessEl = e.target.closest('[data-log-session]');
    if (sessEl) {
      ctx.navigate('sessions', { sessionKey: sessEl.dataset.logSession });
      return;
    }
    const userEl = e.target.closest('[data-log-user]');
    if (userEl) {
      ctx.navigate('memory', { userId: userEl.dataset.logUser });
    }
  });

  jumpBtn?.addEventListener('click', () => {
    if (consoleEl) consoleEl.scrollTop = consoleEl.scrollHeight;
    jumpBtn.style.display = 'none';
  });

  await connectSource();

  return {
    cleanup() {
      destroyed = true;
      closeStreams();
    },
    async refresh() {
      await connectSource();
    },
  };
}
