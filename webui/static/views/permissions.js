import {
  apiDelete,
  apiGet,
  apiPost,
  apiPut,
  confirmModal,
  copyText,
  createIcon,
  escapeHtml,
  formatBytes,
  formatTime,
  renderMarkdown,
  saveShortcutLabel,
  showToast,
} from '../api.js';

const RISK_META = {
  low: { label: '低风险', cls: 'badge-muted' },
  medium: { label: '中风险', cls: 'badge-info' },
  high: { label: '高危', cls: 'badge-amber' },
  critical: { label: '极高危', cls: 'badge-danger' },
};

const BOT_ROLE_META = {
  owner: { label: '群主', cls: 'badge-amber' },
  admin: { label: '群管', cls: 'badge-accent' },
  member: { label: '普通成员', cls: 'badge-muted' },
  unknown: { label: '未知身份', cls: 'badge-muted' },
};

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

function flattenFiles(items, out = []) {
  for (const it of items || []) {
    if (it.is_dir && Array.isArray(it.children)) {
      flattenFiles(it.children, out);
    } else if (!it.is_dir) {
      out.push(it);
    }
  }
  return out;
}

export async function mount(container, ctx) {
  const validTabs = new Set(['global', 'groups', 'limits', 'sandbox']);
  let activeTab = validTabs.has(ctx.params?.tab) ? ctx.params.tab : 'global';

  // Permissions state
  let permData = null;
  let baseGlobal = {};
  let draftGlobal = {};
  let baseLimits = {};
  let draftLimits = {};
  let globalQuery = '';
  let globalFilter = 'all'; // 'all' | 'enabled' | 'high'

  // Group override state
  let selectedGid = '';
  let groupQuery = '';
  let baseGroupOverrides = {};
  let draftGroupOverrides = {};
  let baseGroupLabel = '';
  let draftGroupLabel = '';

  // Simulator state
  let evalCap = '';
  let evalRole = 'member';
  let evalScope = 'dm';
  let evalResult = null;

  // Sandbox & workspace file browser state
  let wsData = null;
  let fileRoot = 'workspace'; // 'workspace' | 'chat-sandbox' | 'backup'
  let fileQuery = '';
  let selectedFile = null; // { root, path }
  let fileContent = null;
  let loadingFile = false;
  let mdViewMode = 'rendered'; // 'rendered' | 'source'
  let wrapLines = true;
  let expandedBackups = new Set();

  let saving = false;
  let destroyed = false;

  container.innerHTML = `
    <div class="stack" style="gap:28px;">
      <div class="page-tabs-row" id="perm-top-bar"></div>
      <div id="perm-body">
        <div class="loading-skeleton" style="height:280px;border-radius:12px;"></div>
      </div>
      <div id="perm-sticky-root"></div>
    </div>
  `;

  function isGlobalDirty() {
    return stableJson(baseGlobal) !== stableJson(draftGlobal);
  }

  function isLimitsDirty() {
    return stableJson(baseLimits) !== stableJson(draftLimits);
  }

  function isGroupDirty() {
    if (!selectedGid) return false;
    return (
      stableJson(baseGroupOverrides) !== stableJson(draftGroupOverrides) ||
      String(baseGroupLabel || '') !== String(draftGroupLabel || '')
    );
  }

  function getGroupObj(gid) {
    return (permData?.groups || []).find((g) => String(g.group_id) === String(gid)) || null;
  }

  function loadGroupDraft(gid) {
    selectedGid = String(gid || '');
    const grp = getGroupObj(selectedGid);
    baseGroupOverrides = deepClone(grp?.overrides || {});
    draftGroupOverrides = deepClone(grp?.overrides || {});
    baseGroupLabel = String(grp?.label || '');
    draftGroupLabel = baseGroupLabel;
  }

  function resolveGroupEffectiveRule(cap, rawOverride) {
    const globalRule = baseGlobal[cap.id] || cap.default || { enabled: false, roles: {} };
    const globalRoles = globalRule.roles || {};
    const base = {
      ...globalRule,
      enabled: Boolean(globalRule.enabled),
      roles: {
        admin: Boolean(globalRoles.admin),
        member: Boolean(globalRoles.member),
        autonomous: Boolean(globalRoles.autonomous),
      },
    };
    if (!rawOverride || typeof rawOverride !== 'object') return base;
    const merged = { ...base, ...rawOverride };
    if (rawOverride.enabled === undefined || rawOverride.enabled === null) {
      merged.enabled = base.enabled;
    } else {
      merged.enabled = Boolean(rawOverride.enabled);
    }
    const ovRoles = rawOverride.roles && typeof rawOverride.roles === 'object' ? rawOverride.roles : {};
    merged.roles = {
      admin: ovRoles.admin !== undefined && ovRoles.admin !== null ? Boolean(ovRoles.admin) : base.roles.admin,
      member: ovRoles.member !== undefined && ovRoles.member !== null ? Boolean(ovRoles.member) : base.roles.member,
      autonomous: ovRoles.autonomous !== undefined && ovRoles.autonomous !== null ? Boolean(ovRoles.autonomous) : base.roles.autonomous,
    };
    return merged;
  }

  function renderTopBar() {
    const bar = container.querySelector('#perm-top-bar');
    if (!bar) return;

    const sum = permData?.summary || {};
    const sandboxClean = wsData ? wsData.sandbox_clean !== false : true;
    const groupsCount = (permData?.groups || []).length;

    bar.innerHTML = `
      <div class="page-tabs" id="perm-tabs">
        <button type="button" class="page-tab ${activeTab === 'global' ? 'active' : ''}" data-ptab="global">
          全局能力 ${isGlobalDirty() ? '<span class="dirty-dot"></span>' : ''}
        </button>
        <button type="button" class="page-tab ${activeTab === 'groups' ? 'active' : ''}" data-ptab="groups">
          按群覆写 <span class="count">${sum.override_groups ?? 0}/${groupsCount}</span>
          ${isGroupDirty() ? '<span class="dirty-dot"></span>' : ''}
        </button>
        <button type="button" class="page-tab ${activeTab === 'limits' ? 'active' : ''}" data-ptab="limits">
          限额与试算 ${isLimitsDirty() ? '<span class="dirty-dot"></span>' : ''}
        </button>
        <button type="button" class="page-tab ${activeTab === 'sandbox' ? 'active' : ''}" data-ptab="sandbox">
          沙盒与文件 ${!sandboxClean ? '<span class="dirty-dot"></span>' : ''}
        </button>
      </div>

      <div class="metric-strip hide-xs" style="padding-bottom:12px;">
        <span class="metric-chip">已启用 <strong>${sum.enabled_global ?? '--'}/${sum.total_capabilities ?? 19}</strong></span>
        <span class="metric-sep"></span>
        <span class="metric-chip">高危开启 <strong class="${(sum.high_risk_enabled || 0) > 2 ? 'text-amber' : ''}">${sum.high_risk_enabled ?? 0}</strong></span>
        <span class="metric-sep"></span>
        <span class="metric-chip">沙盒 <strong class="${sandboxClean ? '' : 'text-danger'}">${sandboxClean ? '洁净' : '有异常文件'}</strong></span>
      </div>
    `;

    bar.querySelectorAll('[data-ptab]').forEach((btn) => {
      btn.addEventListener('click', () => {
        activeTab = btn.dataset.ptab;
        renderTopBar();
        renderBody();
        renderStickyBar();
      });
    });
  }

  function renderStickyBar() {
    const root = container.querySelector('#perm-sticky-root');
    if (!root) return;

    const gDirty = isGlobalDirty() || isLimitsDirty();
    const grpDirty = isGroupDirty();
    if (!gDirty && !grpDirty) {
      root.innerHTML = '';
      return;
    }

    const focusGroup = grpDirty && (activeTab === 'groups' || !gDirty);
    const label = focusGroup
      ? `群 ${selectedGid} 的覆写规则未保存`
      : '全局权限或安全限额未保存';

    root.innerHTML = `
      <div class="sticky-action-bar">
        <div class="row-between wrap">
          <div class="row-sm wrap">
            <span class="dirty-dot"></span>
            <span class="font-semibold text-sm">${escapeHtml(label)}</span>
            <span class="text-xs text-muted">保存后写入 bot_permissions.json，秒级热生效</span>
          </div>
          <div class="row-sm">
            <button type="button" class="btn btn-ghost btn-sm" id="perm-revert-btn" ${saving ? 'disabled' : ''}>放弃</button>
            <button type="button" class="btn btn-primary btn-sm" id="perm-save-btn" ${saving ? 'disabled' : ''}>
              保存并生效 <span style="opacity:0.6;">${saveShortcutLabel()}</span>
            </button>
          </div>
        </div>
      </div>
    `;

    root.querySelector('#perm-revert-btn')?.addEventListener('click', () => {
      if (focusGroup) {
        loadGroupDraft(selectedGid);
      } else {
        draftGlobal = deepClone(baseGlobal);
        draftLimits = deepClone(baseLimits);
      }
      renderTopBar();
      renderBody();
      renderStickyBar();
    });

    root.querySelector('#perm-save-btn')?.addEventListener('click', () => {
      if (focusGroup) saveGroupOverrides();
      else saveGlobalAndLimits();
    });
  }

  async function saveGlobalAndLimits() {
    if (saving) return;
    saving = true;
    renderStickyBar();
    try {
      const res = await apiPut('/api/permissions', { global: draftGlobal, limits: draftLimits });
      if (destroyed) return;
      applyPermResponse(res, { resetGlobal: true, resetLimits: true, resetGroup: false });
      showToast('权限与限额已保存并热生效', 'success');
    } catch (err) {
      if (!destroyed) showToast(`保存失败: ${err.message}`, 'error');
    } finally {
      saving = false;
      if (!destroyed) {
        renderTopBar();
        renderBody();
        renderStickyBar();
      }
    }
  }

  async function saveGroupOverrides() {
    if (saving || !selectedGid) return;
    saving = true;
    renderStickyBar();
    try {
      const res = await apiPut(`/api/permissions/groups/${encodeURIComponent(selectedGid)}`, {
        label: String(draftGroupLabel || '').trim(),
        overrides: draftGroupOverrides,
      });
      if (destroyed) return;
      applyPermResponse(res, { resetGlobal: false, resetLimits: false, resetGroup: true });
      showToast(`群 ${selectedGid} 的独立权限覆写已热生效`, 'success');
    } catch (err) {
      if (!destroyed) showToast(`保存失败: ${err.message}`, 'error');
    } finally {
      saving = false;
      if (!destroyed) {
        renderTopBar();
        renderBody();
        renderStickyBar();
      }
    }
  }

  async function confirmRiskyToggle(cap, field, newVal, isGroup = false) {
    if (!newVal) return true;
    if (field === 'enabled' && cap.risk === 'critical' && !(cap.id === 'admin_dm_local_tools' && !isGroup)) {
      return confirmModal({
        title: `确认开启极高危能力「${cap.label}」？`,
        message: `${cap.desc || ''}\n建议仅对管理员开放，切勿对普通群友放行。`,
        confirmText: '确认开启',
        danger: true,
      });
    }
    if ((field === 'member' || field === 'autonomous') && (cap.risk === 'high' || cap.risk === 'critical')) {
      return confirmModal({
        title: `确认对「${field === 'member' ? '普通群友' : '机器人自主'}」开放高危能力？`,
        message: `能力：${cap.label}（${RISK_META[cap.risk]?.label || cap.risk}）\n开放给非管理员可能引发误操作或刷屏风险。`,
        confirmText: '仍要开放',
        danger: true,
      });
    }
    return true;
  }

  // ---------- Tab 1: Global Capabilities ----------
  async function handleResetDefault() {
    const ok = await confirmModal({
      title: '恢复权限出厂默认？',
      message: '全部全局权限、单群覆写与安全限额都会重置为出厂默认值。',
      confirmText: '恢复默认',
      danger: true,
    });
    if (!ok || destroyed) return;
    try {
      const res = await apiPost('/api/permissions/reset', { keep_groups: false });
      if (destroyed) return;
      applyPermResponse(res, { resetGlobal: true, resetLimits: true, resetGroup: true });
      showToast('已恢复出厂默认权限', 'success');
      renderTopBar();
      renderBody();
      renderStickyBar();
    } catch (err) {
      if (!destroyed) showToast(`重置失败：${err.message}`, 'error');
    }
  }

  const permActionsEl = ctx.setActions(`
    <button type="button" class="btn btn-ghost" id="perm-reset-default">${createIcon('rotateCcw', 15)}恢复出厂默认</button>
  `);
  permActionsEl?.querySelector('#perm-reset-default')?.addEventListener('click', handleResetDefault);

  function renderGlobalTab(body) {
    const templates = permData?.templates || [];
    body.innerHTML = `
      <div class="stack-lg">
        <section class="section">
          <div class="section-head">
            <div>
              <h2 class="section-title">权限模板</h2>
              <p class="section-desc">一键切换全局默认策略，单群覆写与安全限额会保留。</p>
            </div>
          </div>
          <div class="perm-tpl-strip">
            ${templates.map((t) => `
              <button type="button" class="perm-tpl-btn" data-perm-tpl="${escapeHtml(t.id)}" title="${escapeHtml(t.desc || '')}">
                <div class="row-between" style="width:100%;">
                  <span class="font-semibold" style="font-size:14px;">${escapeHtml(t.name)}</span>
                  ${t.badge ? `<span class="badge badge-sm">${escapeHtml(t.badge)}</span>` : ''}
                </div>
                <span class="text-sm text-muted" style="width:100%;line-height:1.5;">${escapeHtml(t.desc || '')}</span>
              </button>
            `).join('')}
          </div>
        </section>

        <section class="section">
          <div class="section-head">
            <div>
              <h2 class="section-title">能力矩阵</h2>
              <p class="section-desc">先打开总开关，再按角色分别授权。高危能力切换时会二次确认。</p>
            </div>
            <div class="row-sm wrap">
              <div class="filter-pills" id="perm-global-pills">
                <button type="button" class="pill-btn ${globalFilter === 'all' ? 'active' : ''}" data-gflt="all">全部</button>
                <button type="button" class="pill-btn ${globalFilter === 'enabled' ? 'active' : ''}" data-gflt="enabled">已开启</button>
                <button type="button" class="pill-btn ${globalFilter === 'high' ? 'active' : ''}" data-gflt="high">高危</button>
              </div>
              <div class="search-box" style="width:220px;">
                <span class="search-icon">${createIcon('search', 14)}</span>
                <input type="text" id="perm-global-search" value="${escapeHtml(globalQuery)}" placeholder="搜索能力" autocomplete="off" />
              </div>
            </div>
          </div>
        </section>

        <div id="perm-global-matrix" class="stack-lg" style="margin-top:-16px;"></div>
      </div>
    `;

    body.querySelectorAll('[data-perm-tpl]').forEach((btn) => {
      btn.addEventListener('click', async () => {
        const tplId = btn.dataset.permTpl;
        const tpl = templates.find((t) => t.id === tplId);
        const ok = await confirmModal({
          title: `套用模板「${tpl?.name || tplId}」？`,
          message: `${tpl?.desc || ''}\n将覆盖全局默认策略（保留单群覆写与限额），立即热生效。`,
          confirmText: '确认套用',
        });
        if (!ok || destroyed) return;
        try {
          const res = await apiPost('/api/permissions/template', { template_id: tplId, keep_groups: true, keep_limits: true });
          if (destroyed) return;
          applyPermResponse(res, { resetGlobal: true, resetLimits: false, resetGroup: false });
          showToast(`已套用「${tpl?.name || tplId}」`, 'success');
          renderTopBar();
          renderGlobalMatrix();
          renderStickyBar();
        } catch (err) {
          if (!destroyed) showToast(`套用失败: ${err.message}`, 'error');
        }
      });
    });


    // IME-safe search updates ONLY #perm-global-matrix
    body.querySelector('#perm-global-search')?.addEventListener('input', (e) => {
      globalQuery = e.target.value;
      renderGlobalMatrix();
    });
    body.querySelectorAll('#perm-global-pills [data-gflt]').forEach((btn) => {
      btn.addEventListener('click', () => {
        globalFilter = btn.dataset.gflt;
        body.querySelectorAll('#perm-global-pills [data-gflt]').forEach((b) => b.classList.toggle('active', b.dataset.gflt === globalFilter));
        renderGlobalMatrix();
      });
    });

    renderGlobalMatrix();
  }

  function renderGlobalMatrix() {
    const slot = container.querySelector('#perm-global-matrix');
    if (!slot || !permData) return;

    const catalog = permData.catalog || [];
    const categories = permData.categories || [];
    const envFlags = permData.env_flags || {};
    const pluginStatus = permData.plugin_status || {};
    const q = globalQuery.trim().toLowerCase();

    const envHintFor = (capId) => {
      if (capId === 'repeat_follow' && envFlags.REPEAT_ENABLED === false) return 'bot.env REPEAT_ENABLED=0';
      if (capId === 'forward_long_msg' && envFlags.FORWARD_LONG_MSG_GROUP === false && envFlags.FORWARD_LONG_MSG_DM === false) return 'bot.env 合并转发未开';
      if (capId === 'admin_dm_local_tools' && envFlags.ADMIN_DM_TURBO === false) return 'bot.env ADMIN_DM_TURBO=0';
      return '';
    };

    const cardsHtml = categories.map((cat) => {
      const caps = catalog.filter((c) => {
        if (c.category !== cat.id) return false;
        const rule = draftGlobal[c.id] || c.default || { enabled: false, roles: {} };
        if (globalFilter === 'enabled' && !rule.enabled) return false;
        if (globalFilter === 'high' && c.risk !== 'high' && c.risk !== 'critical') return false;
        if (q) {
          const hay = `${c.label} ${c.id} ${c.desc || ''} ${cat.title}`.toLowerCase();
          if (!hay.includes(q)) return false;
        }
        return true;
      });
      if (!caps.length) return '';

      const enabledCnt = caps.filter((c) => (draftGlobal[c.id]?.enabled ?? c.default?.enabled)).length;
      return `
        <section class="section" style="gap:10px;">
          <div class="section-head">
            <div>
              <h3 class="section-title" style="font-size:14.5px;">${escapeHtml(cat.title)}</h3>
              ${cat.desc ? `<p class="section-desc">${escapeHtml(cat.desc)}</p>` : ''}
            </div>
            <span class="text-sm text-muted tabular">${enabledCnt}/${caps.length} 已开启</span>
          </div>
          <div class="card table-wrap">
            <table class="data-table perm-matrix-table">
              <thead>
                <tr>
                  <th>能力与说明</th>
                  <th class="center">总开关</th>
                  <th class="center">管理员</th>
                  <th class="center">普通群友</th>
                  <th class="center">机器人自主</th>
                </tr>
              </thead>
              <tbody>
                ${caps.map((cap) => {
                  const rule = draftGlobal[cap.id] || { enabled: false, roles: { admin: false, member: false, autonomous: false } };
                  const roles = rule.roles || {};
                  const locks = cap.role_locks || {};
                  const risk = RISK_META[cap.risk] || RISK_META.low;
                  const metaNotes = [];
                  if (cap.requires_group_admin) metaNotes.push('需机器人为群管');
                  if (cap.group_only) metaNotes.push('仅群聊');
                  if (cap.dm_admin_only) metaNotes.push('仅管理员私聊');
                  const envHint = envHintFor(cap.id);
                  const ps = pluginStatus[cap.id];
                  const pluginDisabled = Boolean(ps && ps.enabled === false);

                  const roleCell = (rKey) => {
                    const locked = locks[rKey] !== undefined && locks[rKey] !== null;
                    const checked = locked ? Boolean(locks[rKey]) : Boolean(roles[rKey]);
                    const disabled = !rule.enabled || locked;
                    return `
                      <td class="center">
                        <label class="switch switch-sm" title="${locked ? '安全策略已锁定' : ''}">
                          <input type="checkbox" data-gcap="${escapeHtml(cap.id)}" data-gfield="${rKey}" ${checked ? 'checked' : ''} ${disabled ? 'disabled' : ''} />
                          <span class="switch-track"></span>
                        </label>
                      </td>
                    `;
                  };

                  return `
                    <tr>
                      <td>
                        <div class="row-sm wrap">
                          <span class="font-semibold">${escapeHtml(cap.label)}</span>
                          ${cap.risk !== 'low' ? `<span class="badge badge-sm ${risk.cls}">${risk.label}</span>` : ''}
                          ${pluginDisabled ? `<button type="button" class="badge badge-sm badge-danger" data-goto-plugin="${escapeHtml(ps.plugin_id)}" style="cursor:pointer;border:none;" title="前往插件页开启「${escapeHtml(ps.plugin_name || ps.plugin_id)}」">插件已关闭</button>` : ''}
                          ${envHint ? `<button type="button" class="badge badge-sm badge-amber" data-goto-settings style="cursor:pointer;border:none;" title="前往行为配置开启">${escapeHtml(envHint)}</button>` : ''}
                        </div>
                        <div class="text-xs text-muted mt-4">
                          ${escapeHtml(cap.desc || '')}${metaNotes.length ? ` · ${metaNotes.join(' / ')}` : ''}
                        </div>
                        ${pluginDisabled ? `
                          <div class="text-xs text-danger mt-4">
                            对应插件「${escapeHtml(ps.plugin_name || ps.plugin_id)}」已关闭，此权限当前不会生效 ·
                            <button type="button" class="link-btn text-xs" data-goto-plugin="${escapeHtml(ps.plugin_id)}" style="color:var(--accent);">前往插件管理开启 →</button>
                          </div>
                        ` : ''}
                      </td>
                      <td class="center">
                        <label class="switch switch-sm">
                          <input type="checkbox" data-gcap="${escapeHtml(cap.id)}" data-gfield="enabled" ${rule.enabled ? 'checked' : ''} />
                          <span class="switch-track"></span>
                        </label>
                      </td>
                      ${roleCell('admin')}
                      ${roleCell('member')}
                      ${roleCell('autonomous')}
                    </tr>
                  `;
                }).join('')}
              </tbody>
            </table>
          </div>
        </section>
      `;
    }).join('');

    slot.innerHTML = cardsHtml || `<div class="card"><div class="empty-state"><div class="text-sm text-muted">没有匹配的权限能力</div></div></div>`;

    slot.querySelectorAll('[data-goto-settings]').forEach((btn) => {
      btn.addEventListener('click', () => ctx.navigate('settings'));
    });

    slot.querySelectorAll('[data-goto-plugin]').forEach((btn) => {
      btn.addEventListener('click', () => {
        const pid = btn.dataset.gotoPlugin;
        ctx.navigate('plugins', pid ? { plugin: pid } : {});
      });
    });

    slot.querySelectorAll('[data-gcap]').forEach((cb) => {
      cb.addEventListener('change', async () => {
        const capId = cb.dataset.gcap;
        const field = cb.dataset.gfield;
        const cap = catalog.find((c) => c.id === capId);
        const nextVal = cb.checked;
        if (cap && !(await confirmRiskyToggle(cap, field, nextVal, false))) {
          cb.checked = !nextVal;
          return;
        }
        if (!draftGlobal[capId]) draftGlobal[capId] = { enabled: false, roles: { admin: false, member: false, autonomous: false } };
        if (field === 'enabled') draftGlobal[capId].enabled = nextVal;
        else {
          if (!draftGlobal[capId].roles) draftGlobal[capId].roles = {};
          draftGlobal[capId].roles[field] = nextVal;
        }
        renderTopBar();
        renderGlobalMatrix();
        renderStickyBar();
      });
    });
  }

  // ---------- Tab 2: Per-Group Overrides (Clean Split Layout, NO Card Wrapper!) ----------
  function renderGroupsTab(body) {
    body.innerHTML = `
      <div class="split-layout split-viewport-with-bar">
        <aside class="list-pane">
          <div class="pane-header">
            <div class="search-box">
              <span class="search-icon">${createIcon('search', 13)}</span>
              <input type="text" id="perm-grp-search" value="${escapeHtml(groupQuery)}" placeholder="搜索群名或群号" autocomplete="off" />
            </div>
            <div class="row-sm">
              <input type="text" class="input input-sm mono flex-1" id="perm-add-gid" placeholder="添加群号" />
              <button type="button" class="btn btn-secondary btn-sm" id="perm-add-grp-btn">${createIcon('plus', 14)}添加</button>
            </div>
          </div>
          <div class="pane-scroll" id="perm-grp-list"></div>
        </aside>

        <section class="detail-pane" id="perm-grp-detail"></section>
      </div>
    `;

    body.querySelector('#perm-grp-search')?.addEventListener('input', (e) => {
      groupQuery = e.target.value;
      renderGroupList();
    });

    body.querySelector('#perm-add-grp-btn')?.addEventListener('click', async () => {
      const gid = (body.querySelector('#perm-add-gid')?.value || '').trim();
      if (!/^\d{5,15}$/.test(gid)) {
        showToast('请输入 5~15 位纯数字 QQ 群号', 'warning');
        return;
      }
      if (!getGroupObj(gid)) {
        permData.groups = permData.groups || [];
        permData.groups.unshift({
          group_id: gid,
          group_name: `QQ 群 ${gid}`,
          label: '',
          member_count: 0,
          bot_role: 'unknown',
          override_count: 0,
          overrides: {},
        });
      }
      body.querySelector('#perm-add-gid').value = '';
      await switchGroup(gid);
    });

    if (!selectedGid && (permData?.groups || []).length) {
      loadGroupDraft(permData.groups[0].group_id);
    }
    renderGroupList();
    renderGroupDetail();
  }

  async function switchGroup(gid) {
    const next = String(gid);
    if (next === selectedGid) return;
    if (isGroupDirty()) {
      const ok = await confirmModal({
        title: '当前群有未保存的权限改动',
        message: '切换群将放弃当前未保存的修改，是否继续？',
        confirmText: '放弃并切换',
        danger: true,
      });
      if (!ok || destroyed) return;
    }
    loadGroupDraft(next);
    renderTopBar();
    renderGroupList();
    renderGroupDetail();
    renderStickyBar();
  }

  function renderGroupList() {
    const listEl = container.querySelector('#perm-grp-list');
    if (!listEl || !permData) return;
    const q = groupQuery.trim().toLowerCase();
    const groups = (permData.groups || []).filter((g) => {
      if (!q) return true;
      return `${g.group_name || ''} ${g.label || ''} ${g.group_id || ''}`.toLowerCase().includes(q);
    });
    if (!groups.length) {
      listEl.innerHTML = `<div class="empty-state"><div class="text-sm text-muted">暂无匹配群聊</div></div>`;
      return;
    }
    listEl.innerHTML = groups.map((g) => {
      const gid = String(g.group_id);
      const active = gid === selectedGid ? ' active' : '';
      const role = BOT_ROLE_META[g.bot_role] || BOT_ROLE_META.unknown;
      const ovCnt = Object.keys(g.overrides || {}).length;
      return `
        <button type="button" class="list-item${active}" data-sel-gid="${escapeHtml(gid)}">
          <div class="flex-1" style="min-width:0;">
            <div class="row-between" style="gap:6px;">
              <span class="font-semibold truncate">${escapeHtml(g.label || g.group_name || gid)}</span>
              ${ovCnt > 0 ? `<span class="text-xs text-accent" style="flex-shrink:0;">${ovCnt} 条覆写</span>` : ''}
            </div>
            <div class="row-between mt-4 text-xs text-muted">
              <span class="mono">${escapeHtml(gid)}${g.member_count ? ` · ${g.member_count}人` : ''}</span>
              <span class="badge badge-sm ${role.cls}">${role.label}</span>
            </div>
          </div>
        </button>
      `;
    }).join('');

    listEl.querySelectorAll('[data-sel-gid]').forEach((btn) => {
      btn.addEventListener('click', () => switchGroup(btn.dataset.selGid));
    });
  }

  function renderGroupDetail() {
    const pane = container.querySelector('#perm-grp-detail');
    if (!pane || !permData) return;
    const grp = getGroupObj(selectedGid);
    if (!grp) {
      pane.innerHTML = `<div class="empty-state" style="height:100%;"><div class="empty-state-title">选择一个 QQ 群配置独立覆写</div></div>`;
      return;
    }

    const role = BOT_ROLE_META[grp.bot_role] || BOT_ROLE_META.unknown;
    const catalog = (permData.catalog || []).filter((c) => !c.dm_admin_only);
    const pluginStatus = permData.plugin_status || {};
    const ovCount = Object.keys(draftGroupOverrides).length;
    const hasPersistedGroupEntry =
      Object.keys(baseGroupOverrides).length > 0 || Boolean(String(baseGroupLabel || '').trim());
    const grpPlaceholder = grp.group_name || `QQ 群 ${selectedGid}`;

    pane.innerHTML = `
      <div class="pane-header">
        <div class="row-between wrap">
          <div class="row-sm wrap">
            <input type="text" class="input input-sm font-semibold" id="perm-grp-label-input" value="${escapeHtml(draftGroupLabel)}" placeholder="${escapeHtml(grpPlaceholder)}" style="width:220px;" title="自定义群备注名称（留空默认显示 ${escapeHtml(grpPlaceholder)}）" />
            <span class="mono text-xs text-muted">${escapeHtml(selectedGid)}</span>
            <span class="badge badge-sm ${role.cls}">${role.label}</span>
            <span class="text-xs text-muted">${ovCount} 条独立规则</span>
          </div>
          <button type="button" class="btn btn-danger btn-xs" id="perm-clear-grp-btn" ${ovCount === 0 && !hasPersistedGroupEntry ? 'disabled' : ''}>
            恢复全部继承
          </button>
        </div>
        ${grp.bot_role === 'member' ? `
          <div class="alert-banner" style="padding:8px 12px;">
            <span class="text-xs text-secondary">机器人在此群当前为普通成员，需要群管权限的能力（撤回他人、禁言、踢人、改名片、设精华、@全体）即使开启也无法由 QQ 执行。</span>
          </div>
        ` : ''}
      </div>
      <div class="pane-scroll" style="padding:0;">
        <table class="data-table perm-matrix-table">
          <thead>
            <tr>
              <th>能力</th>
              <th style="width:96px;">状态</th>
              <th class="center">总开关</th>
              <th class="center">管理员</th>
              <th class="center">普通群友</th>
              <th class="center">机器人自主</th>
            </tr>
          </thead>
          <tbody>
            ${catalog.map((cap) => {
              const hasOv = Object.prototype.hasOwnProperty.call(draftGroupOverrides, cap.id);
              const eff = resolveGroupEffectiveRule(cap, hasOv ? draftGroupOverrides[cap.id] : null);
              const roles = eff.roles || {};
              const locks = cap.role_locks || {};
              const risk = RISK_META[cap.risk] || RISK_META.low;
              const ps = pluginStatus[cap.id];
              const pluginDisabled = Boolean(ps && ps.enabled === false);

              const roleCell = (rKey) => {
                const locked = locks[rKey] !== undefined && locks[rKey] !== null;
                const checked = locked ? Boolean(locks[rKey]) : Boolean(roles[rKey]);
                return `
                  <td class="center">
                    <label class="switch switch-sm">
                      <input type="checkbox" data-ovcap="${escapeHtml(cap.id)}" data-ovfield="${rKey}" ${checked ? 'checked' : ''} ${!eff.enabled || locked ? 'disabled' : ''} />
                      <span class="switch-track"></span>
                    </label>
                  </td>
                `;
              };

              return `
                <tr>
                  <td>
                    <div class="row-sm wrap">
                      <span class="font-semibold">${escapeHtml(cap.label)}</span>
                      ${cap.risk !== 'low' ? `<span class="badge badge-sm ${risk.cls}">${risk.label}</span>` : ''}
                      ${pluginDisabled ? `<button type="button" class="badge badge-sm badge-danger" data-goto-plugin="${escapeHtml(ps.plugin_id)}" style="cursor:pointer;border:none;" title="前往插件页开启「${escapeHtml(ps.plugin_name || ps.plugin_id)}」">插件已关闭</button>` : ''}
                    </div>
                    ${pluginDisabled ? `
                      <div class="text-xs text-danger mt-4">
                        对应插件「${escapeHtml(ps.plugin_name || ps.plugin_id)}」已关闭，此权限当前不会生效 ·
                        <button type="button" class="link-btn text-xs" data-goto-plugin="${escapeHtml(ps.plugin_id)}" style="color:var(--accent);">前往插件管理 →</button>
                      </div>
                    ` : ''}
                  </td>
                  <td>
                    ${hasOv
                      ? `<button type="button" class="btn btn-ghost btn-xs text-accent" data-reset-ov="${escapeHtml(cap.id)}" title="点击恢复继承全局">独立 · 还原</button>`
                      : '<span class="text-xs text-muted">继承全局</span>'}
                  </td>
                  <td class="center">
                    <label class="switch switch-sm">
                      <input type="checkbox" data-ovcap="${escapeHtml(cap.id)}" data-ovfield="enabled" ${eff.enabled ? 'checked' : ''} />
                      <span class="switch-track"></span>
                    </label>
                  </td>
                  ${roleCell('admin')}
                  ${roleCell('member')}
                  ${roleCell('autonomous')}
                </tr>
              `;
            }).join('')}
          </tbody>
        </table>
      </div>
    `;

    pane.querySelectorAll('[data-goto-plugin]').forEach((btn) => {
      btn.addEventListener('click', () => {
        const pid = btn.dataset.gotoPlugin;
        ctx.navigate('plugins', pid ? { plugin: pid } : {});
      });
    });

    pane.querySelector('#perm-grp-label-input')?.addEventListener('input', (e) => {
      draftGroupLabel = e.target.value;
      renderTopBar();
      renderStickyBar();
    });

    pane.querySelector('#perm-clear-grp-btn')?.addEventListener('click', async () => {
      const ok = await confirmModal({
        title: `清空群 ${selectedGid} 的全部独立覆写？`,
        message: '清空后该群所有能力都将继承全局默认策略。',
        confirmText: '确认清空',
        danger: true,
      });
      if (!ok || destroyed) return;
      try {
        const res = await apiDelete(`/api/permissions/groups/${encodeURIComponent(selectedGid)}`);
        if (destroyed) return;
        applyPermResponse(res, { resetGlobal: false, resetLimits: false, resetGroup: true });
        showToast('已恢复全部继承', 'success');
        renderTopBar();
        renderGroupList();
        renderGroupDetail();
        renderStickyBar();
      } catch (err) {
        if (!destroyed) showToast(`操作失败: ${err.message}`, 'error');
      }
    });

    pane.querySelectorAll('[data-reset-ov]').forEach((btn) => {
      btn.addEventListener('click', () => {
        delete draftGroupOverrides[btn.dataset.resetOv];
        renderTopBar();
        renderGroupDetail();
        renderStickyBar();
      });
    });

    pane.querySelectorAll('[data-ovcap]').forEach((cb) => {
      cb.addEventListener('change', async () => {
        const capId = cb.dataset.ovcap;
        const field = cb.dataset.ovfield;
        const cap = (permData.catalog || []).find((c) => c.id === capId);
        const nextVal = cb.checked;
        if (cap && !(await confirmRiskyToggle(cap, field, nextVal, true))) {
          cb.checked = !nextVal;
          return;
        }
        if (!draftGroupOverrides[capId]) {
          draftGroupOverrides[capId] = resolveGroupEffectiveRule(
            cap || { id: capId, default: { enabled: false, roles: { admin: false, member: false, autonomous: false } } },
            null,
          );
        }
        if (field === 'enabled') draftGroupOverrides[capId].enabled = nextVal;
        else {
          if (!draftGroupOverrides[capId].roles || typeof draftGroupOverrides[capId].roles !== 'object') {
            draftGroupOverrides[capId].roles = {};
          }
          draftGroupOverrides[capId].roles[field] = nextVal;
        }
        renderTopBar();
        renderGroupDetail();
        renderStickyBar();
      });
    });
  }

  // ---------- Tab 3: Safety Limits & Simulator ----------
  function renderLimitsTab(body) {
    const specs = permData?.limit_specs || [];
    const catalog = permData?.catalog || [];
    const groups = permData?.groups || [];
    if (!evalCap && catalog.length) evalCap = catalog[0].id;
    const dirs = Array.isArray(draftLimits.allowed_local_image_dirs) ? draftLimits.allowed_local_image_dirs : [];
    const hasUnsaved = isGlobalDirty() || isLimitsDirty() || isGroupDirty();

    body.innerHTML = `
      <div class="grid-2">
        <section class="card">
          <div class="card-header">
            <div>
              <h3 class="card-title">安全限额</h3>
              <p class="card-subtitle">限制单轮动作、@人数、发图张数、禁言时长与本地图片白名单目录</p>
            </div>
          </div>
          <div class="setting-list">
            ${specs.map((sp) => {
              if (sp.key === 'allowed_local_image_dirs' || sp.type === 'list' || sp.type === 'tags') {
                return `
                  <div class="setting-row setting-row-stack">
                    <div class="setting-info">
                      <div class="setting-title-line">
                        <span class="setting-label">${escapeHtml(sp.label)}</span>
                        <span class="setting-key">${escapeHtml(sp.key)}</span>
                      </div>
                      <div class="setting-desc">${escapeHtml(sp.desc || '')}</div>
                    </div>
                    <div class="chip-editor" id="perm-dirs-editor">
                      ${dirs.map((d, idx) => `
                        <span class="chip mono">${escapeHtml(d)} <button type="button" class="chip-remove" data-dir-del="${idx}">×</button></span>
                      `).join('')}
                      <input type="text" id="perm-dirs-input" placeholder="输入相对目录（如 workspace）按回车添加..." />
                    </div>
                  </div>
                `;
              }
              return `
                <div class="setting-row">
                  <div class="setting-info">
                    <div class="setting-title-line">
                      <span class="setting-label">${escapeHtml(sp.label)}</span>
                      <span class="setting-key">${escapeHtml(sp.key)}</span>
                    </div>
                    <div class="setting-desc">${escapeHtml(sp.desc || '')}</div>
                  </div>
                  <div class="setting-control" style="min-width:130px;">
                    <input type="number" class="input input-sm mono" data-lim-key="${escapeHtml(sp.key)}" min="${sp.min ?? 0}" max="${sp.max ?? 9999999}" value="${escapeHtml(draftLimits[sp.key] ?? '')}" style="width:96px;text-align:right;" />
                    ${sp.unit ? `<span class="unit-label">${escapeHtml(sp.unit)}</span>` : ''}
                  </div>
                </div>
              `;
            }).join('')}
          </div>
        </section>

        <section class="card">
          <div class="card-header">
            <div>
              <h3 class="card-title">权限试算</h3>
              <p class="card-subtitle">模拟任意角色与场景下，当前已保存策略是否放行</p>
            </div>
          </div>
          <div class="card-body stack">
            ${hasUnsaved ? `<div class="alert-banner alert-warning text-xs">试算基于已保存的策略，你还有未保存的草稿，保存后再试算才能看到最新效果。</div>` : ''}
            <div class="stack-xs">
              <label class="text-xs text-muted">目标能力</label>
              <select class="select" id="perm-eval-cap">
                ${catalog.map((c) => `<option value="${escapeHtml(c.id)}" ${c.id === evalCap ? 'selected' : ''}>${escapeHtml(c.label)} (${escapeHtml(c.id)})</option>`).join('')}
              </select>
            </div>
            <div class="grid-2">
              <div class="stack-xs">
                <label class="text-xs text-muted">触发身份</label>
                <select class="select" id="perm-eval-role">
                  <option value="admin" ${evalRole === 'admin' ? 'selected' : ''}>管理员 (admin)</option>
                  <option value="member" ${evalRole === 'member' ? 'selected' : ''}>普通群友 (member)</option>
                  <option value="autonomous" ${evalRole === 'autonomous' ? 'selected' : ''}>机器人自主 (autonomous)</option>
                </select>
              </div>
              <div class="stack-xs">
                <label class="text-xs text-muted">发生场景</label>
                <select class="select" id="perm-eval-scope">
                  <option value="dm" ${evalScope === 'dm' ? 'selected' : ''}>私聊 (DM)</option>
                  ${groups.map((g) => `<option value="${escapeHtml(g.group_id)}" ${String(evalScope) === String(g.group_id) ? 'selected' : ''}>群聊：${escapeHtml(g.label || g.group_name || g.group_id)}</option>`).join('')}
                </select>
              </div>
            </div>
            <button type="button" class="btn btn-primary btn-sm" id="perm-run-eval">立即试算</button>
            <div id="perm-eval-result"></div>
          </div>
        </section>
      </div>
    `;

    body.querySelectorAll('[data-lim-key]').forEach((inp) => {
      inp.addEventListener('input', () => {
        draftLimits[inp.dataset.limKey] = inp.value === '' ? '' : Number(inp.value);
        renderTopBar();
        renderStickyBar();
      });
    });

    const dirsInp = body.querySelector('#perm-dirs-input');
    body.querySelector('#perm-dirs-editor')?.addEventListener('click', () => dirsInp?.focus());
    body.querySelectorAll('[data-dir-del]').forEach((btn) => {
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        if (dirs.length <= 1) {
          showToast('至少需要保留 1 个允许的本地图片目录', 'warning');
          return;
        }
        const next = [...dirs];
        next.splice(Number(btn.dataset.dirDel), 1);
        draftLimits.allowed_local_image_dirs = next;
        renderTopBar();
        renderLimitsTab(body);
        renderStickyBar();
      });
    });
    dirsInp?.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ',') {
        e.preventDefault();
        const v = dirsInp.value.trim();
        if (!v) return;
        if (v.includes('..') || v.includes(':') || v.startsWith('/')) {
          showToast('目录必须是相对路径且不能包含 .. 或 :', 'warning');
          return;
        }
        if (!dirs.includes(v)) {
          draftLimits.allowed_local_image_dirs = [...dirs, v];
          renderTopBar();
          renderLimitsTab(body);
          renderStickyBar();
        }
      }
    });

    const runEval = async () => {
      const resBox = body.querySelector('#perm-eval-result');
      if (!resBox || !evalCap) return;
      try {
        const res = await apiPost('/api/permissions/evaluate', {
          capability: evalCap,
          actor_role: evalRole,
          is_dm: evalScope === 'dm',
          group_id: evalScope === 'dm' ? '' : evalScope,
        });
        if (destroyed) return;
        evalResult = res;
        const ok = Boolean(res?.allowed);
        resBox.innerHTML = `
          <div class="alert-banner ${ok ? 'alert-success' : 'alert-danger'}">
            <div class="stack-xs">
              <div class="row-sm">
                <span class="badge badge-sm ${ok ? 'badge-success' : 'badge-danger'}">${ok ? '允许执行' : '拒绝拦截'}</span>
                <span class="font-semibold">${escapeHtml(res?.reason || (ok ? '策略放行' : '策略禁止'))}</span>
              </div>
              ${res?.source ? `<div class="text-xs text-muted">判定来源：${escapeHtml(res.source)}</div>` : ''}
            </div>
          </div>
        `;
      } catch (err) {
        if (!destroyed) showToast(`试算失败: ${err.message}`, 'error');
      }
    };

    body.querySelector('#perm-eval-cap')?.addEventListener('change', (e) => { evalCap = e.target.value; runEval(); });
    body.querySelector('#perm-eval-role')?.addEventListener('change', (e) => { evalRole = e.target.value; runEval(); });
    body.querySelector('#perm-eval-scope')?.addEventListener('change', (e) => { evalScope = e.target.value; runEval(); });
    body.querySelector('#perm-run-eval')?.addEventListener('click', runEval);
    runEval();
  }

  // ---------- Tab 4: Sandbox Isolation & Read-Only File Browser (merged from workspace.js!) ----------
  function renderSandboxTab(body) {
    if (!wsData) {
      body.innerHTML = `<div class="loading-skeleton" style="height:260px;border-radius:12px;"></div>`;
      return;
    }

    const clean = wsData.sandbox_clean !== false;
    const extras = wsData.sandbox_extra_files || [];
    const isoRows = Array.isArray(wsData.permissions) && wsData.permissions.length
      ? wsData.permissions
      : [
          { scope: '管理员私聊', workdir: 'workspace/', local_tools: '允许（读写文件、执行命令）', approval: wsData.admin_dm_turbo ? 'Turbo 自动放行' : '逐条确认', privileged: true, turbo: wsData.admin_dm_turbo },
          { scope: '群聊（含管理员在群里）', workdir: 'chat-sandbox/', local_tools: '禁止本地工具（仅允许只读联网搜索）', approval: '包装层静默拦截', privileged: false },
          { scope: '普通用户私聊', workdir: 'chat-sandbox/', local_tools: '禁止本地工具（仅允许只读联网搜索）', approval: '包装层静默拦截', privileged: false },
        ];

    const wsCount = flattenFiles(wsData.workspace_files).length;
    const sbCount = flattenFiles(wsData.sandbox_files).length;
    const bkCount = (wsData.backups || []).length;

    body.innerHTML = `
      <div class="stack">
        <section class="card">
          <div class="card-header wrap">
            <div>
              <h3 class="card-title">会话目录隔离</h3>
              <p class="card-subtitle"><span class="status-dot ${clean ? 'online' : 'offline'}" style="width:6px;height:6px;margin-right:6px;"></span>${clean ? 'chat-sandbox 洁净，只包含 GEMINI.md' : `chat-sandbox 中有 ${extras.length} 个多余文件`}</p>
            </div>
            <button type="button" class="btn btn-secondary btn-sm" id="perm-sync-sandbox">${createIcon('repeat', 14)}同步人设到沙盒</button>
          </div>
          ${!clean ? `
            <div style="padding:10px 16px 0;">
              <div class="alert-banner alert-danger text-xs">chat-sandbox 发现多余文件：${escapeHtml(extras.join('、'))}</div>
            </div>
          ` : ''}
          <div class="table-wrap">
            <table class="data-table">
              <thead>
                <tr>
                  <th>会话类型</th>
                  <th>工作目录</th>
                  <th>本地文件 / 命令权限</th>
                  <th>放行策略</th>
                </tr>
              </thead>
              <tbody>
                ${isoRows.map((r) => `
                  <tr>
                    <td>
                      <span class="font-semibold">${escapeHtml(r.scope)}</span>
                      <span class="text-xs text-muted" style="margin-left:8px;">${r.privileged ? '特权工作区' : '只读沙盒'}</span>
                    </td>
                    <td class="mono text-xs">${escapeHtml(r.workdir)}</td>
                    <td class="text-sm ${r.privileged ? '' : 'text-secondary'}">${escapeHtml(r.local_tools)}</td>
                    <td class="text-xs text-secondary">${escapeHtml(r.approval)}</td>
                  </tr>
                `).join('')}
              </tbody>
            </table>
          </div>
          <div class="row wrap text-xs text-muted mono" style="padding:10px 20px;border-top:1px solid var(--border-subtle);">
            <span>workspace: ${escapeHtml(wsData.workspace_dir || '')}</span>
            <span>·</span>
            <span>chat-sandbox: ${escapeHtml(wsData.sandbox_dir || '')}</span>
          </div>
        </section>

        <!-- Clean full-width split file browser (NO outer card!) -->
        <div class="split-layout" style="height:560px;">
          <aside class="list-pane">
            <div class="pane-header">
              <div class="tabs" style="width:100%;" id="ws-root-tabs">
                <button type="button" class="tab-btn flex-1 ${fileRoot === 'workspace' ? 'active' : ''}" data-froot="workspace">workspace (${wsCount})</button>
                <button type="button" class="tab-btn flex-1 ${fileRoot === 'chat-sandbox' ? 'active' : ''}" data-froot="chat-sandbox">sandbox (${sbCount})</button>
                <button type="button" class="tab-btn flex-1 ${fileRoot === 'backup' ? 'active' : ''}" data-froot="backup">backup (${bkCount})</button>
              </div>
              <div class="search-box">
                <span class="search-icon">${createIcon('search', 13)}</span>
                <input type="text" id="ws-file-search" value="${escapeHtml(fileQuery)}" placeholder="过滤文件名或路径" autocomplete="off" />
              </div>
            </div>
            <div class="pane-scroll" id="ws-file-list"></div>
          </aside>

          <section class="detail-pane" id="ws-file-viewer"></section>
        </div>
      </div>
    `;

    body.querySelector('#perm-sync-sandbox')?.addEventListener('click', async () => {
      try {
        await apiPost('/api/control/sync-sandbox', {});
        showToast('已同步 workspace/GEMINI.md 到 chat-sandbox', 'success');
        wsData = await apiGet('/api/workspace');
        ctx.refreshGlobalStatus?.({ silent: true });
        renderTopBar();
        renderSandboxTab(body);
      } catch (err) {
        showToast(`同步失败: ${err.message}`, 'error');
      }
    });

    body.querySelectorAll('#ws-root-tabs [data-froot]').forEach((btn) => {
      btn.addEventListener('click', () => {
        fileRoot = btn.dataset.froot;
        body.querySelectorAll('#ws-root-tabs [data-froot]').forEach((b) => b.classList.toggle('active', b.dataset.froot === fileRoot));
        renderFileList();
      });
    });

    // IME-safe file search
    body.querySelector('#ws-file-search')?.addEventListener('input', (e) => {
      fileQuery = e.target.value;
      renderFileList();
    });

    if (expandedBackups.size === 0 && (wsData.backups || []).length > 0) {
      expandedBackups.add(wsData.backups[0].name);
    }

    renderFileList();
    renderFileViewer();
  }

  function renderFileList() {
    const listEl = container.querySelector('#ws-file-list');
    if (!listEl || !wsData) return;
    const q = fileQuery.trim().toLowerCase();

    if (fileRoot === 'backup') {
      const backups = wsData.backups || [];
      const html = backups.map((bk) => {
        const files = (bk.files || []).filter((f) => !q || `${bk.name}/${f.rel_path || f.name}`.toLowerCase().includes(q));
        if (q && !files.length) return '';
        const open = q || expandedBackups.has(bk.name);
        return `
          <div style="margin-bottom:6px;">
            <button type="button" class="list-item" data-bk-toggle="${escapeHtml(bk.name)}" style="padding:6px 8px;">
              ${createIcon(open ? 'chevronDown' : 'chevronRight', 13)}
              <span class="font-semibold text-xs truncate flex-1">${escapeHtml(bk.name)}</span>
              <span class="text-xs text-muted">${files.length}</span>
            </button>
            ${open ? `
              <div style="padding-left:14px;">
                ${files.map((f) => {
                  const rel = f.rel_path || `${bk.name}/${f.name}`;
                  const active = selectedFile && selectedFile.root === 'backup' && selectedFile.path === rel ? ' active' : '';
                  return `
                    <button type="button" class="list-item${active}" data-open-root="backup" data-open-path="${escapeHtml(rel)}" style="padding:6px 8px;">
                      ${createIcon('file', 13)}
                      <span class="text-xs truncate flex-1">${escapeHtml(f.name)}</span>
                      <span class="text-xs text-muted">${formatBytes(f.size)}</span>
                    </button>
                  `;
                }).join('')}
              </div>
            ` : ''}
          </div>
        `;
      }).join('');
      listEl.innerHTML = html || `<div class="empty-state"><div class="text-xs text-muted">无匹配备份文件</div></div>`;
    } else {
      const rawFiles = fileRoot === 'workspace' ? wsData.workspace_files : wsData.sandbox_files;
      const files = flattenFiles(rawFiles).filter((f) => !q || `${f.rel_path || f.name}`.toLowerCase().includes(q));
      listEl.innerHTML = files.length
        ? files.map((f) => {
            const rel = f.rel_path || f.name;
            const active = selectedFile && selectedFile.root === fileRoot && selectedFile.path === rel ? ' active' : '';
            return `
              <button type="button" class="list-item${active}" data-open-root="${escapeHtml(fileRoot)}" data-open-path="${escapeHtml(rel)}">
                ${createIcon('file', 14)}
                <div class="flex-1" style="min-width:0;">
                  <div class="text-sm font-semibold truncate">${escapeHtml(rel)}</div>
                  <div class="text-xs text-muted">${formatBytes(f.size)} · ${formatTime(f.mtime)}</div>
                </div>
              </button>
            `;
          }).join('')
        : `<div class="empty-state"><div class="text-xs text-muted">目录为空或无匹配文件</div></div>`;
    }

    listEl.querySelectorAll('[data-bk-toggle]').forEach((btn) => {
      btn.addEventListener('click', () => {
        const name = btn.dataset.bkToggle;
        if (expandedBackups.has(name)) expandedBackups.delete(name);
        else expandedBackups.add(name);
        renderFileList();
      });
    });

    listEl.querySelectorAll('[data-open-path]').forEach((btn) => {
      btn.addEventListener('click', () => openFile(btn.dataset.openRoot, btn.dataset.openPath));
    });
  }

  async function openFile(root, path) {
    selectedFile = { root, path };
    loadingFile = true;
    fileContent = null;
    renderFileList();
    renderFileViewer();
    try {
      fileContent = await apiGet(`/api/workspace/file?root=${encodeURIComponent(root)}&path=${encodeURIComponent(path)}`);
    } catch (err) {
      fileContent = { error: err.message };
    } finally {
      if (!destroyed) {
        loadingFile = false;
        renderFileViewer();
      }
    }
  }

  function renderFileViewer() {
    const viewer = container.querySelector('#ws-file-viewer');
    if (!viewer) return;
    if (!selectedFile) {
      viewer.innerHTML = `<div class="empty-state" style="height:100%;"><div class="empty-state-icon">${createIcon('folder', 20)}</div><div class="empty-state-title">选择左侧文件只读预览</div></div>`;
      return;
    }
    if (loadingFile) {
      viewer.innerHTML = `<div style="padding:18px;"><div class="loading-skeleton" style="height:180px;border-radius:10px;"></div></div>`;
      return;
    }
    if (!fileContent || fileContent.error) {
      viewer.innerHTML = `<div class="empty-state" style="height:100%;"><div class="text-sm text-danger">${escapeHtml(fileContent?.error || '读取失败')}</div></div>`;
      return;
    }

    const isMd = /\.md$/i.test(selectedFile.path);
    const isGemini = selectedFile.path === 'GEMINI.md' && (selectedFile.root === 'workspace' || selectedFile.root === 'chat-sandbox');

    let bodyHtml = '';
    if (fileContent.binary) {
      bodyHtml = `
        <div class="empty-state" style="height:100%;">
          <div class="empty-state-icon">${createIcon('file', 22)}</div>
          <div class="empty-state-title">${escapeHtml(fileContent.name || selectedFile.path)}</div>
          <div class="text-sm text-muted">二进制文件（${formatBytes(fileContent.size)}），不支持纯文本预览。</div>
        </div>
      `;
    } else if (isMd && mdViewMode === 'rendered') {
      bodyHtml = `<div class="md-preview" style="padding:16px 20px;">${renderMarkdown(fileContent.content || '')}</div>`;
    } else {
      const lines = String(fileContent.content || '').split('\n');
      bodyHtml = `
        <table class="file-code-table${wrapLines ? ' wrap-on' : ''}">
          <tbody>
            ${lines.map((line, i) => `<tr><td class="ln">${i + 1}</td><td class="code">${escapeHtml(line)}</td></tr>`).join('')}
          </tbody>
        </table>
      `;
    }

    viewer.innerHTML = `
      <div class="pane-header">
        <div class="row-between wrap">
          <div class="row-sm wrap">
            <span class="font-semibold mono text-xs">${escapeHtml(selectedFile.root)}/${escapeHtml(selectedFile.path)}</span>
            <span class="text-xs text-muted">${formatBytes(fileContent.size)}</span>
            ${fileContent.truncated ? '<span class="badge badge-sm badge-amber">已截断</span>' : ''}
            ${fileContent.redacted ? '<span class="badge badge-sm badge-info">已脱敏</span>' : ''}
          </div>
          <div class="row-sm wrap">
            ${isGemini ? `<button type="button" class="btn btn-primary btn-xs" id="ws-edit-persona">${createIcon('edit', 12)}去人设页编辑</button>` : ''}
            ${!fileContent.binary && isMd ? `
              <div class="tabs">
                <button type="button" class="tab-btn ${mdViewMode === 'rendered' ? 'active' : ''}" data-mdm="rendered" style="height:24px;padding:0 8px;font-size:11.5px;">渲染</button>
                <button type="button" class="tab-btn ${mdViewMode === 'source' ? 'active' : ''}" data-mdm="source" style="height:24px;padding:0 8px;font-size:11.5px;">源码</button>
              </div>
            ` : ''}
            ${!fileContent.binary && (!isMd || mdViewMode === 'source') ? `
              <button type="button" class="pill-btn ${wrapLines ? 'active' : ''}" id="ws-wrap-btn">自动换行</button>
            ` : ''}
            ${!fileContent.binary ? `<button type="button" class="btn btn-secondary btn-xs" id="ws-copy-file">${createIcon('copy', 12)} 复制</button>` : ''}
          </div>
        </div>
      </div>
      <div class="pane-scroll" style="padding:0;">${bodyHtml}</div>
    `;

    viewer.querySelector('#ws-edit-persona')?.addEventListener('click', () => ctx.navigate('persona'));
    viewer.querySelectorAll('[data-mdm]').forEach((b) => {
      b.addEventListener('click', () => { mdViewMode = b.dataset.mdm; renderFileViewer(); });
    });
    viewer.querySelector('#ws-wrap-btn')?.addEventListener('click', () => { wrapLines = !wrapLines; renderFileViewer(); });
    viewer.querySelector('#ws-copy-file')?.addEventListener('click', () => copyText(fileContent.content || '', '已复制文件内容'));
  }

  function renderBody() {
    const body = container.querySelector('#perm-body');
    if (!body || !permData) return;
    if (activeTab === 'global') renderGlobalTab(body);
    else if (activeTab === 'groups') renderGroupsTab(body);
    else if (activeTab === 'limits') renderLimitsTab(body);
    else if (activeTab === 'sandbox') renderSandboxTab(body);
  }

  function applyPermResponse(data, { resetGlobal = true, resetLimits = true, resetGroup = true } = {}) {
    const globalWasDirty = isGlobalDirty();
    const limitsWasDirty = isLimitsDirty();
    const groupWasDirty = isGroupDirty();
    const prevSelectedGrp = selectedGid ? getGroupObj(selectedGid) : null;

    permData = data;
    if (selectedGid && !resetGroup && groupWasDirty && prevSelectedGrp && !getGroupObj(selectedGid)) {
      permData.groups = [prevSelectedGrp, ...(permData.groups || [])];
    }

    if (resetGlobal || !globalWasDirty) {
      baseGlobal = deepClone(data?.policy?.global || {});
      draftGlobal = deepClone(data?.policy?.global || {});
    }
    if (resetLimits || !limitsWasDirty) {
      baseLimits = deepClone(data?.policy?.limits || {});
      draftLimits = deepClone(data?.policy?.limits || {});
    }
    if (selectedGid && (resetGroup || !groupWasDirty)) {
      loadGroupDraft(selectedGid);
    }
  }

  async function loadAll(forceGroups = false) {
    try {
      const [pData, wData] = await Promise.all([
        apiGet(forceGroups ? '/api/permissions?refresh=1' : '/api/permissions'),
        apiGet('/api/workspace').catch(() => null),
      ]);
      if (destroyed) return;
      applyPermResponse(pData);
      wsData = wData;
      renderTopBar();
      renderBody();
      renderStickyBar();
    } catch (err) {
      if (destroyed) return;
      container.querySelector('#perm-body').innerHTML = `
        <div class="card"><div class="empty-state">
          <div class="empty-state-title">加载权限与沙盒失败</div>
          <div class="text-sm text-muted">${escapeHtml(err.message)}</div>
          <button type="button" class="btn btn-primary btn-sm mt-8" id="perm-retry">重试</button>
        </div></div>
      `;
      container.querySelector('#perm-retry')?.addEventListener('click', () => loadAll(true));
    }
  }

  const onKeyDown = (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 's') {
      const gDirty = isGlobalDirty() || isLimitsDirty();
      const grpDirty = isGroupDirty();
      if (grpDirty && (activeTab === 'groups' || !gDirty)) {
        e.preventDefault();
        saveGroupOverrides();
      } else if (gDirty) {
        e.preventDefault();
        saveGlobalAndLimits();
      }
    }
  };
  document.addEventListener('keydown', onKeyDown);

  await loadAll(false);

  return {
    cleanup() {
      destroyed = true;
      document.removeEventListener('keydown', onKeyDown);
    },
    async refresh() {
      await loadAll(true);
    },
  };
}
