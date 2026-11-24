import {
  apiGet,
  apiPost,
  apiPut,
  confirmModal,
  createIcon,
  escapeHtml,
  formatBytes,
  formatTime,
  renderMarkdown,
  saveShortcutLabel,
  showToast,
} from '../api.js';

export async function mount(container, ctx) {
  let activeTab = 'gemini'; // 'gemini' | 'card' | 'backups'
  let data = null;

  let baseGemini = '';
  let draftGemini = '';
  let baseCard = '';
  let draftCard = '';

  let geminiMode = 'split'; // 'edit' | 'split' | 'preview'
  let cardMode = 'split';
  let optBackup = true;
  let optSync = true;
  let rulesExpanded = false; // Collapsed by default for a clean writing studio!

  let selectedBackupId = '';
  let backupTarget = 'gemini'; // 'gemini' | 'character_card'
  let backupDetail = null;
  let backupView = 'diff'; // 'diff' | 'raw' | 'preview'
  let loadingBackup = false;
  let backupReqSeq = 0;

  let saving = false;
  let loadSeq = 0;
  let previewTimer = null;
  let destroyed = false;

  container.innerHTML = `
    <div class="stack" style="gap:24px;">
      <div class="page-tabs-row" id="per-top-bar"></div>
      <div id="per-body">
        <div class="loading-skeleton" style="height:360px;border-radius:12px;"></div>
      </div>
    </div>
  `;

  function isGeminiDirty() { return draftGemini !== baseGemini; }
  function isCardDirty() { return draftCard !== baseCard; }

  function renderTopBar() {
    const bar = container.querySelector('#per-top-bar');
    if (!bar || !data) return;

    const synced = data.synced !== false && data.sandbox?.in_sync !== false;
    const backupCount = (data.backups || []).length;
    const anyDirty = isGeminiDirty() || isCardDirty();

    bar.innerHTML = `
      <div class="page-tabs" id="per-tabs">
        <button type="button" class="page-tab ${activeTab === 'gemini' ? 'active' : ''}" data-tab="gemini">
          线上人设 ${isGeminiDirty() ? '<span class="dirty-dot"></span>' : ''}
        </button>
        <button type="button" class="page-tab ${activeTab === 'card' ? 'active' : ''}" data-tab="card">
          角色卡设计稿 ${isCardDirty() ? '<span class="dirty-dot"></span>' : ''}
        </button>
        <button type="button" class="page-tab ${activeTab === 'backups' ? 'active' : ''}" data-tab="backups">
          历史版本 <span class="count">${backupCount}</span>
        </button>
      </div>

      <div class="row-sm wrap" style="padding-bottom:10px;">
        ${anyDirty ? '<span class="text-xs text-amber" id="per-dirty-badge">有未保存修改</span><span class="metric-sep"></span>' : ''}
        ${synced
          ? '<span class="row-sm text-sm text-muted"><span class="status-dot online" style="width:6px;height:6px;"></span>沙盒已同步</span>'
          : `<span class="row-sm text-sm text-amber"><span class="status-dot warn" style="width:6px;height:6px;"></span>沙盒未同步</span>
             <button type="button" class="btn btn-secondary btn-xs" id="per-sync-btn">${createIcon('repeat', 12)}同步</button>`}
      </div>
    `;

    bar.querySelectorAll('[data-tab]').forEach((btn) => {
      btn.addEventListener('click', () => {
        activeTab = btn.dataset.tab;
        renderTopBar();
        renderBody();
        if (activeTab === 'backups' && !selectedBackupId && (data.backups || []).length) {
          selectBackup(data.backups[0].id);
        }
      });
    });

    bar.querySelector('#per-sync-btn')?.addEventListener('click', async () => {
      try {
        await apiPost('/api/control/sync-sandbox', {});
        showToast('已同步 workspace/GEMINI.md 到 chat-sandbox', 'success');
        await loadData({ preserveDrafts: true });
        ctx.refreshGlobalStatus?.({ silent: true });
      } catch (err) {
        showToast(`同步失败：${err.message}`, 'error');
      }
    });
  }

  function renderRulesBar() {
    const rules = data?.active_rules || {};
    const calls = Array.isArray(rules.call_names) ? rules.call_names : [];
    const cues = Array.isArray(rules.soft_cues) ? rules.soft_cues : [];
    const motifs = Array.isArray(rules.overused_motifs) ? rules.overused_motifs : [];

    return `
      <div class="alert-banner" style="flex-direction:column;align-items:stretch;gap:0;padding:0;">
        <div class="row-between wrap" style="padding:10px 16px;">
          <div class="row-sm wrap text-sm text-secondary">
            <span class="text-muted">${createIcon('sliders', 15)}</span>
            <span class="font-semibold" style="color:var(--text);">运行期语言规则</span>
            <span class="text-muted">点名词 ${calls.length} · 软提示 ${cues.length} · 口癖拦截 ${motifs.length} · 短句去逗号阈值 ${rules.casual_max_chars ?? 30} 字</span>
          </div>
          <div class="row-sm">
            <button type="button" class="btn btn-ghost btn-xs" id="per-toggle-rules">${rulesExpanded ? '收起' : '展开'}</button>
            <button type="button" class="btn btn-ghost btn-xs" id="per-goto-style">修改 ${createIcon('arrowRight', 12)}</button>
          </div>
        </div>
        ${rulesExpanded ? `
          <div class="grid-3" style="padding:12px 16px 14px;border-top:1px solid var(--border-subtle);gap:16px;">
            <div class="stack-xs">
              <span class="text-xs text-muted">显式点名词</span>
              <div class="row wrap" style="gap:4px;">${calls.map((c) => `<span class="badge badge-sm">${escapeHtml(c)}</span>`).join('') || '<span class="text-xs text-muted">无</span>'}</div>
            </div>
            <div class="stack-xs">
              <span class="text-xs text-muted">软点名 / 隐式提示</span>
              <div class="row wrap" style="gap:4px;">${cues.map((c) => `<span class="badge badge-sm">${escapeHtml(c)}</span>`).join('') || '<span class="text-xs text-muted">无</span>'}</div>
            </div>
            <div class="stack-xs">
              <span class="text-xs text-muted">高频口癖拦截（回看 ${escapeHtml(rules.overused_lookback ?? 8)} 条）</span>
              <div class="row wrap" style="gap:4px;">${motifs.map((c) => `<span class="badge badge-sm badge-amber">${escapeHtml(c)}</span>`).join('') || '<span class="text-xs text-muted">无</span>'}</div>
            </div>
          </div>
        ` : ''}
      </div>
    `;
  }

  function renderEditorTab(kind) {
    const isGemini = kind === 'gemini';
    const mode = isGemini ? geminiMode : cardMode;
    const text = isGemini ? draftGemini : draftCard;
    const dirty = isGemini ? isGeminiDirty() : isCardDirty();
    const mtime = isGemini ? (data.gemini_mtime || data.gemini?.mtime) : (data.character_card_mtime || data.character_card?.mtime);
    const lines = text ? text.split('\n').length : 0;

    return `
      <div class="stack">
        ${isGemini ? renderRulesBar() : `
          <div class="alert-banner text-sm text-secondary">
            <span>docs/CHARACTER_CARD.md 是给人看的角色设定稿，模型不会直接读取；线上生效的人设请编辑「线上人设」。</span>
          </div>
        `}

        <div class="card editor-frame">
          <div class="editor-bar">
            <div class="row wrap" style="gap:12px;">
              <div class="tabs">
                <button type="button" class="tab-btn ${mode === 'edit' ? 'active' : ''}" data-emode="edit">编辑</button>
                <button type="button" class="tab-btn ${mode === 'split' ? 'active' : ''}" data-emode="split">分栏</button>
                <button type="button" class="tab-btn ${mode === 'preview' ? 'active' : ''}" data-emode="preview">预览</button>
              </div>
              <span class="mono text-xs text-muted hide-xs">${isGemini ? 'workspace/GEMINI.md' : 'docs/CHARACTER_CARD.md'}</span>
              <span class="text-xs text-muted tabular" id="per-editor-stats">${lines} 行 · ${text.length} 字符${mtime ? ` · 更新于 ${formatTime(mtime)}` : ''}</span>
              <span class="text-xs ${dirty ? 'text-amber' : 'text-muted'}" id="per-pane-state">${dirty ? '已修改' : '已保存'}</span>
            </div>
            <div class="row-sm wrap">
              ${isGemini ? `
                <label class="row-sm text-sm text-secondary" style="cursor:pointer;gap:6px;">
                  <input type="checkbox" id="per-opt-backup" ${optBackup ? 'checked' : ''} />自动备份
                </label>
                <label class="row-sm text-sm text-secondary" style="cursor:pointer;gap:6px;margin-right:6px;">
                  <input type="checkbox" id="per-opt-sync" ${optSync ? 'checked' : ''} />同步沙盒
                </label>
              ` : ''}
              <button type="button" class="btn btn-ghost btn-sm" id="per-revert-btn" ${!dirty ? 'disabled' : ''}>还原</button>
              <button type="button" class="btn btn-primary btn-sm" id="per-save-btn" ${saving || !dirty ? 'disabled' : ''}>
                保存 <span style="opacity:0.6;">${saveShortcutLabel()}</span>
              </button>
            </div>
          </div>

          <div class="editor-panes${mode !== 'split' ? ' single' : ''}">
            ${mode !== 'preview' ? '<textarea class="persona-textarea" id="per-textarea" spellcheck="false"></textarea>' : ''}
            ${mode !== 'edit' ? `<div class="md-preview persona-preview-body" id="per-preview">${renderMarkdown(text)}</div>` : ''}
          </div>
        </div>
      </div>
    `;
  }

  function renderBackupsTab() {
    const backups = data?.backups || [];
    return `
      <div class="split-layout split-viewport-with-bar">
        <aside class="list-pane">
          <div class="pane-header">
            <span class="font-semibold text-sm">历史备份 <span class="text-muted" style="font-weight:500;">${backups.length}</span></span>
          </div>
          <div class="pane-scroll">
            ${backups.length ? backups.map((b) => `
              <button type="button" class="list-item ${b.id === selectedBackupId ? 'active' : ''}" data-bk-id="${escapeHtml(b.id)}">
                <div class="flex-1" style="min-width:0;">
                  <div class="text-sm truncate" style="font-weight:550;">${escapeHtml(b.name || b.id)}</div>
                  <div class="text-xs text-muted" style="margin-top:2px;">
                    ${[b.has_gemini ? 'GEMINI.md' : '', b.has_character_card ? '角色卡' : '', formatBytes(b.size)].filter(Boolean).join(' · ')}
                  </div>
                </div>
              </button>
            `).join('') : '<div class="empty-state"><div class="text-xs text-muted">暂无历史备份</div></div>'}
          </div>
        </aside>

        <section class="detail-pane" id="per-bk-detail"></section>
      </div>
    `;
  }

  function getBackupMeta(bid) {
    return (data?.backups || []).find((b) => b.id === bid) || null;
  }

  function getDefaultBackupTarget(meta) {
    if (meta && !meta.has_gemini && meta.has_character_card) {
      return 'character_card';
    }
    return 'gemini';
  }

  function renderBackupDetail() {
    const pane = container.querySelector('#per-bk-detail');
    if (!pane) return;
    if (!selectedBackupId) {
      pane.innerHTML = `<div class="empty-state" style="height:100%;"><div class="text-sm text-muted">选择左侧历史备份对比差异</div></div>`;
      return;
    }
    if (loadingBackup) {
      pane.innerHTML = `<div style="padding:18px;"><div class="loading-skeleton" style="height:220px;border-radius:10px;"></div></div>`;
      return;
    }
    if (!backupDetail || backupDetail.error) {
      pane.innerHTML = `<div class="empty-state" style="height:100%;"><div class="text-sm text-danger">${escapeHtml(backupDetail?.error || '读取备份失败')}</div></div>`;
      return;
    }

    const stats = backupDetail.stats || {};
    const meta = getBackupMeta(selectedBackupId);
    const hasGemini = meta
      ? Boolean(meta.has_gemini)
      : (backupDetail.gemini_md !== null && backupDetail.gemini_md !== undefined);
    const hasCard = meta
      ? Boolean(meta.has_character_card)
      : Boolean(backupDetail.character_card_md);
    const effectiveTarget =
      backupDetail.filename === 'CHARACTER_CARD.md' || (!hasGemini && hasCard)
        ? 'character_card'
        : (backupTarget === 'character_card' && hasCard ? 'character_card' : 'gemini');
    const isCardTarget = effectiveTarget === 'character_card';
    const targetFileName = isCardTarget ? 'CHARACTER_CARD.md' : 'GEMINI.md';
    const targetLabel = isCardTarget ? '角色卡' : '线上人设';
    const targetContent = isCardTarget
      ? (typeof backupDetail.character_card_md === 'string' ? backupDetail.character_card_md : '')
      : (typeof backupDetail.gemini_md === 'string' ? backupDetail.gemini_md : '');

    let contentHtml = '';
    if (backupView === 'diff') {
      const diffLines = Array.isArray(backupDetail.diff_lines)
        ? backupDetail.diff_lines
        : String(backupDetail.diff || '').split('\n');
      if (backupDetail.identical || !diffLines.filter(Boolean).length) {
        contentHtml = `<div class="empty-state"><div class="text-sm text-muted">与当前${isCardTarget ? '角色卡' : '线上'} ${targetFileName} 内容完全一致</div></div>`;
      } else {
        contentHtml = `<div class="diff-view">${diffLines.map((line) => {
          const text = typeof line === 'string' ? line : (line.text || '');
          const cls = text.startsWith('+') && !text.startsWith('+++')
            ? 'diff-add'
            : (text.startsWith('-') && !text.startsWith('---') ? 'diff-del' : (text.startsWith('@@') ? 'diff-hunk' : ''));
          return `<div class="diff-line ${cls}">${escapeHtml(text)}</div>`;
        }).join('')}</div>`;
      }
    } else if (backupView === 'raw') {
      contentHtml = `<pre class="code-block" style="border:none;">${escapeHtml(targetContent)}</pre>`;
    } else {
      contentHtml = `<div class="md-preview" style="padding:16px;">${renderMarkdown(targetContent)}</div>`;
    }

    pane.innerHTML = `
      <div class="pane-header">
        <div class="row-between wrap">
          <div class="row-sm wrap">
            <span class="font-semibold mono text-xs">${escapeHtml(selectedBackupId)}</span>
            ${hasGemini && hasCard ? `
              <div class="tabs">
                <button type="button" class="tab-btn ${!isCardTarget ? 'active' : ''}" data-btarget="gemini">GEMINI.md</button>
                <button type="button" class="tab-btn ${isCardTarget ? 'active' : ''}" data-btarget="character_card">CHARACTER_CARD.md</button>
              </div>
            ` : `<span class="badge badge-sm">${targetFileName}</span>`}
            ${stats.added != null ? `<span class="text-xs text-success tabular">+${stats.added}</span>` : ''}
            ${stats.removed != null ? `<span class="text-xs text-danger tabular">-${stats.removed}</span>` : ''}
          </div>
          <div class="row-sm wrap">
            <div class="tabs">
              <button type="button" class="tab-btn ${backupView === 'diff' ? 'active' : ''}" data-bview="diff">差异</button>
              <button type="button" class="tab-btn ${backupView === 'raw' ? 'active' : ''}" data-bview="raw">备份原文</button>
              <button type="button" class="tab-btn ${backupView === 'preview' ? 'active' : ''}" data-bview="preview">预览</button>
            </div>
            <button type="button" class="btn btn-primary btn-sm" id="per-restore-bk" ${!targetContent ? 'disabled' : ''}>载入到${targetLabel}</button>
          </div>
        </div>
      </div>
      <div class="pane-scroll" style="padding:0;">${contentHtml}</div>
    `;

    pane.querySelectorAll('[data-btarget]').forEach((b) => {
      b.addEventListener('click', () => {
        const nextTarget = b.dataset.btarget;
        if (nextTarget && nextTarget !== effectiveTarget) {
          selectBackup(selectedBackupId, nextTarget, { keepView: true });
        }
      });
    });

    pane.querySelectorAll('[data-bview]').forEach((b) => {
      b.addEventListener('click', () => { backupView = b.dataset.bview; renderBackupDetail(); });
    });

    pane.querySelector('#per-restore-bk')?.addEventListener('click', async () => {
      if (!targetContent) return;
      if (isCardTarget) {
        if (isCardDirty()) {
          const ok = await confirmModal({
            title: '覆盖当前未保存的角色卡草稿？',
            message: '当前 CHARACTER_CARD.md 编辑器中有尚未保存的修改，载入历史备份将覆盖它们。',
            confirmText: '确认覆盖',
            danger: true,
          });
          if (!ok || destroyed) return;
        }
        draftCard = targetContent;
        activeTab = 'card';
        showToast('已将历史备份载入角色卡编辑区（请检查后点击保存）', 'info');
      } else {
        if (isGeminiDirty()) {
          const ok = await confirmModal({
            title: '覆盖当前未保存的人设草稿？',
            message: '当前 GEMINI.md 编辑器中有尚未保存的修改，载入历史备份将覆盖它们。',
            confirmText: '确认覆盖',
            danger: true,
          });
          if (!ok || destroyed) return;
        }
        draftGemini = targetContent;
        activeTab = 'gemini';
        showToast('已将历史备份载入线上人设编辑区（请检查后点击保存）', 'info');
      }
      renderTopBar();
      renderBody();
    });
  }

  async function selectBackup(bid, requestedTarget = null, { keepView = false } = {}) {
    const meta = getBackupMeta(bid);
    const nextTarget = requestedTarget === 'character_card' && (!meta || meta.has_character_card)
      ? 'character_card'
      : requestedTarget === 'gemini' && (!meta || meta.has_gemini)
        ? 'gemini'
        : getDefaultBackupTarget(meta);

    const reqSeq = ++backupReqSeq;
    selectedBackupId = bid;
    backupTarget = nextTarget;
    loadingBackup = true;
    backupDetail = null;
    if (!keepView) backupView = 'diff';
    renderBody();
    try {
      const res = await apiGet(`/api/persona/backup/${encodeURIComponent(bid)}?target=${encodeURIComponent(nextTarget)}`);
      if (destroyed || reqSeq !== backupReqSeq) return;
      backupDetail = res;
      if (res?.filename === 'CHARACTER_CARD.md') backupTarget = 'character_card';
      else if (res?.filename === 'GEMINI.md') backupTarget = 'gemini';
    } catch (err) {
      if (destroyed || reqSeq !== backupReqSeq) return;
      backupDetail = { error: err.message };
    } finally {
      if (!destroyed && reqSeq === backupReqSeq) {
        loadingBackup = false;
        renderBackupDetail();
      }
    }
  }

  async function saveCurrent(kind = activeTab) {
    if (saving || (kind !== 'gemini' && kind !== 'card')) return;
    saving = true;
    const btn = container.querySelector('#per-save-btn');
    if (btn) btn.disabled = true;
    try {
      if (kind === 'gemini') {
        const content = draftGemini;
        const syncSandbox = optSync;
        await apiPut('/api/persona/gemini', {
          content,
          create_backup: optBackup,
          sync_sandbox: syncSandbox,
        });
        if (destroyed) return;
        baseGemini = content;
        showToast(syncSandbox ? '人设已保存并同步到 chat-sandbox' : '人设已保存', 'success');
      } else {
        const content = draftCard;
        await apiPut('/api/persona/character-card', { content, create_backup: true });
        if (destroyed) return;
        baseCard = content;
        showToast('角色卡设计稿已保存', 'success');
      }
      await loadData({ preserveDrafts: true });
      ctx.refreshGlobalStatus?.({ silent: true });
    } catch (err) {
      if (!destroyed) showToast(`保存失败：${err.message}`, 'error');
    } finally {
      saving = false;
      if (!destroyed) {
        renderTopBar();
        renderBody();
      }
    }
  }

  function renderBody() {
    const body = container.querySelector('#per-body');
    if (!body || !data) return;

    if (activeTab === 'backups') {
      body.innerHTML = renderBackupsTab();
      body.querySelectorAll('[data-bk-id]').forEach((b) => {
        b.addEventListener('click', () => selectBackup(b.dataset.bkId));
      });
      renderBackupDetail();
      return;
    }

    const isGemini = activeTab === 'gemini';
    body.innerHTML = renderEditorTab(activeTab);

    const ta = body.querySelector('#per-textarea');
    if (ta) {
      ta.value = isGemini ? draftGemini : draftCard;
      ta.addEventListener('input', () => {
        const val = ta.value;
        if (isGemini) draftGemini = val;
        else draftCard = val;

        const dirty = isGemini ? isGeminiDirty() : isCardDirty();
        const statsEl = body.querySelector('#per-editor-stats');
        if (statsEl) statsEl.textContent = `${val ? val.split('\n').length : 0} 行 · ${val.length} 字符`;
        const paneBadge = body.querySelector('#per-pane-state');
        if (paneBadge) {
          paneBadge.textContent = dirty ? '已修改' : '已保存';
          paneBadge.className = `text-xs ${dirty ? 'text-amber' : 'text-muted'}`;
        }
        const saveBtn = body.querySelector('#per-save-btn');
        if (saveBtn) saveBtn.disabled = saving || !dirty;
        const revBtn = body.querySelector('#per-revert-btn');
        if (revBtn) revBtn.disabled = !dirty;
        renderTopBar();

        const previewEl = body.querySelector('#per-preview');
        if (previewEl) {
          if (previewTimer) clearTimeout(previewTimer);
          previewTimer = setTimeout(() => {
            if (!destroyed) previewEl.innerHTML = renderMarkdown(val);
          }, 120);
        }
      });

      ta.addEventListener('keydown', (e) => {
        if (e.key === 'Tab') {
          e.preventDefault();
          const start = ta.selectionStart;
          const end = ta.selectionEnd;
          ta.value = `${ta.value.slice(0, start)}  ${ta.value.slice(end)}`;
          ta.selectionStart = ta.selectionEnd = start + 2;
          ta.dispatchEvent(new Event('input'));
        }
      });
    }

    body.querySelector('#per-toggle-rules')?.addEventListener('click', () => {
      rulesExpanded = !rulesExpanded;
      renderBody();
    });
    body.querySelector('#per-goto-style')?.addEventListener('click', () => {
      ctx.navigate('settings', { tab: 'style' });
    });
    body.querySelectorAll('[data-emode]').forEach((b) => {
      b.addEventListener('click', () => {
        if (isGemini) geminiMode = b.dataset.emode;
        else cardMode = b.dataset.emode;
        renderBody();
      });
    });
    body.querySelector('#per-opt-backup')?.addEventListener('change', (e) => { optBackup = e.target.checked; });
    body.querySelector('#per-opt-sync')?.addEventListener('change', (e) => { optSync = e.target.checked; });
    body.querySelector('#per-revert-btn')?.addEventListener('click', () => {
      if (isGemini) draftGemini = baseGemini;
      else draftCard = baseCard;
      renderTopBar();
      renderBody();
    });
    body.querySelector('#per-save-btn')?.addEventListener('click', () => saveCurrent(activeTab));
  }

  async function loadData({ preserveDrafts = false } = {}) {
    const seq = ++loadSeq;
    try {
      const fresh = await apiGet('/api/persona');
      if (destroyed || seq !== loadSeq) return;
      const hadGemDirty = preserveDrafts && isGeminiDirty();
      const hadCardDirty = preserveDrafts && isCardDirty();
      data = fresh;
      baseGemini = String(data.gemini_md ?? data.gemini?.content ?? '');
      baseCard = String(data.character_card_md ?? data.character_card?.content ?? '');
      if (!hadGemDirty) draftGemini = baseGemini;
      if (!hadCardDirty) draftCard = baseCard;
      renderTopBar();
      renderBody();
    } catch (err) {
      if (destroyed || seq !== loadSeq) return;
      if (data && preserveDrafts) {
        showToast(`刷新人设失败：${err.message}`, 'error');
        return;
      }
      container.querySelector('#per-body').innerHTML = `
        <div class="card"><div class="empty-state">
          <div class="empty-state-title">加载人设失败</div>
          <div class="text-sm text-muted">${escapeHtml(err.message)}</div>
          <button type="button" class="btn btn-primary btn-sm mt-8" id="per-retry">重试</button>
        </div></div>
      `;
      container.querySelector('#per-retry')?.addEventListener('click', () => loadData());
    }
  }

  const onKeyDown = (e) => {
    if (!container.isConnected) return;
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 's') {
      if ((activeTab === 'gemini' && isGeminiDirty()) || (activeTab === 'card' && isCardDirty())) {
        e.preventDefault();
        saveCurrent(activeTab);
      }
    }
  };
  document.addEventListener('keydown', onKeyDown);

  await loadData();

  return {
    cleanup() {
      destroyed = true;
      if (previewTimer) clearTimeout(previewTimer);
      document.removeEventListener('keydown', onKeyDown);
    },
    async refresh() {
      if (!saving) await loadData({ preserveDrafts: true });
    },
  };
}
