import {
  apiGet,
  apiPost,
  confirmModal,
  createIcon,
  escapeHtml,
  formatDuration,
  formatTime,
  formatTokens,
  formatTokensExact,
  showToast,
} from '../api.js';

const PLUGIN_PRESET_NAMES = {
  ultra_lean: '极简省流',
  token_saver: '日常省流',
  balanced_group: '活跃群聊',
  full_featured: '全功能',
  custom: '自定义',
};

const ACTION_META = {
  'restart-ws-guard': { label: '无感重启 ws-guard' },
  'reload-cc-connect': { label: '重载 cc-connect 配置' },
  'sync-sandbox': { label: '同步人设到沙盒' },
  'start-bot': { label: '启动机器人' },
  'restart-bot': {
    label: '重启机器人',
    confirm: true,
    title: '重启机器人？',
    message: '将平滑重启 ws-guard 与 cc-connect 以完整应用最新配置，期间可能有数秒短暂中断。',
  },
  'stop-bot': {
    label: '停止机器人',
    confirm: true,
    danger: true,
    title: '停止机器人？',
    message: '将停止 cc-connect 与 ws-guard，机器人将不再响应 QQ 消息（NapCat 容器不受影响）。',
  },
};

function getSvcState(svc) {
  if (!svc || typeof svc !== 'object') return 'offline';
  const s = String(svc.status || '').toLowerCase();
  if (s === 'degraded') return 'warn';
  if (svc.ok === true || svc.online === true) return 'online';
  if (svc.running === true && (svc.ok === false || svc.online === false)) return 'warn';
  return svc.running ? 'online' : 'offline';
}

function safeAvatarUrl(rawUrl) {
  const s = String(rawUrl || '').trim();
  if (!s) return '';
  if (/^https?:\/\//i.test(s) || /^data:image\/(png|jpe?g|gif|webp|avif);base64,/i.test(s)) {
    return s;
  }
  return '';
}

function avatarUrl(uin) {
  const id = String(uin || '').replace(/\D/g, '');
  return id ? safeAvatarUrl(`https://q1.qlogo.cn/g?b=qq&nk=${id}&s=140`) : '';
}

function render24hChart(hourly) {
  const list = Array.isArray(hourly) ? hourly : [];
  if (!list.length) {
    return `<div class="empty-state" style="padding:40px 12px;">暂无 24 小时数据</div>`;
  }
  const maxVal = Math.max(1, ...list.map((h) => Number(h.total || (h.replied || 0) + (h.silent || 0))));
  const W = 640;
  const H = 200;
  const padL = 30;
  const padR = 4;
  const padT = 12;
  const padB = 24;
  const chartW = W - padL - padR;
  const chartH = H - padT - padB;
  const step = chartW / list.length;
  const barW = Math.max(6, Math.min(16, step * 0.58));

  const grid = [0, 0.5, 1].map((ratio) => {
    const y = padT + chartH * (1 - ratio);
    return `
      <line x1="${padL}" y1="${y.toFixed(1)}" x2="${W - padR}" y2="${y.toFixed(1)}" stroke="var(--border-subtle)" />
      <text x="${padL - 8}" y="${(y + 3.5).toFixed(1)}" text-anchor="end" fill="var(--text-muted)" font-size="10">${Math.round(maxVal * ratio)}</text>
    `;
  }).join('');

  const bars = list.map((h, i) => {
    const replied = Number(h.replied || 0);
    const silent = Number(h.silent || 0);
    const total = Number(h.total ?? replied + silent);
    const x = padL + i * step + (step - barW) / 2;
    const totalH = (total / maxVal) * chartH;
    const repH = (replied / maxVal) * chartH;
    const silH = Math.max(0, totalH - repH);
    const yTop = padT + chartH - totalH;
    const yRep = padT + chartH - repH;
    const label = h.label || `${String(h.hour ?? i).padStart(2, '0')}:00`;
    const showTick = i % 4 === 0 || i === list.length - 1;
    return `
      <g class="ov-chart-col" data-idx="${i}" style="cursor:default;">
        <rect x="${(padL + i * step).toFixed(1)}" y="${padT}" width="${step.toFixed(1)}" height="${chartH}" fill="transparent" />
        ${silH > 0 ? `<rect x="${x.toFixed(1)}" y="${yTop.toFixed(1)}" width="${barW.toFixed(1)}" height="${silH.toFixed(1)}" rx="2.5" fill="var(--chart-muted)" />` : ''}
        ${repH > 0 ? `<rect x="${x.toFixed(1)}" y="${yRep.toFixed(1)}" width="${barW.toFixed(1)}" height="${repH.toFixed(1)}" rx="2.5" fill="var(--accent)" />` : ''}
        ${showTick ? `<text x="${(x + barW / 2).toFixed(1)}" y="${H - 6}" text-anchor="middle" fill="var(--text-muted)" font-size="10">${escapeHtml(String(label).slice(0, 5))}</text>` : ''}
      </g>
    `;
  }).join('');

  return `<svg viewBox="0 0 ${W} ${H}" style="width:100%;height:auto;display:block;" role="img" aria-label="24 小时消息趋势">${grid}${bars}</svg>`;
}

