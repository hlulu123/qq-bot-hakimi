import {
  apiDelete,
  apiGet,
  apiPost,
  confirmModal,
  createIcon,
  escapeHtml,
  formatRelativeTime,
  formatTime,
  showToast,
} from '../api.js';

function getAvatarUrl(isGroup, targetId) {
  const id = String(targetId || '').replace(/\D/g, '');
  if (!id) return '';
  return isGroup
    ? `https://p.qlogo.cn/gh/${id}/${id}/100/`
    : `https://q1.qlogo.cn/g?b=qq&nk=${id}&s=100`;
}

function normalizeSession(raw) {
  const key = String(raw.session_key || raw.key || '');
  const isGroup =
    Boolean(raw.is_group) ||
    raw.type === 'group' ||
    key.includes(':g:') ||
    key.includes(':group:') ||
    key.startsWith('group:');
  const targetId = String(
    raw.target_id ||
      (isGroup ? raw.group_id : raw.user_id) ||
      (key.match(/(\d{5,15})$/)?.[1] || ''),
  );
  const name = raw.display_name || raw.name || raw.chat_name || raw.user_name || key || targetId || '未命名会话';
  return {
    ...raw,
    id: String(raw.id || key),
    session_key: key,
    is_group: isGroup,
    target_id: targetId,
    display_name: name,
    is_live: Boolean(raw.live || raw.active),
    count: Number(raw.history_count ?? raw.message_count ?? 0),
    updated: raw.last_user_activity || raw.updated_at || raw.created_at || '',
  };
}

function formatRichMessageHtml(msg) {
  if (Array.isArray(msg.message) && msg.message.length) {
    return msg.message.map((seg) => {
      const t = seg?.type || 'text';
      const d = seg?.data || {};
      if (t === 'text') return escapeHtml(d.text || '');
      if (t === 'at') return `<span class="cq-tag">@${escapeHtml(d.name || d.qq || '成员')}</span>`;
      if (t === 'image') return `<span class="cq-tag">[图片]</span>`;
      if (t === 'face') return `<span class="cq-tag">[表情:${escapeHtml(d.id || '')}]</span>`;
      if (t === 'reply') return `<span class="cq-tag">[引用]</span>`;
      if (t === 'forward') return `<span class="cq-tag">[合并转发]</span>`;
      if (t === 'record') return `<span class="cq-tag">[语音]</span>`;
      if (t === 'video') return `<span class="cq-tag">[视频]</span>`;
      if (t === 'file') return `<span class="cq-tag">[文件:${escapeHtml(d.name || '')}]</span>`;
      return `<span class="cq-tag">[${escapeHtml(t)}]</span>`;
    }).join('');
  }
  const raw = String(msg.content ?? msg.text ?? msg.raw_message ?? '');
  return escapeHtml(raw).replace(/\[CQ:([a-zA-Z0-9_-]+)([^\]]*)\]/g, (_, cqType) => {
    const map = { image: '图片', at: '@成员', face: '表情', reply: '引用', forward: '合并转发', record: '语音', video: '视频', file: '文件' };
    return `<span class="cq-tag">[${map[cqType] || cqType}]</span>`;
  });
}

function extractSenderFromUserContent(content) {
  const s = String(content || '');
  const m = s.match(/^\[?([^\](\n:：]{1,32})\((\d{5,15})\)\]?\s*[:：]\s*([\s\S]*)$/);
  if (m) return { senderName: m[1].trim(), senderId: m[2], body: m[3] };
  return { senderName: '', senderId: '', body: s };
}

