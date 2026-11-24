import {
  apiGet,
  apiPost,
  apiPut,
  confirmModal,
  createIcon,
  escapeHtml,
  formatTime,
  saveShortcutLabel,
  showToast,
} from '../api.js';

const VIEW_LINK_META = {
  permissions: { label: '权限与沙盒', route: 'permissions' },
  memory: { label: '长期记忆', route: 'memory' },
  config: { label: '行为与配置', route: 'settings' },
  workbench: { label: '行为与配置', route: 'settings' },
  settings: { label: '行为与配置', route: 'settings' },
  persona: { label: '人设与指令', route: 'persona' },
  overview: { label: '运行总览', route: 'overview' },
  logs: { label: '实时日志', route: 'logs' },
  sessions: { label: '会话与聊天', route: 'sessions' },
  napcat: { label: 'QQ 与 NapCat', route: 'napcat' },
  requests: { label: '入群监控', route: 'requests' },
};

const PRESET_FALLBACK_LABELS = {
  ultra_lean: '极简省流模式',
  token_saver: '日常省流均衡',
  balanced_group: '活跃群聊均衡',
  full_featured: '全功能完整模式',
  custom: '自定义配置',
};

function deepClone(obj) {
  return JSON.parse(JSON.stringify(obj ?? {}));
}

function stableJson(obj) {
  if (obj === null || typeof obj !== 'object') return JSON.stringify(obj);
  if (Array.isArray(obj)) return `[${obj.map(stableJson).join(',')}]`;
  const keys = Object.keys(obj).sort();
  return `{${keys.map((k) => `${JSON.stringify(k)}:${stableJson(obj[k])}`).join(',')}}`;
}

function tokenLevelBadgeMeta(impact) {
  const rawLevel = String(impact?.level || '').trim().toLowerCase();
  const label = String(impact?.level_label || '').trim();
  if (rawLevel === 'very_high' || rawLevel === 'extreme' || rawLevel === 'critical' || label.includes('极高')) {
    return { cls: 'badge-danger', label: label || '极高耗' };
  }
  if (rawLevel === 'high' || label === '高' || label.includes('高耗')) {
    return { cls: 'badge-warning', label: label || '高耗' };
  }
  if (rawLevel === 'medium' || rawLevel === 'mid' || label === '中' || label.includes('中')) {
    return { cls: 'badge-amber', label: label || '中耗' };
  }
  if (rawLevel === 'low' || label === '低' || label.includes('低')) {
    return { cls: 'badge-info', label: label || '低耗' };
  }
  return { cls: 'badge-muted', label: label || '零开销' };
}

function isHighTokenPlugin(p) {
  const lvl = String(p?.token_impact?.level || '').trim().toLowerCase();
  if (['very_high', 'extreme', 'critical', 'high'].includes(lvl)) return true;
  const lbl = String(p?.token_impact?.level_label || '');
  if (lbl.includes('极高') || lbl.includes('高')) return true;
  if (Number(p?.token_impact?.per_turn_tokens || 0) >= 150) return true;
  if (p?.token_impact?.background_llm || p?.token_impact?.extra_turns) return true;
  return false;
}

function normalizePluginDraftEntry(p, cfgEntry) {
  const enabled =
    cfgEntry && typeof cfgEntry.enabled === 'boolean'
      ? cfgEntry.enabled
      : Boolean(p?.enabled);
  const settings = {};
  const schema = Array.isArray(p?.settings_schema) ? p.settings_schema : [];
  const rawSettings =
    (cfgEntry && cfgEntry.settings && typeof cfgEntry.settings === 'object' ? cfgEntry.settings : null) ||
    (p?.settings && typeof p.settings === 'object' ? p.settings : {});

  schema.forEach((field) => {
    if (!field || !field.key) return;
    if (Object.prototype.hasOwnProperty.call(rawSettings, field.key)) {
      settings[field.key] = rawSettings[field.key];
    } else if (field.default !== undefined) {
      settings[field.key] = field.default;
    }
  });
  Object.entries(rawSettings || {}).forEach(([k, v]) => {
    if (!Object.prototype.hasOwnProperty.call(settings, k)) {
      settings[k] = v;
    }
  });
  return { enabled, settings };
}