export async function mount(container, ctx) {
  let overview = null;
  let analytics = null;
  let lastSeenOverviewTs = 0;
  let busyAction = '';
  let showHealthDetails = false;
  let hoveringChart = false;
  let destroyed = false;
  let syncTimer = null;
  let analyticsTimer = null;
  let followTimer = null;

  const onAvatarError = (e) => {
    if (e.target && e.target.tagName === 'IMG' && e.target.classList.contains('js-ov-avatar-img')) {
      e.target.remove();
    }
  };
  container.addEventListener('error', onAvatarError, true);

  async function fetchData({ silent = false, forceOverview = false, syncGlobal = false } = {}) {
    try {
      const globalFresh =
        !forceOverview &&
        !syncGlobal &&
        ctx.state?.overview &&
        Date.now() - Number(ctx.state.lastUpdated || 0) < 4000;

      const ovUrl = forceOverview ? '/api/overview?refresh=1' : '/api/overview';
      const ovPromise = globalFresh
        ? Promise.resolve(ctx.state.overview)
        : syncGlobal && typeof ctx.refreshGlobalStatus === 'function'
          ? ctx.refreshGlobalStatus({ silent: true, force: forceOverview }).then((d) => d || apiGet(ovUrl))
          : apiGet(ovUrl);

      const [ov, an] = await Promise.all([
        ovPromise,
        apiGet('/api/analytics').catch(() => null),
      ]);
      if (destroyed) return;
      overview = ov;
      lastSeenOverviewTs = Number(ctx.state?.lastUpdated || Date.now());
      analytics = an;
      render();
    } catch (err) {
      if (destroyed || silent) return;
      container.innerHTML = `
        <div class="card">
          <div class="empty-state">
            <div class="empty-state-icon">${createIcon('alert', 20)}</div>
            <div class="empty-state-title">加载总览失败</div>
            <div class="text-sm">${escapeHtml(err.message)}</div>
            <button type="button" class="btn btn-primary btn-sm mt-8" id="ov-retry-btn">${createIcon('refresh', 14)} 重试</button>
          </div>
        </div>
      `;
      container.querySelector('#ov-retry-btn')?.addEventListener('click', () => fetchData({ forceOverview: true }));
    }
  }

  async function runServiceAction(action) {
    if (busyAction) return;
    const meta = ACTION_META[action] || { label: action };
    if (meta.confirm) {
      const ok = await confirmModal({
        title: meta.title || `确认${meta.label}？`,
        message: meta.message || '',
        confirmText: meta.label,
        danger: Boolean(meta.danger),
      });
      if (!ok || destroyed) return;
    }
    busyAction = action;
    render();
    try {
      const res = await apiPost(`/api/control/${encodeURIComponent(action)}`, {});
      if (destroyed) return;
      showToast((res && res.message) || `${meta.label}已完成`, 'success');
      await fetchData({ silent: true, forceOverview: true, syncGlobal: true });
      if (['restart-bot', 'start-bot', 'stop-bot', 'restart-ws-guard'].includes(action)) {
        if (followTimer) clearTimeout(followTimer);
        followTimer = setTimeout(() => {
          if (!destroyed) {
            fetchData({ silent: true, forceOverview: true, syncGlobal: true });
          }
        }, 3200);
      }
    } catch (err) {
      if (!destroyed) showToast(`${meta.label}失败：${err.message}`, 'error');
    } finally {
      if (!destroyed) {
        busyAction = '';
        render();
      }
    }
  }

  function renderHeaderActions(botRunning) {
    const dis = busyAction ? 'disabled' : '';
    const spin = (act) => (busyAction === act ? 'spin' : '');
    const el = ctx.setActions(`
      <button type="button" class="btn btn-ghost" data-ov-action="sync-sandbox" ${dis} title="将 workspace/GEMINI.md 同步到 chat-sandbox/">
        <span class="${spin('sync-sandbox')}">${createIcon('repeat', 15)}</span>同步沙盒
      </button>
      ${botRunning ? `
        <button type="button" class="btn btn-secondary" data-ov-action="restart-bot" ${dis}>
          <span class="${spin('restart-bot')}">${createIcon('rotate', 15)}</span>重启
        </button>
        <button type="button" class="btn btn-danger" data-ov-action="stop-bot" ${dis}>
          ${createIcon('power', 15)}停止
        </button>
      ` : `
        <button type="button" class="btn btn-primary" data-ov-action="start-bot" ${dis}>
          <span class="${spin('start-bot')}">${createIcon('play', 14)}</span>启动机器人
        </button>
      `}
    `);
    el?.querySelectorAll('[data-ov-action]').forEach((btn) => {
      btn.addEventListener('click', () => runServiceAction(btn.dataset.ovAction));
    });
  }

  function render() {
    if (!overview) return;
    const services = overview.services || {};
    const napcat = services.napcat || {};
    const ws = services.ws_guard || {};
    const cc = services.cc_connect || {};
    const acp = services.acp || services.acp_wrappers || {};
    const health = overview.config_health || {};
    const mem = overview.memory_summary || {};
    const rt = overview.runtime || {};
    const bot = overview.bot || {};
    const tok = overview.token_summary && typeof overview.token_summary === 'object' ? overview.token_summary : null;
    const tokBySession = tok?.tokens_by_session && typeof tok.tokens_by_session === 'object' ? tok.tokens_by_session : {};
    const tokTopGroups = Array.isArray(tok?.top_groups) ? tok.top_groups.slice(0, 4) : [];
    const tokH24 = Number(tok?.last_24h_tokens || 0);
    const tokToday = Number(tok?.today_tokens || 0);
    const tokAll = Number(tok?.all_time_tokens || 0);
    const tokSilentPct = Number(tok?.last_24h_silent_pct || 0);
    const tokCacheHit = tok?.cache_hit_rate != null ? Number(tok.cache_hit_rate) : null;
    const tokPrimaryVal = tokH24 > 0 ? tokH24 : tokAll;
    const tokPrimaryCalls = tokH24 > 0 ? Number(tok?.last_24h_calls || 0) : Number(tok?.all_time_calls || 0);
    const ps = overview.plugins_summary && typeof overview.plugins_summary === 'object' ? overview.plugins_summary : null;
    const reqSummary = overview.group_requests_summary && typeof overview.group_requests_summary === 'object'
      ? overview.group_requests_summary
      : null;
    const pendingJoinRequests = Number(reqSummary?.pending_count || 0);

    const napState = getSvcState(napcat);
    const wsState = getSvcState(ws);
    const ccState = getSvcState(cc);
    const upCount = [napState, wsState, ccState].filter((s) => s === 'online').length;
    const botRunning = wsState === 'online' || ccState === 'online';
    const overall = upCount === 3 ? 'online' : upCount > 0 ? 'warn' : 'offline';
    const acpCount = Number(acp.count ?? acp.process_count ?? 0);

    const problems = Array.isArray(health.problems) ? health.problems : [];
    const checks = Array.isArray(health.checks) ? health.checks : [];
    const passedChecks = checks.filter((c) => c.ok !== false).length;
    const personaSynced = health.persona_in_sync !== false;
    const sandboxClean = health.sandbox_clean !== false;
    const allHealthy = health.valid !== false && problems.length === 0 && personaSynced && sandboxClean;

    const summary = analytics?.summary || {};
    const hourly = Array.isArray(analytics?.hourly) ? analytics.hourly : [];
    const topChats = Array.isArray(analytics?.top_chats) ? analytics.top_chats.slice(0, 6) : [];
    const recentTurns = Array.isArray(analytics?.recent_turns) ? analytics.recent_turns.slice(0, 10) : [];
    const totalTurns = Number(summary.total_turns || 0);
    const repliedTurns = Number(summary.replied_turns || 0);
    const silentTurns = Number(summary.silent_turns || 0);
    const repeatTurns = Number(summary.repeat_joins ?? summary.repeat_turns ?? 0);
    const replyRate = totalTurns > 0
      ? (summary.reply_rate !== undefined ? Number(summary.reply_rate) : (repliedTurns / totalTurns) * 100)
      : 0;

    renderHeaderActions(botRunning);

    const nickname = napcat.nickname || bot.nickname || '哈基米';
    const uin = napcat.user_id || napcat.qq || bot.qq || '';
    const avatar = avatarUrl(uin);
    const stateText = overall === 'online' ? '运行正常' : overall === 'warn' ? `部分服务异常 · ${upCount}/3 在线` : '机器人已停止';
    const groupCount = napcat.group_count ?? napcat.groups_count;
    const friendCount = napcat.friend_count ?? napcat.friends_count;
    const liveSessions = cc.live_sessions ?? cc.active_sessions ?? overview.sessions_summary?.live_count ?? 0;

    const node = ({ state, name, stateLabel, kvs, action }) => `
      <div class="pipeline-node ${state}">
        <div class="pipeline-node-head">
          <span class="pipeline-node-name"><span class="status-dot ${state}"></span>${escapeHtml(name)}</span>
          <span class="pipeline-node-state">${escapeHtml(stateLabel)}</span>
        </div>
        <div class="kv-list">
          ${kvs.map(([k, v]) => `<div class="kv"><span>${escapeHtml(k)}</span><strong title="${escapeHtml(String(v))}">${escapeHtml(String(v))}</strong></div>`).join('')}
        </div>
        <div class="pipeline-actions">${action}</div>
      </div>
    `;
    const dis = busyAction ? 'disabled' : '';
    const spin = (act) => (busyAction === act ? 'spin' : '');

    container.innerHTML = `
      <div class="stack-lg">
        ${allHealthy ? '' : `
          <div class="alert-banner alert-warning">
            <div class="row flex-1">
              <span class="text-amber">${createIcon('alert', 16)}</span>
              <span>
                ${!personaSynced ? '聊天沙盒中的 GEMINI.md 与线上人设未同步。' : ''}
                ${!sandboxClean ? '聊天沙盒内存在多余文件。' : ''}
                ${problems.length ? escapeHtml(problems.slice(0, 2).join('；')) : ''}
              </span>
            </div>
            <div class="row-sm">
              ${!personaSynced ? `<button type="button" class="btn btn-primary btn-sm" data-ov-action="sync-sandbox" ${dis}>立即同步</button>` : ''}
              ${problems.length ? `<button type="button" class="btn btn-secondary btn-sm" data-ov-nav="settings" data-ov-tab="system">前往修复</button>` : ''}
              ${!sandboxClean ? `<button type="button" class="btn btn-secondary btn-sm" data-ov-nav="permissions" data-ov-tab="sandbox">检查沙盒</button>` : ''}
            </div>
          </div>
        `}

        ${pendingJoinRequests > 0 ? `
          <div class="alert-banner alert-info" id="ov-requests-banner">
            <div class="row flex-1" style="min-width:0;">
              <span class="text-accent">${createIcon('userPlus', 16)}</span>
              <span>
                当前有 <strong>${escapeHtml(String(pendingJoinRequests))}</strong> 条待审批的入群申请，可前往处理或打开独立盯盘窗口。
              </span>
            </div>
            <div class="row-sm">
              <a class="btn btn-primary btn-sm" href="#requests" data-ov-nav="requests">前往入群监控 ${createIcon('arrowRight', 14)}</a>
              <button type="button" class="btn btn-secondary btn-sm" id="ov-open-join-monitor">${createIcon('popout', 14)} 独立盯盘窗</button>
            </div>
          </div>
        ` : ''}

        <section class="hero">
          <div class="hero-main">
            <span class="hero-avatar">
              ${escapeHtml(String(nickname).trim()[0] || '哈')}
              ${avatar ? `<img class="js-ov-avatar-img" src="${escapeHtml(avatar)}" alt="" style="position:absolute;inset:0;" />` : ''}
            </span>
            <div style="min-width:0;">
              <div class="hero-title">
                <span class="truncate">${escapeHtml(nickname)}</span>
                <span class="hero-state ${overall}"><span class="status-dot ${overall}"></span>${escapeHtml(stateText)}</span>
              </div>
              <div class="hero-meta">
                ${uin ? `<span class="mono">${escapeHtml(uin)}</span><span>·</span>` : ''}
                <span>${groupCount != null ? `${escapeHtml(groupCount)} 个群` : '群数未知'}</span>
                ${friendCount != null ? `<span>·</span><span>${escapeHtml(friendCount)} 位好友</span>` : ''}
                ${napcat.napcat_version ? `<span>·</span><span>NapCat v${escapeHtml(napcat.napcat_version)}</span>` : ''}
              </div>
            </div>
          </div>
          <div class="hero-side">
            <div class="hero-stat"><span class="hero-stat-label">已运行</span><span class="hero-stat-value">${ccState === 'online' ? formatDuration(cc.uptime_seconds) : '--'}</span></div>
            <div class="hero-stat"><span class="hero-stat-label">活跃会话</span><span class="hero-stat-value">${escapeHtml(liveSessions)}</span></div>
            <div class="hero-stat"><span class="hero-stat-label">配置体检</span><span class="hero-stat-value ${allHealthy ? '' : 'text-amber'}">${checks.length ? `${passedChecks}/${checks.length}` : (allHealthy ? '通过' : '异常')}</span></div>
          </div>
        </section>

        <section class="kpi-strip">
          <div class="kpi">
            <span class="kpi-label">处理轮次</span>
            <span class="kpi-value">${totalTurns.toLocaleString()}</span>
            <span class="kpi-sub">回复 ${repliedTurns.toLocaleString()} · 潜水 ${silentTurns.toLocaleString()}</span>
          </div>
          <div class="kpi">
            <span class="kpi-label">回复率</span>
            <span class="kpi-value">${replyRate.toFixed(1)}<span class="kpi-unit">%</span></span>
            <div class="progress-bar" style="margin-top:6px;"><div class="progress-fill" style="width:${Math.min(100, Math.max(0, replyRate)).toFixed(1)}%;"></div></div>
          </div>
          <div class="kpi">
            <span class="kpi-label">平均响应</span>
            <span class="kpi-value">${formatDuration(summary.avg_duration_sec)}</span>
            <span class="kpi-sub">P50 ${formatDuration(summary.p50_duration_sec)} · P95 ${formatDuration(summary.p95_duration_sec)}</span>
          </div>
          <div class="kpi clickable" data-ov-nav="tokens" title="查看 Token 明细">
            <span class="kpi-label"><span>${tokH24 > 0 ? '近 24 小时 Token' : '累计 Token'}</span>${createIcon('arrowUpRight', 14)}</span>
            <span class="kpi-value" title="${escapeHtml(formatTokensExact(tokPrimaryVal))} tokens">${escapeHtml(formatTokens(tokPrimaryVal))}</span>
            <span class="kpi-sub">${escapeHtml(tokPrimaryCalls.toLocaleString())} 次调用 · 今日 ${escapeHtml(formatTokens(tokToday))}</span>
          </div>
        </section>

        <section class="section">
          <div class="section-head">
            <div>
              <h2 class="section-title">服务链路</h2>
              <p class="section-desc">QQ 消息经 NapCat → ws-guard → cc-connect 交给 ACP 智能体处理。</p>
            </div>
            ${checks.length ? `<button type="button" class="link-btn" id="ov-toggle-health">${showHealthDetails ? '收起体检明细' : `体检明细 · ${passedChecks}/${checks.length} 通过`} ${createIcon(showHealthDetails ? 'chevronUp' : 'chevronDown', 14)}</button>` : ''}
          </div>
          ${showHealthDetails && checks.length ? `
            <div class="card card-body" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:10px 24px;">
              ${checks.map((c) => `
                <div class="row-sm text-sm" style="min-width:0;">
                  <span class="${c.ok !== false ? 'text-success' : 'text-danger'}">${createIcon(c.ok !== false ? 'check' : 'alert', 14)}</span>
                  <span class="font-semibold" style="white-space:nowrap;">${escapeHtml(c.name || '')}</span>
                  <span class="text-muted truncate" title="${escapeHtml(c.detail || '')}">${escapeHtml(c.detail || '')}</span>
                </div>
              `).join('')}
            </div>
          ` : ''}
          <div class="pipeline-grid">
            ${node({
              state: napState,
              name: 'NapCat',
              stateLabel: napState === 'online' ? '在线' : napState === 'warn' ? '待登录' : '离线',
              kvs: [
                ['账号', napcat.nickname || '--'],
                ['端口', `${napcat.http_port || 3000} / ${napcat.ws_port || 3001}`],
                ['群 / 好友', `${groupCount ?? '--'} / ${friendCount ?? '--'}`],
              ],
              action: `<button type="button" class="btn btn-ghost btn-sm" style="margin-left:-8px;" data-ov-nav="napcat">管理 QQ 连接 ${createIcon('arrowRight', 14)}</button>`,
            })}
            ${node({
              state: wsState,
              name: 'ws-guard',
              stateLabel: wsState === 'online' ? '运行中' : wsState === 'warn' ? '异常' : '未运行',
              kvs: [
                ['PID / 端口', `${ws.pid || '--'} · ${ws.listen_port || 3002}`],
                ['内存', ws.rss_mb != null ? `${Number(ws.rss_mb).toFixed(1)} MB` : '--'],
                ['复读跟进', `${repeatTurns} 次`],
              ],
              action: `<button type="button" class="btn btn-ghost btn-sm" style="margin-left:-8px;" data-ov-action="restart-ws-guard" ${dis}><span class="${spin('restart-ws-guard')}">${createIcon('zap', 14)}</span>无感重启</button>`,
            })}
            ${node({
              state: ccState,
              name: 'cc-connect',
              stateLabel: ccState === 'online' ? '运行中' : ccState === 'warn' ? '就绪中' : '未运行',
              kvs: [
                ['版本', cc.version || '--'],
                ['运行时长', formatDuration(cc.uptime_seconds)],
                ['活跃会话', `${liveSessions} 个`],
              ],
              action: `<button type="button" class="btn btn-ghost btn-sm" style="margin-left:-8px;" data-ov-action="reload-cc-connect" ${dis}><span class="${spin('reload-cc-connect')}">${createIcon('refresh', 14)}</span>重载配置</button>`,
            })}
            ${node({
              state: acpCount > 0 ? 'online' : '',
              name: 'ACP 与记忆',
              stateLabel: `${acpCount} 个进程`,
              kvs: [
                ['ACP 内存', acp.total_rss_mb != null || acp.rss_mb != null ? `${Number(acp.total_rss_mb ?? acp.rss_mb).toFixed(1)} MB` : '--'],
                ['建档群友', `${mem.total_users ?? 0} 人`],
                ['记忆条目', `${mem.total_facts ?? 0} 条`],
              ],
              action: `<button type="button" class="btn btn-ghost btn-sm" style="margin-left:-8px;" data-ov-nav="memory">打开记忆库 ${createIcon('arrowRight', 14)}</button>`,
            })}
          </div>
        </section>

        <section class="section">
          <div class="section-head">
            <div>
              <h2 class="section-title">活跃度</h2>
              <p class="section-desc">近 24 小时的消息处理与最活跃的会话。</p>
            </div>
          </div>
          <div class="overview-analytics-grid">
            <div class="card">
              <div class="card-header">
                <h3 class="card-title">24 小时趋势</h3>
                <span class="legend">
                  <span class="legend-item"><span class="legend-swatch" style="background:var(--accent);"></span>回复</span>
                  <span class="legend-item"><span class="legend-swatch" style="background:var(--chart-muted);"></span>潜水</span>
                </span>
              </div>
              <div class="card-body">
                <div id="ov-chart-wrap">${render24hChart(hourly)}</div>
                <div class="chart-readout mt-8" id="ov-chart-readout">将鼠标移到柱上查看每小时详情</div>
              </div>
            </div>

            <div class="card">
              <div class="card-header">
                <h3 class="card-title">活跃会话</h3>
                <button type="button" class="link-btn" data-ov-nav="sessions">全部 ${createIcon('arrowRight', 14)}</button>
              </div>
              ${topChats.length ? `
                <table class="data-table" style="table-layout:fixed;">
                  <colgroup><col /><col style="width:64px;" /><col style="width:72px;" /><col style="width:76px;" /></colgroup>
                  <thead>
                    <tr><th>会话</th><th style="text-align:right;">轮次</th><th style="text-align:right;">回复率</th><th style="text-align:right;">Token</th></tr>
                  </thead>
                  <tbody>
                    ${topChats.map((c) => {
                      const rate = c.reply_rate != null ? Math.round(Number(c.reply_rate)) : (c.total ? Math.round(((c.replied || 0) / c.total) * 100) : 0);
                      const chatTok = Number(tokBySession[c.session_key] || 0);
                      return `
                        <tr class="clickable" data-ov-session="${escapeHtml(c.session_key || '')}">
                          <td>
                            <div class="row-sm" style="min-width:0;">
                              <span class="badge badge-sm">${c.is_group ? '群' : '私'}</span>
                              <span class="truncate" title="${escapeHtml(c.name || c.session_key || '')}">${escapeHtml(c.name || c.session_key || '--')}</span>
                            </div>
                          </td>
                          <td class="tabular" style="text-align:right;">${escapeHtml(c.total ?? 0)}</td>
                          <td class="tabular text-secondary" style="text-align:right;">${rate}%</td>
                          <td class="tabular" style="text-align:right;">
                            ${chatTok > 0
                              ? `<button type="button" class="link-btn mono" style="margin-left:auto;" data-ov-token-chat="${escapeHtml(c.session_key || '')}" title="${escapeHtml(formatTokensExact(chatTok))} tokens">${escapeHtml(formatTokens(chatTok))}</button>`
                              : '<span class="text-muted">--</span>'}
                          </td>
                        </tr>
                      `;
                    }).join('')}
                  </tbody>
                </table>
              ` : '<div class="empty-state" style="padding:40px 12px;">暂无活跃会话</div>'}
            </div>
          </div>
        </section>

        <section class="section">
          <div class="section-head">
            <div>
              <h2 class="section-title">消耗与功能</h2>
              <p class="section-desc">Token 花费去向，以及当前启用的插件和行为模式。</p>
            </div>
          </div>
          <div class="token-ov-card" id="ov-token-card" role="button" tabindex="0" title="查看 Token 明细">
            <div class="token-ov-left">
              <div class="row-between">
                <span class="text-sm text-muted">${tokH24 > 0 ? '近 24 小时消耗' : '累计消耗'}</span>
                <span class="link-btn">查看明细 ${createIcon('arrowRight', 14)}</span>
              </div>
              <div>
                <div class="kpi-value" style="font-size:36px;" title="${escapeHtml(formatTokensExact(tokPrimaryVal))} tokens">${escapeHtml(formatTokens(tokPrimaryVal))}<span class="kpi-unit">tokens</span></div>
                <div class="metric-strip mt-8">
                  <span class="metric-chip">今日 <strong>${escapeHtml(formatTokens(tokToday))}</strong></span>
                  <span class="metric-sep"></span>
                  <span class="metric-chip">累计 <strong>${escapeHtml(formatTokens(tokAll))}</strong></span>
                  <span class="metric-sep"></span>
                  <span class="metric-chip">调用 <strong>${escapeHtml(tokPrimaryCalls.toLocaleString())}</strong></span>
                </div>
              </div>
              <div class="row-sm wrap">
                ${tokPrimaryVal > 0 ? `
                  <span class="badge ${tokSilentPct >= 70 ? 'badge-amber' : ''}">沉默监听 ${tokSilentPct.toFixed(1)}%</span>
                  ${tokCacheHit !== null ? `<span class="badge badge-success">缓存命中 ${tokCacheHit.toFixed(1)}%</span>` : ''}
                  <span class="badge">${escapeHtml(String(tok?.all_time_groups ?? 0))} 个计费群</span>
                ` : '<span class="text-sm text-muted">暂无 Token 消耗记录，收到新消息后将自动累计。</span>'}
              </div>
            </div>
            <div class="token-ov-right">
              <div class="text-xs text-muted" style="margin-bottom:6px;">消耗最多的会话（${tok?.top_groups_window === '24h' ? '近 24h' : '累计'}）</div>
              ${tokTopGroups.length ? tokTopGroups.map((g, idx) => `
                <div class="token-ov-group-row" data-ov-token-chat="${escapeHtml(g.session_key || '')}" title="${escapeHtml(formatTokensExact(g.total_tokens))} tokens">
                  <span class="text-xs text-muted tabular" style="width:14px;">${idx + 1}</span>
                  <span class="truncate text-sm flex-1">${escapeHtml(g.name || g.session_key || '--')}</span>
                  <div class="token-ov-group-bar-wrap" aria-hidden="true"><div class="token-ov-group-bar-fill" style="width:${Math.min(100, Math.max(4, Number(g.share_pct || 0))).toFixed(1)}%;"></div></div>
                  <span class="tabular text-sm font-semibold" style="min-width:52px;text-align:right;">${escapeHtml(formatTokens(g.total_tokens))}</span>
                  <span class="tabular text-xs text-muted" style="min-width:40px;text-align:right;">${Number(g.share_pct || 0).toFixed(1)}%</span>
                </div>
              `).join('') : '<div class="text-sm text-muted" style="padding:12px 0;">暂无分群消耗记录</div>'}
            </div>
          </div>

          <div class="grid-2">
            ${ps && Number(ps.total_count) > 0 ? `
              <div class="stat-card clickable" data-ov-nav="plugins">
                <div class="stat-label"><span>插件</span>${createIcon('arrowUpRight', 14)}</div>
                <div class="row" style="align-items:baseline;gap:10px;">
                  <span class="stat-value">${escapeHtml(String(ps.enabled_count ?? 0))}<span class="kpi-unit">/ ${escapeHtml(String(ps.total_count ?? 0))} 已启用</span></span>
                </div>
                <div class="stat-sub">
                  <span class="badge badge-sm">${escapeHtml(PLUGIN_PRESET_NAMES[ps.active_preset] || ps.active_preset || '自定义')}</span>
                  ${Number(ps.per_turn_saved_tokens || 0) > 0
                    ? `<span>每轮节省 <strong class="text-success">${escapeHtml(String(ps.per_turn_saved_tokens))}</strong> tok（-${escapeHtml(String(ps.per_turn_save_pct ?? 0))}%）</span>`
                    : `<span>单轮附加约 ${escapeHtml(String(ps.per_turn_active_tokens ?? 0))} tok</span>`}
                  <span>· 后台 LLM ${escapeHtml(String(ps.background_llm_active_count ?? 0))} 个</span>
                </div>
              </div>
            ` : ''}
            <div class="stat-card clickable" data-ov-nav="settings">
              <div class="stat-label"><span>行为模式</span>${createIcon('arrowUpRight', 14)}</div>
              <div class="row-sm wrap" style="margin:4px 0 2px;">
                <span class="badge ${rt.ambient_listen_enabled ? 'badge-accent' : ''}">${rt.ambient_listen_enabled ? '全群监听' : '仅点名回复'}</span>
                <span class="badge ${rt.repeat_enabled ?? bot.repeat_enabled ? 'badge-accent' : ''}">复读${(rt.repeat_enabled ?? bot.repeat_enabled) ? '开启' : '关闭'}</span>
                <span class="badge ${rt.admin_dm_turbo ?? bot.admin_dm_turbo ? 'badge-accent' : ''}">Turbo ${(rt.admin_dm_turbo ?? bot.admin_dm_turbo) ? '开启' : '关闭'}</span>
              </div>
              <div class="stat-sub">思考强度：潜水 ${escapeHtml(rt.lurk_thought_level || bot.thought_levels?.lurk || 'low')} · 对话 ${escapeHtml(rt.thought_level || bot.thought_levels?.chat || 'medium')} · 任务 ${escapeHtml(rt.task_thought_level || bot.thought_levels?.task || 'xhigh')}</div>
            </div>
          </div>
        </section>

        <section class="section">
          <div class="section-head">
            <div>
              <h2 class="section-title">最近轮次</h2>
              <p class="section-desc">机器人最近处理的消息，潜水表示判断后选择不回复。</p>
            </div>
            <button type="button" class="link-btn" data-ov-nav="logs">实时日志 ${createIcon('arrowRight', 14)}</button>
          </div>
          <div class="card">
            ${recentTurns.length ? `
              <div class="table-wrap">
                <table class="data-table">
                  <thead>
                    <tr><th style="width:96px;">时间</th><th>会话</th><th>发言人</th><th style="width:86px;text-align:right;">耗时</th><th style="width:120px;">结果</th></tr>
                  </thead>
                  <tbody>
                    ${recentTurns.map((t) => {
                      const userRaw = String(t.user || '');
                      const uid = (userRaw.match(/(\d{5,15})/) || [])[1] || '';
                      return `
                        <tr>
                          <td class="mono text-xs text-muted">${escapeHtml(t.time_short || formatTime(t.time))}</td>
                          <td><button type="button" class="link-btn" style="max-width:280px;color:var(--text);" data-ov-session="${escapeHtml(t.session || '')}"><span class="truncate">${escapeHtml(t.name || t.session || '--')}</span></button></td>
                          <td>${uid ? `<button type="button" class="link-btn" data-ov-user="${escapeHtml(uid)}">${escapeHtml(userRaw)}</button>` : `<span class="text-secondary">${escapeHtml(userRaw || '--')}</span>`}</td>
                          <td class="tabular text-muted" style="text-align:right;">${formatDuration(t.duration_sec)}</td>
                          <td>${t.silent ? '<span class="text-muted text-sm">潜水</span>' : `<span class="row-sm text-sm"><span class="status-dot online" style="width:6px;height:6px;"></span>回复 ${escapeHtml(t.response_len ?? '')} 字</span>`}</td>
                        </tr>
                      `;
                    }).join('')}
                  </tbody>
                </table>
              </div>
            ` : '<div class="empty-state" style="padding:40px 12px;">暂无近期轮次</div>'}
          </div>
        </section>
      </div>
    `;

    bindEvents(hourly);
  }

  function bindEvents(hourly) {
    container.querySelector('#ov-toggle-health')?.addEventListener('click', () => {
      showHealthDetails = !showHealthDetails;
      render();
    });

    container.querySelectorAll('[data-ov-action]').forEach((btn) => {
      btn.addEventListener('click', () => runServiceAction(btn.dataset.ovAction));
    });

    container.querySelectorAll('[data-ov-nav]').forEach((el) => {
      el.addEventListener('click', (e) => {
        if (el.tagName === 'A') e.preventDefault();
        const tab = el.dataset.ovTab;
        ctx.navigate(el.dataset.ovNav, tab ? { tab } : {});
      });
    });

    container.querySelector('#ov-open-join-monitor')?.addEventListener('click', () => {
      const win = window.open(
        '/join-monitor',
        'hajimi_join_monitor',
        'width=460,height=720,resizable=yes,scrollbars=yes',
      );
      if (win) {
        try { win.focus(); } catch (_) {}
      } else {
        showToast('独立窗口被浏览器拦截，请允许弹窗或手动访问 /join-monitor', 'warning');
      }
    });

    const tokCard = container.querySelector('#ov-token-card');
    if (tokCard) {
      tokCard.addEventListener('click', (e) => {
        if (e.target.closest('[data-ov-token-chat]')) return;
        ctx.navigate('tokens');
      });
      tokCard.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          ctx.navigate('tokens');
        }
      });
    }

    container.querySelectorAll('[data-ov-token-chat]').forEach((el) => {
      el.addEventListener('click', (e) => {
        e.stopPropagation();
        const key = el.dataset.ovTokenChat;
        ctx.navigate('tokens', key ? { chat: key } : {});
      });
    });

    container.querySelectorAll('[data-ov-session]').forEach((el) => {
      el.addEventListener('click', (e) => {
        if (e.target.closest('[data-ov-token-chat]')) return;
        const key = el.dataset.ovSession;
        if (key) ctx.navigate('sessions', { sessionKey: key });
      });
    });

    container.querySelectorAll('[data-ov-user]').forEach((el) => {
      el.addEventListener('click', () => {
        const uid = el.dataset.ovUser;
        if (uid) ctx.navigate('memory', { userId: uid });
      });
    });

    const readout = container.querySelector('#ov-chart-readout');
    const chartWrap = container.querySelector('#ov-chart-wrap');
    if (chartWrap && readout) {
      chartWrap.addEventListener('mouseenter', () => { hoveringChart = true; });
      chartWrap.addEventListener('mouseleave', () => {
        hoveringChart = false;
        readout.textContent = '将鼠标移到柱上查看每小时详情';
      });
      chartWrap.querySelectorAll('.ov-chart-col').forEach((col) => {
        col.addEventListener('mouseenter', () => {
          const item = hourly[Number(col.dataset.idx)];
          if (!item) return;
          const label = item.label || `${String(item.hour ?? '').padStart(2, '0')}:00`;
          readout.textContent = `${label}　共 ${item.total ?? 0} 轮 · 回复 ${item.replied ?? 0} · 潜水 ${item.silent ?? 0} · 平均 ${formatDuration(item.avg_sec)}`;
        });
      });
    }
  }

  await fetchData();

  // Reuse app.js's 15s global /api/overview timer instead of polling /api/overview twice
  syncTimer = setInterval(() => {
    if (destroyed || document.hidden || busyAction || hoveringChart) return;
    const globalTs = Number(ctx.state?.lastUpdated || 0);
    if (ctx.state?.overview && globalTs > lastSeenOverviewTs) {
      overview = ctx.state.overview;
      lastSeenOverviewTs = globalTs;
      render();
    }
  }, 3000);

  analyticsTimer = setInterval(async () => {
    if (destroyed || document.hidden || busyAction || hoveringChart) return;
    try {
      const an = await apiGet('/api/analytics');
      if (destroyed) return;
      analytics = an;
      if (ctx.state?.overview && Number(ctx.state.lastUpdated || 0) > lastSeenOverviewTs) {
        overview = ctx.state.overview;
        lastSeenOverviewTs = Number(ctx.state.lastUpdated || 0);
      }
      render();
    } catch (_) {}
  }, 30000);

  return {
    cleanup() {
      destroyed = true;
      container.removeEventListener('error', onAvatarError, true);
      if (syncTimer) clearInterval(syncTimer);
      if (analyticsTimer) clearInterval(analyticsTimer);
      if (followTimer) clearTimeout(followTimer);
    },
    async refresh() {
      await fetchData({ silent: false, forceOverview: true });
    },
  };
}
