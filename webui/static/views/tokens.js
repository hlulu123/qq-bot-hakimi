import {
  apiGet,
  apiPost,
  createIcon,
  escapeHtml,
  formatDuration,
  formatRelativeTime,
  formatTime,
  formatTokens,
  formatTokensExact,
  showToast,
} from '../api.js';

const RANGES = [
  { id: 'today', label: '今日' },
  { id: '24h', label: '24 小时' },
  { id: '7d', label: '7 天' },
  { id: '30d', label: '30 天' },
  { id: 'all', label: '全部' },
];

const FILTERS = [
  { id: 'all', label: '全部会话' },
  { id: 'group', label: '群聊' },
  { id: 'dm', label: '私聊' },
  { id: 'system', label: '后台' },
];

const SORTS = [
  { id: 'total_tokens', label: '按总消耗' },
  { id: 'calls', label: '按调用次数' },
  { id: 'avg_tokens', label: '按单轮均值' },
  { id: 'silent_tokens', label: '按沉默监听' },
  { id: 'last_active', label: '按最近活跃' },
];

const SOURCE_META = {
  chat: { label: '主对话', cls: 'badge-accent' },
  history_followup: { label: '翻查历史', cls: 'badge-info' },
  memory_extract: { label: '记忆提炼', cls: 'badge-success' },
  memory_sleep: { label: '夜间整理', cls: 'badge-info' },
  log_backfill: { label: '日志回溯', cls: '' },
};

const COLOR_REPLY = 'var(--accent)';
const COLOR_SILENT = 'var(--chart-muted)';
const COLOR_MEMORY = 'var(--info)';

function getAvatarUrl(chatType, targetId) {
  const id = String(targetId || '').replace(/\D/g, '');
  if (!id) return '';
  if (chatType === 'group') return `https://p.qlogo.cn/gh/${id}/${id}/100/`;
  if (chatType === 'dm') return `https://q1.qlogo.cn/g?b=qq&nk=${id}&s=100`;
  return '';
}

function renderAvatar(chatType, targetId, name, sizeCls = 'avatar-sm') {
  const url = getAvatarUrl(chatType, targetId);
  const initial = escapeHtml((String(name || '?').trim()[0] || '?').toUpperCase());
  if (!url) {
    return `<span class="avatar ${sizeCls}">${chatType === 'system' ? createIcon('cpu', 14) : initial}</span>`;
  }
  return `
    <span class="avatar ${sizeCls}">
      <img src="${escapeHtml(url)}" alt="" loading="lazy" data-tok-avatar="1" />
      <span class="tok-avatar-fallback" style="display:none;">${initial}</span>
    </span>
  `;
}

function typeLabel(chatType) {
  if (chatType === 'group') return '群聊';
  if (chatType === 'dm') return '私聊';
  return '后台';
}

function legendHtml() {
  return `
    <span class="legend">
      <span class="legend-item"><span class="legend-swatch" style="background:${COLOR_REPLY};"></span>回复</span>
      <span class="legend-item"><span class="legend-swatch" style="background:${COLOR_SILENT};"></span>沉默监听</span>
      <span class="legend-item"><span class="legend-swatch" style="background:${COLOR_MEMORY};opacity:0.85;"></span>后台记忆</span>
    </span>
  `;
}

function renderTrendChart(buckets, chartId) {
  const list = Array.isArray(buckets) ? buckets : [];
  if (!list.length) {
    return '<div class="empty-state" style="padding:40px 12px;">暂无该时段的消耗趋势</div>';
  }
  const maxVal = Math.max(1, ...list.map((b) => Number(b.total_tokens || 0)));
  const W = 640;
  const H = 200;
  const padL = 46;
  const padR = 4;
  const padT = 12;
  const padB = 24;
  const chartW = W - padL - padR;
  const chartH = H - padT - padB;
  const step = chartW / list.length;
  const barW = Math.max(5, Math.min(18, step * 0.58));
  const tickEvery = list.length <= 8 ? 1 : list.length <= 16 ? 2 : list.length <= 25 ? 4 : Math.ceil(list.length / 7);

  const grid = [0, 0.5, 1].map((ratio) => {
    const y = padT + chartH * (1 - ratio);
    return `
      <line x1="${padL}" y1="${y.toFixed(1)}" x2="${W - padR}" y2="${y.toFixed(1)}" stroke="var(--border-subtle)" />
      <text x="${padL - 8}" y="${(y + 3.5).toFixed(1)}" text-anchor="end" fill="var(--text-muted)" font-size="10">${escapeHtml(formatTokens(maxVal * ratio))}</text>
    `;
  }).join('');

  const bars = list.map((b, i) => {
    const tot = Number(b.total_tokens || 0);
    const rep = Number(b.reply_tokens || 0);
    const sil = Number(b.silent_tokens || 0);
    const x = padL + i * step + (step - barW) / 2;
    const totalH = tot > 0 ? Math.max(2, (tot / maxVal) * chartH) : 0;
    const scale = tot > 0 ? totalH / tot : 0;
    const repH = rep * scale;
    const silH = sil * scale;
    const memH = Math.max(0, totalH - repH - silH);
    const yBase = padT + chartH;
    const yRep = yBase - repH;
    const ySil = yRep - silH;
    const yMem = ySil - memH;
    const showTick = i % tickEvery === 0 || i === list.length - 1;
    return `
      <g class="tok-chart-col" data-chart="${escapeHtml(chartId)}" data-idx="${i}">
        <rect x="${(padL + i * step).toFixed(1)}" y="${padT}" width="${step.toFixed(1)}" height="${chartH}" fill="transparent" />
        ${memH > 0.3 ? `<rect x="${x.toFixed(1)}" y="${yMem.toFixed(1)}" width="${barW.toFixed(1)}" height="${memH.toFixed(1)}" rx="2" fill="${COLOR_MEMORY}" opacity="0.85" />` : ''}
        ${silH > 0.3 ? `<rect x="${x.toFixed(1)}" y="${ySil.toFixed(1)}" width="${barW.toFixed(1)}" height="${silH.toFixed(1)}" rx="2" fill="${COLOR_SILENT}" />` : ''}
        ${repH > 0.3 ? `<rect x="${x.toFixed(1)}" y="${yRep.toFixed(1)}" width="${barW.toFixed(1)}" height="${repH.toFixed(1)}" rx="2" fill="${COLOR_REPLY}" />` : ''}
        ${showTick ? `<text x="${(x + barW / 2).toFixed(1)}" y="${H - 6}" text-anchor="middle" fill="var(--text-muted)" font-size="10">${escapeHtml(String(b.label || ''))}</text>` : ''}
      </g>
    `;
  }).join('');

  return `<svg viewBox="0 0 ${W} ${H}" style="width:100%;height:auto;display:block;" role="img" aria-label="Token 消耗趋势">${grid}${bars}</svg>`;
}