export async function mount(container, ctx) {
  let destroyed = false;
  let focusTimer = null;
  let scrolledToFocus = false;

  const initialFocusPlugin = ctx?.params?.plugin ? String(ctx.params.plugin).trim() : '';

  const state = {
    loading: true,
    saving: false,
    scanning: false,
    applyingPreset: '',
    error: null,
    data: null,
    serverPlugins: {}, // canonical normalized { [id]: { enabled, settings } }
    draftPlugins: {},  // editable working copy { [id]: { enabled, settings } }
    categoryFilter: 'all',
    quickFilter: 'all', // 'all' | 'high_token' | 'enabled' | 'disabled'
    search: '',
    expandedSettings: new Set(),
    focusedPluginId: initialFocusPlugin,
  };

  function hydrateFromData(data) {
    state.data = data || {};
    const plugins = Array.isArray(data?.plugins) ? data.plugins : [];
    const cfgPlugins = data?.config?.plugins && typeof data.config.plugins === 'object' ? data.config.plugins : {};
    const nextServer = {};
    plugins.forEach((p) => {
      if (!p || !p.id) return;
      nextServer[p.id] = normalizePluginDraftEntry(p, cfgPlugins[p.id]);
    });
    state.serverPlugins = deepClone(nextServer);
    state.draftPlugins = deepClone(nextServer);
  }

  function isPluginDirty(pid) {
    return stableJson(state.draftPlugins[pid] || {}) !== stableJson(state.serverPlugins[pid] || {});
  }

  function getChangedPluginIds() {
    const ids = Object.keys(state.draftPlugins || {});
    return ids.filter((id) => isPluginDirty(id));
  }

  function isDirty() {
    return getChangedPluginIds().length > 0;
  }

  function getDraftEnabled(pid) {
    if (state.draftPlugins[pid] && typeof state.draftPlugins[pid].enabled === 'boolean') {
      return state.draftPlugins[pid].enabled;
    }
    const found = (state.data?.plugins || []).find((p) => p.id === pid);
    return Boolean(found?.enabled);
  }

  function computeDraftEffectiveMap() {
    const plugins = Array.isArray(state.data?.plugins) ? state.data.plugins : [];
    const eff = {};
    const missingMap = {};

    // Match backend bot_plugins.is_enabled(): determined by enabled config + transitive requires
    // (env_disabled_keys are surfaced as separate warnings and do not alter effective_enabled)
    plugins.forEach((p) => {
      eff[p.id] = getDraftEnabled(p.id);
      missingMap[p.id] = [];
    });

    // Multi-pass fixed point for transitive requires
    const maxPasses = Math.max(4, plugins.length);
    for (let pass = 0; pass < maxPasses; pass += 1) {
      let changed = false;
      plugins.forEach((p) => {
        const reqs = Array.isArray(p.requires) ? p.requires : [];
        const missing = reqs.filter((r) => !eff[r]);
        missingMap[p.id] = missing;
        const nextEff = getDraftEnabled(p.id) && missing.length === 0;
        if (nextEff !== eff[p.id]) {
          eff[p.id] = nextEff;
          changed = true;
        }
      });
      if (!changed) break;
    }

    return { eff, missingMap };
  }

  function computeLiveSummary() {
    const srv = state.data?.summary || {};
    const plugins = Array.isArray(state.data?.plugins) ? state.data.plugins : [];
    const total = Number(srv.total_count ?? plugins.length);
    const builtinCount = Number(srv.builtin_count ?? plugins.filter((p) => p.builtin).length);
    const customCount = Number(srv.custom_count ?? Math.max(0, total - builtinCount));

    const dirty = isDirty();
    if (!dirty) {
      return {
        dirty: false,
        enabled_count: Number(srv.enabled_count ?? 0),
        disabled_count: Number(srv.disabled_count ?? 0),
        total_count: total,
        builtin_count: builtinCount,
        custom_count: customCount,
        per_turn_active_tokens: Number(srv.per_turn_active_tokens ?? 0),
        per_turn_max_tokens: Number(srv.per_turn_max_tokens ?? 0),
        per_turn_saved_tokens: Number(srv.per_turn_saved_tokens ?? 0),
        per_turn_save_pct: Number(srv.per_turn_save_pct ?? 0),
        per_session_active_tokens: Number(srv.per_session_active_tokens ?? 0),
        per_session_max_tokens: Number(srv.per_session_max_tokens ?? 0),
        per_session_saved_tokens: Number(srv.per_session_saved_tokens ?? 0),
        per_session_save_pct: Number(srv.per_session_save_pct ?? 0),
        sandbox_gemini_raw_chars: Number(srv.sandbox_gemini_raw_chars ?? 0),
        sandbox_gemini_compiled_chars: Number(srv.sandbox_gemini_compiled_chars ?? 0),
        sandbox_gemini_saved_chars: Number(srv.sandbox_gemini_saved_chars ?? 0),
        sandbox_gemini_raw_tokens: Number(srv.sandbox_gemini_raw_tokens ?? 0),
        sandbox_gemini_compiled_tokens: Number(srv.sandbox_gemini_compiled_tokens ?? 0),
        sandbox_gemini_saved_tokens: Number(srv.sandbox_gemini_saved_tokens ?? 0),
        background_llm_active_count: Number(srv.background_llm_active_count ?? 0),
        background_llm_max_count: Number(srv.background_llm_max_count ?? 0),
        background_llm_disabled_count: Number(srv.background_llm_disabled_count ?? 0),
        extra_turns_active_count: Number(srv.extra_turns_active_count ?? 0),
        extra_turns_max_count: Number(srv.extra_turns_max_count ?? 0),
        extra_turns_disabled_count: Number(srv.extra_turns_disabled_count ?? 0),
        saved_delta_per_turn: 0,
      };
    }

    const { eff } = computeDraftEffectiveMap();
    let deltaEnabled = 0;
    let deltaPerTurn = 0;
    let deltaPerSession = 0;
    let deltaBg = 0;
    let deltaExtra = 0;

    plugins.forEach((p) => {
      const srvEff = Boolean(p.effective_enabled);
      const draftEff = Boolean(eff[p.id]);
      if (draftEff !== srvEff) {
        const sign = draftEff ? 1 : -1;
        deltaEnabled += sign;
        const turnCost = Number(
          srvEff
            ? (p.token_estimate?.per_turn_active ?? p.token_estimate?.per_turn_max ?? p.token_impact?.per_turn_tokens ?? 0)
            : (p.token_estimate?.per_turn_max ?? p.token_impact?.per_turn_tokens ?? 0),
        );
        const sessCost = Number(
          srvEff
            ? (p.token_estimate?.per_session_active ?? p.token_estimate?.per_session_max ?? p.token_impact?.per_session_tokens ?? 0)
            : (p.token_estimate?.per_session_max ?? p.token_impact?.per_session_tokens ?? 0),
        );
        const bgCost = Number(
          srvEff
            ? (p.token_estimate?.background_llm_active ? 1 : 0)
            : (p.token_estimate?.background_llm_max ?? (p.token_impact?.background_llm ? 1 : 0)),
        );
        const extraCost = Number(
          srvEff
            ? (p.token_estimate?.extra_turns_active ? 1 : 0)
            : (p.token_estimate?.extra_turns_max ?? (p.token_impact?.extra_turns ? 1 : 0)),
        );
        deltaPerTurn += sign * turnCost;
        deltaPerSession += sign * sessCost;
        deltaBg += sign * bgCost;
        deltaExtra += sign * extraCost;
      }
    });

    const clamp = (val, lo, hi) => Math.max(lo, Math.min(hi, val));
    const enabledCount = clamp(Number(srv.enabled_count ?? 0) + deltaEnabled, 0, total);
    const disabledCount = Math.max(0, total - enabledCount);

    const perTurnMax = Number(srv.per_turn_max_tokens ?? 0);
    const perTurnActive = clamp(Number(srv.per_turn_active_tokens ?? 0) + deltaPerTurn, 0, perTurnMax);
    const perTurnSaved = Math.max(0, perTurnMax - perTurnActive);
    const perTurnSavePct = perTurnMax > 0 ? Math.round((perTurnSaved / perTurnMax) * 100) : 0;

    const perSessMax = Number(srv.per_session_max_tokens ?? 0);
    const perSessActive = clamp(Number(srv.per_session_active_tokens ?? 0) + deltaPerSession, 0, perSessMax);
    const perSessSaved = Math.max(0, perSessMax - perSessActive);
    const perSessSavePct = perSessMax > 0 ? Math.round((perSessSaved / perSessMax) * 100) : 0;

    const bgMax = Number(srv.background_llm_max_count ?? 0);
    const bgActive = clamp(Number(srv.background_llm_active_count ?? 0) + deltaBg, 0, bgMax);
    const bgDisabled = Math.max(0, bgMax - bgActive);

    const extraMax = Number(srv.extra_turns_max_count ?? 0);
    const extraActive = clamp(Number(srv.extra_turns_active_count ?? 0) + deltaExtra, 0, extraMax);
    const extraDisabled = Math.max(0, extraMax - extraActive);

    const savedDeltaPerTurn = perTurnSaved - Number(srv.per_turn_saved_tokens ?? 0);

    return {
      dirty: true,
      enabled_count: enabledCount,
      disabled_count: disabledCount,
      total_count: total,
      builtin_count: builtinCount,
      custom_count: customCount,
      per_turn_active_tokens: perTurnActive,
      per_turn_max_tokens: perTurnMax,
      per_turn_saved_tokens: perTurnSaved,
      per_turn_save_pct: perTurnSavePct,
      per_session_active_tokens: perSessActive,
      per_session_max_tokens: perSessMax,
      per_session_saved_tokens: perSessSaved,
      per_session_save_pct: perSessSavePct,
      sandbox_gemini_raw_chars: Number(srv.sandbox_gemini_raw_chars ?? 0),
      sandbox_gemini_compiled_chars: Number(srv.sandbox_gemini_compiled_chars ?? 0),
      sandbox_gemini_saved_chars: Number(srv.sandbox_gemini_saved_chars ?? 0),
      sandbox_gemini_raw_tokens: Number(srv.sandbox_gemini_raw_tokens ?? 0),
      sandbox_gemini_compiled_tokens: Number(srv.sandbox_gemini_compiled_tokens ?? 0),
      sandbox_gemini_saved_tokens: Number(srv.sandbox_gemini_saved_tokens ?? 0),
      background_llm_active_count: bgActive,
      background_llm_max_count: bgMax,
      background_llm_disabled_count: bgDisabled,
      extra_turns_active_count: extraActive,
      extra_turns_max_count: extraMax,
      extra_turns_disabled_count: extraDisabled,
      saved_delta_per_turn: savedDeltaPerTurn,
    };
  }

  async function loadPlugins({ refresh = false } = {}) {
    state.loading = !state.data;
    state.error = null;
    if (state.loading) render();
    try {
      const data = await apiGet(`/api/plugins${refresh ? '?refresh=1' : ''}`);
      if (destroyed) return;
      hydrateFromData(data);
      state.loading = false;
      render();
      scrollToFocusedPluginIfNeeded();
    } catch (err) {
      if (destroyed) return;
      state.loading = false;
      state.error = err?.message || '加载插件配置失败';
      render();
    }
  }

  function scrollToFocusedPluginIfNeeded() {
    if (!state.focusedPluginId || scrolledToFocus) return;
    const card = container.querySelector(`[data-plugin-card="${CSS.escape(state.focusedPluginId)}"]`);
    if (card) {
      scrolledToFocus = true;
      window.requestAnimationFrame(() => {
        if (!destroyed) {
          card.scrollIntoView({ behavior: 'smooth', block: 'center' });
        }
      });
      if (focusTimer) clearTimeout(focusTimer);
      focusTimer = setTimeout(() => {
        if (!destroyed) {
          state.focusedPluginId = '';
          card.classList.remove('is-focused');
        }
      }, 4200);
    }
  }

  async function saveDrafts() {
    if (state.saving || !isDirty()) return;
    state.saving = true;
    render();
    try {
      const data = await apiPut('/api/plugins', { plugins: state.draftPlugins });
      if (destroyed) return;
      hydrateFromData(data);
      state.saving = false;
      const savedTok = Number(data?.summary?.per_turn_saved_tokens || 0);
      if (data?.sandbox_synced === false && data?.sandbox_sync_error) {
        showToast(`插件配置已保存，但沙盒指令同步失败：${data.sandbox_sync_error}`, 'warning');
      } else {
        showToast(
          savedTok > 0
            ? `插件配置已保存并热生效（单轮预计省 ${savedTok} tok）`
            : '插件配置已保存并热生效（无需重启机器人）',
          'success',
        );
      }
      render();
      ctx?.refreshGlobalStatus?.({ silent: true });
    } catch (err) {
      if (destroyed) return;
      state.saving = false;
      showToast(`保存插件配置失败：${err?.message || '未知错误'}`, 'error');
      render();
    }
  }

  async function handleApplyPreset(preset) {
    if (!preset || state.saving || state.applyingPreset) return;
    if (!isDirty() && state.data?.active_preset === preset.id) {
      showToast(`当前已处于「${preset.name}」`, 'info');
      return;
    }
    if (isDirty()) {
      const ok = await confirmModal({
        title: `切换预设：${preset.name}`,
        message: `当前有 ${getChangedPluginIds().length} 个插件存在未保存的改动。切换预设将覆盖未保存修改并立即热生效，确定继续？`,
        confirmText: '切换并生效',
        danger: false,
      });
      if (!ok || destroyed) return;
    }

    state.applyingPreset = preset.id;
    render();
    try {
      const data = await apiPost('/api/plugins/preset', { preset_id: preset.id });
      if (destroyed) return;
      hydrateFromData(data);
      state.applyingPreset = '';
      const savedTok = Number(data?.summary?.per_turn_saved_tokens || 0);
      showToast(
        savedTok > 0
          ? `已应用「${preset.name}」并热生效 · 单轮预计省 ${savedTok} tok`
          : `已应用「${preset.name}」并热生效`,
        'success',
      );
      render();
      ctx?.refreshGlobalStatus?.({ silent: true });
    } catch (err) {
      if (destroyed) return;
      state.applyingPreset = '';
      showToast(`应用预设失败：${err?.message || '未知错误'}`, 'error');
      render();
    }
  }

  async function handleRescan() {
    if (state.scanning || state.saving) return;
    if (isDirty()) {
      const ok = await confirmModal({
        title: '重新扫描插件目录',
        message: `当前有 ${getChangedPluginIds().length} 个插件存在未保存的修改。重新扫描将重新载入磁盘插件列表并放弃未保存草稿，确定继续？`,
        confirmText: '重新扫描',
        danger: false,
      });
      if (!ok || destroyed) return;
    }

    state.scanning = true;
    render();
    try {
      const data = await apiPost('/api/plugins/rescan', {});
      if (destroyed) return;
      hydrateFromData(data);
      state.scanning = false;
      const errCount = Array.isArray(data?.load_errors) ? data.load_errors.length : 0;
      const total = Number(data?.summary?.total_count ?? (data?.plugins || []).length);
      if (errCount > 0) {
        showToast(`扫描完成：发现 ${total} 个插件，另有 ${errCount} 个文件加载出错`, 'warning');
      } else {
        showToast(`插件扫描完成，共发现 ${total} 个可用插件`, 'success');
      }
      render();
      ctx?.refreshGlobalStatus?.({ silent: true });
    } catch (err) {
      if (destroyed) return;
      state.scanning = false;
      showToast(`扫描插件失败：${err?.message || '未知错误'}`, 'error');
      render();
    }
  }

  async function handleResetDefaults() {
    if (state.saving || state.scanning) return;
    const ok = await confirmModal({
      title: '恢复插件默认配置',
      message: '将把所有插件的开关状态与微调参数恢复为默认配置，并同步刷新沙盒指令与立即热生效。确定继续？',
      confirmText: '恢复默认',
      danger: true,
    });
    if (!ok || destroyed) return;

    state.saving = true;
    render();
    try {
      const data = await apiPost('/api/plugins/reset', {});
      if (destroyed) return;
      hydrateFromData(data);
      state.saving = false;
      showToast('已恢复全部插件默认配置并热生效', 'success');
      render();
      ctx?.refreshGlobalStatus?.({ silent: true });
    } catch (err) {
      if (destroyed) return;
      state.saving = false;
      showToast(`恢复默认失败：${err?.message || '未知错误'}`, 'error');
      render();
    }
  }

  function resolveActivePresetLabel() {
    const activeId = String(state.data?.active_preset || 'custom');
    const found = (state.data?.presets || []).find((pr) => pr.id === activeId);
    return found?.name || PRESET_FALLBACK_LABELS[activeId] || activeId;
  }

  function renderKpis() {
    return '';
  }

  function renderTokenBudgetCard(sum) {
    const maxTok = Math.max(1, sum.per_turn_max_tokens);
    const activePct = sum.per_turn_max_tokens > 0
      ? Math.max(0, Math.min(100, Math.round((sum.per_turn_active_tokens / maxTok) * 100)))
      : 0;
    const savedPct = sum.per_turn_max_tokens > 0 ? Math.max(0, 100 - activePct) : 0;
    const sandboxSynced = state.data?.sandbox_synced !== false;
    const updatedLabel = state.data?.updated_at ? formatTime(state.data.updated_at) : '默认配置';

    return `
      <section class="card plug-budget">
        <div class="plug-budget-main">
          <div class="row-between">
            <span class="text-sm text-muted">每轮提示词附加开销</span>
            ${sum.dirty ? '<span class="badge badge-sm badge-amber">草稿预估 · 未保存</span>' : '<span class="text-xs text-muted">已热生效</span>'}
          </div>
          <div class="row wrap" style="align-items:baseline;gap:14px;">
            <span class="kpi-value" style="font-size:36px;">${sum.per_turn_active_tokens.toLocaleString()}<span class="kpi-unit">tok / 轮</span></span>
            ${sum.per_turn_saved_tokens > 0
              ? `<span class="text-success font-semibold">已省 ${sum.per_turn_saved_tokens.toLocaleString()} tok（${sum.per_turn_save_pct}%）</span>`
              : '<span class="text-sm text-muted">满配，未裁剪</span>'}
          </div>
          <div class="plug-budget-bar" role="img" aria-label="当前开销占满配 ${activePct}%，已节省 ${savedPct}%">
            <div class="plug-budget-active" style="width:${activePct}%;"></div>
            <div class="plug-budget-saved" style="width:${savedPct}%;"></div>
          </div>
          <div class="plug-budget-legend">
            <span class="row-sm"><span class="plug-legend-dot" style="background:var(--accent);"></span>当前 ${activePct}%</span>
            <span class="row-sm"><span class="plug-legend-dot" style="background:var(--success);opacity:0.45;"></span>已省 ${savedPct}%</span>
            <span>满配 ${sum.per_turn_max_tokens.toLocaleString()} tok</span>
            <span class="mono" style="margin-left:auto;">${escapeHtml(state.data?.file_path || 'data/bot_plugins.json')} · ${escapeHtml(updatedLabel)}</span>
          </div>
        </div>
        <div class="plug-budget-side">
          <div class="plug-mini">
            <span class="plug-mini-label">已启用插件</span>
            <span class="plug-mini-value">${sum.enabled_count}<small>/ ${sum.total_count}</small></span>
          </div>
          <div class="plug-mini">
            <span class="plug-mini-label">会话初始化</span>
            <span class="plug-mini-value">${sum.per_session_active_tokens.toLocaleString()}<small>tok</small></span>
          </div>
          <div class="plug-mini">
            <span class="plug-mini-label">沙盒 GEMINI.md${!sandboxSynced ? ' · <span class="text-amber">同步异常</span>' : ''}</span>
            <span class="plug-mini-value">${sum.sandbox_gemini_compiled_tokens.toLocaleString()}<small>/ ${sum.sandbox_gemini_raw_tokens.toLocaleString()} tok</small></span>
          </div>
          <div class="plug-mini">
            <span class="plug-mini-label">后台 LLM · 额外轮次</span>
            <span class="plug-mini-value">${sum.background_llm_active_count}<small>/ ${sum.background_llm_max_count}</small> · ${sum.extra_turns_active_count}<small>/ ${sum.extra_turns_max_count}</small></span>
          </div>
        </div>
      </section>
    `;
  }

  function renderPresetsCard() {
    const presets = Array.isArray(state.data?.presets) ? state.data.presets : [];
    if (!presets.length) return '';
    const activePreset = state.data?.active_preset || 'custom';
    const dirty = isDirty();
    const busy = state.saving || state.scanning || Boolean(state.applyingPreset);

    return `
      <section class="section">
        <div class="section-head">
          <div>
            <h2 class="section-title">场景预设</h2>
            <p class="section-desc">按成本与互动需求一键切换插件组合，点击即保存并热生效。</p>
          </div>
          <span class="text-sm text-muted">${dirty ? '有未保存的草稿' : `当前：${escapeHtml(resolveActivePresetLabel())}`}</span>
        </div>
        <div class="plug-preset-grid">
          ${presets.map((pr) => {
            const isActive = !dirty && activePreset === pr.id;
            const isLoading = state.applyingPreset === pr.id;
            return `
              <button type="button" class="plug-preset-card ${isActive ? 'is-active' : ''}"
                data-action="apply-preset" data-preset="${escapeHtml(pr.id)}" ${busy ? 'disabled' : ''}>
                <div class="plug-preset-head">
                  <span>${escapeHtml(pr.name || pr.id)}</span>
                  ${pr.badge ? `<span class="badge badge-sm">${escapeHtml(pr.badge)}</span>` : ''}
                </div>
                <div class="plug-preset-desc">${escapeHtml(pr.desc || '')}</div>
                <div class="plug-preset-foot">
                  <span>启用 ${escapeHtml(String(pr.enabled_count ?? '--'))} 个</span>
                  <span>${isLoading ? '应用中…' : isActive ? '<strong style="color:var(--text);">当前生效</strong>' : '切换'}</span>
                </div>
              </button>
            `;
          }).join('')}
        </div>
      </section>
    `;
  }

  function renderLoadErrorsBanner() {
    const errs = Array.isArray(state.data?.load_errors) ? state.data.load_errors : [];
    const syncErr = state.data?.sandbox_synced === false ? String(state.data?.sandbox_sync_error || '沙盒指令同步失败') : '';
    const blocks = [];

    if (errs.length > 0) {
      blocks.push(`
        <div class="alert-banner alert-danger" style="align-items:flex-start;">
          <div class="stack-xs flex-1">
            <div class="row-sm font-semibold">
              <span class="text-danger">${createIcon('alert', 15)}</span>
              <span>扫描插件目录时有 ${errs.length} 个文件加载失败（已自动跳过，不影响其余插件运行）：</span>
            </div>
            <ul style="margin:2px 0 0 20px;padding:0;font-size:12px;">
              ${errs.map((e) => `
                <li><code class="mono">${escapeHtml(e.file || 'unknown')}</code>：${escapeHtml(e.error || '未知错误')}</li>
              `).join('')}
            </ul>
          </div>
          <button type="button" class="btn btn-secondary btn-xs" data-action="rescan" ${state.scanning ? 'disabled' : ''}>
            重新扫描
          </button>
        </div>
      `);
    }

    if (syncErr) {
      blocks.push(`
        <div class="alert-banner alert-warning">
          <div class="row-sm flex-1">
            <span class="text-amber">${createIcon('alert', 15)}</span>
            <span><strong>沙盒 GEMINI.md 同步提示：</strong>${escapeHtml(syncErr)}</span>
          </div>
          <button type="button" class="btn btn-secondary btn-xs" data-action="goto-view" data-view="persona">前往人设与指令</button>
        </div>
      `);
    }

    return blocks.join('');
  }

  function renderToolbar(plugins, categories) {
    const catCounts = {};
    plugins.forEach((p) => {
      const c = p.category || 'other';
      catCounts[c] = (catCounts[c] || 0) + 1;
    });
    const highCount = plugins.filter(isHighTokenPlugin).length;
    const enabledCount = plugins.filter((p) => getDraftEnabled(p.id)).length;
    const disabledCount = plugins.length - enabledCount;
    const filtering = state.categoryFilter !== 'all' || state.quickFilter !== 'all' || state.search.trim();

    return `
      <div class="plug-toolbar">
        <div class="section-head">
          <div>
            <h2 class="section-title">全部插件 <span class="text-muted" style="font-weight:500;">${plugins.length}</span></h2>
            <p class="section-desc">拨动开关可实时预估省流幅度，确认后在底部保存即可热生效。</p>
          </div>
          <div class="search-box" style="width:260px;max-width:100%;">
            <span class="search-icon">${createIcon('search', 14)}</span>
            <input type="text" id="plug-search-input" placeholder="搜索插件名、ID 或说明" value="${escapeHtml(state.search)}" autocomplete="off" />
            ${state.search ? `<button type="button" class="search-clear" data-action="clear-search" aria-label="清空搜索">${createIcon('x', 13)}</button>` : ''}
          </div>
        </div>
        <div class="plug-toolbar-row">
          <div class="tabs" role="tablist" aria-label="按分类筛选">
            <button type="button" class="tab-btn ${state.categoryFilter === 'all' ? 'active' : ''}" data-action="cat-filter" data-val="all">全部</button>
            ${categories.map((cat) => {
              const cnt = catCounts[cat.id] || 0;
              if (cnt === 0) return '';
              return `<button type="button" class="tab-btn ${state.categoryFilter === cat.id ? 'active' : ''}" data-action="cat-filter" data-val="${escapeHtml(cat.id)}">${escapeHtml(cat.title || cat.id)}</button>`;
            }).join('')}
          </div>
          <div class="row-sm wrap">
            <div class="filter-pills" aria-label="状态筛选">
              <button type="button" class="pill-btn ${state.quickFilter === 'all' ? 'active' : ''}" data-action="quick-filter" data-val="all">全部状态</button>
              <button type="button" class="pill-btn ${state.quickFilter === 'high_token' ? 'active' : ''}" data-action="quick-filter" data-val="high_token">高耗 ${highCount}</button>
              <button type="button" class="pill-btn ${state.quickFilter === 'enabled' ? 'active' : ''}" data-action="quick-filter" data-val="enabled">已启用 ${enabledCount}</button>
              <button type="button" class="pill-btn ${state.quickFilter === 'disabled' ? 'active' : ''}" data-action="quick-filter" data-val="disabled">已关闭 ${disabledCount}</button>
            </div>
            ${filtering ? '<button type="button" class="btn btn-ghost btn-xs" data-action="reset-filters">清除筛选</button>' : ''}
          </div>
        </div>
      </div>
    `;
  }

  function renderSettingField(p, field, currentVal) {
    const pid = p.id;
    const key = field.key;
    const type = String(field.type || 'str').toLowerCase();
    const val = currentVal !== undefined ? currentVal : field.default;

    let ctrlHtml = '';
    if (type === 'bool') {
      const checked = Boolean(val);
      ctrlHtml = `
        <label class="switch switch-sm">
          <input type="checkbox"
            data-plug-setting-bool="${escapeHtml(pid)}"
            data-setting-key="${escapeHtml(key)}"
            ${checked ? 'checked' : ''} />
          <span class="switch-track"></span>
        </label>
      `;
    } else if (type === 'select') {
      const opts = Array.isArray(field.options) ? field.options : [];
      ctrlHtml = `
        <select class="select select-sm"
          data-plug-setting-input="${escapeHtml(pid)}"
          data-setting-key="${escapeHtml(key)}"
          data-setting-type="select">
          ${opts.map((opt) => {
            const oVal = typeof opt === 'object' && opt !== null ? opt.value : opt;
            const oLbl = typeof opt === 'object' && opt !== null ? (opt.label ?? opt.value) : opt;
            return `<option value="${escapeHtml(String(oVal))}" ${String(oVal) === String(val ?? '') ? 'selected' : ''}>${escapeHtml(String(oLbl))}</option>`;
          }).join('')}
        </select>
      `;
    } else if (type === 'int' || type === 'float') {
      const minAttr = field.min !== undefined && field.min !== null ? `min="${escapeHtml(String(field.min))}"` : '';
      const maxAttr = field.max !== undefined && field.max !== null ? `max="${escapeHtml(String(field.max))}"` : '';
      const stepAttr = type === 'int' ? 'step="1"' : 'step="any"';
      ctrlHtml = `
        <input type="number" class="input input-sm mono" style="width:104px;text-align:right;"
          data-plug-setting-input="${escapeHtml(pid)}"
          data-setting-key="${escapeHtml(key)}"
          data-setting-type="${type}"
          ${minAttr} ${maxAttr} ${stepAttr}
          value="${escapeHtml(val === undefined || val === null ? '' : String(val))}" />
      `;
    } else {
      ctrlHtml = `
        <input type="text" class="input input-sm" style="width:158px;"
          data-plug-setting-input="${escapeHtml(pid)}"
          data-setting-key="${escapeHtml(key)}"
          data-setting-type="str"
          value="${escapeHtml(val === undefined || val === null ? '' : String(val))}" />
      `;
    }

    return `
      <div class="plug-setting-item">
        <div class="plug-setting-meta">
          <div class="plug-setting-label">${escapeHtml(field.label || field.key)}</div>
          ${field.description ? `<div class="plug-setting-desc">${escapeHtml(field.description)}</div>` : ''}
        </div>
        <div class="plug-setting-ctrl">${ctrlHtml}</div>
      </div>
    `;
  }

  function renderPluginCard(p, pluginById, draftEffMap) {
    const draft = state.draftPlugins[p.id] || { enabled: Boolean(p.enabled), settings: {} };
    const enabled = Boolean(draft.enabled);
    const dirty = isPluginDirty(p.id);
    const focused = state.focusedPluginId === p.id;
    const impact = p.token_impact || {};
    const lvl = tokenLevelBadgeMeta(impact);
    const highCost = ['badge-danger', 'badge-warning'].includes(lvl.cls);

    const envDisabled = Array.isArray(p.env_disabled_keys) ? p.env_disabled_keys : [];
    const missingReqs = draftEffMap.missingMap[p.id] || [];
    const missingNames = missingReqs.map((rid) => pluginById[rid]?.name || rid);
    const schema = Array.isArray(p.settings_schema) ? p.settings_schema : [];
    const settingsOpen = state.expandedSettings.has(p.id);
    const rawRelatedViews = Array.isArray(p.related_views) ? p.related_views : [];
    const seenRelatedRoutes = new Set();
    const relatedViewLinks = [];
    rawRelatedViews.forEach((vid) => {
      const vm = VIEW_LINK_META[vid] || { label: vid, route: vid };
      if (!vm.route || seenRelatedRoutes.has(vm.route)) return;
      seenRelatedRoutes.add(vm.route);
      relatedViewLinks.push(vm);
    });
    const capsCount = Array.isArray(p.capabilities) ? p.capabilities.length : 0;
    const perTurnTok = Number(impact.per_turn_tokens ?? p.token_estimate?.per_turn_max ?? 0);
    const perSessTok = Number(impact.per_session_tokens ?? p.token_estimate?.per_session_max ?? 0);

    const meta = [
      perTurnTok > 0
        ? (enabled ? `<span><strong>~${perTurnTok}</strong> tok/轮</span>` : `<span class="text-success">已省 ~${perTurnTok} tok/轮</span>`)
        : '<span>0 tok/轮</span>',
      perSessTok > 0 ? `<span>~${perSessTok} tok/会话</span>` : '',
      highCost ? `<span class="warn">${escapeHtml(lvl.label)}</span>` : '',
      impact.background_llm ? '<span class="warn">后台 LLM</span>' : '',
      impact.extra_turns ? '<span class="warn">额外轮次</span>' : '',
      p.affects_sandbox_prompt ? '<span>影响沙盒指令</span>' : '',
      `<span>${p.hot_reload !== false ? '热生效' : '需重启'}</span>`,
      `<span>${p.builtin ? '内置' : '自定义扩展'}</span>`,
    ].filter(Boolean).join('');

    return `
      <article class="plug-card ${enabled ? '' : 'is-disabled'} ${dirty ? 'is-dirty' : ''} ${focused ? 'is-focused' : ''}" data-plugin-card="${escapeHtml(p.id)}">
        <div class="plug-card-body">
          <div class="plug-card-top">
            <div class="plug-card-heading">
              <div class="plug-card-title-line">
                <span class="plug-card-title">${escapeHtml(p.name || p.id)}</span>
                ${p.version ? `<span class="plug-version">v${escapeHtml(p.version)}</span>` : ''}
                <span class="badge badge-sm badge-amber" data-plug-dirty-badge="${escapeHtml(p.id)}" style="${dirty ? '' : 'display:none;'}">已修改</span>
              </div>
              <div class="plug-card-desc">${escapeHtml(p.description || '暂无说明')}</div>
            </div>
            <label class="switch" title="${enabled ? '关闭此插件' : '启用此插件'}" style="margin-top:2px;">
              <input type="checkbox" data-plug-toggle="${escapeHtml(p.id)}" ${enabled ? 'checked' : ''} />
              <span class="switch-track"></span>
            </label>
          </div>

          ${impact.summary ? `<div class="plug-impact-summary">${escapeHtml(impact.summary)}</div>` : ''}
          <div class="plug-meta">${meta}</div>

          ${envDisabled.length > 0 ? `
            <div class="plug-warn-strip">
              <div class="plug-warn-strip-main">
                <span class="text-amber">${createIcon('alert', 14)}</span>
                <span><code class="mono">${escapeHtml(envDisabled.join(', '))}=0</code> 已在 bot.env 中禁用</span>
              </div>
              <button type="button" class="btn btn-ghost btn-xs" data-action="goto-view" data-view="config">去开启</button>
            </div>
          ` : ''}

          ${missingReqs.length > 0 && enabled ? `
            <div class="plug-warn-strip">
              <div class="plug-warn-strip-main">
                <span class="text-amber">${createIcon('alert', 14)}</span>
                <span>依赖未开启：<strong>${escapeHtml(missingNames.join('、'))}</strong>，暂不会生效</span>
              </div>
              <button type="button" class="btn btn-amber btn-xs" data-action="enable-requires" data-plugin-id="${escapeHtml(p.id)}">开启依赖</button>
            </div>
          ` : ''}
        </div>

        ${schema.length > 0 ? `
          <div class="plug-settings-toggle-wrap">
            <button type="button" class="plug-settings-toggle" data-action="toggle-settings" data-plugin-id="${escapeHtml(p.id)}" aria-expanded="${settingsOpen ? 'true' : 'false'}">
              <span class="row-sm">${createIcon('sliders', 14)}<span>参数 · ${schema.length} 项</span></span>
              <span>${createIcon(settingsOpen ? 'chevronUp' : 'chevronDown', 14)}</span>
            </button>
            ${settingsOpen ? `
              <div class="plug-settings-panel">
                ${schema.map((field) => renderSettingField(p, field, draft.settings?.[field.key])).join('')}
              </div>
            ` : ''}
          </div>
        ` : ''}

        <div class="plug-card-footer">
          <div class="row-sm" style="min-width:0;">
            <code class="plug-source" title="${escapeHtml(p.source_file || `plugins/${p.id}.py`)}">${escapeHtml(p.source_file || `plugins/${p.id}.py`)}</code>
            ${capsCount > 0 ? `<span class="text-xs text-muted">· ${capsCount} 项权限</span>` : ''}
          </div>
          ${relatedViewLinks.length > 0 ? `
            <div class="plug-links">
              ${relatedViewLinks.map((vm) => `<button type="button" class="plug-link-btn" data-action="goto-view" data-view="${escapeHtml(vm.route)}">${escapeHtml(vm.label)}${createIcon('arrowRight', 12)}</button>`).join('')}
            </div>
          ` : ''}
        </div>
      </article>
    `;
  }

  function renderPluginGroups(plugins, categories) {
    const q = state.search.trim().toLowerCase();
    const pluginById = Object.fromEntries(plugins.map((p) => [p.id, p]));
    const draftEffMap = computeDraftEffectiveMap();

    const filtered = plugins.filter((p) => {
      if (state.categoryFilter !== 'all' && p.category !== state.categoryFilter) return false;
      const enabled = getDraftEnabled(p.id);
      if (state.quickFilter === 'high_token' && !isHighTokenPlugin(p)) return false;
      if (state.quickFilter === 'enabled' && !enabled) return false;
      if (state.quickFilter === 'disabled' && enabled) return false;
      if (!q) return true;
      const haystack = [
        p.id,
        p.name,
        p.description,
        p.author,
        p.source_file,
        p.token_impact?.summary,
        ...(Array.isArray(p.capabilities) ? p.capabilities : []),
      ]
        .filter(Boolean)
        .join(' ')
        .toLowerCase();
      return haystack.includes(q);
    });

    if (filtered.length === 0) {
      return `
        <section class="card">
          <div class="empty-state">
            <div class="empty-state-icon">${createIcon('puzzle', 20)}</div>
            <div class="empty-state-title">没有匹配当前筛选条件的插件</div>
            <div class="text-sm text-muted">可以清空搜索关键词或切换全部分类查看所有插件。</div>
            <button type="button" class="btn btn-secondary btn-sm mt-8" data-action="reset-filters">
              清除筛选条件
            </button>
          </div>
        </section>
      `;
    }

    const sortedCats = [...categories].sort((a, b) => Number(a.order ?? 99) - Number(b.order ?? 99));
    const knownCatIds = new Set(sortedCats.map((c) => c.id));
    const extraPlugins = filtered.filter((p) => !knownCatIds.has(p.category));

    const sections = sortedCats
      .map((cat) => {
        const list = filtered
          .filter((p) => p.category === cat.id)
          .sort((a, b) => Number(a.order ?? 99) - Number(b.order ?? 99));
        if (list.length === 0) return '';
        const enabledInCat = list.filter((p) => getDraftEnabled(p.id)).length;
        return `
          <section class="plug-cat-section">
            <div class="plug-cat-head">
              <div>
                <div class="plug-cat-title"><span class="plug-cat-icon">${createIcon(cat.icon || 'puzzle', 16)}</span>${escapeHtml(cat.title || cat.id)}</div>
                ${cat.desc ? `<div class="plug-cat-desc">${escapeHtml(cat.desc)}</div>` : ''}
              </div>
              <span class="text-sm text-muted tabular">${enabledInCat} / ${list.length} 已启用</span>
            </div>
            <div class="plug-grid">
              ${list.map((p) => renderPluginCard(p, pluginById, draftEffMap)).join('')}
            </div>
          </section>
        `;
      })
      .filter(Boolean);

    if (extraPlugins.length > 0) {
      const enabledExtra = extraPlugins.filter((p) => getDraftEnabled(p.id)).length;
      sections.push(`
        <section class="plug-cat-section">
          <div class="plug-cat-head">
            <div>
              <div class="plug-cat-title"><span class="plug-cat-icon">${createIcon('puzzle', 16)}</span>其他自定义扩展</div>
              <div class="plug-cat-desc">扫描到的自定义分类插件</div>
            </div>
            <span class="text-sm text-muted tabular">${enabledExtra} / ${extraPlugins.length} 已启用</span>
          </div>
          <div class="plug-grid">
            ${extraPlugins.map((p) => renderPluginCard(p, pluginById, draftEffMap)).join('')}
          </div>
        </section>
      `);
    }

    return sections.join('');
  }

  function renderExtendGuideCard() {
    const dir = state.data?.plugins_dir || 'plugins';
    return `
      <details class="card accordion-card">
        <summary>
          <div>
            <div class="font-semibold">如何添加新插件</div>
            <div class="text-xs text-muted">在 <span class="mono">${escapeHtml(dir)}/</span> 目录放入 .py 文件，重新扫描即可，无需重启</div>
          </div>
          <span class="accordion-chevron">${createIcon('chevronDown', 15)}</span>
        </summary>
        <div class="card-body">
          <ol class="plug-guide-list">
            <li>
              在项目 <code>${escapeHtml(dir)}/</code> 目录下新建一个 <code>.py</code> 文件（推荐直接复制
              <code>${escapeHtml(dir)}/_template.py.example</code> 作为起点，完整规范见 <code>${escapeHtml(dir)}/README.md</code>）。
            </li>
            <li>
              在模块中声明插件元信息（ID、名称、分类、预估 Token 影响、可选的 <code>settings_schema</code> 参数定义以及受控能力列表）。
            </li>
            <li>
              保存文件后点击页面右上角的<strong>「重新扫描」</strong>即可立即出现在此列表中，无需修改主程序代码或重启服务。
            </li>
            <li>
              所有开关与参数微调均持久化至 <code>${escapeHtml(state.data?.file_path || 'data/bot_plugins.json')}</code>，保存后 wrapper 与 ws-guard 秒级热重载；涉及沙盒指令裁剪的插件会在同步 <code>chat-sandbox/GEMINI.md</code> 后于新会话自动生效。
            </li>
          </ol>
        </div>
      </details>
    `;
  }

  function renderStickyBar(sum) {
    const changedIds = getChangedPluginIds();
    const visible = changedIds.length > 0;
    const delta = sum.saved_delta_per_turn || 0;
    const deltaHtml =
      delta > 0
        ? `<span class="text-xs text-success">每轮再省 ${delta} tok</span>`
        : delta < 0
          ? `<span class="text-xs text-amber">每轮增加 ${Math.abs(delta)} tok</span>`
          : '';

    return `
      <div id="plug-sticky-wrap" style="display:${visible ? 'block' : 'none'};">
        <div class="sticky-action-bar">
          <div class="row-between wrap">
            <div class="row-sm wrap">
              <span class="dirty-dot"></span>
              <span class="font-semibold text-sm" id="plug-sticky-count">已修改 ${changedIds.length} 个插件</span>
              <span id="plug-sticky-delta">${deltaHtml}</span>
              <span class="text-xs text-muted">保存后立即热生效，无需重启</span>
            </div>
            <div class="row-sm">
              <button type="button" class="btn btn-ghost btn-sm" data-action="discard-drafts" ${state.saving ? 'disabled' : ''}>放弃</button>
              <button type="button" class="btn btn-primary btn-sm" data-action="save-drafts" ${state.saving ? 'disabled' : ''}>
                ${state.saving ? '保存中…' : `保存并生效 <span style="opacity:0.6;">${saveShortcutLabel()}</span>`}
              </button>
            </div>
          </div>
        </div>
      </div>
    `;
  }

  function syncDirtyUiInPlace(changedPid) {
    const changedIds = getChangedPluginIds();
    if (changedPid) {
      const dirty = isPluginDirty(changedPid);
      const card = container.querySelector(`[data-plugin-card="${CSS.escape(changedPid)}"]`);
      const badge = container.querySelector(`[data-plug-dirty-badge="${CSS.escape(changedPid)}"]`);
      if (card) card.classList.toggle('is-dirty', dirty);
      if (badge) badge.style.display = dirty ? '' : 'none';
    }

    const wrap = container.querySelector('#plug-sticky-wrap');
    if (wrap) wrap.style.display = changedIds.length > 0 ? 'block' : 'none';
    const countEl = container.querySelector('#plug-sticky-count');
    if (countEl) countEl.textContent = `已修改 ${changedIds.length} 个插件`;
  }

  function render() {
    if (destroyed) return;
    const plugins = Array.isArray(state.data?.plugins) ? state.data.plugins : [];
    const categories = Array.isArray(state.data?.categories) ? state.data.categories : [];
    const sum = state.data ? computeLiveSummary() : null;
    const busy = state.saving || state.scanning || Boolean(state.applyingPreset);

    const actionsEl = ctx.setActions(`
      <button type="button" class="btn btn-ghost" data-action="reset-defaults" ${busy ? 'disabled' : ''}>${createIcon('rotateCcw', 15)}恢复默认</button>
      <button type="button" class="btn btn-secondary" data-action="rescan" ${busy ? 'disabled' : ''}>
        <span class="${state.scanning ? 'spin' : ''}">${createIcon('refresh', 15)}</span>${state.scanning ? '扫描中…' : '重新扫描'}
      </button>
    `);
    actionsEl?.querySelectorAll('[data-action]').forEach((el) => {
      el.addEventListener('click', () => handleActionClick(el));
    });

    container.innerHTML = `
      <div class="stack-lg">
        ${state.loading ? `
          <div class="stack">
            <div class="loading-skeleton" style="height:150px;"></div>
            <div class="loading-skeleton" style="height:140px;"></div>
            <div class="loading-skeleton" style="height:260px;"></div>
          </div>
        ` : state.error ? `
          <div class="alert-banner alert-danger">
            <div class="row-sm">
              <span class="text-danger">${createIcon('alert', 16)}</span>
              <span>${escapeHtml(state.error)}</span>
            </div>
            <button type="button" class="btn btn-secondary btn-sm" data-action="retry-load">重试</button>
          </div>
        ` : `
          ${renderLoadErrorsBanner()}
          ${renderTokenBudgetCard(sum)}
          ${renderPresetsCard()}
          <section class="section" style="gap:28px;">
            ${renderToolbar(plugins, categories)}
            ${renderPluginGroups(plugins, categories)}
          </section>
          ${renderExtendGuideCard()}
          ${renderStickyBar(sum)}
        `}
      </div>
    `;

    bindEvents();
  }

  async function handleActionClick(el) {
        const action = el.dataset.action;
        if (action === 'retry-load') {
          await loadPlugins({ refresh: true });
        } else if (action === 'rescan') {
          await handleRescan();
        } else if (action === 'reset-defaults') {
          await handleResetDefaults();
        } else if (action === 'apply-preset') {
          const preset = (state.data?.presets || []).find((pr) => pr.id === el.dataset.preset);
          if (preset) await handleApplyPreset(preset);
        } else if (action === 'cat-filter') {
          state.categoryFilter = el.dataset.val || 'all';
          render();
        } else if (action === 'quick-filter') {
          state.quickFilter = el.dataset.val || 'all';
          render();
        } else if (action === 'clear-search') {
          state.search = '';
          render();
          container.querySelector('#plug-search-input')?.focus();
        } else if (action === 'reset-filters') {
          state.categoryFilter = 'all';
          state.quickFilter = 'all';
          state.search = '';
          render();
        } else if (action === 'toggle-settings') {
          const pid = el.dataset.pluginId;
          if (!pid) return;
          if (state.expandedSettings.has(pid)) state.expandedSettings.delete(pid);
          else state.expandedSettings.add(pid);
          render();
        } else if (action === 'enable-requires') {
          const pid = el.dataset.pluginId;
          const plugins = Array.isArray(state.data?.plugins) ? state.data.plugins : [];
          const byId = Object.fromEntries(plugins.map((p) => [p.id, p]));
          const visited = new Set();
          const enableRecursive = (id) => {
            if (!id || visited.has(id)) return;
            visited.add(id);
            const target = byId[id];
            if (!target) return;
            if (!state.draftPlugins[id]) {
              state.draftPlugins[id] = { enabled: true, settings: deepClone(target.settings || {}) };
            } else {
              state.draftPlugins[id].enabled = true;
            }
            (target.requires || []).forEach(enableRecursive);
          };
          const self = byId[pid];
          (self?.requires || []).forEach(enableRecursive);
          showToast('已在草稿中开启所需依赖插件，记得点击底部保存', 'info');
          render();
        } else if (action === 'goto-view') {
          const view = el.dataset.view;
          if (view) ctx?.navigate?.(view);
        } else if (action === 'discard-drafts') {
          state.draftPlugins = deepClone(state.serverPlugins);
          showToast('已放弃未保存的插件修改', 'info');
          render();
        } else if (action === 'save-drafts') {
          await saveDrafts();
        }
  }

  function bindEvents() {
    container.querySelectorAll('[data-action]').forEach((el) => {
      el.addEventListener('click', () => handleActionClick(el));
    });

    // Main plugin enable/disable switches (re-render to update KPIs & token budget live)
    container.querySelectorAll('[data-plug-toggle]').forEach((cb) => {
      cb.addEventListener('change', () => {
        const pid = cb.dataset.plugToggle;
        if (!pid) return;
        const cur = state.draftPlugins[pid] || { enabled: false, settings: {} };
        cur.enabled = Boolean(cb.checked);
        state.draftPlugins[pid] = cur;
        render();
      });
    });

    // Plugin settings boolean switches
    container.querySelectorAll('[data-plug-setting-bool]').forEach((cb) => {
      cb.addEventListener('change', () => {
        const pid = cb.dataset.plugSettingBool;
        const key = cb.dataset.settingKey;
        if (!pid || !key) return;
        const cur = state.draftPlugins[pid] || { enabled: getDraftEnabled(pid), settings: {} };
        cur.settings = { ...(cur.settings || {}), [key]: Boolean(cb.checked) };
        state.draftPlugins[pid] = cur;
        syncDirtyUiInPlace(pid);
      });
    });

    // Plugin settings text / number / select inputs (update in-place to preserve typing focus!)
    container.querySelectorAll('[data-plug-setting-input]').forEach((inp) => {
      const pid = inp.dataset.plugSettingInput;
      const key = inp.dataset.settingKey;
      const type = inp.dataset.settingType || 'str';
      const evt = inp.tagName === 'SELECT' ? 'change' : 'input';

      inp.addEventListener(evt, () => {
        if (!pid || !key) return;
        const cur = state.draftPlugins[pid] || { enabled: getDraftEnabled(pid), settings: {} };
        let parsed = inp.value;
        if (type === 'int') {
          if (inp.value === '' || inp.value === '-') return;
          const n = Number(inp.value);
          if (!Number.isFinite(n)) return;
          parsed = Math.round(n);
        } else if (type === 'float') {
          if (inp.value === '' || inp.value === '-' || inp.value.endsWith('.')) return;
          const n = Number(inp.value);
          if (!Number.isFinite(n)) return;
          parsed = n;
        }
        cur.settings = { ...(cur.settings || {}), [key]: parsed };
        state.draftPlugins[pid] = cur;
        syncDirtyUiInPlace(pid);
      });
    });

    // Search input (preserve focus & selection range cleanly across re-renders)
    const searchInp = container.querySelector('#plug-search-input');
    if (searchInp) {
      searchInp.addEventListener('input', () => {
        const start = searchInp.selectionStart;
        const end = searchInp.selectionEnd;
        state.search = searchInp.value;
        render();
        const next = container.querySelector('#plug-search-input');
        if (next) {
          next.focus();
          try {
            next.setSelectionRange(start, end);
          } catch (_) {}
        }
      });
    }
  }

  const onKeyDown = (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 's') {
      if (isDirty() && !state.saving) {
        e.preventDefault();
        saveDrafts();
      }
    }
  };
  document.addEventListener('keydown', onKeyDown);

  await loadPlugins({ refresh: false });

  return {
    refresh: () => loadPlugins({ refresh: true }),
    cleanup: () => {
      destroyed = true;
      if (focusTimer) clearTimeout(focusTimer);
      document.removeEventListener('keydown', onKeyDown);
    },
  };
}
