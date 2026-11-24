import {
  apiDelete,
  apiGet,
  apiPost,
  apiPut,
  confirmModal,
  copyText,
  createIcon,
  escapeHtml,
  formatRelativeTime,
  saveShortcutLabel,
  showToast,
} from '../api.js';

const USER_FACT_CATS = ['喜好', '生活', '事件', '性格', '技术', '身份', '关系', '其他'];
const GROUP_ITEM_CATS = ['事件', '共识', '群规', '梗', '活动', '喜好', '技术', '其他'];

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

function safeAvatarUrl(rawUrl) {
  const s = String(rawUrl || '').trim();
  if (!s) return '';
  if (/^https?:\/\//i.test(s) || /^data:image\/(png|jpe?g|gif|webp|avif);base64,/i.test(s)) {
    return s;
  }
  return '';
}

function avatarUrl(uid) {
  const clean = String(uid || '').replace(/\D/g, '');
  return clean ? safeAvatarUrl(`https://q1.qlogo.cn/g?b=qq&nk=${clean}&s=100`) : '';
}

function normalizeGroupDraft(det) {
  const d = det && typeof det === 'object' ? det : {};
  return {
    summary: String(d.summary || ''),
    items: (Array.isArray(d.items) ? d.items : []).map((it) => ({
      id: String(it.id || ''),
      text: String(it.text || ''),
      category: String(it.category || '事件'),
      kind: String(it.kind || 'group_event'),
      fact_key: String(it.fact_key || ''),
      importance: Number(it.importance || 3),
      proof_count: Number(it.proof_count || 1),
      occurred_at: String(it.occurred_at || ''),
    })),
  };
}

function normalizeProfile(raw) {
  const p = raw && typeof raw === 'object' ? raw : {};
  const normItem = (it, isJoke) => ({
    id: String(it.id || ''),
    text: String(it.text || ''),
    category: String(it.category || (isJoke ? '梗' : '其他')),
    kind: String(it.kind || (isJoke ? 'joke' : 'stable')),
    fact_key: String(it.fact_key || ''),
    importance: Number(it.importance || 3),
    proof_count: Number(it.proof_count || 1),
    occurred_at: String(it.occurred_at || ''),
    updated_at: String(it.updated_at || ''),
  });
  return {
    relationship: String(p.relationship || ''),
    summary: String(p.summary || ''),
    facts: (Array.isArray(p.facts) ? p.facts : []).map((f) => normItem(f, false)),
    inside_jokes: (Array.isArray(p.inside_jokes) ? p.inside_jokes : []).map((j) => normItem(j, true)),
  };
}

function normalizeUserDraft(rec) {
  return {
    public_nicknames: [...(rec.public_nicknames || [])],
    dm_nicknames: [...(rec.dm_nicknames || [])],
    public_profile: normalizeProfile(rec.public_profile),
    dm_profile: normalizeProfile(rec.dm_profile),
  };
}