export async function mount(container, ctx) {
  let sessions = [];
  let selectedId = '';
  let listFilter = 'all';
  let listQuery = '';

  let detailTab = 'cc'; // 'cc' | 'napcat'
  let ccLimit = 80;
  let napcatCount = 40;
  let hideSilent = false;
  let onlyBot = false;
  let chatQuery = '';

  let ccDetail = null;
  let napcatDetail = null;
  let loadingDetail = false;
  let detailError = '';
  let detailLoadSeq = 0;

  let composerText = '';
  let sendChannel = 'cc-connect';
  let sending = false;
  let destroyed = false;

  // Root static shell so search inputs & composer NEVER lose focus or drafts
  container.innerHTML = `
    <div class="split-layout split-viewport chat-layout">
      <aside class="list-pane">
        <div class="pane-header">
          <div class="search-box">
            <span class="search-icon">${createIcon('search', 14)}</span>
            <input type="text" id="sess-list-search" placeholder="搜索群名、昵称或 QQ 号" autocomplete="off" />
          </div>
          <div class="row-between">
            <div class="tabs" id="sess-filter-pills">
              <button type="button" class="tab-btn active" data-flt="all">全部</button>
              <button type="button" class="tab-btn" data-flt="group">群聊</button>
              <button type="button" class="tab-btn" data-flt="private">私聊</button>
              <button type="button" class="tab-btn" data-flt="live">活跃</button>
            </div>
            <span class="text-xs text-muted tabular" id="sess-count-badge">0</span>
          </div>
        </div>
        <div class="pane-scroll" id="sess-list-body"></div>
      </aside>

      <section class="detail-pane" id="sess-detail-pane"></section>
    </div>
  `;

  // Capture-phase fallback for broken QQ avatars (CSP-safe, no inline onerror)
  container.addEventListener(
    'error',
    (e) => {
      if (e.target && e.target.tagName === 'IMG' && e.target.classList.contains('js-avatar-img')) {
        e.target.style.display = 'none';
      }
    },
    true,
  );

  function getSelected() {
    return sessions.find((s) => s.id === selectedId) || null;
  }

  function renderSessionList() {
    const body = container.querySelector('#sess-list-body');
    const countBadge = container.querySelector('#sess-count-badge');
    if (!body) return;

    const q = listQuery.trim().toLowerCase();
    const filtered = sessions.filter((s) => {
      if (listFilter === 'group' && !s.is_group) return false;
      if (listFilter === 'private' && s.is_group) return false;
      if (listFilter === 'live' && !s.is_live) return false;
      if (q) {
        const hay = `${s.display_name} ${s.session_key} ${s.target_id} ${s.id}`.toLowerCase();
        if (!hay.includes(q)) return false;
      }
      return true;
    });

    if (countBadge) countBadge.textContent = `${filtered.length} 个`;

    if (!filtered.length) {
      body.innerHTML = `<div class="empty-state" style="padding:36px 12px;"><div class="text-sm text-muted">无匹配会话</div></div>`;
      return;
    }

    body.innerHTML = filtered.map((s) => {
      const active = s.id === selectedId ? ' active' : '';
      const avUrl = getAvatarUrl(s.is_group, s.target_id);
      const initials = escapeHtml((s.display_name || '?').slice(0, 1));
      const kind = s.is_admin_dm ? '管理员私聊' : s.is_group ? '群聊' : '私聊';
      return `
        <button type="button" class="list-item${active}" data-sid="${escapeHtml(s.id)}">
          <span class="avatar">
            <span>${initials}</span>
            ${avUrl ? `<img class="js-avatar-img" src="${escapeHtml(avUrl)}" alt="" loading="lazy" style="position:absolute;inset:0;" />` : ''}
            ${s.is_live ? '<span class="avatar-live" title="活跃中"></span>' : ''}
          </span>
          <div class="flex-1" style="min-width:0;">
            <div class="row-between" style="gap:8px;">
              <span class="truncate" style="font-size:13.5px;font-weight:550;">${escapeHtml(s.display_name)}</span>
              <span class="text-xs text-muted" style="flex-shrink:0;">${formatRelativeTime(s.updated)}</span>
            </div>
            <div class="text-xs text-muted truncate" style="margin-top:2px;">${kind} · ${s.count} 条记录</div>
          </div>
        </button>
      `;
    }).join('');

    body.querySelectorAll('[data-sid]').forEach((btn) => {
      btn.addEventListener('click', () => selectSession(btn.dataset.sid));
    });
  }

  function renderDetailShell() {
    const pane = container.querySelector('#sess-detail-pane');
    if (!pane) return;
    const s = getSelected();
    if (!s) {
      pane.innerHTML = `
        <div class="empty-state" style="height:100%;">
          <div class="empty-state-icon">${createIcon('chat', 22)}</div>
          <div class="empty-state-title">选择一个会话</div>
          <div class="text-sm" style="max-width:360px;">查看机器人的会话上下文，或对比 NapCat 拉取的群聊原文，也可以直接发送消息。</div>
        </div>
      `;
      return;
    }

    const avUrl = getAvatarUrl(s.is_group, s.target_id);
    const workdir = s.is_admin_dm ? 'workspace/（管理员）' : 'chat-sandbox/';

    pane.innerHTML = `
      <div class="chat-head">
        <div class="row" style="min-width:0;gap:12px;">
          <span class="avatar">
            <span>${escapeHtml((s.display_name || '?').slice(0, 1))}</span>
            ${avUrl ? `<img class="js-avatar-img" src="${escapeHtml(avUrl)}" alt="" style="position:absolute;inset:0;" />` : ''}
          </span>
          <div style="min-width:0;line-height:1.35;">
            <div class="row-sm" style="min-width:0;">
              <span class="truncate" style="font-size:15px;font-weight:600;max-width:360px;">${escapeHtml(s.display_name)}</span>
              ${s.is_live ? '<span class="badge badge-sm badge-success">活跃</span>' : ''}
            </div>
            <div class="text-xs text-muted truncate">
              <span class="mono">${escapeHtml(s.session_key || s.id)}</span> · ${s.count} 条 · 工作目录 ${escapeHtml(workdir)}
            </div>
          </div>
        </div>
        <div class="row-sm" style="flex-shrink:0;">
          ${s.is_group ? `<button type="button" class="btn btn-ghost btn-icon btn-sm" id="sess-jump-logs" title="在日志中筛选此群">${createIcon('terminal', 16)}</button>` : ''}
          <button type="button" class="btn btn-ghost btn-icon btn-sm" id="sess-refresh-detail" title="刷新记录">${createIcon('refresh', 16)}</button>
          <button type="button" class="btn btn-ghost btn-icon btn-sm" id="sess-delete-btn" title="删除会话上下文" style="color:var(--danger);">${createIcon('trash', 16)}</button>
        </div>
      </div>

      <div class="chat-toolbar">
        <div class="tabs">
          <button type="button" class="tab-btn ${detailTab === 'cc' ? 'active' : ''}" data-dtab="cc">AI 会话</button>
          ${s.is_group ? `<button type="button" class="tab-btn ${detailTab === 'napcat' ? 'active' : ''}" data-dtab="napcat">群聊原文</button>` : ''}
        </div>
        <div class="row-sm wrap" id="sess-toolbar-filters"></div>
      </div>

      <div class="chat-stream" id="sess-chat-stream"></div>

      <div class="chat-composer">
        <div class="composer-box">
          <textarea id="sess-composer-input" rows="2" placeholder="${s.is_group ? '发送到群聊（发送前会二次确认）' : `发送给 ${escapeHtml(s.target_id || '对方')}`}"></textarea>
          <div class="composer-bar">
            <select class="composer-select" id="sess-send-channel" title="发送通道">
              <option value="cc-connect" ${sendChannel === 'cc-connect' ? 'selected' : ''}>经 AI 处理（cc-connect 注入）</option>
              <option value="napcat" ${sendChannel === 'napcat' ? 'selected' : ''}>直接发到 QQ（NapCat）</option>
            </select>
            <div class="row-sm">
              <span class="text-xs text-muted hide-xs">⌘ / Ctrl + Enter</span>
              <button type="button" class="btn btn-primary btn-sm" id="sess-send-btn">${createIcon('send', 14)}发送</button>
            </div>
          </div>
        </div>
      </div>
    `;

    // Restore unsent composer text across tab switches
    const ta = pane.querySelector('#sess-composer-input');
    if (ta) {
      ta.value = composerText;
      ta.addEventListener('input', () => { composerText = ta.value; });
      ta.addEventListener('keydown', (e) => {
        if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
          e.preventDefault();
          handleSend();
        }
      });
    }

    pane.querySelector('#sess-send-channel')?.addEventListener('change', (e) => {
      sendChannel = e.target.value;
    });
    pane.querySelector('#sess-send-btn')?.addEventListener('click', handleSend);
    pane.querySelector('#sess-refresh-detail')?.addEventListener('click', () => loadDetail(true));
    pane.querySelector('#sess-delete-btn')?.addEventListener('click', handleDeleteSession);
    pane.querySelector('#sess-jump-logs')?.addEventListener('click', () => {
      ctx.navigate('logs', { q: s.target_id });
    });

    pane.querySelectorAll('[data-dtab]').forEach((btn) => {
      btn.addEventListener('click', () => {
        const next = btn.dataset.dtab;
        if (next === detailTab) return;
        detailTab = next;
        pane.querySelectorAll('[data-dtab]').forEach((b) => b.classList.toggle('active', b.dataset.dtab === detailTab));
        renderToolbarFilters();
        loadDetail(false);
      });
    });

    renderToolbarFilters();
    renderChatStream();
  }

  function renderToolbarFilters() {
    const slot = container.querySelector('#sess-toolbar-filters');
    if (!slot) return;
    if (detailTab === 'cc') {
      slot.innerHTML = `
        <label class="switch switch-sm text-sm text-secondary">
          <input type="checkbox" id="sess-hide-silent" ${hideSilent ? 'checked' : ''} />
          <span class="switch-track"></span>隐藏潜水
        </label>
        <label class="switch switch-sm text-sm text-secondary">
          <input type="checkbox" id="sess-only-bot" ${onlyBot ? 'checked' : ''} />
          <span class="switch-track"></span>只看机器人
        </label>
        <select class="select select-sm" id="sess-cc-limit" style="width:auto;">
          <option value="80" ${ccLimit === 80 ? 'selected' : ''}>最近 80 条</option>
          <option value="200" ${ccLimit === 200 ? 'selected' : ''}>最近 200 条</option>
          <option value="500" ${ccLimit === 500 ? 'selected' : ''}>最近 500 条</option>
        </select>
        <div class="search-box" style="width:170px;">
          <span class="search-icon">${createIcon('search', 13)}</span>
          <input type="text" id="sess-chat-search" value="${escapeHtml(chatQuery)}" placeholder="筛选内容" style="height:30px;" />
        </div>
      `;
      slot.querySelector('#sess-hide-silent')?.addEventListener('change', (e) => {
        hideSilent = e.target.checked;
        loadDetail(true);
      });
      slot.querySelector('#sess-only-bot')?.addEventListener('change', (e) => {
        onlyBot = e.target.checked;
        renderChatStream();
      });
      slot.querySelector('#sess-cc-limit')?.addEventListener('change', (e) => {
        ccLimit = Number(e.target.value) || 80;
        loadDetail(true);
      });
    } else {
      slot.innerHTML = `
        <select class="select select-sm" id="sess-nc-count" style="width:auto;">
          <option value="30" ${napcatCount === 30 ? 'selected' : ''}>拉取 30 条</option>
          <option value="40" ${napcatCount === 40 ? 'selected' : ''}>拉取 40 条</option>
          <option value="80" ${napcatCount === 80 ? 'selected' : ''}>拉取 80 条</option>
        </select>
        <div class="search-box" style="width:180px;">
          <span class="search-icon">${createIcon('search', 13)}</span>
          <input type="text" id="sess-chat-search" value="${escapeHtml(chatQuery)}" placeholder="筛选消息" style="height:30px;" />
        </div>
      `;
      slot.querySelector('#sess-nc-count')?.addEventListener('change', (e) => {
        napcatCount = Number(e.target.value) || 40;
        loadDetail(true);
      });
    }
    slot.querySelector('#sess-chat-search')?.addEventListener('input', (e) => {
      chatQuery = e.target.value;
      renderChatStream();
    });
  }

  function renderChatStream() {
    const stream = container.querySelector('#sess-chat-stream');
    if (!stream) return;

    if (loadingDetail) {
      stream.innerHTML = `
        <div class="stack-sm">
          <div class="loading-skeleton" style="height:48px;border-radius:14px;width:60%;"></div>
          <div class="loading-skeleton" style="height:48px;border-radius:14px;width:45%;align-self:flex-end;"></div>
          <div class="loading-skeleton" style="height:48px;border-radius:14px;width:66%;"></div>
        </div>
      `;
      return;
    }

    if (detailError) {
      stream.innerHTML = `
        <div class="empty-state">
          <div class="empty-state-icon">${createIcon('alert', 20)}</div>
          <div class="empty-state-title">加载聊天记录失败</div>
          <div class="text-sm text-muted">${escapeHtml(detailError)}</div>
          <button type="button" class="btn btn-secondary btn-sm mt-8" id="sess-retry-detail">${createIcon('refresh', 13)} 重试</button>
        </div>
      `;
      stream.querySelector('#sess-retry-detail')?.addEventListener('click', () => loadDetail(true));
      return;
    }

    const q = chatQuery.trim().toLowerCase();
    const botUin = String(ctx.state.overview?.services?.napcat?.user_id || ctx.state.overview?.bot?.qq || '');

    if (detailTab === 'cc') {
      const msgs = Array.isArray(ccDetail?.messages) ? ccDetail.messages : [];
      let hiddenSilent = 0;
      const visible = msgs.filter((m) => {
        const isSilent = Boolean(m.is_silent) || String(m.content || '').trim() === 'NO_REPLY';
        if (hideSilent && isSilent) { hiddenSilent += 1; return false; }
        const isBot = m.role === 'assistant';
        if (onlyBot && (!isBot || isSilent)) return false;
        if (q) {
          const hay = `${m.sender_name || ''} ${m.sender_id || ''} ${m.content || ''}`.toLowerCase();
          if (!hay.includes(q)) return false;
        }
        return true;
      });

      const totalHiddenSilent = hideSilent
        ? Math.max(hiddenSilent, Number(ccDetail?.silent_count || 0))
        : 0;

      if (!visible.length) {
        stream.innerHTML = `<div class="empty-state"><div class="text-sm text-muted">当前筛选条件下暂无消息${totalHiddenSilent ? `（已隐藏 ${totalHiddenSilent} 条潜水记录）` : ''}</div></div>`;
        return;
      }

      const banner = totalHiddenSilent > 0
        ? `<div class="chat-silent-pill">已隐藏 ${totalHiddenSilent} 条潜水记录</div>`
        : '';

      stream.innerHTML = banner + visible.map((m) => {
        const isSilent = Boolean(m.is_silent) || String(m.content || '').trim() === 'NO_REPLY';
        if (isSilent) {
          return `<div class="chat-silent-pill">${escapeHtml(formatTime(m.timestamp))} · 选择潜水，未回复</div>`;
        }
        const isBot = m.role === 'assistant';
        const isRepeat = Boolean(m.is_follow_repeat);
        let senderName = isBot ? '哈基米' : (m.sender_name || '群友');
        let senderId = m.sender_id || '';
        let bodyText = String(m.content || '');
        if (!isBot) {
          const parsed = extractSenderFromUserContent(bodyText);
          if (parsed.senderId) {
            senderName = parsed.senderName || senderName;
            senderId = parsed.senderId;
            bodyText = parsed.body;
          }
        }
        const senderEl = !isBot && senderId
          ? `<button type="button" class="link-btn" style="font-size:11.5px;color:var(--text-secondary);" data-jump-uid="${escapeHtml(senderId)}" title="查看 ${escapeHtml(senderId)} 的记忆">${escapeHtml(senderName)}</button>`
          : `<span>${escapeHtml(senderName)}</span>`;

        return `
          <div class="chat-msg ${isBot ? 'bot' : 'user'} ${isRepeat ? 'repeat' : ''}">
            <span class="avatar avatar-sm ${isBot ? 'avatar-bot' : ''}">${isBot ? createIcon('cat', 14) : escapeHtml(senderName.slice(0, 1))}</span>
            <div style="min-width:0;">
              <div class="chat-meta">
                ${senderEl}
                ${isRepeat ? '<span class="badge badge-sm badge-amber">复读</span>' : ''}
                <span>${escapeHtml(formatTime(m.timestamp))}</span>
              </div>
              <div class="chat-bubble">${escapeHtml(bodyText || (m.media_only ? '[图片 / 表情包 / 非文本消息]' : ''))}</div>
            </div>
          </div>
        `;
      }).join('');
    } else {
      const msgs = Array.isArray(napcatDetail?.messages) ? napcatDetail.messages : [];
      const visible = msgs.filter((m) => {
        if (!q) return true;
        const hay = `${m.sender_name || ''} ${m.user_id || ''} ${m.text || m.raw_message || ''}`.toLowerCase();
        return hay.includes(q);
      });

      if (!visible.length) {
        stream.innerHTML = `<div class="empty-state"><div class="text-sm text-muted">暂无 NapCat 原始群聊记录</div></div>`;
        return;
      }

      stream.innerHTML = visible.map((m) => {
        const uid = String(m.user_id || m.sender_id || m.sender?.user_id || '');
        const isBot = Boolean(m.is_bot) || (botUin && uid === botUin);
        const sname = m.sender_name || m.sender?.card || m.sender?.nickname || uid || '群友';
        const avUrl = uid ? getAvatarUrl(false, uid) : '';
        const senderEl = !isBot && uid
          ? `<button type="button" class="link-btn" style="font-size:11.5px;color:var(--text-secondary);" data-jump-uid="${escapeHtml(uid)}" title="查看 ${escapeHtml(uid)} 的记忆">${escapeHtml(sname)}</button>`
          : `<span>${escapeHtml(sname)}</span>`;
        return `
          <div class="chat-msg ${isBot ? 'bot' : 'user'}">
            <span class="avatar avatar-sm">
              <span>${escapeHtml(sname.slice(0, 1))}</span>
              ${avUrl ? `<img class="js-avatar-img" src="${escapeHtml(avUrl)}" alt="" loading="lazy" style="position:absolute;inset:0;" />` : ''}
            </span>
            <div style="min-width:0;">
              <div class="chat-meta">
                ${senderEl}
                <span>${escapeHtml(formatTime(m.timestamp || m.time))}</span>
              </div>
              <div class="chat-bubble">${formatRichMessageHtml(m)}</div>
            </div>
          </div>
        `;
      }).join('');
    }

    stream.querySelectorAll('[data-jump-uid]').forEach((btn) => {
      btn.addEventListener('click', () => ctx.navigate('memory', { userId: btn.dataset.jumpUid }));
    });
    stream.scrollTop = stream.scrollHeight;
  }

  async function loadDetail(force = false) {
    const s = getSelected();
    if (!s) return;
    if (detailTab === 'napcat' && !s.is_group) detailTab = 'cc';

    const seq = ++detailLoadSeq;
    loadingDetail = true;
    detailError = '';
    renderChatStream();

    try {
      if (detailTab === 'cc') {
        const qs = new URLSearchParams({ limit: String(ccLimit) });
        if (hideSilent) qs.set('hide_silent', '1');
        const data = await apiGet(`/api/sessions/${encodeURIComponent(s.id)}?${qs.toString()}`);
        if (seq !== detailLoadSeq || destroyed) return;
        ccDetail = data;
      } else {
        if (force || !napcatDetail || napcatDetail._gid !== s.target_id || napcatDetail._cnt !== napcatCount) {
          const data = await apiGet(`/api/napcat/group-history?group_id=${encodeURIComponent(s.target_id)}&count=${napcatCount}`);
          if (seq !== detailLoadSeq || destroyed) return;
          napcatDetail = { ...data, _gid: s.target_id, _cnt: napcatCount };
        }
      }
    } catch (err) {
      if (seq !== detailLoadSeq || destroyed) return;
      detailError = err.message;
    } finally {
      if (seq === detailLoadSeq && !destroyed) {
        loadingDetail = false;
        renderChatStream();
      }
    }
  }

  function selectSession(sid) {
    if (selectedId === sid && ccDetail) return;
    selectedId = sid;
    detailTab = 'cc';
    ccDetail = null;
    napcatDetail = null;
    chatQuery = '';
    renderSessionList();
    renderDetailShell();
    loadDetail(true);
  }

  async function handleSend() {
    const s = getSelected();
    if (!s || sending) return;
    const text = composerText.trim();
    if (!text) {
      showToast('请输入消息内容', 'warning');
      return;
    }
    if (s.is_group) {
      const ok = await confirmModal({
        title: `确认发送到群「${s.display_name}」？`,
        message: `通道：${sendChannel === 'cc-connect' ? 'cc-connect 会话注入' : 'NapCat 直发 QQ'}\n内容：${text.slice(0, 120)}`,
        confirmText: '发送',
      });
      if (!ok || destroyed) return;
    }

    sending = true;
    const btn = container.querySelector('#sess-send-btn');
    if (btn) btn.disabled = true;
    try {
      await apiPost('/api/sessions/send', {
        session_id: s.id,
        session_key: s.session_key,
        chat_type: s.is_group ? 'group' : 'private',
        target_id: s.target_id,
        group_id: s.is_group ? s.target_id : '',
        user_id: s.is_group ? '' : s.target_id,
        channel: sendChannel,
        via: sendChannel,
        message: text,
        text,
      });
      if (destroyed) return;
      composerText = '';
      const ta = container.querySelector('#sess-composer-input');
      if (ta) ta.value = '';
      showToast('消息已发送', 'success');
      await loadDetail(true);
    } catch (err) {
      if (!destroyed) showToast(`发送失败：${err.message}`, 'error');
    } finally {
      sending = false;
      if (btn) btn.disabled = false;
    }
  }

  async function handleDeleteSession() {
    const s = getSelected();
    if (!s) return;
    const ok = await confirmModal({
      title: `删除「${s.display_name}」的会话上下文？`,
      message: '只删除 cc-connect 本地会话上下文，QQ 聊天记录与长期记忆不受影响。',
      confirmText: '删除',
      danger: true,
    });
    if (!ok || destroyed) return;
    try {
      await apiDelete(`/api/sessions/${encodeURIComponent(s.id)}`);
      if (destroyed) return;
      showToast('会话已删除', 'success');
      selectedId = '';
      await loadSessions();
    } catch (err) {
      if (!destroyed) showToast(`删除失败：${err.message}`, 'error');
    }
  }

  async function loadSessions() {
    try {
      const data = await apiGet('/api/sessions');
      if (destroyed) return;
      const list = Array.isArray(data?.sessions) ? data.sessions : [];
      sessions = list.map(normalizeSession);

      // Deep link resolution
      const paramKey = ctx.params?.sessionKey || ctx.params?.session || '';
      const paramId = ctx.params?.sessionId || '';
      if (!selectedId) {
        const matched =
          (paramId && sessions.find((s) => s.id === paramId)) ||
          (paramKey && sessions.find((s) => s.id === paramKey || s.session_key === paramKey || s.target_id === paramKey)) ||
          sessions[0];
        if (matched) selectedId = matched.id;
      } else if (!sessions.some((s) => s.id === selectedId)) {
        selectedId = sessions[0]?.id || '';
      }

      renderSessionList();
      renderDetailShell();
      if (selectedId) await loadDetail(true);
    } catch (err) {
      if (destroyed) return;
      const body = container.querySelector('#sess-list-body');
      if (body) {
        body.innerHTML = `<div class="empty-state"><div class="text-sm text-danger">${escapeHtml(err.message)}</div></div>`;
      }
    }
  }

  container.querySelector('#sess-list-search')?.addEventListener('input', (e) => {
    listQuery = e.target.value;
    renderSessionList();
  });
  container.querySelectorAll('#sess-filter-pills .tab-btn').forEach((btn) => {
    btn.addEventListener('click', () => {
      listFilter = btn.dataset.flt;
      container.querySelectorAll('#sess-filter-pills .tab-btn').forEach((b) => b.classList.toggle('active', b.dataset.flt === listFilter));
      renderSessionList();
    });
  });

  await loadSessions();

  return {
    cleanup() {
      destroyed = true;
    },
    async refresh() {
      await loadSessions();
    },
  };
}