function renderStackedBar(replyTok, silentTok, memoryTok, totalTok) {
  const tot = Math.max(1, Number(totalTok || 0));
  const repPct = Math.min(100, (Number(replyTok || 0) / tot) * 100);
  const silPct = Math.min(100 - repPct, (Number(silentTok || 0) / tot) * 100);
  const memPct = Math.min(100 - repPct - silPct, (Number(memoryTok || 0) / tot) * 100);
  return `
    <div class="tok-bar-track" aria-hidden="true">
      ${repPct > 0 ? `<div class="tok-bar-reply" style="width:${repPct.toFixed(1)}%;"></div>` : ''}
      ${silPct > 0 ? `<div class="tok-bar-silent" style="width:${silPct.toFixed(1)}%;"></div>` : ''}
      ${memPct > 0 ? `<div class="tok-bar-memory" style="width:${memPct.toFixed(1)}%;"></div>` : ''}
    </div>
  `;
}

export async function mount(container, ctx) {
  let rangeKey = RANGES.some((r) => r.id === ctx.params?.range) ? ctx.params.range : '24h';
  let chatFilter = FILTERS.some((f) => f.id === ctx.params?.filter) ? ctx.params.filter : 'all';
  let sortBy = 'total_tokens';
  let searchQuery = '';

  let dashboard = null;
  let selectedChatKey = String(ctx.params?.chat || '').trim();
  let chatDetail = null;
  let detailLoading = false;
  let detailError = '';
  let syncing = false;
  let hoveringChart = false;
  let destroyed = false;
  let pollTimer = null;
  let detailReqSeq = 0;

  container.innerHTML = `
    <div id="tok-main"></div>
    <div class="drawer-backdrop" id="tok-drawer-backdrop"></div>
    <aside class="drawer" id="tok-drawer" aria-label="会话消耗明细" aria-hidden="true"></aside>
  `;
  const mainEl = container.querySelector('#tok-main');
  const drawerEl = container.querySelector('#tok-drawer');
  const backdropEl = container.querySelector('#tok-drawer-backdrop');

  const onAvatarError = (e) => {
    const img = e.target;
    if (img && img.tagName === 'IMG' && img.dataset?.tokAvatar === '1') {
      img.style.display = 'none';
      const fallback = img.nextElementSibling;
      if (fallback && fallback.classList.contains('tok-avatar-fallback')) {
        fallback.style.display = 'inline';
      }
    }
  };
  container.addEventListener('error', onAvatarError, true);

  function renderHeaderActions() {
    const el = ctx.setActions(`
      <button type="button" class="btn btn-secondary" id="tok-sync-btn" ${syncing ? 'disabled' : ''} title="从 cc-connect.log 增量同步历史轮次">
        <span class="${syncing ? 'spin' : ''}">${createIcon('download', 15)}</span>同步日志
      </button>
    `);
    el?.querySelector('#tok-sync-btn')?.addEventListener('click', () => triggerSync());
  }

  async function fetchDashboard({ silent = false, loadSelectedDetail = true } = {}) {
    try {
      const qs = new URLSearchParams({ range: rangeKey, filter: chatFilter, sort: sortBy });
      const data = await apiGet(`/api/tokens?${qs.toString()}`);
      if (destroyed) return;
      dashboard = data;
      render();
      if (loadSelectedDetail && selectedChatKey) {
        await fetchChatDetail(selectedChatKey, { silent: true });
      }
    } catch (err) {
      if (destroyed || silent) return;
      mainEl.innerHTML = `
        <div class="card">
          <div class="empty-state">
            <div class="empty-state-icon">${createIcon('alert', 20)}</div>
            <div class="empty-state-title">加载 Token 统计失败</div>
            <div class="text-sm">${escapeHtml(err.message)}</div>
            <button type="button" class="btn btn-primary btn-sm mt-8" id="tok-retry-btn">${createIcon('refresh', 14)} 重试</button>
          </div>
        </div>
      `;
      mainEl.querySelector('#tok-retry-btn')?.addEventListener('click', () => fetchDashboard());
    }
  }

  async function fetchChatDetail(sessionKey, { silent = false } = {}) {
    const key = String(sessionKey || '').trim();
    if (!key) {
      closeDetail();
      return;
    }
    const seq = ++detailReqSeq;
    selectedChatKey = key;
    if (!silent) {
      detailLoading = true;
      detailError = '';
      renderDrawer();
      render();
    }
    try {
      const qs = new URLSearchParams({ range: rangeKey, limit: '50' });
      const data = await apiGet(`/api/tokens/chat/${encodeURIComponent(key)}?${qs.toString()}`);
      if (destroyed || seq !== detailReqSeq) return;
      chatDetail = data;
      detailLoading = false;
      detailError = '';
      renderDrawer();
    } catch (err) {
      if (destroyed || seq !== detailReqSeq) return;
      detailLoading = false;
      detailError = err.message || '加载明细失败';
      renderDrawer();
    }
  }

  function closeDetail() {
    selectedChatKey = '';
    chatDetail = null;
    detailError = '';
    detailLoading = false;
    detailReqSeq += 1;
    renderDrawer();
    render();
  }

  async function triggerSync() {
    if (syncing) return;
    syncing = true;
    renderHeaderActions();
    try {
      const res = await apiPost('/api/tokens/sync', { force: false });
      if (destroyed) return;
      const ins = Number(res?.inserted || 0);
      showToast(ins > 0 ? `已从日志新增同步 ${ins} 条轮次记录` : '已是最新，没有新增记录', 'success');
      await fetchDashboard({ silent: true });
      ctx.refreshGlobalStatus?.({ silent: true });
    } catch (err) {
      if (!destroyed) showToast(`同步失败：${err.message}`, 'error');
    } finally {
      if (!destroyed) {
        syncing = false;
        renderHeaderActions();
      }
    }
  }

  function getFilteredChats() {
    const list = Array.isArray(dashboard?.chats) ? dashboard.chats : [];
    const q = searchQuery.trim().toLowerCase();
    if (!q) return list;
    return list.filter((c) =>
      String(c.name || '').toLowerCase().includes(q) ||
      String(c.session_key || '').toLowerCase().includes(q) ||
      String(c.target_id || '').toLowerCase().includes(q));
  }

  // -------------------------------------------------------------------------
  // Drawer: single chat detail
  // -------------------------------------------------------------------------
  function renderDrawer() {
    const open = Boolean(selectedChatKey);
    drawerEl.classList.toggle('open', open);
    backdropEl.classList.toggle('open', open);
    drawerEl.setAttribute('aria-hidden', open ? 'false' : 'true');
    if (!open) return;

    const closeBtn = `<button type="button" class="btn btn-ghost btn-icon" data-tok-close title="关闭" aria-label="关闭">${createIcon('x', 18)}</button>`;

    if (detailLoading && (!chatDetail || chatDetail.chat?.session_key !== selectedChatKey)) {
      drawerEl.innerHTML = `
        <div class="drawer-head"><div><h2 class="drawer-title">正在加载明细…</h2></div>${closeBtn}</div>
        <div class="drawer-body">
          <div class="loading-skeleton" style="height:96px;"></div>
          <div class="loading-skeleton" style="height:220px;"></div>
          <div class="loading-skeleton" style="height:260px;"></div>
        </div>
      `;
      bindDrawer();
      return;
    }
    if (detailError) {
      drawerEl.innerHTML = `
        <div class="drawer-head"><div><h2 class="drawer-title">明细加载失败</h2><div class="drawer-sub">${escapeHtml(detailError)}</div></div>${closeBtn}</div>
        <div class="drawer-body"><div class="empty-state"><button type="button" class="btn btn-primary btn-sm" id="tok-detail-retry">${createIcon('refresh', 14)} 重试</button></div></div>
      `;
      bindDrawer();
      drawerEl.querySelector('#tok-detail-retry')?.addEventListener('click', () => fetchChatDetail(selectedChatKey));
      return;
    }
    if (!chatDetail || !chatDetail.chat) return;

    const c = chatDetail.chat;
    const dTrend = Array.isArray(chatDetail.trend) ? chatDetail.trend : [];
    const members = Array.isArray(chatDetail.top_members) ? chatDetail.top_members : [];
    const recent = Array.isArray(chatDetail.recent_events) ? chatDetail.recent_events : [];
    const exactCalls = Number(c.exact_calls || 0);
    const cacheRate = c.cache_hit_rate != null ? Number(c.cache_hit_rate) : null;

    const prevScroll = drawerEl.querySelector('.drawer-body')?.scrollTop || 0;
    const sameChat = drawerEl.dataset.chat === selectedChatKey;
    drawerEl.dataset.chat = selectedChatKey;
    drawerEl.innerHTML = `
      <div class="drawer-head">
        <div class="row" style="gap:14px;min-width:0;">
          ${renderAvatar(c.chat_type, c.target_id, c.name, 'avatar-lg')}
          <div style="min-width:0;">
            <h2 class="drawer-title truncate">${escapeHtml(c.name || c.session_key)}</h2>
            <div class="drawer-sub">
              ${escapeHtml(typeLabel(c.chat_type))}${c.target_id ? ` · <span class="mono">${escapeHtml(c.target_id)}</span>` : ''}
              · ${escapeHtml(chatDetail.range_label || '')} · 占总消耗 ${Number(c.share_pct || 0).toFixed(1)}%
              · 最近活跃 ${c.last_active_iso ? escapeHtml(formatRelativeTime(c.last_active_iso)) : '--'}
            </div>
          </div>
        </div>
        <div class="row-sm" style="flex-shrink:0;">
          ${c.chat_type !== 'system' ? `<button type="button" class="btn btn-secondary btn-sm" data-tok-open-session="${escapeHtml(c.session_key)}">${createIcon('chat', 14)}聊天记录</button>` : ''}
          ${closeBtn}
        </div>
      </div>

      <div class="drawer-body">
        <div class="kpi-strip">
          <div class="kpi">
            <span class="kpi-label">总消耗</span>
            <span class="kpi-value" style="font-size:24px;" title="${escapeHtml(formatTokensExact(c.total_tokens))} tokens">${escapeHtml(formatTokens(c.total_tokens))}</span>
            <span class="kpi-sub">${Number(c.calls || 0).toLocaleString()} 次调用</span>
          </div>
          <div class="kpi">
            <span class="kpi-label">单轮均值</span>
            <span class="kpi-value" style="font-size:24px;">${escapeHtml(formatTokens(c.avg_tokens))}</span>
            <span class="kpi-sub">上下文峰值 ${escapeHtml(formatTokens(c.max_context_used))}</span>
          </div>
          <div class="kpi">
            <span class="kpi-label">沉默监听</span>
            <span class="kpi-value" style="font-size:24px;">${Number(c.silent_share_pct || 0).toFixed(1)}<span class="kpi-unit">%</span></span>
            ${renderStackedBar(c.reply_tokens, c.silent_tokens, c.memory_tokens, c.total_tokens)}
          </div>
          <div class="kpi">
            <span class="kpi-label">${exactCalls > 0 ? '新输入' : '后台记忆'}</span>
            <span class="kpi-value" style="font-size:24px;">${escapeHtml(formatTokens(exactCalls > 0 ? c.input_tokens : c.memory_tokens))}</span>
            <span class="kpi-sub">${exactCalls > 0
              ? `缓存 ${escapeHtml(formatTokens(c.cached_read_tokens))} · 输出 ${escapeHtml(formatTokens(c.output_tokens))}${cacheRate !== null ? ` · 命中 ${cacheRate.toFixed(1)}%` : ''}`
              : `日志回溯 ${Number(c.log_backfill_calls || 0)} 次`}</span>
          </div>
        </div>

        <div class="section">
          <div class="section-head">
            <h3 class="section-title" style="font-size:15px;">消耗趋势</h3>
            ${legendHtml()}
          </div>
          <div>
            <div id="tok-detail-chart-wrap">${renderTrendChart(dTrend, 'detail')}</div>
            <div class="chart-readout mt-8" id="tok-detail-chart-readout">将鼠标移到柱上查看该时段消耗</div>
          </div>
        </div>

        <div class="section">
          <div class="section-head">
            <h3 class="section-title" style="font-size:15px;">触发消耗的成员</h3>
            <span class="text-xs text-muted">前 ${Math.min(10, members.length)} 位</span>
          </div>
          ${members.length ? `
            <div class="card">
              <table class="data-table">
                <thead><tr><th style="width:40px;">#</th><th>成员</th><th style="text-align:right;">回复 / 潜水</th><th style="text-align:right;">消耗</th><th style="text-align:right;width:70px;">占比</th></tr></thead>
                <tbody>
                  ${members.slice(0, 10).map((m, idx) => `
                    <tr>
                      <td class="text-muted tabular">${idx + 1}</td>
                      <td>${m.sender_id
                        ? `<button type="button" class="link-btn" style="color:var(--text);" data-tok-user="${escapeHtml(m.sender_id)}">${escapeHtml(m.sender_name || m.sender_id)}</button>`
                        : `<span>${escapeHtml(m.sender_name || '未知')}</span>`}</td>
                      <td class="tabular text-muted" style="text-align:right;">${Number(m.reply_calls || 0)} / ${Number(m.silent_calls || 0)}</td>
                      <td class="tabular font-semibold" style="text-align:right;" title="${escapeHtml(formatTokensExact(m.total_tokens))} tokens">${escapeHtml(formatTokens(m.total_tokens))}</td>
                      <td class="tabular text-muted" style="text-align:right;">${Number(m.share_pct || 0).toFixed(1)}%</td>
                    </tr>
                  `).join('')}
                </tbody>
              </table>
            </div>
          ` : '<div class="card"><div class="empty-state" style="padding:28px;">暂无成员消耗记录</div></div>'}
        </div>

        <div class="section">
          <div class="section-head">
            <h3 class="section-title" style="font-size:15px;">最近调用</h3>
            <span class="text-xs text-muted">最新 ${recent.length} 条，含主对话、历史翻查与后台记忆</span>
          </div>
          ${recent.length ? `
            <div class="card table-wrap">
              <table class="data-table">
                <thead>
                  <tr>
                    <th style="width:96px;">时间</th><th>触发人</th><th style="width:96px;">来源</th><th style="width:110px;">结果</th>
                    <th style="text-align:right;">新入 / 缓存 / 输出</th><th style="width:80px;text-align:right;">总量</th><th style="width:64px;text-align:right;">耗时</th>
                  </tr>
                </thead>
                <tbody>
                  ${recent.map((ev) => {
                    const sm = SOURCE_META[ev.source] || { label: ev.source, cls: '' };
                    const isMem = ev.source === 'memory_extract' || ev.source === 'memory_sleep';
                    const status = isMem
                      ? '<span class="text-muted text-sm">后台提炼</span>'
                      : ev.is_silent
                        ? '<span class="text-muted text-sm">潜水</span>'
                        : `<span class="row-sm text-sm"><span class="status-dot online" style="width:6px;height:6px;"></span>回复${ev.response_len ? ` ${ev.response_len} 字` : ''}</span>`;
                    const split = ev.precision === 'exact'
                      ? `<span class="mono text-xs text-secondary">${escapeHtml(formatTokens(ev.input_tokens))} / ${escapeHtml(formatTokens(ev.cached_read_tokens))} / ${escapeHtml(formatTokens(ev.output_tokens))}</span>`
                      : '<span class="text-xs text-muted">仅总量</span>';
                    return `
                      <tr>
                        <td class="mono text-xs text-muted">${escapeHtml(formatTime(ev.time_iso))}</td>
                        <td class="truncate" style="max-width:150px;" title="${escapeHtml(ev.sender_name || ev.sender_id || '--')}">${escapeHtml(ev.sender_name || ev.sender_id || '--')}</td>
                        <td><span class="badge badge-sm ${sm.cls}">${escapeHtml(sm.label)}</span></td>
                        <td>${status}</td>
                        <td style="text-align:right;">${split}</td>
                        <td class="tabular font-semibold" style="text-align:right;" title="${escapeHtml(formatTokensExact(ev.total_tokens))} tokens">${escapeHtml(formatTokens(ev.total_tokens))}</td>
                        <td class="tabular text-xs text-muted" style="text-align:right;">${formatDuration(ev.duration_sec)}</td>
                      </tr>
                    `;
                  }).join('')}
                </tbody>
              </table>
            </div>
          ` : '<div class="card"><div class="empty-state" style="padding:28px;">该时间段内暂无记录</div></div>'}
        </div>
      </div>
    `;
    if (sameChat && prevScroll) {
      const body = drawerEl.querySelector('.drawer-body');
      if (body) body.scrollTop = prevScroll;
    }
    bindDrawer();
    bindChartHover(drawerEl, '#tok-detail-chart-wrap', '#tok-detail-chart-readout', dTrend, '将鼠标移到柱上查看该时段消耗');
  }

  function bindDrawer() {
    drawerEl.querySelectorAll('[data-tok-close]').forEach((b) => b.addEventListener('click', closeDetail));
    drawerEl.querySelectorAll('[data-tok-open-session]').forEach((btn) => {
      btn.addEventListener('click', () => {
        const key = btn.dataset.tokOpenSession;
        if (key) ctx.navigate('sessions', { sessionKey: key });
      });
    });
    drawerEl.querySelectorAll('[data-tok-user]').forEach((btn) => {
      btn.addEventListener('click', () => {
        const uid = btn.dataset.tokUser;
        if (uid) ctx.navigate('memory', { userId: uid });
      });
    });
  }

  // -------------------------------------------------------------------------
  // Main page
  // -------------------------------------------------------------------------
  function render() {
    renderHeaderActions();
    if (!dashboard) return;
    const sum = dashboard.summary || {};
    const trend = Array.isArray(dashboard.trend) ? dashboard.trend : [];
    const groupShares = Array.isArray(dashboard.group_shares) ? dashboard.group_shares : [];
    const chats = getFilteredChats();

    const totalTok = Number(sum.total_tokens || 0);
    const totalCalls = Number(sum.total_calls || 0);
    const groupTok = Number(sum.group_tokens || 0);
    const groupPct = Number(sum.group_share_pct || 0);
    const silentTok = Number(sum.silent_tokens || 0);
    const silentPct = Number(sum.silent_share_pct || 0);
    const exactCalls = Number(sum.exact_calls || 0);
    const logCalls = Number(sum.log_backfill_calls || 0);
    const cacheRate = sum.cache_hit_rate != null ? Number(sum.cache_hit_rate) : null;
    const topGroup = sum.top_group || null;

    mainEl.innerHTML = `
      <div class="stack-lg">
        <div class="toolbar-bar">
          <div class="tabs" role="tablist" aria-label="统计时间范围">
            ${RANGES.map((r) => `<button type="button" class="tab-btn ${rangeKey === r.id ? 'active' : ''}" data-tok-range="${r.id}">${escapeHtml(r.label)}</button>`).join('')}
          </div>
          <div class="row wrap">
            ${exactCalls > 0 || logCalls > 0 ? `
              <span class="text-xs text-muted" title="精确分项来自 agy-acp-wrapper 与后台记忆提炼；日志回溯来自历史 cc-connect.log">
                ${exactCalls > 0 ? `精确记录 ${exactCalls.toLocaleString()}` : ''}${exactCalls > 0 && logCalls > 0 ? ' · ' : ''}${logCalls > 0 ? `日志回溯 ${logCalls.toLocaleString()}` : ''}
              </span>
            ` : ''}
            <div class="filter-pills" role="group" aria-label="会话类型">
              ${FILTERS.map((f) => `<button type="button" class="pill-btn ${chatFilter === f.id ? 'active' : ''}" data-tok-filter="${f.id}">${escapeHtml(f.label)}</button>`).join('')}
            </div>
          </div>
        </div>

        <section class="kpi-strip">
          <div class="kpi">
            <span class="kpi-label">总消耗 · ${escapeHtml(dashboard.range_label || '')}</span>
            <span class="kpi-value" title="${escapeHtml(formatTokensExact(totalTok))} tokens">${escapeHtml(formatTokens(totalTok))}</span>
            <span class="kpi-sub">${totalCalls.toLocaleString()} 次调用 · 均 ${escapeHtml(formatTokens(sum.avg_tokens_per_call))}/次</span>
          </div>
          <div class="kpi ${topGroup ? 'clickable' : ''}" ${topGroup ? `data-tok-select="${escapeHtml(topGroup.session_key)}" title="查看最高消耗群"` : ''}>
            <span class="kpi-label"><span>群聊消耗</span>${topGroup ? createIcon('arrowUpRight', 14) : ''}</span>
            <span class="kpi-value" title="${escapeHtml(formatTokensExact(groupTok))} tokens">${escapeHtml(formatTokens(groupTok))}</span>
            <span class="kpi-sub">${topGroup
              ? `占 ${groupPct.toFixed(1)}% · 最高 <span class="truncate" style="max-width:120px;display:inline-block;vertical-align:bottom;">${escapeHtml(topGroup.name)}</span>`
              : `活跃群 ${Number(sum.active_groups_count || 0)} · 私聊 ${Number(sum.active_dms_count || 0)}`}</span>
          </div>
          <div class="kpi">
            <span class="kpi-label">沉默监听</span>
            <span class="kpi-value ${silentPct >= 70 ? 'text-amber' : ''}">${silentPct.toFixed(1)}<span class="kpi-unit">%</span></span>
            <span class="kpi-sub">${escapeHtml(formatTokens(silentTok))} · ${Number(sum.silent_calls || 0)} 次潜水${silentPct >= 50 ? ` · <button type="button" class="link-btn" style="color:var(--accent);font-size:12.5px;" data-tok-nav="plugins">去省流</button>` : ''}</span>
          </div>
          <div class="kpi">
            <span class="kpi-label">${exactCalls > 0 ? '新输入 / 缓存 / 输出' : '后台记忆'}</span>
            <span class="kpi-value">${escapeHtml(formatTokens(exactCalls > 0 ? sum.input_tokens : sum.memory_tokens))}</span>
            <span class="kpi-sub">${exactCalls > 0
              ? `缓存 ${escapeHtml(formatTokens(sum.cached_read_tokens))} · 输出 ${escapeHtml(formatTokens(sum.output_tokens))}${cacheRate !== null ? ` · 命中 ${cacheRate.toFixed(1)}%` : ''}`
              : '新会话将自动记录输入 / 缓存 / 思考分项'}</span>
          </div>
        </section>

        <section class="overview-analytics-grid">
          <div class="card">
            <div class="card-header">
              <h3 class="card-title">消耗趋势</h3>
              ${legendHtml()}
            </div>
            <div class="card-body">
              <div id="tok-main-chart-wrap">${renderTrendChart(trend, 'main')}</div>
              <div class="chart-readout mt-8" id="tok-main-chart-readout">将鼠标移到柱上查看各时段消耗构成</div>
            </div>
          </div>

          <div class="card" style="display:flex;flex-direction:column;">
            <div class="card-header">
              <h3 class="card-title">各群占比</h3>
              <span class="text-xs text-muted">点击查看明细</span>
            </div>
            <div style="padding:8px;flex:1;min-height:0;">
              ${groupShares.length ? `
                <div class="tok-share-list" style="max-height:268px;overflow-y:auto;">
                  ${groupShares.map((g, idx) => `
                    <div class="tok-share-item ${selectedChatKey === g.session_key ? 'is-selected' : ''}" data-tok-select="${escapeHtml(g.session_key)}">
                      <div class="row-between">
                        <div class="row-sm flex-1" style="min-width:0;">
                          <span class="text-xs text-muted tabular" style="width:16px;">${idx + 1}</span>
                          <span class="truncate text-sm">${escapeHtml(g.name || g.session_key)}</span>
                        </div>
                        <div class="row-sm tabular" style="flex-shrink:0;">
                          <strong class="text-sm">${escapeHtml(formatTokens(g.total_tokens))}</strong>
                          <span class="text-xs text-muted" style="min-width:42px;text-align:right;">${Number(g.share_pct || 0).toFixed(1)}%</span>
                        </div>
                      </div>
                      <div class="row" style="gap:10px;padding-left:22px;">
                        <div class="progress-bar flex-1" style="height:4px;"><div class="progress-fill" style="width:${Math.min(100, Math.max(2, Number(g.share_pct || 0))).toFixed(1)}%;"></div></div>
                        <span class="text-xs text-muted tabular" style="flex-shrink:0;">${Number(g.calls || 0)} 次 · 潜水 ${Number(g.silent_share_pct || 0).toFixed(0)}%</span>
                      </div>
                    </div>
                  `).join('')}
                </div>
              ` : '<div class="empty-state" style="padding:40px 12px;">该时间段暂无群聊记录</div>'}
            </div>
          </div>
        </section>

        <section class="section">
          <div class="section-head">
            <div>
              <h2 class="section-title">全部会话 <span class="text-muted" style="font-weight:500;">${chats.length}</span></h2>
              <p class="section-desc">点击任意一行，在右侧查看该会话的消耗明细。</p>
            </div>
            <div class="row-sm wrap">
              <div class="search-box" style="width:220px;">
                <span class="search-icon">${createIcon('search', 14)}</span>
                <input type="text" id="tok-search-input" value="${escapeHtml(searchQuery)}" placeholder="搜索群名或 QQ 号" autocomplete="off" />
                ${searchQuery ? `<button type="button" class="search-clear" id="tok-search-clear" title="清空">${createIcon('x', 12)}</button>` : ''}
              </div>
              <select class="select" id="tok-sort-select" style="width:132px;" aria-label="排序方式">
                ${SORTS.map((s) => `<option value="${s.id}" ${sortBy === s.id ? 'selected' : ''}>${escapeHtml(s.label)}</option>`).join('')}
              </select>
            </div>
          </div>
          <div class="card table-wrap">
            ${chats.length ? `
              <table class="data-table">
                <thead>
                  <tr>
                    <th style="min-width:220px;">会话</th>
                    <th style="min-width:170px;">消耗</th>
                    <th style="text-align:right;">单轮均值</th>
                    <th>调用</th>
                    <th style="text-align:right;">沉默占比</th>
                    <th style="text-align:right;">上下文峰值</th>
                    <th style="text-align:right;">最近活跃</th>
                  </tr>
                </thead>
                <tbody>
                  ${chats.map((c) => {
                    const silPct = Number(c.silent_share_pct || 0);
                    return `
                      <tr class="clickable ${selectedChatKey === c.session_key ? 'tok-row-active' : ''}" data-tok-select="${escapeHtml(c.session_key)}">
                        <td>
                          <div class="row" style="gap:10px;">
                            ${renderAvatar(c.chat_type, c.target_id, c.name, 'avatar-sm')}
                            <div style="min-width:0;line-height:1.35;">
                              <div class="truncate font-semibold" style="max-width:220px;" title="${escapeHtml(c.name || c.session_key)}">${escapeHtml(c.name || c.session_key)}</div>
                              <div class="text-xs text-muted">${escapeHtml(typeLabel(c.chat_type))}${c.target_id ? ` · <span class="mono">${escapeHtml(c.target_id)}</span>` : ''}</div>
                            </div>
                          </div>
                        </td>
                        <td>
                          <div class="row-between tabular" style="margin-bottom:6px;">
                            <strong title="${escapeHtml(formatTokensExact(c.total_tokens))} tokens">${escapeHtml(formatTokens(c.total_tokens))}</strong>
                            <span class="text-xs text-muted">${Number(c.share_pct || 0).toFixed(1)}%</span>
                          </div>
                          ${renderStackedBar(c.reply_tokens, c.silent_tokens, c.memory_tokens, c.total_tokens)}
                        </td>
                        <td class="tabular text-secondary" style="text-align:right;">${escapeHtml(formatTokens(c.avg_tokens))}</td>
                        <td class="tabular text-sm text-secondary" style="white-space:nowrap;">${Number(c.calls || 0)} <span class="text-muted text-xs">回 ${Number(c.reply_calls || 0)} · 潜 ${Number(c.silent_calls || 0)}${Number(c.memory_calls || 0) > 0 ? ` · 记 ${Number(c.memory_calls)}` : ''}</span></td>
                        <td class="tabular ${silPct >= 75 ? 'text-amber' : 'text-secondary'}" style="text-align:right;">${silPct.toFixed(1)}%</td>
                        <td class="tabular text-muted" style="text-align:right;">${Number(c.max_context_used || 0) > 0 ? escapeHtml(formatTokens(c.max_context_used)) : '--'}</td>
                        <td class="text-sm text-muted" style="text-align:right;white-space:nowrap;">${c.last_active_iso ? escapeHtml(formatRelativeTime(c.last_active_iso)) : '--'}</td>
                      </tr>
                    `;
                  }).join('')}
                </tbody>
              </table>
            ` : `
              <div class="empty-state">
                <div class="empty-state-icon">${createIcon('barChart', 20)}</div>
                <div class="empty-state-title">${searchQuery ? '没有匹配的会话' : '这个时间范围内没有消耗记录'}</div>
                <div class="text-sm">${searchQuery ? '换个关键词试试，或清空搜索。' : '可以切换到「全部」查看历史，或点击右上角「同步日志」。'}</div>
              </div>
            `}
          </div>
        </section>
      </div>
    `;

    bindEvents(trend);
  }

  function bindChartHover(root, wrapSel, readoutSel, buckets, defaultText) {
    const wrap = root.querySelector(wrapSel);
    const readout = root.querySelector(readoutSel);
    if (!wrap || !readout) return;
    wrap.addEventListener('mouseenter', () => { hoveringChart = true; });
    wrap.addEventListener('mouseleave', () => {
      hoveringChart = false;
      readout.textContent = defaultText;
    });
    wrap.querySelectorAll('.tok-chart-col').forEach((col) => {
      col.addEventListener('mouseenter', () => {
        const b = buckets[Number(col.dataset.idx)];
        if (!b) return;
        readout.textContent =
          `${b.label}　共 ${formatTokens(b.total_tokens)}（${formatTokensExact(b.total_tokens)}）· ` +
          `回复 ${formatTokens(b.reply_tokens)} · 沉默 ${formatTokens(b.silent_tokens)}` +
          (Number(b.memory_tokens || 0) > 0 ? ` · 记忆 ${formatTokens(b.memory_tokens)}` : '') +
          ` · ${Number(b.calls || 0)} 次调用`;
      });
    });
  }

  function bindEvents(mainTrend) {
    mainEl.querySelectorAll('[data-tok-nav]').forEach((el) => {
      el.addEventListener('click', (e) => {
        e.stopPropagation();
        ctx.navigate(el.dataset.tokNav);
      });
    });

    mainEl.querySelectorAll('[data-tok-range]').forEach((btn) => {
      btn.addEventListener('click', () => {
        const next = btn.dataset.tokRange;
        if (next && next !== rangeKey) {
          rangeKey = next;
          fetchDashboard();
        }
      });
    });

    mainEl.querySelectorAll('[data-tok-filter]').forEach((btn) => {
      btn.addEventListener('click', () => {
        const next = btn.dataset.tokFilter;
        if (next && next !== chatFilter) {
          chatFilter = next;
          fetchDashboard();
        }
      });
    });

    const searchInput = mainEl.querySelector('#tok-search-input');
    searchInput?.addEventListener('input', () => {
      const pos = searchInput.selectionStart;
      searchQuery = searchInput.value;
      render();
      const nextInput = mainEl.querySelector('#tok-search-input');
      if (nextInput) {
        nextInput.focus();
        if (pos != null) nextInput.setSelectionRange(pos, pos);
      }
    });
    mainEl.querySelector('#tok-search-clear')?.addEventListener('click', () => {
      searchQuery = '';
      render();
    });

    mainEl.querySelector('#tok-sort-select')?.addEventListener('change', (e) => {
      sortBy = e.target.value || 'total_tokens';
      fetchDashboard({ loadSelectedDetail: false });
    });

    mainEl.querySelectorAll('[data-tok-select]').forEach((el) => {
      el.addEventListener('click', (e) => {
        if (e.target.closest('button')) return;
        const key = el.dataset.tokSelect;
        if (key) fetchChatDetail(key);
      });
    });

    bindChartHover(mainEl, '#tok-main-chart-wrap', '#tok-main-chart-readout', mainTrend, '将鼠标移到柱上查看各时段消耗构成');
  }

  backdropEl.addEventListener('click', closeDetail);
  const onKeyDown = (e) => {
    if (e.key === 'Escape' && selectedChatKey && !document.querySelector('.modal-backdrop')) closeDetail();
  };
  document.addEventListener('keydown', onKeyDown);

  renderHeaderActions();
  await fetchDashboard({ loadSelectedDetail: false });
  if (selectedChatKey) fetchChatDetail(selectedChatKey);

  pollTimer = setInterval(() => {
    if (!destroyed && !document.hidden && !syncing && !hoveringChart && document.activeElement?.id !== 'tok-search-input') {
      fetchDashboard({ silent: true, loadSelectedDetail: Boolean(selectedChatKey) });
    }
  }, 20000);

  return {
    cleanup() {
      destroyed = true;
      if (pollTimer) clearInterval(pollTimer);
      document.removeEventListener('keydown', onKeyDown);
      container.removeEventListener('error', onAvatarError, true);
    },
    async refresh() {
      await fetchDashboard({ silent: false, loadSelectedDetail: Boolean(selectedChatKey) });
    },
  };
}