export async function mount(container, ctx) {
  const validTopTabs = new Set(['users', 'groups', 'conflicts']);
  let topTab = validTopTabs.has(ctx.params?.tab) ? ctx.params.tab : 'users';

  // List & stats state
  let listData = null;
  let userQuery = '';
  let userFilter = 'all'; // 'all' | 'has_memory' | 'has_dm' | 'has_pending'
  let userGroupFilter = '';
  let userSort = 'last_seen';

  // Selected user state
  let selectedUid = String(ctx.params?.userId || '');
  let userDetail = null;
  let userLoadedAt = 0;
  let loadingUser = false;
  let userLoadSeq = 0;
  let userInnerTab = 'profile'; // 'profile' | 'archive' | 'traces' | 'preview'
  let baseUserDraft = null;
  let userDraft = null;
  let clearPendingPublic = false;
  let clearPendingDm = false;
  let expandedMeta = new Set();
  let expandedPending = new Set();
  let tracesData = null;
  let loadingTraces = false;
  let tracesSubTab = 'recall'; // 'recall' | 'audit'
  let previewSubTab = 'both'; // 'both' | 'group' | 'dm'

  // Group memories state
  let groupsList = [];
  let groupSearch = '';
  let selectedGid = String(ctx.params?.groupId || '');
  let groupDetail = null;
  let groupLoadedAt = 0;
  let loadingGroup = false;
  let groupLoadSeq = 0;
  let baseGroupDraft = null;
  let groupDraft = null;

  // Conflicts state
  let conflicts = [];
  let loadingConflicts = false;

  let busyMaint = '';
  let saving = false;
  let searchDebounce = null;
  let destroyed = false;

  container.innerHTML = `
    <div class="stack" style="gap:24px;">
      <div class="page-tabs-row" id="mem-top-bar"></div>
      <div id="mem-disabled-banner"></div>
      <div id="mem-body">
        <div class="loading-skeleton" style="height:360px;border-radius:12px;"></div>
      </div>
    </div>
  `;

  const onAvatarError = (e) => {
    if (e.target && e.target.tagName === 'IMG' && e.target.classList.contains('js-avatar-img')) {
      e.target.style.display = 'none';
    }
  };
  container.addEventListener('error', onAvatarError, true);

  function isUserDirty() {
    if (!baseUserDraft || !userDraft) return false;
    return (
      stableJson(baseUserDraft) !== stableJson(userDraft) ||
      clearPendingPublic ||
      clearPendingDm
    );
  }

  function countProfileChanges(baseProf, draftProf) {
    if (!baseProf || !draftProf) return 0;
    let cnt = 0;
    if (String(baseProf.relationship || '') !== String(draftProf.relationship || '')) cnt += 1;
    if (String(baseProf.summary || '') !== String(draftProf.summary || '')) cnt += 1;
    for (const key of ['facts', 'inside_jokes']) {
      const bArr = Array.isArray(baseProf[key]) ? baseProf[key] : [];
      const dArr = Array.isArray(draftProf[key]) ? draftProf[key] : [];
      const maxLen = Math.max(bArr.length, dArr.length);
      for (let i = 0; i < maxLen; i += 1) {
        if (stableJson(bArr[i] ?? null) !== stableJson(dArr[i] ?? null)) cnt += 1;
      }
    }
    return cnt;
  }

  function countUserDirtyChanges() {
    if (!isUserDirty()) return 0;
    let cnt = 0;
    for (const key of ['public_nicknames', 'dm_nicknames']) {
      if (stableJson(baseUserDraft[key]) !== stableJson(userDraft[key])) cnt += 1;
    }
    cnt += countProfileChanges(baseUserDraft.public_profile, userDraft.public_profile);
    cnt += countProfileChanges(baseUserDraft.dm_profile, userDraft.dm_profile);
    if (clearPendingPublic) cnt += 1;
    if (clearPendingDm) cnt += 1;
    return Math.max(1, cnt);
  }

  function isGroupDirty() {
    if (!baseGroupDraft || !groupDraft) return false;
    return stableJson(baseGroupDraft) !== stableJson(groupDraft);
  }

  function countGroupDirtyChanges() {
    if (!isGroupDirty()) return 0;
    let cnt = 0;
    if (String(baseGroupDraft.summary || '') !== String(groupDraft.summary || '')) cnt += 1;
    const bArr = Array.isArray(baseGroupDraft.items) ? baseGroupDraft.items : [];
    const dArr = Array.isArray(groupDraft.items) ? groupDraft.items : [];
    const maxLen = Math.max(bArr.length, dArr.length);
    for (let i = 0; i < maxLen; i += 1) {
      if (stableJson(bArr[i] ?? null) !== stableJson(dArr[i] ?? null)) cnt += 1;
    }
    return Math.max(1, cnt);
  }

  function renderTopBar() {
    const bar = container.querySelector('#mem-top-bar');
    const banner = container.querySelector('#mem-disabled-banner');
    if (!bar) return;

    const st = listData?.stats || {};
    const conflictCnt = Number(st.conflicts_count ?? conflicts.length ?? 0);
    const pendingQueueCount = Number(st.pending_queue ?? 0);

    bar.innerHTML = `
      <div class="page-tabs" id="mem-top-tabs">
        <button type="button" class="page-tab ${topTab === 'users' ? 'active' : ''}" data-mtab="users">
          群友记忆 <span class="count">${st.total_users ?? 0}</span>
        </button>
        <button type="button" class="page-tab ${topTab === 'groups' ? 'active' : ''}" data-mtab="groups">
          群共享记忆 <span class="count">${st.group_memories_count ?? 0}</span>
        </button>
        <button type="button" class="page-tab ${topTab === 'conflicts' ? 'active' : ''}" data-mtab="conflicts">
          冲突裁决 <span class="count ${conflictCnt > 0 ? 'text-amber' : ''}">${conflictCnt}</span>
        </button>
      </div>

      <div class="metric-strip hide-xs" style="padding-bottom:12px;">
        <span class="metric-chip">画像 <strong>${st.with_public ?? 0}</strong></span>
        <span class="metric-sep"></span>
        <span class="metric-chip">私聊 <strong>${st.with_dm ?? 0}</strong></span>
        <span class="metric-sep"></span>
        <span class="metric-chip">事实 <strong>${st.total_facts ?? 0}</strong></span>
        <span class="metric-sep"></span>
        <span class="metric-chip">待整理 <strong class="${pendingQueueCount > 0 ? 'text-amber' : ''}">${pendingQueueCount}</strong></span>
      </div>
    `;

    const memActions = ctx.setActions(`
      <button type="button" class="btn btn-ghost" id="mem-flush-btn" ${busyMaint ? 'disabled' : ''} title="立即消化所有积压的待整理消息">
        <span class="${busyMaint === 'flush' ? 'spin' : ''}">${createIcon('zap', 15)}</span>消化积压
      </button>
      <button type="button" class="btn btn-secondary" id="mem-sleep-btn" ${busyMaint ? 'disabled' : ''} title="运行 Sleep 归并、衰减与冲突检测">
        <span class="${busyMaint === 'sleep' ? 'spin' : ''}">${createIcon('moon', 15)}</span>Sleep 整理
      </button>
    `);

    if (banner) {
      banner.innerHTML = listData && listData.memory_enabled === false
        ? `
          <div class="alert-banner alert-warning">
            <span>长期记忆引擎已关闭（USER_MEMORY_ENABLED=0），目前只能浏览历史档案。</span>
            <button type="button" class="btn btn-primary btn-sm" id="mem-goto-cfg">去开启</button>
          </div>
        `
        : '';
      banner.querySelector('#mem-goto-cfg')?.addEventListener('click', () => ctx.navigate('settings', { tab: 'memory_cfg' }));
    }

    bar.querySelectorAll('[data-mtab]').forEach((b) => {
      b.addEventListener('click', () => {
        if (saving || b.dataset.mtab === topTab) return;
        topTab = b.dataset.mtab;
        renderTopBar();
        renderBody();
        if (topTab === 'users') loadUsersList();
        if (topTab === 'groups') loadGroupsList();
        if (topTab === 'conflicts') loadConflicts();
      });
    });

    memActions?.querySelector('#mem-flush-btn')?.addEventListener('click', async () => {
      const ok = await confirmModal({
        title: '立即消化积压的待整理消息？',
        message: pendingQueueCount > 0
          ? `当前有 ${pendingQueueCount} 条待整理消息，将忽略空闲等待时间（idle_mins=0）立即在后台触发提炼入库。`
          : '将忽略空闲等待时间（idle_mins=0）立即检查并提炼所有未整理的会话窗口。',
        confirmText: '立即消化',
      });
      if (!ok || destroyed) return;
      busyMaint = 'flush';
      renderTopBar();
      try {
        const res = await apiPost('/api/memories/maintenance/flush', { async: true, idle_mins: 0 });
        showToast(res?.message || '已触发后台消化积压任务', res?.triggered === false ? 'info' : 'success');
      } catch (err) {
        showToast(`触发失败：${err.message}`, 'error');
      } finally {
        busyMaint = '';
        if (!destroyed) renderTopBar();
      }
    });

    memActions?.querySelector('#mem-sleep-btn')?.addEventListener('click', async () => {
      const ok = await confirmModal({
        title: '立即运行 Sleep 深度记忆整理？',
        message: '将对全库执行记忆归并、衰减清理与冲突检测（后台异步执行）。',
        confirmText: '开始整理',
      });
      if (!ok || destroyed) return;
      busyMaint = 'sleep';
      renderTopBar();
      try {
        const res = await apiPost('/api/memories/maintenance/sleep', { async: true, force: true });
        const tone = res?.started === false ? 'warning' : 'success';
        showToast(res?.message || '已启动 Sleep 后台整理任务', tone);
      } catch (err) {
        showToast(`启动失败：${err.message}`, 'error');
      } finally {
        busyMaint = '';
        if (!destroyed) renderTopBar();
      }
    });
  }

  // ---------- Tab 1: Users ----------
  function renderUsersTab(body) {
    const groupsObj = listData?.groups || {};
    body.innerHTML = `
      <div class="split-layout split-viewport-with-bar">
        <aside class="list-pane">
          <div class="pane-header">
            <div class="search-box">
              <span class="search-icon">${createIcon('search', 13)}</span>
              <input type="text" id="mem-user-search" value="${escapeHtml(userQuery)}" placeholder="搜索 QQ 号、昵称或记忆内容" autocomplete="off" />
            </div>
            <div class="tabs" id="mem-user-pills">
              <button type="button" class="tab-btn ${userFilter === 'all' ? 'active' : ''}" data-uflt="all">全部</button>
              <button type="button" class="tab-btn ${userFilter === 'has_memory' ? 'active' : ''}" data-uflt="has_memory">有画像</button>
              <button type="button" class="tab-btn ${userFilter === 'has_dm' ? 'active' : ''}" data-uflt="has_dm">有私聊</button>
              <button type="button" class="tab-btn ${userFilter === 'has_pending' ? 'active' : ''}" data-uflt="has_pending">待整理</button>
            </div>
            <div class="row-sm">
              <select class="select select-sm flex-1" id="mem-group-flt">
                <option value="">全部群聊</option>
                ${Object.entries(groupsObj).map(([gid, gname]) => `
                  <option value="${escapeHtml(gid)}" ${userGroupFilter === gid ? 'selected' : ''}>${escapeHtml(gname || gid)}</option>
                `).join('')}
              </select>
              <select class="select select-sm" id="mem-sort-flt" style="width:112px;min-width:108px;flex-shrink:0;">
                <option value="last_seen" ${userSort === 'last_seen' ? 'selected' : ''}>最近活跃</option>
                <option value="msg_count" ${userSort === 'msg_count' ? 'selected' : ''}>消息最多</option>
                <option value="facts" ${userSort === 'facts' ? 'selected' : ''}>事实最多</option>
              </select>
            </div>
          </div>
          <div class="pane-scroll" id="mem-user-list"></div>
        </aside>

        <section class="detail-pane" id="mem-user-detail"></section>
      </div>
    `;

    body.querySelector('#mem-user-search')?.addEventListener('input', (e) => {
      userQuery = e.target.value;
      if (searchDebounce) clearTimeout(searchDebounce);
      searchDebounce = setTimeout(() => loadUsersList(), 240);
    });
    body.querySelectorAll('#mem-user-pills [data-uflt]').forEach((b) => {
      b.addEventListener('click', () => {
        userFilter = b.dataset.uflt;
        body.querySelectorAll('#mem-user-pills [data-uflt]').forEach((x) => x.classList.toggle('active', x.dataset.uflt === userFilter));
        loadUsersList();
      });
    });
    body.querySelector('#mem-group-flt')?.addEventListener('change', (e) => { userGroupFilter = e.target.value; loadUsersList(); });
    body.querySelector('#mem-sort-flt')?.addEventListener('change', (e) => { userSort = e.target.value; loadUsersList(); });

    renderUserListItems();
    renderUserDetailPane();
  }

  function renderUserListItems() {
    const listEl = container.querySelector('#mem-user-list');
    if (!listEl) return;
    const users = listData?.users || [];
    if (!users.length) {
      listEl.innerHTML = `<div class="empty-state"><div class="text-sm text-muted">暂无匹配群友</div></div>`;
      return;
    }

    listEl.innerHTML = users.map((u) => {
      const uid = String(u.user_id);
      const active = uid === selectedUid ? ' active' : '';
      const name = u.display_name || uid;
      const factsCnt = Number(u.facts_count ?? u.public_facts_count ?? 0);
      const pendCnt = Number(u.pending_total ?? u.pending_count ?? 0);
      const tags = [
        u.is_admin ? '<span class="text-amber">管理员</span>' : '',
        u.has_dm ? '<span>私聊</span>' : '',
        factsCnt > 0 ? `<span>${factsCnt} 条事实</span>` : '',
        pendCnt > 0 ? `<span class="text-amber">待整理 ${pendCnt}</span>` : '',
      ].filter(Boolean).join('<span>·</span>');
      const av = avatarUrl(uid);
      return `
        <button type="button" class="list-item${active}" data-uid="${escapeHtml(uid)}" style="align-items:flex-start;">
          <span class="avatar">
            <span>${escapeHtml(String(name).slice(0, 1))}</span>
            ${av ? `<img class="js-avatar-img" src="${escapeHtml(av)}" alt="" loading="lazy" style="position:absolute;inset:0;" />` : ''}
          </span>
          <div class="flex-1" style="min-width:0;">
            <div class="row-between" style="gap:8px;">
              <span class="truncate" style="font-size:13.5px;font-weight:550;">${escapeHtml(name)}</span>
              <span class="text-xs text-muted tabular" style="flex-shrink:0;">${u.msg_count ?? 0} 条消息</span>
            </div>
            <div class="row-sm wrap text-xs text-muted" style="margin-top:2px;gap:4px;">
              <span class="mono">${escapeHtml(uid)}</span>${tags ? `<span>·</span>${tags}` : ''}
            </div>
            ${u.public_summary ? `<div class="text-xs text-secondary truncate" style="margin-top:4px;">${escapeHtml(u.public_summary)}</div>` : ''}
          </div>
        </button>
      `;
    }).join('');

    listEl.querySelectorAll('[data-uid]').forEach((b) => {
      b.addEventListener('click', () => selectUser(b.dataset.uid));
    });
  }

  async function selectUser(uid, { discardDraft = false } = {}) {
    const next = String(uid || '');
    if (!next || saving || destroyed) return;
    if (isUserDirty() && !discardDraft) {
      if (next === selectedUid) return;
      const ok = await confirmModal({
        title: '当前用户记忆有未保存修改',
        message: '切换用户将放弃当前草稿，是否继续？',
        confirmText: '放弃并切换',
        danger: true,
      });
      if (!ok || destroyed || saving) return;
    }
    const seq = ++userLoadSeq;
    selectedUid = next;
    userDetail = null;
    userDraft = null;
    baseUserDraft = null;
    userInnerTab = 'profile';
    tracesData = null;
    loadingTraces = false;
    clearPendingPublic = false;
    clearPendingDm = false;
    expandedMeta.clear();
    expandedPending.clear();
    loadingUser = true;
    renderUserListItems();
    renderUserDetailPane();

    try {
      const det = await apiGet(`/api/memories/${encodeURIComponent(next)}`);
      if (destroyed || seq !== userLoadSeq) return;
      userDetail = det;
      userLoadedAt = Number(det.loaded_at || Math.floor(Date.now() / 1000));
      const draftObj = normalizeUserDraft(det.record || {});
      baseUserDraft = deepClone(draftObj);
      userDraft = deepClone(draftObj);
    } catch (err) {
      if (!destroyed && seq === userLoadSeq) showToast(`加载用户档案失败: ${err.message}`, 'error');
    } finally {
      if (!destroyed && seq === userLoadSeq) {
        loadingUser = false;
        renderUserDetailPane();
      }
    }
  }

  function renderFactListHtml(scope, listKey, items, cats) {
    const isJoke = listKey === 'inside_jokes';
    return `
      <div class="stack-xs">
        ${items.map((item, idx) => {
          const mk = `${scope}:${listKey}:${idx}`;
          const openMeta = expandedMeta.has(mk);
          const kindLabel = item.kind === 'episodic' ? '短期事件' : (isJoke ? '内部梗' : '长期特征');
          return `
            <div class="mem-fact-row">
              <div class="row-sm">
                ${!isJoke ? `
                  <select class="select select-sm" data-mscope="${scope}" data-mlist="${listKey}" data-midx="${idx}" data-mfield="category" style="width:74px;">
                    ${cats.map((c) => `<option value="${escapeHtml(c)}" ${item.category === c ? 'selected' : ''}>${escapeHtml(c)}</option>`).join('')}
                  </select>
                ` : ''}
                <input type="text" class="input input-sm flex-1" data-mscope="${scope}" data-mlist="${listKey}" data-midx="${idx}" data-mfield="text" value="${escapeHtml(item.text)}" placeholder="${isJoke ? '记录共同梗 / 笑点...' : '记录客观事实或偏好...'}" />
                <button type="button" class="btn btn-ghost btn-xs" data-toggle-meta="${escapeHtml(mk)}" title="展开或收起元信息">${openMeta ? '收起' : '元信息'}</button>
                <button type="button" class="btn btn-ghost btn-icon btn-xs text-danger" data-del-item="${escapeHtml(mk)}" title="删除">${createIcon('x', 13)}</button>
              </div>
              <div class="row-between text-xs text-muted" style="padding:0 2px;">
                <span>${kindLabel} · ★${item.importance || 3} · 印证 ×${item.proof_count || 1}${item.fact_key ? ` · ${escapeHtml(item.fact_key)}` : ''}${item.occurred_at ? ` · ${escapeHtml(item.occurred_at)}` : ''}</span>
              </div>
              ${openMeta ? `
                <div class="mem-meta-editor">
                  <div class="stack-xs">
                    <label class="text-xs text-muted">类型 / 重要度 (1-5)</label>
                    <div class="row-sm">
                      ${!isJoke ? `
                        <select class="select select-sm flex-1" data-mscope="${scope}" data-mlist="${listKey}" data-midx="${idx}" data-mfield="kind">
                          <option value="stable" ${item.kind === 'stable' ? 'selected' : ''}>长期 stable</option>
                          <option value="episodic" ${item.kind === 'episodic' ? 'selected' : ''}>短期 episodic</option>
                          ${scope === 'group' ? `<option value="group_event" ${item.kind === 'group_event' ? 'selected' : ''}>群事件</option>` : ''}
                        </select>
                      ` : ''}
                      <input type="number" class="input input-sm mono" min="1" max="5" data-mscope="${scope}" data-mlist="${listKey}" data-midx="${idx}" data-mfield="importance" value="${item.importance || 3}" style="width:62px;" />
                    </div>
                  </div>
                  <div class="stack-xs">
                    <label class="text-xs text-muted">属性键 (fact_key)</label>
                    <input type="text" class="input input-sm mono" data-mscope="${scope}" data-mlist="${listKey}" data-midx="${idx}" data-mfield="fact_key" value="${escapeHtml(item.fact_key || '')}" placeholder="如 job / city" />
                  </div>
                  <div class="stack-xs">
                    <label class="text-xs text-muted">发生日期</label>
                    <input type="text" class="input input-sm mono" data-mscope="${scope}" data-mlist="${listKey}" data-midx="${idx}" data-mfield="occurred_at" value="${escapeHtml(item.occurred_at || '')}" placeholder="YYYY-MM-DD" />
                  </div>
                </div>
              ` : ''}
            </div>
          `;
        }).join('')}
        <div>
          <button type="button" class="btn btn-ghost btn-xs text-accent" data-add-item="${scope}:${listKey}">
            ${createIcon('plus', 12)} 添加${isJoke ? '内部梗' : '事实条目'}
          </button>
        </div>
      </div>
    `;
  }

  function renderProfileSection(scope, title, prof, pendingMsgs, clearFlag, isDm, groupNames = {}) {
    const pendOpen = expandedPending.has(scope);
    return `
      <div class="card">
        <div class="card-header">
          <div class="row-sm">
            <h3 class="card-title">${isDm ? createIcon('lock', 15) : createIcon('users', 15)} ${escapeHtml(title)}</h3>
            <span class="text-xs text-muted">${prof.facts.length} 条事实 · ${prof.inside_jokes.length} 个梗</span>
          </div>
          ${pendingMsgs.length > 0 ? `<span class="badge badge-sm badge-amber">待整理 ${pendingMsgs.length} 条</span>` : ''}
        </div>
        <div class="card-body stack">
          ${isDm ? '<div class="alert-banner alert-warning text-xs" style="padding:8px 12px;">私密域：只在与本人的私聊中注入，绝不会进入任何群聊上下文。</div>' : ''}
          <div class="stack-xs">
            <label class="text-xs text-muted" for="mem-nick-${scope}">${isDm ? '私聊昵称' : '公开昵称'}</label>
            <div class="chip-editor" style="max-width:360px;">
              ${userDraft[`${scope}_nicknames`].map((name, idx) => `
                <span class="chip">${escapeHtml(name)} <button type="button" class="chip-remove" data-nick-scope="${scope}" data-nick-del="${idx}" aria-label="移除昵称">×</button></span>
              `).join('')}
              <input type="text" id="mem-nick-${scope}" data-nick-input="${scope}" placeholder="添加称呼或昵称" />
            </div>
          </div>
          <div class="grid-2">
            <div class="stack-xs">
              <label class="text-xs text-muted">与哈基米的关系</label>
              <input type="text" class="input input-sm" data-pscope="${scope}" data-pfield="relationship" value="${escapeHtml(prof.relationship)}" placeholder="如：熟人、群友、管理员..." />
            </div>
            <div class="stack-xs">
              <label class="text-xs text-muted">一句话画像摘要</label>
              <input type="text" class="input input-sm" data-pscope="${scope}" data-pfield="summary" value="${escapeHtml(prof.summary)}" placeholder="简洁概括 TA 的特点与近期关注点..." />
            </div>
          </div>

          <div class="stack-xs">
            <label class="text-xs text-muted font-semibold">客观事实与偏好</label>
            ${renderFactListHtml(scope, 'facts', prof.facts, USER_FACT_CATS)}
          </div>

          <div class="stack-xs">
            <label class="text-xs text-muted font-semibold">共同梗 / 笑点</label>
            ${renderFactListHtml(scope, 'inside_jokes', prof.inside_jokes, USER_FACT_CATS)}
          </div>

          ${pendingMsgs.length > 0 ? `
            <div style="padding:10px 12px;border-radius:8px;background:var(--bg-elevated);border:1px solid var(--border-subtle);" class="stack-sm">
              <div class="row-between wrap">
                <span class="text-xs font-semibold">待提炼消息队列 (${pendingMsgs.length} 条)</span>
                <div class="row-sm">
                  <button type="button" class="btn btn-ghost btn-xs" data-toggle-pend="${scope}">${pendOpen ? '收起原文' : '预览原文'}</button>
                  <button type="button" class="btn btn-secondary btn-xs" data-consolidate="${scope}">${createIcon('zap', 12)} 立即后台整理</button>
                  <label class="row-sm text-xs text-muted" style="cursor:pointer;">
                    <input type="checkbox" data-clear-pend="${scope}" ${clearFlag ? 'checked' : ''} /> 保存时清空队列
                  </label>
                </div>
              </div>
              ${pendOpen ? `
                <div class="stack-xs text-xs text-secondary" style="max-height:140px;overflow-y:auto;">
                  ${pendingMsgs.slice(0, 20).map((m) => {
                    const rawGrp = String(m.group || m.group_name || m.group_id || '').trim();
                    const grpLabel = !isDm && rawGrp ? (groupNames[rawGrp] || rawGrp) : '';
                    return `<div><span class="mono text-muted">${escapeHtml((m.ts || '').slice(5, 16))}</span>${grpLabel ? ` <span class="badge badge-sm badge-muted">${escapeHtml(grpLabel)}</span>` : ''} ${escapeHtml(m.text || '')}</div>`;
                  }).join('')}
                </div>
              ` : ''}
            </div>
          ` : ''}
        </div>
      </div>
    `;
  }

  function renderUserDetailPane() {
    const pane = container.querySelector('#mem-user-detail');
    if (!pane) return;

    if (!selectedUid) {
      pane.innerHTML = `
        <div class="empty-state" style="height:100%;">
          <div class="empty-state-icon">${createIcon('brain', 22)}</div>
          <div class="empty-state-title">选择一位群友查看长期记忆</div>
          <div class="text-sm text-muted">可编辑群聊公开画像与私聊私密记忆，审阅关系图谱、召回追踪与提示词注入预览。</div>
        </div>
      `;
      return;
    }

    if (loadingUser) {
      pane.innerHTML = `<div style="padding:18px;"><div class="loading-skeleton" style="height:240px;border-radius:10px;"></div></div>`;
      return;
    }

    if (!userDetail || !userDraft) {
      pane.innerHTML = `<div class="empty-state" style="height:100%;"><div class="text-sm text-muted">无法加载该用户档案</div></div>`;
      return;
    }

    const rec = userDetail.record || {};
    const groupNames = userDetail.group_names || {};
    const relations = rec.relations || [];
    const archived = rec.archived_items || [];
    const dirty = isUserDirty();

    let tabBodyHtml = '';
    if (userInnerTab === 'profile') {
      tabBodyHtml = `
        <div class="stack">
          ${renderProfileSection('public', '群聊公开记忆', userDraft.public_profile, rec.pending_public_msgs || [], clearPendingPublic, false, groupNames)}
          ${renderProfileSection('dm', '私聊私密记忆', userDraft.dm_profile, rec.pending_dm_msgs || [], clearPendingDm, true, groupNames)}
        </div>
      `;
    } else if (userInnerTab === 'archive') {
      tabBodyHtml = `
        <div class="stack">
          <div class="card">
            <div class="card-header"><h3 class="card-title">人物关系图谱 (${relations.length})</h3></div>
            <div class="card-body">
              ${relations.length ? `
                <div class="stack-xs">
                  ${relations.map((r) => `
                    <div class="row-between wrap text-xs" style="padding:7px 10px;border-radius:8px;background:var(--bg-elevated);">
                      <div class="row-sm wrap">
                        <span class="font-semibold">${escapeHtml(r.src_name || r.src_id)}</span>
                        <span class="badge badge-sm badge-accent">${escapeHtml(r.rel_type)}</span>
                        <button type="button" class="btn btn-ghost btn-xs text-accent" style="height:auto;padding:0;" data-jump-rel="${escapeHtml(r.dst_id)}">${escapeHtml(r.dst_name || r.dst_id)}</button>
                        ${r.note ? `<span class="text-muted">· ${escapeHtml(r.note)}</span>` : ''}
                      </div>
                      <span class="text-muted">印证 ×${r.proof_count || 1}</span>
                    </div>
                  `).join('')}
                </div>
              ` : '<div class="text-xs text-muted">暂无关系记录</div>'}
            </div>
          </div>

          <div class="card">
            <div class="card-header"><h3 class="card-title">历史 / 已取代 / 已归档条目 (${archived.length})</h3></div>
            <div class="card-body">
              ${archived.length ? `
                <div class="stack-xs">
                  ${archived.map((it) => `
                    <div class="row-between wrap" style="padding:8px 10px;border-radius:8px;background:var(--bg-elevated);">
                      <div class="flex-1" style="min-width:0;">
                        <div class="text-sm">${escapeHtml(it.text || '')}</div>
                        <div class="text-xs text-muted mt-4">${it.status === 'ended' ? '已结束' : escapeHtml(it.status || 'archived')} · ${escapeHtml(it.scope || '')} · ${escapeHtml(it.ended_at || it.updated_at || '')}</div>
                      </div>
                      <button type="button" class="btn btn-secondary btn-xs" data-restore-item="${escapeHtml(it.id)}">恢复</button>
                    </div>
                  `).join('')}
                </div>
              ` : '<div class="text-xs text-muted">暂无历史归档条目</div>'}
            </div>
          </div>
        </div>
      `;
    } else if (userInnerTab === 'traces') {
      const recalls = tracesData?.recall_traces || [];
      const audits = tracesData?.audit_logs || [];
      tabBodyHtml = loadingTraces
        ? `<div class="loading-skeleton" style="height:180px;border-radius:10px;"></div>`
        : `
          <div class="stack">
            <div class="row-between">
              <div class="tabs">
                <button type="button" class="tab-btn ${tracesSubTab === 'recall' ? 'active' : ''}" data-tsub="recall">召回轨迹 (${recalls.length})</button>
                <button type="button" class="tab-btn ${tracesSubTab === 'audit' ? 'active' : ''}" data-tsub="audit">变更审计 (${audits.length})</button>
              </div>
              <button type="button" class="btn btn-ghost btn-xs" id="mem-reload-traces">${createIcon('refresh', 12)} 刷新</button>
            </div>
            ${tracesSubTab === 'recall'
              ? (recalls.length ? recalls.map((t) => {
                  const injected = t.injected || t.selected || [];
                  const omitted = t.omitted || [];
                  const queryStr = t.query || t.query_text || '(无查询文本)';
                  const gate = t.gate_info && typeof t.gate_info === 'object' ? t.gate_info : {};
                  const channels = Array.isArray(gate.channels)
                    ? gate.channels
                    : (Array.isArray(t.channels) ? t.channels : []);
                  const candCnt = gate.candidate_count ?? t.candidate_count ?? (injected.length + omitted.length);
                  return `
                  <div class="card">
                    <div class="card-body stack-xs text-xs">
                      <div class="row-between">
                        <span class="font-semibold">${escapeHtml(queryStr)}</span>
                        <span class="mono text-muted">${escapeHtml(t.created_at || '')}</span>
                      </div>
                      <div class="text-muted">已注入 ${injected.length} 条 · 候选 ${candCnt} 条${channels.length ? ` · 渠道 ${escapeHtml(channels.join(', '))}` : ''}</div>
                      ${injected.map((x) => `<div style="padding:4px 8px;border-radius:6px;background:var(--bg-elevated);">${escapeHtml(x.text || x.content || '')}</div>`).join('')}
                    </div>
                  </div>
                `;
                }).join('') : '<div class="card"><div class="empty-state"><div class="text-xs text-muted">暂无近期召回轨迹</div></div></div>')
              : (audits.length ? `
                  <div class="card"><div class="table-wrap"><table class="data-table">
                    <thead><tr><th>时间</th><th>类型</th><th>动作</th><th>详情</th></tr></thead>
                    <tbody>
                      ${audits.map((a) => `
                        <tr>
                          <td class="mono text-xs text-muted">${escapeHtml(a.created_at || '')}</td>
                          <td><span class="badge badge-sm badge-muted">${escapeHtml(a.run_type || '')}</span></td>
                          <td class="text-xs">${escapeHtml(a.action || '')}</td>
                          <td class="text-xs text-muted">${escapeHtml(typeof a.detail === 'string' ? a.detail : JSON.stringify(a.detail || ''))}</td>
                        </tr>
                      `).join('')}
                    </tbody>
                  </table></div></div>
                ` : '<div class="card"><div class="empty-state"><div class="text-xs text-muted">暂无变更审计日志</div></div></div>')}
          </div>
        `;
    } else if (userInnerTab === 'preview') {
      const grpPrompt = userDetail.preview_group_prompt || userDetail.previews?.group || '(群聊无注入内容)';
      const dmPrompt = userDetail.preview_dm_prompt || userDetail.previews?.dm || '(私聊无注入内容)';
      tabBodyHtml = `
        <div class="stack">
          <div class="tabs">
            <button type="button" class="tab-btn ${previewSubTab === 'both' ? 'active' : ''}" data-psub="both">双栏对比</button>
            <button type="button" class="tab-btn ${previewSubTab === 'group' ? 'active' : ''}" data-psub="group">仅群聊</button>
            <button type="button" class="tab-btn ${previewSubTab === 'dm' ? 'active' : ''}" data-psub="dm">仅私聊</button>
          </div>
          <div class="${previewSubTab === 'both' ? 'grid-2' : 'stack'}">
            ${previewSubTab !== 'dm' ? `
              <div class="card">
                <div class="card-header"><h3 class="card-title">群聊上下文注入预览</h3></div>
                <div class="card-body"><pre class="code-block" style="white-space:pre-wrap;">${escapeHtml(grpPrompt)}</pre></div>
              </div>
            ` : ''}
            ${previewSubTab !== 'group' ? `
              <div class="card">
                <div class="card-header"><h3 class="card-title">私聊上下文注入预览</h3></div>
                <div class="card-body"><pre class="code-block" style="white-space:pre-wrap;">${escapeHtml(dmPrompt)}</pre></div>
              </div>
            ` : ''}
          </div>
        </div>
      `;
    }

    pane.innerHTML = `
      <div class="pane-header" style="gap:14px;padding:18px 20px 14px;">
        <div class="row-between wrap">
          <div class="row" style="gap:14px;">
            <span class="avatar avatar-lg">
              <span>${escapeHtml(String(userDetail.display_name || selectedUid).slice(0, 1))}</span>
              ${avatarUrl(selectedUid) ? `<img class="js-avatar-img" src="${escapeHtml(avatarUrl(selectedUid))}" alt="" style="position:absolute;inset:0;" />` : ''}
            </span>
            <div>
              <div class="row-sm wrap">
                <span style="font-size:17px;font-weight:650;letter-spacing:-0.01em;">${escapeHtml(userDetail.display_name || selectedUid)}</span>
                ${userDetail.is_admin ? '<span class="badge badge-sm badge-amber">管理员</span>' : ''}
                <button type="button" class="btn btn-ghost btn-xs mono text-muted" style="height:auto;padding:0 4px;" id="mem-copy-uid" title="复制 QQ 号">${escapeHtml(selectedUid)}</button>
              </div>
              <div class="text-xs text-muted mt-4">
                累计 ${rec.msg_count ?? 0} 消息 · 最近活跃 ${formatRelativeTime(rec.last_seen)}
                ${Object.keys(groupNames).length ? ` · 活跃群：${Object.values(groupNames).slice(0, 3).map(escapeHtml).join('、')}` : ''}
              </div>
            </div>
          </div>
          <button type="button" class="btn btn-ghost btn-sm" id="mem-delete-user" style="color:var(--danger);">${createIcon('trash', 14)}删除档案</button>
        </div>

        <div class="row-between wrap" style="gap:8px;">
          <div class="tabs" id="mem-user-inner-tabs">
            <button type="button" class="tab-btn ${userInnerTab === 'profile' ? 'active' : ''}" data-uitab="profile">记忆档案</button>
            <button type="button" class="tab-btn ${userInnerTab === 'archive' ? 'active' : ''}" data-uitab="archive">关系与归档 <span class="text-muted">${relations.length + archived.length}</span></button>
            <button type="button" class="tab-btn ${userInnerTab === 'traces' ? 'active' : ''}" data-uitab="traces">召回与审计</button>
            <button type="button" class="tab-btn ${userInnerTab === 'preview' ? 'active' : ''}" data-uitab="preview">注入预览</button>
          </div>
        </div>
      </div>

      <div class="pane-scroll" style="padding:16px 20px;">
        <div class="stack">${tabBodyHtml}</div>
        <div class="sticky-action-bar" id="mem-user-sticky" style="${dirty ? '' : 'display:none;'}" ${dirty ? '' : 'hidden'}>
          <div class="row-between wrap">
            <div class="row-sm">
              <span class="dirty-dot"></span>
              <span class="font-semibold text-sm" data-sticky-label>${dirty ? `记忆有 ${countUserDirtyChanges()} 处未保存的修改` : '记忆有未保存的修改'}</span>
            </div>
            <div class="row-sm">
              <button type="button" class="btn btn-ghost btn-sm" id="mem-user-revert">放弃</button>
              <button type="button" class="btn btn-primary btn-sm" id="mem-user-save" ${saving ? 'disabled' : ''}>
                保存 <span style="opacity:0.6;">${saveShortcutLabel()}</span>
              </button>
            </div>
          </div>
        </div>
      </div>
    `;

    bindUserDetailEvents(pane);
  }

  function bindUserDetailEvents(pane) {
    pane.querySelector('#mem-copy-uid')?.addEventListener('click', () => copyText(selectedUid, `已复制 QQ ${selectedUid}`));

    pane.querySelector('#mem-delete-user')?.addEventListener('click', async () => {
      const uid = selectedUid;
      const seq = userLoadSeq;
      const ok = await confirmModal({
        title: `删除用户 ${userDetail?.display_name || uid} 的全部长期记忆？`,
        message: '将永久删除该用户的个人画像、个人长期记忆、记忆库中的私聊记录，以及本人在各群的待整理消息。',
        confirmText: '确认删除',
        danger: true,
      });
      if (!ok || destroyed || saving || seq !== userLoadSeq) return;
      try {
        await apiDelete(`/api/memories/${encodeURIComponent(uid)}`);
        if (destroyed) return;
        showToast('用户记忆档案已删除', 'success');
        if (selectedUid === uid) {
          userLoadSeq += 1;
          selectedUid = '';
          userDetail = null;
          userDraft = null;
          baseUserDraft = null;
          loadingUser = false;
          renderUserDetailPane();
        }
        await loadUsersList();
      } catch (err) {
        showToast(`删除失败: ${err.message}`, 'error');
      }
    });

    pane.querySelectorAll('#mem-user-inner-tabs [data-uitab]').forEach((b) => {
      b.addEventListener('click', () => {
        userInnerTab = b.dataset.uitab;
        renderUserDetailPane();
        if (userInnerTab === 'traces' && !tracesData) loadUserTraces();
      });
    });

    // Nickname chip editor
    pane.querySelectorAll('[data-nick-del]').forEach((b) => {
      b.addEventListener('click', (e) => {
        e.stopPropagation();
        userDraft[`${b.dataset.nickScope}_nicknames`].splice(Number(b.dataset.nickDel), 1);
        renderUserDetailPane();
      });
    });
    pane.querySelectorAll('[data-nick-input]').forEach((input) => {
      input.addEventListener('keydown', (e) => {
        if (e.isComposing || (e.key !== 'Enter' && e.key !== ',')) return;
        e.preventDefault();
        const name = input.value.trim();
        const names = userDraft[`${input.dataset.nickInput}_nicknames`];
        if (name && !names.includes(name)) {
          names.push(name);
          renderUserDetailPane();
        }
      });
    });

    // Profile fields
    pane.querySelectorAll('[data-pscope]').forEach((inp) => {
      inp.addEventListener('input', () => {
        const prof = inp.dataset.pscope === 'dm' ? userDraft.dm_profile : userDraft.public_profile;
        prof[inp.dataset.pfield] = inp.value;
        updateStickyVisibility(pane, isUserDirty(), countUserDirtyChanges(), 'user');
      });
    });

    // Fact & joke fields
    pane.querySelectorAll('[data-mscope]').forEach((inp) => {
      const evName = inp.tagName === 'SELECT' ? 'change' : 'input';
      inp.addEventListener(evName, () => {
        const prof = inp.dataset.mscope === 'dm' ? userDraft.dm_profile : userDraft.public_profile;
        const item = prof[inp.dataset.mlist]?.[Number(inp.dataset.midx)];
        if (!item) return;
        item[inp.dataset.mfield] = inp.dataset.mfield === 'importance' ? Number(inp.value || 3) : inp.value;
        updateStickyVisibility(pane, isUserDirty(), countUserDirtyChanges(), 'user');
      });
    });

    pane.querySelectorAll('[data-toggle-meta]').forEach((b) => {
      b.addEventListener('click', () => {
        const k = b.dataset.toggleMeta;
        if (expandedMeta.has(k)) expandedMeta.delete(k);
        else expandedMeta.add(k);
        renderUserDetailPane();
      });
    });

    pane.querySelectorAll('[data-del-item]').forEach((b) => {
      b.addEventListener('click', () => {
        const [scope, listKey, idxStr] = b.dataset.delItem.split(':');
        const prof = scope === 'dm' ? userDraft.dm_profile : userDraft.public_profile;
        prof[listKey]?.splice(Number(idxStr), 1);
        renderUserDetailPane();
      });
    });

    pane.querySelectorAll('[data-add-item]').forEach((b) => {
      b.addEventListener('click', () => {
        const [scope, listKey] = b.dataset.addItem.split(':');
        const prof = scope === 'dm' ? userDraft.dm_profile : userDraft.public_profile;
        prof[listKey].push({
          id: '',
          text: '',
          category: listKey === 'inside_jokes' ? '梗' : '其他',
          kind: listKey === 'inside_jokes' ? 'joke' : 'stable',
          fact_key: '',
          importance: 3,
          proof_count: 1,
          occurred_at: '',
        });
        renderUserDetailPane();
      });
    });

    pane.querySelectorAll('[data-toggle-pend]').forEach((b) => {
      b.addEventListener('click', () => {
        const s = b.dataset.togglePend;
        if (expandedPending.has(s)) expandedPending.delete(s);
        else expandedPending.add(s);
        renderUserDetailPane();
      });
    });

    pane.querySelectorAll('[data-clear-pend]').forEach((cb) => {
      cb.addEventListener('change', () => {
        if (cb.dataset.clearPend === 'dm') clearPendingDm = cb.checked;
        else clearPendingPublic = cb.checked;
        updateStickyVisibility(pane, isUserDirty(), countUserDirtyChanges(), 'user');
      });
    });

    pane.querySelectorAll('[data-consolidate]').forEach((b) => {
      b.addEventListener('click', async () => {
        try {
          await apiPost(`/api/memories/${encodeURIComponent(selectedUid)}/consolidate`, { scope: b.dataset.consolidate });
          showToast('已触发后台记忆整理', 'success');
        } catch (err) {
          showToast(`触发失败：${err.message}`, 'error');
        }
      });
    });

    pane.querySelectorAll('[data-jump-rel]').forEach((b) => {
      b.addEventListener('click', () => selectUser(b.dataset.jumpRel));
    });

    pane.querySelectorAll('[data-restore-item]').forEach((b) => {
      b.addEventListener('click', async () => {
        const uid = selectedUid;
        const seq = userLoadSeq;
        const dirty = isUserDirty();
        if (dirty) {
          const ok = await confirmModal({ title: '恢复将重新加载档案', message: '当前有未保存草稿，是否放弃并恢复该条目？', confirmText: '继续', danger: true });
          if (!ok || destroyed || saving || seq !== userLoadSeq) return;
        }
        const previousDraft = stableJson(userDraft);
        const pendingBeforeRestore = [clearPendingPublic, clearPendingDm];
        try {
          await apiPost(`/api/memories/items/${encodeURIComponent(b.dataset.restoreItem)}/restore`, {});
          showToast('已恢复该记忆条目', 'success');
          if (seq === userLoadSeq && previousDraft === stableJson(userDraft)
            && clearPendingPublic === pendingBeforeRestore[0] && clearPendingDm === pendingBeforeRestore[1]) {
            await selectUser(uid, { discardDraft: dirty });
          }
        } catch (err) {
          showToast(`恢复失败: ${err.message}`, 'error');
        }
      });
    });

    pane.querySelectorAll('[data-tsub]').forEach((b) => {
      b.addEventListener('click', () => { tracesSubTab = b.dataset.tsub; renderUserDetailPane(); });
    });
    pane.querySelector('#mem-reload-traces')?.addEventListener('click', () => loadUserTraces());
    pane.querySelectorAll('[data-psub]').forEach((b) => {
      b.addEventListener('click', () => { previewSubTab = b.dataset.psub; renderUserDetailPane(); });
    });

    pane.querySelector('#mem-user-revert')?.addEventListener('click', () => {
      userDraft = deepClone(baseUserDraft);
      clearPendingPublic = false;
      clearPendingDm = false;
      renderUserDetailPane();
    });
    pane.querySelector('#mem-user-save')?.addEventListener('click', saveUserMemory);
  }

  function updateStickyVisibility(pane, dirty, count = 0, mode = 'user') {
    const bar = pane?.querySelector('.sticky-action-bar');
    if (!bar) return;
    bar.style.display = dirty ? '' : 'none';
    bar.hidden = !dirty;
    const labelEl = bar.querySelector('[data-sticky-label]');
    if (labelEl) {
      const n = Math.max(1, Number(count || 0));
      labelEl.textContent = mode === 'group'
        ? `群共享记忆有 ${n} 处未保存修改`
        : `记忆有 ${n} 处未保存的修改`;
    }
  }

  async function loadUserTraces() {
    if (!selectedUid || loadingTraces) return;
    const seq = userLoadSeq;
    loadingTraces = true;
    renderUserDetailPane();
    try {
      const data = await apiGet(`/api/memories/${encodeURIComponent(selectedUid)}/traces?limit=20`);
      if (!destroyed && seq === userLoadSeq) tracesData = data;
    } catch (err) {
      if (!destroyed && seq === userLoadSeq) showToast(`加载召回轨迹失败: ${err.message}`, 'error');
    } finally {
      if (!destroyed && seq === userLoadSeq) {
        loadingTraces = false;
        renderUserDetailPane();
      }
    }
  }

  async function saveUserMemory() {
    if (saving || loadingUser || !selectedUid || !userDraft || !isUserDirty()) return;
    const pane = container.querySelector('#mem-user-detail');
    saving = true;
    pane.inert = true;
    try {
      const changes = {
        clear_pending_public: clearPendingPublic,
        clear_pending_dm: clearPendingDm,
        loaded_at: userLoadedAt,
        revisions: userDetail.revisions,
      };
      for (const key of Object.keys(userDraft)) {
        if (stableJson(userDraft[key]) !== stableJson(baseUserDraft[key])) changes[key] = userDraft[key];
      }
      const det = await apiPut(`/api/memories/${encodeURIComponent(selectedUid)}`, changes);
      if (destroyed) return;
      userDetail = det;
      userLoadedAt = Number(det.loaded_at || Math.floor(Date.now() / 1000));
      const fresh = normalizeUserDraft(det.record || {});
      baseUserDraft = deepClone(fresh);
      userDraft = deepClone(fresh);
      clearPendingPublic = false;
      clearPendingDm = false;
      showToast('用户长期记忆已保存', 'success');
      await loadUsersList({ keepSelection: true });
    } catch (err) {
      if (!destroyed) showToast(`保存失败: ${err.message}`, 'error');
    } finally {
      saving = false;
      pane.inert = false;
      if (!destroyed) renderUserDetailPane();
    }
  }

  // ---------- Tab 2: Group Shared Memories ----------
  function renderGroupsTab(body) {
    body.innerHTML = `
      <div class="split-layout split-viewport-with-bar">
        <aside class="list-pane">
          <div class="pane-header">
            <div class="search-box">
              <span class="search-icon">${createIcon('search', 13)}</span>
              <input type="text" id="mem-grp-search" value="${escapeHtml(groupSearch)}" placeholder="搜索群名或群号..." autocomplete="off" />
            </div>
          </div>
          <div class="pane-scroll" id="mem-grp-list"></div>
        </aside>
        <section class="detail-pane" id="mem-grp-detail"></section>
      </div>
    `;
    body.querySelector('#mem-grp-search')?.addEventListener('input', (e) => {
      groupSearch = e.target.value;
      renderGroupListItems();
    });
    renderGroupListItems();
    renderGroupDetailPane();
  }

  function renderGroupListItems() {
    const el = container.querySelector('#mem-grp-list');
    if (!el) return;
    const q = groupSearch.trim().toLowerCase();
    const list = groupsList.filter((g) => !q || `${g.group_name || ''} ${g.group_id || ''}`.toLowerCase().includes(q));
    if (!list.length) {
      el.innerHTML = `<div class="empty-state"><div class="text-sm text-muted">暂无群共享记忆</div></div>`;
      return;
    }
    el.innerHTML = list.map((g) => {
      const gid = String(g.group_id);
      return `
        <button type="button" class="list-item ${gid === selectedGid ? 'active' : ''}" data-mgid="${escapeHtml(gid)}">
          <div class="flex-1" style="min-width:0;">
            <div class="row-between">
              <span class="font-semibold truncate">${escapeHtml(g.group_name || gid)}</span>
              <span class="badge badge-sm badge-accent">${g.active_count ?? g.items_count ?? 0} 条</span>
            </div>
            <div class="text-xs text-muted mono mt-4">${escapeHtml(gid)}</div>
          </div>
        </button>
      `;
    }).join('');
    el.querySelectorAll('[data-mgid]').forEach((b) => {
      b.addEventListener('click', () => selectGroupMemory(b.dataset.mgid));
    });
  }

  async function selectGroupMemory(gid) {
    const next = String(gid || '');
    if (!next || saving || destroyed) return;
    if (isGroupDirty()) {
      if (next === selectedGid) return;
      const ok = await confirmModal({ title: '当前群共享记忆有未保存修改', message: '切换群将放弃当前草稿，是否继续？', confirmText: '继续', danger: true });
      if (!ok || destroyed || saving) return;
    }
    const seq = ++groupLoadSeq;
    selectedGid = next;
    groupDetail = null;
    groupDraft = null;
    baseGroupDraft = null;
    loadingGroup = true;
    renderGroupListItems();
    renderGroupDetailPane();
    try {
      const det = await apiGet(`/api/memories/groups/${encodeURIComponent(next)}`);
      if (destroyed || seq !== groupLoadSeq) return;
      groupDetail = det;
      groupLoadedAt = Number(det.loaded_at || Math.floor(Date.now() / 1000));
      const draft = normalizeGroupDraft(det);
      baseGroupDraft = deepClone(draft);
      groupDraft = deepClone(draft);
    } catch (err) {
      if (!destroyed && seq === groupLoadSeq) showToast(`加载群记忆失败: ${err.message}`, 'error');
    } finally {
      if (!destroyed && seq === groupLoadSeq) {
        loadingGroup = false;
        renderGroupDetailPane();
      }
    }
  }

  function renderGroupDetailPane() {
    const pane = container.querySelector('#mem-grp-detail');
    if (!pane) return;
    if (!selectedGid) {
      pane.innerHTML = `<div class="empty-state" style="height:100%;"><div class="empty-state-title">选择一个群聊查看共享记忆与对话交接</div></div>`;
      return;
    }
    if (loadingGroup) {
      pane.innerHTML = `<div style="padding:18px;"><div class="loading-skeleton" style="height:220px;border-radius:10px;"></div></div>`;
      return;
    }
    if (!groupDetail || !groupDraft) {
      pane.innerHTML = `
        <div class="empty-state">
          <div class="text-sm text-muted">无法加载该群的共享记忆</div>
          <button type="button" class="btn btn-secondary btn-sm" id="mem-grp-retry">重试</button>
        </div>
      `;
      pane.querySelector('#mem-grp-retry')?.addEventListener('click', () => selectGroupMemory(selectedGid));
      return;
    }

    const handoffs = groupDetail.handoffs || [];
    const archived = groupDetail.archived_items || [];
    const dirty = isGroupDirty();

    pane.innerHTML = `
      <div class="pane-header">
        <div class="row-between">
          <div>
            <span class="font-semibold">${escapeHtml(groupDetail.group_name || selectedGid)}</span>
            <span class="mono text-xs text-muted" style="margin-left:8px;">group:${escapeHtml(selectedGid)}</span>
          </div>
          <span class="text-xs text-muted">最近更新 ${formatRelativeTime(groupDetail.last_seen)}</span>
        </div>
      </div>
      <div class="pane-scroll stack" style="padding:12px 14px;">
        <div class="card">
          <div class="card-header"><h3 class="card-title">群共享摘要与事件 (${groupDraft.items.length})</h3></div>
          <div class="card-body stack">
            <textarea class="textarea" id="mem-grp-summary" rows="2" placeholder="概括本群的氛围、常用梗或共享共识...">${escapeHtml(groupDraft.summary)}</textarea>
            ${renderFactListHtml('group', 'items', groupDraft.items, GROUP_ITEM_CATS)}
          </div>
        </div>

        ${handoffs.length ? `
          <div class="card">
            <div class="card-header"><h3 class="card-title">近期对话交接摘要 (${handoffs.length})</h3></div>
            <div class="card-body stack-sm">
              ${handoffs.map((h) => `
                <div style="padding:8px 10px;border-radius:8px;background:var(--bg-elevated);" class="stack-xs text-xs">
                  <div class="text-muted mono">${escapeHtml(h.created_at || '')}</div>
                  <div>${escapeHtml(h.summary || '')}</div>
                  ${h.open_threads ? `<div class="text-amber">未完待续：${escapeHtml(Array.isArray(h.open_threads) ? h.open_threads.join('；') : h.open_threads)}</div>` : ''}
                </div>
              `).join('')}
            </div>
          </div>
        ` : ''}

        ${archived.length ? `
          <div class="card">
            <div class="card-header"><h3 class="card-title">群记忆历史 (${archived.length})</h3></div>
            <div class="card-body stack-xs">
              ${archived.map((it) => `
                <div class="row-between text-xs" style="padding:7px 10px;border-radius:8px;background:var(--bg-elevated);">
                  <span>${escapeHtml(it.text || '')}${it.status === 'ended' ? ` · 已结束 ${escapeHtml(it.ended_at || '')}` : ''}</span>
                  <button type="button" class="btn btn-secondary btn-xs" data-restore-gitem="${escapeHtml(it.id)}">恢复</button>
                </div>
              `).join('')}
            </div>
          </div>
        ` : ''}

        <div class="sticky-action-bar" id="mem-grp-sticky" style="${dirty ? '' : 'display:none;'}" ${dirty ? '' : 'hidden'}>
          <div class="row-between">
            <span class="badge badge-amber" data-sticky-label>${dirty ? `群共享记忆有 ${countGroupDirtyChanges()} 处未保存修改` : '群共享记忆有未保存修改'}</span>
            <div class="row-sm">
              <button type="button" class="btn btn-ghost btn-sm" id="mem-grp-revert">放弃</button>
              <button type="button" class="btn btn-primary btn-sm" id="mem-grp-save" ${saving ? 'disabled' : ''}>保存 (${saveShortcutLabel()})</button>
            </div>
          </div>
        </div>
      </div>
    `;

    pane.querySelector('#mem-grp-summary')?.addEventListener('input', (e) => {
      groupDraft.summary = e.target.value;
      updateStickyVisibility(pane, isGroupDirty(), countGroupDirtyChanges(), 'group');
    });
    pane.querySelectorAll('[data-mscope="group"]').forEach((inp) => {
      const ev = inp.tagName === 'SELECT' ? 'change' : 'input';
      inp.addEventListener(ev, () => {
        const item = groupDraft.items[Number(inp.dataset.midx)];
        if (!item) return;
        item[inp.dataset.mfield] = inp.dataset.mfield === 'importance' ? Number(inp.value || 3) : inp.value;
        updateStickyVisibility(pane, isGroupDirty(), countGroupDirtyChanges(), 'group');
      });
    });
    pane.querySelectorAll('[data-toggle-meta]').forEach((b) => {
      b.addEventListener('click', () => {
        const k = b.dataset.toggleMeta;
        if (expandedMeta.has(k)) expandedMeta.delete(k);
        else expandedMeta.add(k);
        renderGroupDetailPane();
      });
    });
    pane.querySelectorAll('[data-del-item]').forEach((b) => {
      b.addEventListener('click', () => {
        groupDraft.items.splice(Number(b.dataset.delItem.split(':')[2]), 1);
        renderGroupDetailPane();
      });
    });
    pane.querySelector('[data-add-item="group:items"]')?.addEventListener('click', () => {
      groupDraft.items.push({ id: '', text: '', category: '事件', kind: 'group_event', fact_key: '', importance: 3, proof_count: 1, occurred_at: '' });
      renderGroupDetailPane();
    });
    pane.querySelectorAll('[data-restore-gitem]').forEach((b) => {
      b.addEventListener('click', async () => {
        const gid = selectedGid;
        const seq = groupLoadSeq;
        try {
          await apiPost(`/api/memories/items/${encodeURIComponent(b.dataset.restoreGitem)}/restore`, {});
          showToast('已恢复群记忆条目', 'success');
          if (seq === groupLoadSeq) await selectGroupMemory(gid);
        } catch (err) {
          showToast(`恢复失败: ${err.message}`, 'error');
        }
      });
    });
    pane.querySelector('#mem-grp-revert')?.addEventListener('click', () => {
      groupDraft = deepClone(baseGroupDraft);
      renderGroupDetailPane();
    });
    pane.querySelector('#mem-grp-save')?.addEventListener('click', saveGroupMemory);
  }

  async function saveGroupMemory() {
    if (saving || loadingGroup || String(groupDetail?.group_id) !== selectedGid || !isGroupDirty()) return;
    const pane = container.querySelector('#mem-grp-detail');
    saving = true;
    pane.inert = true;
    try {
      const det = await apiPut(`/api/memories/groups/${encodeURIComponent(selectedGid)}`, {
        ...groupDraft,
        loaded_at: groupLoadedAt,
        revision: groupDetail.revision,
      });
      if (destroyed) return;
      groupDetail = det;
      groupLoadedAt = Number(det.loaded_at || Math.floor(Date.now() / 1000));
      const freshDraft = normalizeGroupDraft(det);
      baseGroupDraft = deepClone(freshDraft);
      groupDraft = deepClone(freshDraft);
      const grpEntry = groupsList.find((g) => String(g.group_id) === String(selectedGid));
      if (grpEntry) {
        grpEntry.summary = det.summary || '';
        grpEntry.active_count = Array.isArray(det.items) ? det.items.length : 0;
        grpEntry.last_seen = det.last_seen || grpEntry.last_seen;
      }
      showToast('群共享记忆已保存', 'success');
      renderGroupListItems();
    } catch (err) {
      if (!destroyed) showToast(`保存失败: ${err.message}`, 'error');
    } finally {
      saving = false;
      pane.inert = false;
      if (!destroyed) renderGroupDetailPane();
    }
  }

  // ---------- Tab 3: Conflicts ----------
  function renderConflictsTab(body) {
    if (loadingConflicts) {
      body.innerHTML = `<div class="loading-skeleton" style="height:200px;border-radius:12px;"></div>`;
      return;
    }
    if (!conflicts.length) {
      body.innerHTML = `
        <div class="card"><div class="empty-state">
          <div class="empty-state-icon">${createIcon('checkCircle', 22)}</div>
          <div class="empty-state-title">没有待裁决的冲突</div>
          <div class="text-sm">所有群友的记忆条目之间都没有矛盾。</div>
          <button type="button" class="btn btn-secondary btn-sm mt-8" id="mem-recheck-conf">重新检查</button>
        </div></div>
      `;
      body.querySelector('#mem-recheck-conf')?.addEventListener('click', loadConflicts);
      return;
    }

    body.innerHTML = `
      <div class="stack">
        ${conflicts.map((c, idx) => {
          const left = c.item || c.left || c;
          const rightObj = c.partner || c.right || null;
          const right = rightObj || {};
          const leftId = left.id || c.id;
          const rightId = right.id || c.partner_id || '';
          const hasPartner = Boolean(rightObj && rightId);
          return `
            <section class="card">
              <div class="card-header wrap">
                <div class="row-sm wrap">
                  <span class="text-xs text-amber tabular">#${idx + 1}</span>
                  <span class="font-semibold">${escapeHtml(c.owner_name || c.owner_uid || '')}</span>
                  ${c.fact_key ? `<span class="badge badge-sm mono">${escapeHtml(c.fact_key)}</span>` : ''}
                  ${c.reason ? `<span class="text-xs text-muted">${escapeHtml(c.reason)}</span>` : ''}
                </div>
                <div class="row-sm wrap">
                  <button type="button" class="btn btn-secondary btn-xs" data-resolve="keep_this" data-cid="${escapeHtml(leftId)}" data-pid="${escapeHtml(hasPartner ? rightId : '')}">保留左侧</button>
                  <button type="button" class="btn btn-secondary btn-xs" data-resolve="keep_partner" data-cid="${escapeHtml(leftId)}" data-pid="${escapeHtml(hasPartner ? rightId : '')}" ${hasPartner ? '' : 'disabled title="右侧冲突条目已不存在，无法保留右侧"'}>保留右侧</button>
                  <button type="button" class="btn btn-ghost btn-xs" data-resolve="keep_both" data-cid="${escapeHtml(leftId)}" data-pid="${escapeHtml(hasPartner ? rightId : '')}">两者并存</button>
                  <button type="button" class="btn btn-danger btn-xs" data-resolve="archive_both" data-cid="${escapeHtml(leftId)}" data-pid="${escapeHtml(hasPartner ? rightId : '')}">全部归档</button>
                </div>
              </div>
              <div class="card-body mem-conflict-grid">
                <div class="mem-conflict-side">
                  <div class="text-xs text-muted mb-8">左侧 · 当前条目</div>
                  <div class="text-sm">${escapeHtml(left.text || '')}</div>
                </div>
                <div class="mem-conflict-side">
                  <div class="text-xs text-muted mb-8">右侧 · 冲突条目</div>
                  <div class="text-sm">${hasPartner ? escapeHtml(right.text || '') : '<span class="text-muted">（原冲突配对条目已删除或不存在）</span>'}</div>
                </div>
              </div>
            </section>
          `;
        }).join('')}
      </div>
    `;

    body.querySelectorAll('[data-resolve]').forEach((b) => {
      b.addEventListener('click', async () => {
        if (b.disabled) return;
        const action = b.dataset.resolve;
        const pid = String(b.dataset.pid || '').trim();
        if (action === 'keep_partner' && !pid) return;
        try {
          await apiPost(`/api/memories/conflicts/${encodeURIComponent(b.dataset.cid)}/resolve`, {
            action,
            partner_id: pid,
          });
          showToast('冲突已裁决', 'success');
          await Promise.all([loadConflicts(), loadUsersList({ keepSelection: true })]);
          ctx.refreshGlobalStatus?.({ silent: true });
        } catch (err) {
          showToast(`裁决失败: ${err.message}`, 'error');
        }
      });
    });
  }

  function renderBody() {
    const body = container.querySelector('#mem-body');
    if (!body) return;
    if (topTab === 'users') renderUsersTab(body);
    else if (topTab === 'groups') renderGroupsTab(body);
    else if (topTab === 'conflicts') renderConflictsTab(body);
  }

  async function loadUsersList({ keepSelection = true } = {}) {
    try {
      const qs = new URLSearchParams({
        q: userQuery,
        filter: userFilter,
        group_id: userGroupFilter,
        sort: userSort,
      });
      listData = await apiGet(`/api/memories?${qs.toString()}`);
      if (destroyed) return;
      renderTopBar();
      if (topTab === 'users') {
        if (!container.querySelector('#mem-user-list')) {
          renderUsersTab(container.querySelector('#mem-body'));
        } else {
          renderUserListItems();
        }
        if (selectedUid && !userDetail && !loadingUser) {
          await selectUser(selectedUid);
        } else if (!keepSelection && !selectedUid && listData.users?.length) {
          await selectUser(listData.users[0].user_id);
        }
      }
    } catch (err) {
      if (!destroyed) showToast(`加载用户记忆列表失败: ${err.message}`, 'error');
    }
  }

  async function loadGroupsList() {
    try {
      const data = await apiGet('/api/memories/groups');
      if (destroyed) return;
      groupsList = data?.groups || [];
      renderGroupListItems();
      if (topTab === 'groups' && !loadingGroup) {
        await selectGroupMemory(selectedGid || groupsList[0]?.group_id);
      }
    } catch (err) {
      if (!destroyed) showToast(`加载群共享记忆失败: ${err.message}`, 'error');
    }
  }

  async function loadConflicts() {
    loadingConflicts = true;
    if (topTab === 'conflicts') renderConflictsTab(container.querySelector('#mem-body'));
    try {
      const data = await apiGet('/api/memories/conflicts');
      if (destroyed) return;
      conflicts = data?.conflicts || [];
    } catch (err) {
      if (!destroyed) showToast(`加载冲突列表失败: ${err.message}`, 'error');
    } finally {
      loadingConflicts = false;
      if (!destroyed) {
        renderTopBar();
        if (topTab === 'conflicts') renderConflictsTab(container.querySelector('#mem-body'));
      }
    }
  }

  const onKeyDown = (e) => {
    if (!container.isConnected) return;
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 's') {
      if (topTab === 'users' && isUserDirty()) {
        e.preventDefault();
        saveUserMemory();
      } else if (topTab === 'groups' && isGroupDirty()) {
        e.preventDefault();
        saveGroupMemory();
      }
    }
  };
  document.addEventListener('keydown', onKeyDown);

  if (topTab !== 'users') renderBody();
  await loadUsersList({ keepSelection: true });
  if (topTab === 'groups') await loadGroupsList();
  else if (topTab === 'conflicts') await loadConflicts();

  return {
    cleanup() {
      destroyed = true;
      container.removeEventListener('error', onAvatarError, true);
      if (searchDebounce) clearTimeout(searchDebounce);
      document.removeEventListener('keydown', onKeyDown);
    },
    async refresh() {
      await loadUsersList({ keepSelection: true });
      if (topTab === 'users' && userDetail && !loadingUser) await selectUser(selectedUid);
      if (topTab === 'groups') await loadGroupsList();
      if (topTab === 'conflicts') await loadConflicts();
    },
  };
}
