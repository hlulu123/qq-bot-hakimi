import {
  apiDelete,
  apiGet,
  apiPost,
  apiPut,
  confirmModal,
  copyText,
  createIcon,
  escapeHtml,
  saveShortcutLabel,
  showToast,
} from '../api.js';

const TOML_SPECS = [
  {
    key: 'require_mention',
    source: 'toml',
    tab: 'listening',
    label: '严格仅 @ 点名模式',
    type: 'boolean',
    restart: 'full',
    desc: '开启后群聊只响应显式 @机器人，忽略被动潜水和普通叫名字。',
  },
  {
    key: 'display_thinking_messages',
    source: 'toml',
    tab: 'style',
    label: '显示思考过程消息',
    type: 'boolean',
    restart: 'full',
    desc: '是否将模型的思考过程输出到聊天窗口（建议关闭以保持自然聊天）。',
  },
  {
    key: 'display_tool_messages',
    source: 'toml',
    tab: 'style',
    label: '显示工具调用提示',
    type: 'boolean',
    restart: 'full',
    desc: '是否向 QQ 输出工具调用状态提示（建议关闭）。',
  },
  {
    key: 'display_reply_footer',
    source: 'toml',
    tab: 'style',
    label: '显示回复底部脚注',
    type: 'boolean',
    restart: 'full',
    desc: '是否在每条回复底部附加模型/耗时脚注（建议关闭）。',
  },
  {
    key: 'auto_compress_enabled',
    source: 'toml',
    tab: 'memory_cfg',
    label: '会话上下文自动压缩',
    type: 'boolean',
    restart: 'full',
    desc: '对话 token 接近阈值时自动总结旧上下文，避免超限报错。',
  },
  {
    key: 'auto_compress_max_tokens',
    source: 'toml',
    tab: 'memory_cfg',
    label: '自动压缩触发阈值',
    type: 'number',
    min: 8000,
    max: 1000000,
    step: 1000,
    unit: 'tokens',
    restart: 'full',
    desc: '估算会话上下文达到此 token 数时触发自动压缩。',
  },
  {
    key: 'auto_compress_min_gap_mins',
    source: 'toml',
    tab: 'memory_cfg',
    label: '自动压缩最小间隔',
    type: 'number',
    min: 1,
    max: 1440,
    step: 1,
    unit: '分',
    restart: 'full',
    desc: '同一会话两次自动压缩之间至少间隔多少分钟。',
  },
  {
    key: 'auto_compress_hard_max_tokens',
    source: 'toml',
    tab: 'memory_cfg',
    label: '自动压缩硬上限',
    type: 'number',
    min: 0,
    max: 2000000,
    step: 1000,
    unit: 'tokens',
    restart: 'full',
    desc: '上下文超过此值时不必等满最小间隔就再次压缩（0 = 关闭），防止高活跃群在间隔期内滚雪球。',
  },
  {
    key: 'auto_compress_hard_min_gap_mins',
    source: 'toml',
    tab: 'memory_cfg',
    label: '硬上限压缩间隔',
    type: 'number',
    min: 1,
    max: 1440,
    step: 1,
    unit: '分',
    restart: 'full',
    desc: '超过硬上限时，两次压缩之间至少间隔多少分钟（防止反复压缩）。',
  },
  {
    key: 'agent_session_idle_timeout_mins',
    source: 'toml',
    tab: 'memory_cfg',
    label: 'ACP 进程空闲回收',
    type: 'number',
    min: 1,
    max: 1440,
    step: 1,
    unit: '分',
    restart: 'full',
    desc: '空闲超过此时长自动释放底层 ACP 子进程（下次聊天自动重建）。',
  },
  {
    key: 'reset_on_idle_mins',
    source: 'toml',
    tab: 'memory_cfg',
    label: '长时静默重置上下文',
    type: 'number',
    min: 0,
    max: 10080,
    step: 1,
    unit: '分',
    restart: 'full',
    desc: '群聊或私聊长时间无消息后开启全新对话上下文（0 = 关闭自动重置）。',
  },
];

const THOUGHT_KEYS = new Set([
  'AGY_LURK_THOUGHT_LEVEL',
  'AGY_THOUGHT_LEVEL',
  'AGY_TASK_THOUGHT_LEVEL',
  'MEMORY_EFFORT',
]);

const THOUGHT_LABELS = {
  minimal: '极简',
  low: '轻度',
  medium: '均衡',
  high: '较深',
  xhigh: '深度',
};

const ENV_TAB_MAP = {
  // Listening & pacing
  GUARD_AMBIENT_LISTEN: 'listening',
  GUARD_AMBIENT_GAP_SECS: 'listening',
  AMBIENT_MAX_PER_WINDOW: 'listening',
  AMBIENT_WINDOW_SECS: 'listening',
  AMBIENT_MIN_GAP_SECS: 'listening',
  ACTIVE_CONV_WINDOW_SECS: 'listening',
  ACTIVE_CONV_MIN_GAP_SECS: 'listening',
  ACTIVE_CONV_MAX_PER_WINDOW: 'listening',
  GROUP_HISTORY_COUNT: 'listening',
  // Reasoning & models
  AGY_LURK_THOUGHT_LEVEL: 'reasoning',
  AGY_THOUGHT_LEVEL: 'reasoning',
  AGY_TASK_THOUGHT_LEVEL: 'reasoning',
  MEMORY_EFFORT: 'reasoning',
  MEMORY_MODEL: 'reasoning',
  // Repeat & forward
  REPEAT_ENABLED: 'repeat',
  REPEAT_PHOTOS: 'repeat',
  REPEAT_MIN_COUNT: 'repeat',
  REPEAT_WINDOW_SECS: 'repeat',
  REPEAT_COOLDOWN_SECS: 'repeat',
  REPEAT_MAX_CHARS: 'repeat',
  REPEAT_BLOCK_WORDS: 'repeat',
  FORWARD_LONG_MSG_GROUP: 'repeat',
  FORWARD_LONG_MSG_DM: 'repeat',
  FORWARD_MIN_CHARS: 'repeat',
  FORWARD_MIN_LINES: 'repeat',
  FORWARD_NODE_MAX_CHARS: 'repeat',
  FORWARD_NICKNAME: 'repeat',
  // Style & display
  BOT_CALL_NAMES: 'style',
  BOT_SOFT_CALL_CUES: 'style',
  BOT_OVERUSED_MOTIFS: 'style',
  BOT_OVERUSED_LOOKBACK: 'style',
  BOT_CASUAL_MAX_CHARS: 'style',
  BOT_FILLER_OPENER_LOOKBACK: 'style',
  CC_SILENT_QUEUE_NOTICE: 'style',
  CC_SILENT_AUTO_COMPRESS: 'style',
  CC_SILENT_IDLE_RESET: 'style',
  ADMIN_DM_TURBO: 'style',
};

const MEMORY_COMMON_KEYS = new Set([
  'USER_MEMORY_ENABLED',
  'MEMORY_GROUP_MEMORY_ENABLED',
  'MEMORY_SLEEP_ENABLED',
  'CHAT_HISTORY_ENABLED',
]);

const TABS = [
  { id: 'presets', label: '场景预设', icon: 'layers' },
  { id: 'listening', label: '监听与冒泡', icon: 'activity' },
  { id: 'reasoning', label: '模型与思考', icon: 'cpu' },
  { id: 'repeat', label: '复读与转发', icon: 'repeat' },
  { id: 'style', label: '口癖与显示', icon: 'chat' },
  { id: 'memory_cfg', label: '记忆与会话', icon: 'brain' },
  { id: 'system', label: '基础与连接', icon: 'server' },
];

const REPEAT_QUICK_SAMPLES = [
  '666',
  '哈基米出来玩',
  'https://example.com',
  '13800138000',
  '笑死我了',
  '/help',
  '确实',
  '猫猫今天吃啥',
];

function toBool(v) {
  if (typeof v === 'boolean') return v;
  return ['1', 'true', 'yes', 'on'].includes(String(v ?? '').trim().toLowerCase());
}

function splitTags(val) {
  return String(val ?? '')
    .split(/[,，\n]/)
    .map((s) => s.trim())
    .filter(Boolean);
}

function valuesEqual(a, b, type) {
  if (type === 'boolean') return toBool(a) === toBool(b);
  if (type === 'number') {
    const sa = String(a ?? '').trim();
    const sb = String(b ?? '').trim();
    if (sa !== '' && sb !== '' && !Number.isNaN(Number(sa)) && !Number.isNaN(Number(sb))) {
      return Number(sa) === Number(sb);
    }
    return sa === sb;
  }
  if (type === 'tags') {
    return splitTags(a).join(',') === splitTags(b).join(',');
  }
  return String(a ?? '').trim() === String(b ?? '').trim();
}

function numOr(val, fallback) {
  if (val === undefined || val === null) return fallback;
  const s = String(val).trim();
  if (s === '') return fallback;
  const n = Number(s);
  return Number.isFinite(n) ? n : fallback;
}

const FULL_RESTART_GUARD_KEYS = new Set([
  'GUARD_UDP_PORT',
  'GUARD_LISTEN_PORT',
]);

function isWsGuardOnlyKey(key) {
  if (FULL_RESTART_GUARD_KEYS.has(key)) return false;
  return key.startsWith('GUARD_') || key.startsWith('FORWARD_') || key.startsWith('GROUP_WHITELIST_');
}

export async function mount(container, ctx) {
  const validTabIds = new Set(TABS.map((t) => t.id));
  let activeTab = validTabIds.has(ctx.params?.tab) ? ctx.params.tab : 'presets';
  let searchQuery = '';

  let wbData = null;
  let cfgData = null;
  let allFields = []; // unified list of { key, source:'env'|'toml', tab, advanced?, ...schema }
  let fieldMap = new Map();

  let baseEnv = {};
  let baseToml = {};
  let draftEnv = {};
  let draftToml = {};
  let selectedPresetId = '';
  let showPresetForm = false;
  let showDiffDrawer = false;
  let revealedSecrets = new Set();
  let lastPositiveVals = new Map();
  let saving = false;

  function isZeroUnlimited(field, val) {
    if (!field?.zero_label) return false;
    const s = String(val ?? '').trim();
    return s !== '' && Number(s) === 0;
  }

  function syncZeroControlUi(body, fid, field, rawVal) {
    if (!field?.zero_label) return;
    const s = String(rawVal ?? '').trim();
    const num = s === '' ? NaN : Number(s);
    const zeroOn = Number.isFinite(num) && num === 0;
    if (Number.isFinite(num) && num > 0) {
      lastPositiveVals.set(fid, num);
    }
    const wrap = body.querySelector(`[data-zero-wrap="${CSS.escape(fid)}"]`);
    if (wrap) wrap.classList.toggle('is-unlimited', zeroOn);
    const btn = body.querySelector(`[data-zero-toggle="${CSS.escape(fid)}"]`);
    if (btn) {
      btn.classList.toggle('active', zeroOn);
      btn.setAttribute('aria-pressed', zeroOn ? 'true' : 'false');
    }
  }

  // Repeat tester state
  let repeatInput = '';
  let repeatResult = null;
  let repeatHistory = [];

  let destroyed = false;

  // Build static top shell so search box NEVER loses IME focus
  container.innerHTML = `
    <div class="settings-layout">
      <aside class="settings-nav">
        <div class="search-box">
          <span class="search-icon">${createIcon('search', 14)}</span>
          <input type="text" id="set-search-input" placeholder="搜索全部参数" autocomplete="off" />
          <button type="button" class="search-clear" id="set-search-clear" style="display:none;" aria-label="清除搜索">${createIcon('x', 12)}</button>
        </div>
        <nav class="vnav" id="set-tabs">
          ${TABS.map((t) => `
            <button type="button" class="vnav-item ${t.id === activeTab ? 'active' : ''}" data-tab="${t.id}">
              ${createIcon(t.icon, 16)}
              <span>${escapeHtml(t.label)}</span>
              <span class="dirty-dot" data-tab-dot="${t.id}" style="display:none;" title="有未保存的改动"></span>
            </button>
          `).join('')}
        </nav>
      </aside>

      <div class="settings-main">
        <div id="set-body">
          <div class="loading-skeleton" style="height:260px;"></div>
        </div>
        <div id="set-sticky-root"></div>
      </div>
    </div>
  `;

  function buildFieldCatalog() {
    const list = [];
    const schemaGroups = Array.isArray(cfgData?.schema) ? cfgData.schema : [];

    for (const grp of schemaGroups) {
      const gid = grp.id || grp.group || '';
      const items = grp.fields || grp.items || [];
      for (const it of items) {
        let tab = ENV_TAB_MAP[it.key];
        let advanced = false;
        if (!tab) {
          if (gid === 'memory') {
            tab = 'memory_cfg';
            advanced = !MEMORY_COMMON_KEYS.has(it.key);
          } else if (gid === 'identity' || gid === 'connection') {
            tab = 'system';
          } else if (gid === 'persona_cues') {
            tab = 'style';
          } else if (gid === 'repeat' || gid === 'forward') {
            tab = 'repeat';
          } else if (gid === 'pacing') {
            tab = 'listening';
          } else {
            tab = 'system';
          }
        }
        const zeroFallback = it.key === 'ACTIVE_CONV_MAX_PER_WINDOW'
          ? { zero_label: '无限制', restore_default: 4 }
          : null;
        list.push({
          ...(zeroFallback || {}),
          ...it,
          source: 'env',
          tab,
          schemaGroup: gid,
          advanced,
        });
      }
    }

    for (const ts of TOML_SPECS) {
      list.push({ ...ts, schemaGroup: 'toml', advanced: false });
    }

    allFields = list;
    fieldMap = new Map(list.map((f) => [`${f.source}:${f.key}`, f]));
  }

  function getVal(field, fromBase = false) {
    if (field.source === 'toml') {
      return (fromBase ? baseToml : draftToml)[field.key];
    }
    return (fromBase ? baseEnv : draftEnv)[field.key];
  }

  function setVal(field, val) {
    if (field.source === 'toml') {
      draftToml[field.key] = val;
    } else {
      draftEnv[field.key] = val;
    }
    selectedPresetId = '';
  }

  function computeDiff() {
    const envChanged = {};
    const tomlChanged = {};
    const items = [];
    for (const f of allFields) {
      const base = getVal(f, true);
      const cur = getVal(f, false);
      if (!valuesEqual(base, cur, f.type)) {
        if (f.source === 'toml') tomlChanged[f.key] = cur;
        else envChanged[f.key] = cur;
        items.push({ field: f, from: base, to: cur });
      }
    }
    const envKeys = Object.keys(envChanged);
    const tomlKeys = Object.keys(tomlChanged);
    let scope = 'none';
    if (tomlKeys.length > 0) scope = 'full';
    else if (envKeys.length > 0) {
      scope = envKeys.every(isWsGuardOnlyKey) ? 'ws-guard' : 'full';
    }
    return { envChanged, tomlChanged, items, total: items.length, scope };
  }

  function updateTabDirtyDots() {
    const diff = computeDiff();
    const dirtyTabs = new Set(diff.items.map((x) => x.field.tab));
    for (const t of TABS) {
      const dot = container.querySelector(`[data-tab-dot="${t.id}"]`);
      if (dot) dot.style.display = dirtyTabs.has(t.id) ? 'inline-block' : 'none';
    }
    refreshPacingSummary();
    renderStickyBar(diff);
  }

  function formatDisplayVal(field, val) {
    if (field.type === 'boolean') return toBool(val) ? '开' : '关';
    if (field.type === 'secret') return '••••••';
    const s = String(val ?? '').trim();
    if (field.type === 'number' && field.zero_label && s !== '' && Number(s) === 0) {
      return field.zero_label;
    }
    if (THOUGHT_KEYS.has(field.key) && THOUGHT_LABELS[s]) return THOUGHT_LABELS[s];
    return field.unit && s ? `${s}${field.unit}` : (s || '(空)');
  }

  function renderStickyBar(diff = computeDiff()) {
    const root = container.querySelector('#set-sticky-root');
    if (!root) return;
    if (diff.total === 0) {
      root.innerHTML = '';
      showDiffDrawer = false;
      return;
    }

    const isWsOnly = diff.scope === 'ws-guard';
    const primaryLabel = isWsOnly ? '保存并重启 ws-guard' : '保存并重启机器人';

    root.innerHTML = `
      <div class="sticky-action-bar">
        <div class="row-between wrap">
          <div class="row-sm wrap">
            <span class="dirty-dot"></span>
            <span class="font-semibold text-sm">${diff.total} 项改动未保存</span>
            <span class="text-xs text-muted">${isWsOnly ? '保存后无感重启 ws-guard 即可生效' : '需要重启机器人才能完全生效'}</span>
            <button type="button" class="link-btn text-xs" id="set-toggle-diff">${showDiffDrawer ? '收起' : '查看改动'}</button>
          </div>
          <div class="row-sm wrap">
            <button type="button" class="btn btn-ghost btn-sm" id="set-revert-btn" ${saving ? 'disabled' : ''}>撤销</button>
            <button type="button" class="btn btn-secondary btn-sm" id="set-save-only-btn" ${saving ? 'disabled' : ''}>仅保存 <span class="text-muted">${saveShortcutLabel()}</span></button>
            <button type="button" class="btn btn-primary btn-sm" id="set-save-apply-btn" ${saving ? 'disabled' : ''}>${escapeHtml(primaryLabel)}</button>
          </div>
        </div>
        ${showDiffDrawer ? `
          <div class="sticky-diff-panel">
            ${diff.items.map(({ field, to }) => `
              <span class="badge">${escapeHtml(field.label)} → <strong style="color:var(--text);">${escapeHtml(formatDisplayVal(field, to))}</strong></span>
            `).join('')}
          </div>
        ` : ''}
      </div>
    `;

    root.querySelector('#set-toggle-diff')?.addEventListener('click', () => {
      showDiffDrawer = !showDiffDrawer;
      renderStickyBar();
    });
    root.querySelector('#set-revert-btn')?.addEventListener('click', () => {
      draftEnv = { ...baseEnv };
      draftToml = { ...baseToml };
      selectedPresetId = '';
      renderBody();
      updateTabDirtyDots();
    });
    root.querySelector('#set-save-only-btn')?.addEventListener('click', () => saveChanges('none'));
    root.querySelector('#set-save-apply-btn')?.addEventListener('click', () => {
      saveChanges(isWsOnly ? 'ws-guard' : 'restart-bot');
    });
  }

  async function saveChanges(applyRestart = 'none', explicitPayload = null) {
    if (saving) return;
    const diff = explicitPayload ? null : computeDiff();
    const envUpdates = explicitPayload ? explicitPayload.env : diff.envChanged;
    const tomlUpdates = explicitPayload ? explicitPayload.toml : diff.tomlChanged;
    const totalCount = Object.keys(envUpdates).length + Object.keys(tomlUpdates).length;
    if (totalCount === 0) {
      showToast('当前没有待保存的改动', 'info');
      return;
    }

    if (applyRestart === 'restart-bot') {
      const ok = await confirmModal({
        title: '保存配置并平滑重启机器人？',
        message: `将写入 ${totalCount} 项配置并自动平滑重启机器人以完整生效，期间会有数秒短暂中断。`,
        confirmText: '保存并重启',
      });
      if (!ok || destroyed) return;
    }

    saving = true;
    renderStickyBar();
    try {
      const res = await apiPut('/api/workbench', {
        env_updates: envUpdates,
        toml_updates: tomlUpdates,
        apply_restart: applyRestart,
      });
      if (destroyed) return;
      const ctrl = res?.control_result;
      if (ctrl && ctrl.ok === false) {
        showToast(`配置已保存，但重启失败: ${ctrl.error || ctrl.message}`, 'warning');
      } else if (applyRestart === 'ws-guard') {
        showToast(`已保存 ${totalCount} 项配置并无感重启 ws-guard`, 'success');
      } else if (applyRestart === 'restart-bot') {
        showToast(`已保存 ${totalCount} 项配置并平滑重启机器人`, 'success');
      } else {
        showToast(`已保存 ${totalCount} 项配置到磁盘`, 'success');
      }
      await loadAll({ keepTab: true, wbOverride: res?.state || null });
      ctx.refreshGlobalStatus?.({ silent: true });
    } catch (err) {
      if (!destroyed) showToast(`保存失败: ${err.message}`, 'error');
    } finally {
      saving = false;
      if (!destroyed) updateTabDirtyDots();
    }
  }

  function renderFieldRow(field) {
    const fid = `${field.source}:${field.key}`;
    const val = getVal(field, false);
    const dirty = !valuesEqual(getVal(field, true), val, field.type);
    const isStack = field.type === 'tags' || field.key === 'MEMORY_MODEL';

    let controlHtml = '';
    if (field.type === 'boolean') {
      const checked = toBool(val);
      controlHtml = `
        <label class="switch">
          <input type="checkbox" data-fid="${escapeHtml(fid)}" ${checked ? 'checked' : ''} />
          <span class="switch-track"></span>
        </label>
      `;
    } else if (THOUGHT_KEYS.has(field.key)) {
      const opts = Array.isArray(wbData?.thought_options) && wbData.thought_options.length
        ? wbData.thought_options
        : (field.options || ['minimal', 'low', 'medium', 'high', 'xhigh']);
      const cur = String(val || 'medium');
      controlHtml = `
        <div class="segment-group" data-seg-fid="${escapeHtml(fid)}">
          ${opts.map((o) => `
            <button type="button" class="segment-btn ${cur === o ? 'active' : ''}" data-seg-val="${escapeHtml(o)}">
              ${escapeHtml(THOUGHT_LABELS[o] || o)}
            </button>
          `).join('')}
        </div>
      `;
    } else if (field.key === 'MEMORY_MODEL') {
      const presets = Array.isArray(wbData?.memory_model_options)
        ? wbData.memory_model_options
        : ['gemini-4-neon', 'gemini-3-flash', 'gemini-2.5-pro', 'gemini-2.5-flash'];
      const cur = String(val || '');
      controlHtml = `
        <div class="stack-sm" style="width:100%;">
          <div class="filter-pills">
            ${presets.map((m) => `
              <button type="button" class="pill-btn ${cur === m ? 'active' : ''}" data-model-pick="${escapeHtml(m)}">${escapeHtml(m)}</button>
            `).join('')}
          </div>
          <input type="text" class="input input-sm mono" data-fid="${escapeHtml(fid)}" value="${escapeHtml(cur)}" placeholder="或输入自定义模型 ID" style="max-width:340px;" />
        </div>
      `;
    } else if (field.type === 'select') {
      const opts = field.options || [];
      controlHtml = `
        <select class="select select-sm" data-fid="${escapeHtml(fid)}" style="width:180px;">
          ${opts.map((o) => `<option value="${escapeHtml(o)}" ${String(val) === String(o) ? 'selected' : ''}>${escapeHtml(o)}</option>`).join('')}
        </select>
      `;
    } else if (field.type === 'number') {
      const hasRange = field.min !== undefined && field.max !== undefined && (field.max - field.min) <= 200000;
      const numVal = val !== undefined && val !== null && val !== '' ? Number(val) : '';
      const step = field.step || (field.max > 10000 ? 1000 : 1);
      const zeroOn = isZeroUnlimited(field, val);
      if (field.zero_label && numVal !== '' && Number(numVal) > 0) {
        lastPositiveVals.set(fid, Number(numVal));
      }
      controlHtml = `
        <div class="range-control${field.zero_label ? ' has-zero-toggle' : ''}${zeroOn ? ' is-unlimited' : ''}" data-zero-wrap="${escapeHtml(fid)}">
          ${field.zero_label ? `<button type="button" class="pill-btn range-unlimited-btn${zeroOn ? ' active' : ''}" data-zero-toggle="${escapeHtml(fid)}" aria-pressed="${zeroOn ? 'true' : 'false'}" title="设为 0 表示${escapeHtml(field.zero_label)}">${escapeHtml(field.zero_label)}</button>` : ''}
          ${hasRange ? `<input type="range" data-range-fid="${escapeHtml(fid)}" min="${field.min}" max="${field.max}" step="${step}" value="${numVal === '' ? field.min : Math.min(field.max, Math.max(field.min, numVal))}" />` : ''}
          <input type="number" class="input input-sm mono" data-fid="${escapeHtml(fid)}" ${field.min !== undefined ? `min="${field.min}"` : ''} ${field.max !== undefined ? `max="${field.max}"` : ''} step="${step}" value="${escapeHtml(numVal)}" />
          ${field.unit ? `<span class="unit-label">${escapeHtml(field.unit)}</span>` : ''}
        </div>
      `;
    } else if (field.type === 'secret') {
      const shown = revealedSecrets.has(fid);
      const strVal = String(val || '');
      const isMasked = strVal.includes('•');
      controlHtml = `
        <div class="row-sm" style="width:280px;">
          <input type="${shown ? 'text' : 'password'}" class="input input-sm mono flex-1" data-fid="${escapeHtml(fid)}" value="${escapeHtml(strVal)}" placeholder="${isMasked ? '已脱敏，输入新值可覆盖' : '输入密钥'}" autocomplete="off" />
          <button type="button" class="btn btn-ghost btn-icon btn-sm" data-toggle-secret="${escapeHtml(fid)}" title="${shown ? '隐藏' : '显示'}">${createIcon(shown ? 'eyeOff' : 'eye', 14)}</button>
          <button type="button" class="btn btn-ghost btn-icon btn-sm" data-copy-secret="${escapeHtml(fid)}" title="${isMasked ? '密钥已脱敏' : '复制'}">${createIcon('copy', 14)}</button>
        </div>
      `;
    } else if (field.type === 'tags') {
      const tags = splitTags(val);
      controlHtml = `
        <div class="chip-editor" data-chip-fid="${escapeHtml(fid)}" style="width:100%;">
          ${tags.map((t, idx) => `
            <span class="chip">
              <span>${escapeHtml(t)}</span>
              <button type="button" class="chip-remove" data-chip-del="${idx}" aria-label="删除">×</button>
            </span>
          `).join('')}
          <input type="text" data-chip-input="${escapeHtml(fid)}" placeholder="输入后按回车或逗号添加" />
        </div>
      `;
    } else {
      controlHtml = `
        <input type="text" class="input input-sm mono" data-fid="${escapeHtml(fid)}" value="${escapeHtml(val ?? '')}" style="width:280px;" />
      `;
    }

    const extraAction = field.key === 'GROUP_WHITELIST_IDS'
      ? `<button type="button" class="link-btn text-xs" data-goto-napcat-wl style="margin-top:6px;color:var(--accent);">在 QQ 连接页按群勾选 ${createIcon('arrowRight', 12)}</button>`
      : '';

    return `
      <div class="setting-row${isStack ? ' setting-row-stack' : ''}${dirty ? ' is-dirty' : ''}" data-row-fid="${escapeHtml(fid)}">
        <div class="setting-info">
          <div class="setting-title-line">
            <span class="setting-label">${escapeHtml(field.label || field.key)}</span>
            <span class="setting-key">${escapeHtml(field.source === 'toml' ? `config.toml · ${field.key}` : field.key)}</span>
          </div>
          ${field.desc ? `<div class="setting-desc">${escapeHtml(field.desc)}</div>` : ''}
          ${extraAction}
        </div>
        <div class="setting-control">${controlHtml}</div>
      </div>
    `;
  }

  function renderPacingSummary() {
    const ambientOn = toBool(draftEnv.GUARD_AMBIENT_LISTEN);
    const reqMention = toBool(draftToml.require_mention);
    const winMin = Math.max(1, Math.round(numOr(draftEnv.AMBIENT_WINDOW_SECS, 600) / 60));
    const maxBubble = numOr(draftEnv.AMBIENT_MAX_PER_WINDOW, 4);
    const minGap = numOr(draftEnv.AMBIENT_MIN_GAP_SECS, 45);
    const acWin = numOr(draftEnv.ACTIVE_CONV_WINDOW_SECS, 60);
    const acMax = numOr(draftEnv.ACTIVE_CONV_MAX_PER_WINDOW, 4);
    const bubbleText = maxBubble > 0
      ? `每 ${winMin} 分钟最多主动冒泡 ${maxBubble} 次`
      : '主动冒泡次数不限';
    const acText = acMax > 0
      ? `热聊 ${acWin}s 内最多追问 ${acMax} 次`
      : `热聊 ${acWin}s 内追问不限次数`;
    const modeText = reqMention
      ? '严格仅 @ 模式（群里只回显式 @机器人，被动冒泡已屏蔽）'
      : ambientOn
        ? `全群被动监听已开 · ${bubbleText}（间隔 ≥${minGap}s）· ${acText}`
        : '被动监听已关闭（仅回复 @机器人 / 点名唤起）';
    return `
      <div class="alert-banner ${reqMention ? 'alert-warning' : ''}" id="set-pacing-summary">
        <div class="row-sm">
          <span class="${reqMention ? 'text-amber' : 'text-muted'}">${createIcon(reqMention ? 'alert' : 'info', 16)}</span>
          <span>${escapeHtml(modeText)}</span>
        </div>
      </div>
    `;
  }

  function refreshPacingSummary() {
    const el = container.querySelector('#set-pacing-summary');
    if (!el) return;
    el.outerHTML = renderPacingSummary();
  }

  function renderPresetsTab() {
    const presets = Array.isArray(wbData?.presets) ? wbData.presets : [];
    const activePresetId = wbData?.active_preset_id || '';
    const ambientOn = toBool(draftEnv.GUARD_AMBIENT_LISTEN);
    const reqMention = toBool(draftToml.require_mention);
    const repeatOn = toBool(draftEnv.REPEAT_ENABLED);
    const presetMaxBubble = numOr(draftEnv.AMBIENT_MAX_PER_WINDOW, 4);
    const presetWinMin = Math.max(1, Math.round(numOr(draftEnv.AMBIENT_WINDOW_SECS, 600) / 60));
    const lurk = THOUGHT_LABELS[draftEnv.AGY_LURK_THOUGHT_LEVEL] || draftEnv.AGY_LURK_THOUGHT_LEVEL || '轻度';
    const chat = THOUGHT_LABELS[draftEnv.AGY_THOUGHT_LEVEL] || draftEnv.AGY_THOUGHT_LEVEL || '均衡';
    const task = THOUGHT_LABELS[draftEnv.AGY_TASK_THOUGHT_LEVEL] || draftEnv.AGY_TASK_THOUGHT_LEVEL || '深度';

    return `
      <div class="stack-lg">
        <section class="section">
          <div class="section-head">
            <div>
              <h2 class="section-title">当前生效</h2>
              <p class="section-desc">机器人此刻的行为组合，可以保存为自定义预设随时切回。</p>
            </div>
            <button type="button" class="btn btn-secondary btn-sm" id="set-toggle-preset-form">
              ${createIcon(showPresetForm ? 'x' : 'plus', 14)}${showPresetForm ? '取消' : '存为预设'}
            </button>
          </div>
          <div class="kpi-strip">
            <div class="kpi"><span class="kpi-label">监听模式</span><span class="kpi-value" style="font-size:20px;">${reqMention ? '仅 @ 回复' : ambientOn ? '全群监听' : '点名模式'}</span></div>
            <div class="kpi"><span class="kpi-label">主动冒泡</span><span class="kpi-value" style="font-size:20px;">${presetMaxBubble > 0 ? `≤ ${escapeHtml(presetMaxBubble)} 次<span class="kpi-unit">/ ${presetWinMin} 分钟</span>` : '不限次数'}</span></div>
            <div class="kpi"><span class="kpi-label">思考强度</span><span class="kpi-value" style="font-size:20px;">${escapeHtml(chat)}</span><span class="kpi-sub">潜水 ${escapeHtml(lurk)} · 任务 ${escapeHtml(task)}</span></div>
            <div class="kpi"><span class="kpi-label">群聊复读</span><span class="kpi-value" style="font-size:20px;">${repeatOn ? '开启' : '关闭'}</span></div>
          </div>
          ${showPresetForm ? `
            <div class="card card-body">
              <div class="row wrap" style="gap:10px;">
                <input type="text" class="input" id="set-preset-name" maxlength="24" placeholder="预设名称，如：周末摸鱼模式" style="width:240px;" />
                <input type="text" class="input flex-1" id="set-preset-tagline" maxlength="64" placeholder="一句话说明（选填）" />
                <button type="button" class="btn btn-ghost btn-sm" id="set-cancel-preset">取消</button>
                <button type="button" class="btn btn-primary btn-sm" id="set-save-preset">保存预设</button>
              </div>
            </div>
          ` : ''}
        </section>

        <section class="section">
          <div class="section-head">
            <div>
              <h2 class="section-title">场景预设</h2>
              <p class="section-desc">点击卡片把参数载入草稿预览，确认后保存；或直接一键应用。</p>
            </div>
          </div>
        <div class="preset-grid">
          ${presets.map((p) => {
            const isActive = activePresetId === p.id;
            const isSelected = selectedPresetId === p.id;
            const pEnv = p.env || {};
            const pToml = p.toml || {};
            const tags = [];
            if (pToml.require_mention) tags.push('严格仅 @');
            else if (pEnv.GUARD_AMBIENT_LISTEN === '0') tags.push('关闭冒泡');
            else tags.push('全群监听');
            if (pEnv.AMBIENT_MAX_PER_WINDOW !== undefined && pEnv.AMBIENT_MAX_PER_WINDOW !== null && String(pEnv.AMBIENT_MAX_PER_WINDOW).trim() !== '') {
              tags.push(Number(pEnv.AMBIENT_MAX_PER_WINDOW) > 0 ? `冒泡 ≤${pEnv.AMBIENT_MAX_PER_WINDOW}次` : '冒泡不限次');
            }
            if (pEnv.ACTIVE_CONV_MAX_PER_WINDOW !== undefined && pEnv.ACTIVE_CONV_MAX_PER_WINDOW !== null && String(pEnv.ACTIVE_CONV_MAX_PER_WINDOW).trim() !== '' && Number(pEnv.ACTIVE_CONV_MAX_PER_WINDOW) === 0) {
              tags.push('接话不限次');
            }
            if (pEnv.AGY_THOUGHT_LEVEL) tags.push(`思考 ${THOUGHT_LABELS[pEnv.AGY_THOUGHT_LEVEL] || pEnv.AGY_THOUGHT_LEVEL}`);
            if (pEnv.REPEAT_ENABLED !== undefined) tags.push(toBool(pEnv.REPEAT_ENABLED) ? '复读开' : '复读关');

            return `
              <div class="preset-card${isActive ? ' is-active' : ''}${isSelected ? ' is-selected' : ''}" data-preset-preview="${escapeHtml(p.id)}">
                <div class="row-between">
                  <div class="row-sm" style="min-width:0;">
                    <span class="font-semibold truncate" style="font-size:14.5px;">${escapeHtml(p.name)}</span>
                    ${isActive ? '<span class="badge badge-sm badge-success">生效中</span>' : p.badge ? `<span class="badge badge-sm">${escapeHtml(p.badge)}</span>` : ''}
                  </div>
                  ${!p.builtin ? `<button type="button" class="btn btn-ghost btn-icon btn-xs" data-preset-del="${escapeHtml(p.id)}" title="删除自定义预设">${createIcon('trash', 13)}</button>` : ''}
                </div>
                <div class="text-sm text-muted" style="min-height:40px;">${escapeHtml(p.tagline || '一键切换整组行为参数')}</div>
                <div class="text-xs text-secondary">${tags.map((t) => escapeHtml(t)).join(' · ')}</div>
                <div class="row-between" style="padding-top:12px;margin-top:4px;border-top:1px solid var(--border-subtle);">
                  <span class="text-xs ${isSelected ? 'text-accent' : 'text-muted'}">${isSelected ? '已载入草稿' : '点击预览'}</span>
                  <button type="button" class="btn btn-secondary btn-xs" data-preset-apply="${escapeHtml(p.id)}">应用</button>
                </div>
              </div>
            `;
          }).join('')}
        </div>
        </section>
      </div>
    `;
  }

  function renderRepeatTesterCard() {
    const repeatOk = Boolean(repeatResult && (repeatResult.qualifies || repeatResult.allowed || repeatResult.pass || repeatResult.eligible));
    const resHtml = repeatResult
      ? `
        <div class="alert-banner ${repeatOk ? 'alert-success' : 'alert-warning'} mt-8">
          <div class="row-sm">
            <span class="badge badge-sm ${repeatOk ? 'badge-success' : 'badge-amber'}">
              ${repeatOk ? '允许复读' : '拦截 / 跳过'}
            </span>
            <span class="text-sm">${escapeHtml(repeatResult.reason || repeatResult.message || '已完成规则判定')}</span>
          </div>
          ${repeatResult.key_desc || repeatResult.norm_key ? `<span class="mono text-xs text-muted">${escapeHtml(repeatResult.key_desc || repeatResult.norm_key)}</span>` : ''}
        </div>
      `
      : '';

    return `
      <section class="section">
        <div class="section-head">
          <div>
            <h2 class="section-title">复读规则测试</h2>
            <p class="section-desc">用已保存的 repeat_chain.py 规则，验证一条消息是否会被跟进复读。</p>
          </div>
        </div>
        <div class="card card-body">
          <div class="row">
            <input type="text" class="input flex-1" id="set-repeat-test-input" value="${escapeHtml(repeatInput)}" placeholder="输入一条群聊消息" />
            <button type="button" class="btn btn-primary" id="set-repeat-test-btn">测试</button>
          </div>
          <div class="filter-pills mt-8">
            ${REPEAT_QUICK_SAMPLES.map((s) => `<button type="button" class="pill-btn" data-repeat-sample="${escapeHtml(s)}">${escapeHtml(s)}</button>`).join('')}
          </div>
          ${resHtml}
          ${repeatHistory.length > 1 ? `
            <div class="table-wrap mt-12">
              <table class="data-table">
                <thead><tr><th>测试文本</th><th style="width:90px;">判定</th><th>原因</th></tr></thead>
                <tbody>
                  ${repeatHistory.slice(0, 6).map((h) => `
                    <tr>
                      <td class="mono text-xs">${escapeHtml(h.text)}</td>
                      <td><span class="badge badge-sm ${h.ok ? 'badge-success' : 'badge-amber'}">${h.ok ? '允许' : '拦截'}</span></td>
                      <td class="text-xs text-muted">${escapeHtml(h.reason)}</td>
                    </tr>
                  `).join('')}
                </tbody>
              </table>
            </div>
          ` : ''}
        </div>
      </section>
    `;
  }

  function renderPluginBadge(pluginId) {
    if (!pluginId) return '';
    const pf = cfgData?.plugin_flags?.[pluginId];
    if (!pf) return '';
    const on = Boolean(pf.enabled);
    const label = on ? `插件「${pf.name || pluginId}」已启用` : `插件「${pf.name || pluginId}」未启用`;
    return `<button type="button" class="link-btn text-xs" data-goto-plugin="${escapeHtml(pluginId)}" title="前往插件页开关此功能"><span class="status-dot ${on ? 'online' : 'warn'}" style="width:6px;height:6px;"></span>${escapeHtml(label)} ${createIcon('arrowRight', 12)}</button>`;
  }

  function renderFieldsCard(title, subtitle, fields, pluginId = '') {
    if (!fields.length) return '';
    return `
      <section class="section">
        <div class="section-head">
          <div>
            <h2 class="section-title">${escapeHtml(title)}</h2>
            ${subtitle ? `<p class="section-desc">${escapeHtml(subtitle)}</p>` : ''}
          </div>
          ${renderPluginBadge(pluginId)}
        </div>
        <div class="card setting-list">${fields.map(renderFieldRow).join('')}</div>
      </section>
    `;
  }

  function renderBody() {
    const body = container.querySelector('#set-body');
    if (!body || !wbData || !cfgData) return;

    const q = searchQuery.trim().toLowerCase();
    if (q) {
      const matched = allFields.filter((f) => {
        const hay = `${f.label || ''} ${f.key || ''} ${f.desc || ''}`.toLowerCase();
        return hay.includes(q);
      });
      body.innerHTML = matched.length
        ? renderFieldsCard(`「${searchQuery.trim()}」的搜索结果`, `在 bot.env 与 config.toml 的全部参数中找到 ${matched.length} 项`, matched)
        : `<div class="card"><div class="empty-state"><div class="empty-state-icon">${createIcon('search', 20)}</div><div class="empty-state-title">没有找到「${escapeHtml(searchQuery.trim())}」</div><div class="text-sm">换个关键词试试，支持中文名称和参数键名。</div></div></div>`;
      bindBodyEvents();
      return;
    }

    let html = '';
    if (activeTab === 'presets') {
      html = renderPresetsTab();
    } else if (activeTab === 'listening') {
      const fields = allFields.filter((f) => f.tab === 'listening');
      html = `<div class="stack-lg">${renderPacingSummary()}${renderFieldsCard('监听与群聊冒泡节流', '控制全群被动监听、主动接话频率与热聊连续追问节奏', fields, 'ambient_listen')}</div>`;
    } else if (activeTab === 'reasoning') {
      const fields = allFields.filter((f) => f.tab === 'reasoning');
      html = renderFieldsCard('思考强度与记忆模型', '按场景分层调节思考深度（潜水 / 闲聊 / 复杂任务），平衡智商与响应速度', fields);
    } else if (activeTab === 'repeat') {
      const repeatFields = allFields.filter((f) => f.tab === 'repeat' && f.key.startsWith('REPEAT_'));
      const fwdFields = allFields.filter((f) => f.tab === 'repeat' && f.key.startsWith('FORWARD_'));
      html = `
        <div class="stack-lg">
          ${renderFieldsCard('群聊复读机制', '控制复读阈值、窗口、冷却与屏蔽词', repeatFields, 'repeat_chain')}
          ${renderRepeatTesterCard()}
          ${renderFieldsCard('长消息合并转发', '长回复自动转为 QQ 合并转发卡片，避免刷屏', fwdFields, 'long_msg_forward')}
        </div>
      `;
    } else if (activeTab === 'style') {
      const cueFields = allFields.filter((f) => f.tab === 'style' && f.key.startsWith('BOT_'));
      const dispFields = allFields.filter((f) => f.tab === 'style' && !f.key.startsWith('BOT_'));
      html = `
        <div class="stack-lg">
          ${renderFieldsCard('点名唤起与口癖兜底', '配置唤起昵称、软点名提示词以及高频口癖防复读规则', cueFields, 'style_hygiene')}
          ${renderFieldsCard('过程消息显示与静默抑制', '控制思考/工具提示显示、队列通知静默与管理员私聊 Turbo', dispFields)}
        </div>
      `;
    } else if (activeTab === 'memory_cfg') {
      const commonFields = allFields.filter((f) => f.tab === 'memory_cfg' && !f.advanced);
      const advFields = allFields.filter((f) => f.tab === 'memory_cfg' && f.advanced);
      html = `
        <div class="stack-lg">
          ${renderFieldsCard('常用记忆与会话生命周期', '开关长期记忆、群共享记忆、Sleep 整理与会话自动压缩/回收', commonFields, 'memory_recall')}
          <details class="card accordion-card">
            <summary>
              <div>
                <div class="font-semibold">高级参数 <span class="text-muted" style="font-weight:500;">${advFields.length}</span></div>
                <div class="text-xs text-muted">SQLite 路径、提取窗口、衰减半衰期、Embedding 与历史查询</div>
              </div>
              <span class="accordion-chevron">${createIcon('chevronDown', 15)}</span>
            </summary>
            <div class="setting-list">${advFields.map(renderFieldRow).join('')}</div>
          </details>
        </div>
      `;
    } else if (activeTab === 'system') {
      const consistency = cfgData.consistency || {};
      const problems = Array.isArray(consistency.problems) ? consistency.problems : [];
      const idFields = allFields.filter((f) => f.tab === 'system' && f.schemaGroup === 'identity');
      const connFields = allFields.filter((f) => f.tab === 'system' && f.schemaGroup !== 'identity');

      const consistencyHtml = consistency.valid === false || problems.length > 0
        ? `
          <div class="alert-banner alert-danger">
            <div class="row-sm">
              <span class="text-danger">${createIcon('alert', 16)}</span>
              <span>配置不一致：${escapeHtml(problems.join('；') || 'bot.env 与 config.toml 存在差异')}</span>
            </div>
            <button type="button" class="btn btn-primary btn-sm" id="set-sync-toml-btn">同步到 config.toml</button>
          </div>
        `
        : `
          <div class="alert-banner">
            <div class="row-sm text-secondary">
              <span class="text-success">${createIcon('checkCircle', 16)}</span>
              <span>bot.env 与 config.toml 的关键字段一致（管理员 QQ、NapCat Token）</span>
            </div>
            <button type="button" class="btn btn-ghost btn-sm" id="set-sync-toml-btn">手动同步</button>
          </div>
        `;

      html = `
        <div class="stack-lg">
          ${consistencyHtml}
          ${renderFieldsCard('身份、管理员与群白名单', '配置机器人 QQ、管理员白名单与忽略的其他机器人', idFields)}
          ${renderFieldsCard('NapCat 连接、端口与路径', '修改端口或连接地址后需重启对应服务生效', connFields)}
          <details class="card accordion-card">
            <summary>
              <div>
                <div class="font-semibold">原始配置文件</div>
                <div class="text-xs text-muted">bot.env 与 config.toml 只读预览，敏感值已脱敏</div>
              </div>
              <span class="accordion-chevron">${createIcon('chevronDown', 15)}</span>
            </summary>
            <div class="card-body">
              <div class="grid-2">
                <div class="stack-sm">
                  <div class="row-between">
                    <span class="font-semibold mono text-xs">bot.env</span>
                    <button type="button" class="btn btn-ghost btn-xs" id="set-copy-env">${createIcon('copy', 12)} 复制</button>
                  </div>
                  <pre class="code-block" style="max-height:320px;">${escapeHtml(cfgData.env_raw || '')}</pre>
                </div>
                <div class="stack-sm">
                  <div class="row-between">
                    <span class="font-semibold mono text-xs">config.toml</span>
                    <button type="button" class="btn btn-ghost btn-xs" id="set-copy-toml">${createIcon('copy', 12)} 复制</button>
                  </div>
                  <pre class="code-block" style="max-height:320px;">${escapeHtml(cfgData.toml_raw || '')}</pre>
                </div>
              </div>
            </div>
          </details>
        </div>
      `;
    }

    body.innerHTML = html;
    bindBodyEvents();
  }

  function bindBodyEvents() {
    const body = container.querySelector('#set-body');
    if (!body) return;

    // Presets interactions
    body.querySelector('#set-toggle-preset-form')?.addEventListener('click', () => {
      showPresetForm = !showPresetForm;
      renderBody();
    });
    body.querySelector('#set-cancel-preset')?.addEventListener('click', () => {
      showPresetForm = false;
      renderBody();
    });
    body.querySelector('#set-save-preset')?.addEventListener('click', async () => {
      const name = (body.querySelector('#set-preset-name')?.value || '').trim();
      const tagline = (body.querySelector('#set-preset-tagline')?.value || '').trim();
      if (!name) {
        showToast('请填写预设名称', 'warning');
        return;
      }
      try {
        const res = await apiPost('/api/workbench/presets', { name, tagline, env: draftEnv, toml: draftToml });
        if (destroyed) return;
        showToast(`自定义预设「${name}」已保存`, 'success');
        showPresetForm = false;
        if (res?.state) {
          wbData = res.state;
          renderBody();
          updateTabDirtyDots();
        } else {
          await loadAll({ keepTab: true });
        }
      } catch (err) {
        showToast(`保存预设失败: ${err.message}`, 'error');
      }
    });

    body.querySelectorAll('[data-preset-preview]').forEach((card) => {
      card.addEventListener('click', (e) => {
        if (e.target.closest('button')) return;
        const pid = card.dataset.presetPreview;
        const p = (wbData?.presets || []).find((x) => x.id === pid);
        if (!p) return;
        draftEnv = { ...baseEnv, ...(p.env || {}) };
        draftToml = { ...baseToml, ...(p.toml || {}) };
        selectedPresetId = pid;
        renderBody();
        updateTabDirtyDots();
        showToast(`已将「${p.name}」载入草稿预览，可点击底部保存条应用`, 'info');
      });
    });

    body.querySelectorAll('[data-preset-apply]').forEach((btn) => {
      btn.addEventListener('click', async (e) => {
        e.stopPropagation();
        const pid = btn.dataset.presetApply;
        const p = (wbData?.presets || []).find((x) => x.id === pid);
        if (!p) return;
        const pEnv = p.env || {};
        const pToml = p.toml || {};
        const needsFull = Object.keys(pToml).length > 0 || !Object.keys(pEnv).every(isWsGuardOnlyKey);
        await saveChanges(needsFull ? 'restart-bot' : 'ws-guard', { env: pEnv, toml: pToml });
      });
    });

    body.querySelectorAll('[data-preset-del]').forEach((btn) => {
      btn.addEventListener('click', async (e) => {
        e.stopPropagation();
        const pid = btn.dataset.presetDel;
        const p = (wbData?.presets || []).find((x) => x.id === pid);
        const ok = await confirmModal({
          title: `删除自定义预设「${p?.name || pid}」？`,
          message: '删除后不可恢复，但不影响当前运行配置。',
          confirmText: '确认删除',
          danger: true,
        });
        if (!ok || destroyed) return;
        try {
          const res = await apiDelete(`/api/workbench/presets/${encodeURIComponent(pid)}`);
          if (destroyed) return;
          showToast('预设已删除', 'success');
          if (selectedPresetId === pid) selectedPresetId = '';
          if (res?.state) {
            wbData = res.state;
            renderBody();
            updateTabDirtyDots();
          } else {
            await loadAll({ keepTab: true });
          }
        } catch (err) {
          showToast(`删除失败: ${err.message}`, 'error');
        }
      });
    });

    // Standard inputs & switches
    body.querySelectorAll('[data-fid]').forEach((el) => {
      const fid = el.dataset.fid;
      const field = fieldMap.get(fid);
      if (!field) return;

      const onChange = () => {
        if (field.type === 'boolean') {
          const checked = el.checked;
          setVal(field, field.source === 'toml' ? checked : (checked ? '1' : '0'));
        } else if (field.type === 'number') {
          const v = el.value;
          setVal(field, field.source === 'toml' ? (v === '' ? '' : Number(v)) : v);
          const rangeEl = body.querySelector(`[data-range-fid="${CSS.escape(fid)}"]`);
          if (rangeEl && v !== '') {
            const clamped = Math.min(field.max, Math.max(field.min, Number(v)));
            if (Number.isFinite(clamped)) rangeEl.value = String(clamped);
          }
          syncZeroControlUi(body, fid, field, v);
        } else {
          setVal(field, el.value);
          if (fid === 'env:MEMORY_MODEL') {
            body.querySelectorAll('[data-model-pick]').forEach((b) => b.classList.toggle('active', b.dataset.modelPick === el.value));
          }
        }
        const row = body.querySelector(`[data-row-fid="${CSS.escape(fid)}"]`);
        if (row) {
          row.classList.toggle('is-dirty', !valuesEqual(getVal(field, true), getVal(field, false), field.type));
        }
        updateTabDirtyDots();
      };

      el.addEventListener(el.type === 'checkbox' || el.tagName === 'SELECT' ? 'change' : 'input', onChange);
    });

    // Range sliders synced with number inputs
    body.querySelectorAll('[data-range-fid]').forEach((rangeEl) => {
      const fid = rangeEl.dataset.rangeFid;
      const field = fieldMap.get(fid);
      if (!field) return;
      rangeEl.addEventListener('input', () => {
        const numInput = body.querySelector(`input[type="number"][data-fid="${CSS.escape(fid)}"]`);
        if (numInput) numInput.value = rangeEl.value;
        setVal(field, field.source === 'toml' ? Number(rangeEl.value) : rangeEl.value);
        syncZeroControlUi(body, fid, field, rangeEl.value);
        const row = body.querySelector(`[data-row-fid="${CSS.escape(fid)}"]`);
        if (row) row.classList.toggle('is-dirty', !valuesEqual(getVal(field, true), getVal(field, false), field.type));
        updateTabDirtyDots();
      });
    });

    // Unlimited (0) toggle pills for number fields with zero_label
    body.querySelectorAll('[data-zero-toggle]').forEach((btn) => {
      const fid = btn.dataset.zeroToggle;
      const field = fieldMap.get(fid);
      if (!field) return;
      btn.addEventListener('click', () => {
        const curVal = getVal(field, false);
        const currentlyZero = isZeroUnlimited(field, curVal);
        let nextNum;
        if (currentlyZero) {
          const remembered = lastPositiveVals.get(fid);
          const fallback = field.restore_default ?? Math.min(field.max ?? 4, Math.max((field.min ?? 0) + 1, 4));
          nextNum = Number.isFinite(Number(remembered)) && Number(remembered) > 0
            ? Number(remembered)
            : fallback;
        } else {
          const curNum = Number(curVal);
          if (Number.isFinite(curNum) && curNum > 0) lastPositiveVals.set(fid, curNum);
          nextNum = 0;
        }
        const nextVal = field.source === 'toml' ? nextNum : String(nextNum);
        setVal(field, nextVal);
        const numInput = body.querySelector(`input[type="number"][data-fid="${CSS.escape(fid)}"]`);
        if (numInput) numInput.value = String(nextNum);
        const rangeEl = body.querySelector(`[data-range-fid="${CSS.escape(fid)}"]`);
        if (rangeEl) {
          const clamped = Math.min(field.max, Math.max(field.min, nextNum));
          if (Number.isFinite(clamped)) rangeEl.value = String(clamped);
        }
        syncZeroControlUi(body, fid, field, nextVal);
        const row = body.querySelector(`[data-row-fid="${CSS.escape(fid)}"]`);
        if (row) row.classList.toggle('is-dirty', !valuesEqual(getVal(field, true), getVal(field, false), field.type));
        updateTabDirtyDots();
      });
    });

    // Segmented thought-level buttons
    body.querySelectorAll('[data-seg-fid]').forEach((grp) => {
      const fid = grp.dataset.segFid;
      const field = fieldMap.get(fid);
      if (!field) return;
      grp.querySelectorAll('[data-seg-val]').forEach((btn) => {
        btn.addEventListener('click', () => {
          setVal(field, btn.dataset.segVal);
          grp.querySelectorAll('[data-seg-val]').forEach((b) => b.classList.toggle('active', b === btn));
          const row = body.querySelector(`[data-row-fid="${CSS.escape(fid)}"]`);
          if (row) row.classList.toggle('is-dirty', !valuesEqual(getVal(field, true), getVal(field, false), field.type));
          updateTabDirtyDots();
        });
      });
    });

    // Model quick pick chips
    body.querySelectorAll('[data-model-pick]').forEach((btn) => {
      btn.addEventListener('click', () => {
        const field = fieldMap.get('env:MEMORY_MODEL');
        if (!field) return;
        const val = btn.dataset.modelPick;
        setVal(field, val);
        const inp = body.querySelector('[data-fid="env:MEMORY_MODEL"]');
        if (inp) inp.value = val;
        body.querySelectorAll('[data-model-pick]').forEach((b) => b.classList.toggle('active', b === btn));
        const row = body.querySelector('[data-row-fid="env:MEMORY_MODEL"]');
        if (row) {
          row.classList.toggle('is-dirty', !valuesEqual(getVal(field, true), getVal(field, false), field.type));
        }
        updateTabDirtyDots();
      });
    });

    // Secret reveal & copy
    body.querySelectorAll('[data-toggle-secret]').forEach((btn) => {
      btn.addEventListener('click', () => {
        const fid = btn.dataset.toggleSecret;
        if (revealedSecrets.has(fid)) revealedSecrets.delete(fid);
        else revealedSecrets.add(fid);
        renderBody();
      });
    });
    body.querySelectorAll('[data-copy-secret]').forEach((btn) => {
      btn.addEventListener('click', () => {
        const field = fieldMap.get(btn.dataset.copySecret);
        if (!field) return;
        const val = String(getVal(field, false) || '');
        if (val.includes('•')) {
          showToast('密钥已脱敏显示，如需完整密钥请直接查看本地 bot.env', 'info');
          return;
        }
        copyText(val, '已复制密钥');
      });
    });

    // Chip tag editors
    body.querySelectorAll('[data-chip-fid]').forEach((editor) => {
      const fid = editor.dataset.chipFid;
      const field = fieldMap.get(fid);
      if (!field) return;
      const inp = editor.querySelector('input');

      const commitTokens = (raw) => {
        const existing = splitTags(getVal(field, false));
        const incoming = splitTags(raw);
        let changed = false;
        for (const t of incoming) {
          if (!existing.includes(t)) {
            existing.push(t);
            changed = true;
          }
        }
        if (changed) {
          setVal(field, existing.join(','));
          renderBody();
          updateTabDirtyDots();
        }
      };

      editor.addEventListener('click', () => inp?.focus());
      editor.querySelectorAll('[data-chip-del]').forEach((delBtn) => {
        delBtn.addEventListener('click', (e) => {
          e.stopPropagation();
          const idx = Number(delBtn.dataset.chipDel);
          const existing = splitTags(getVal(field, false));
          existing.splice(idx, 1);
          setVal(field, existing.join(','));
          renderBody();
          updateTabDirtyDots();
        });
      });

      inp?.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ',' || e.key === '，') {
          e.preventDefault();
          const v = inp.value.trim();
          if (v) {
            inp.value = '';
            commitTokens(v);
          }
        } else if (e.key === 'Backspace' && !inp.value) {
          const existing = splitTags(getVal(field, false));
          if (existing.length) {
            existing.pop();
            setVal(field, existing.join(','));
            renderBody();
            updateTabDirtyDots();
          }
        }
      });
      inp?.addEventListener('blur', () => {
        const v = inp.value.trim();
        if (v) {
          inp.value = '';
          commitTokens(v);
        }
      });
    });

    // Jump to NapCat whitelist
    body.querySelector('[data-goto-napcat-wl]')?.addEventListener('click', () => {
      ctx.navigate('napcat', { tab: 'contacts' });
    });

    // Repeat tester
    const runRepeatTest = async (txt) => {
      const val = String(txt ?? repeatInput).trim();
      if (!val) return;
      repeatInput = val;
      try {
        const res = await apiPost('/api/tools/test-repeat', { text: val });
        if (destroyed) return;
        repeatResult = res;
        const ok = Boolean(res?.qualifies || res?.allowed || res?.pass || res?.eligible);
        repeatHistory.unshift({ text: val, ok, reason: res?.reason || res?.message || '' });
        if (repeatHistory.length > 8) repeatHistory.pop();
        renderBody();
      } catch (err) {
        if (!destroyed) showToast(`测试失败: ${err.message}`, 'error');
      }
    };
    const rInput = body.querySelector('#set-repeat-test-input');
    rInput?.addEventListener('input', () => { repeatInput = rInput.value; });
    rInput?.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); runRepeatTest(rInput.value); }
    });
    body.querySelector('#set-repeat-test-btn')?.addEventListener('click', () => runRepeatTest(rInput?.value));
    body.querySelectorAll('[data-repeat-sample]').forEach((btn) => {
      btn.addEventListener('click', () => runRepeatTest(btn.dataset.repeatSample));
    });

    // Sync config.toml & Copy raw previews
    body.querySelector('#set-sync-toml-btn')?.addEventListener('click', async () => {
      try {
        await apiPost('/api/config/sync-toml', {});
        showToast('已同步 ADMIN_QQ_IDS 与 NAPCAT_TOKEN 到 config.toml', 'success');
        await loadAll({ keepTab: true });
        ctx.refreshGlobalStatus?.({ silent: true });
      } catch (err) {
        showToast(`同步失败: ${err.message}`, 'error');
      }
    });
    body.querySelector('#set-copy-env')?.addEventListener('click', () => copyText(cfgData?.env_raw || '', '已复制 bot.env 预览'));
    body.querySelector('#set-copy-toml')?.addEventListener('click', () => copyText(cfgData?.toml_raw || '', '已复制 config.toml 预览'));
    body.querySelectorAll('[data-goto-plugin]').forEach((btn) => {
      btn.addEventListener('click', () => {
        const pid = btn.dataset.gotoPlugin;
        if (pid) ctx.navigate?.('plugins', { plugin: pid });
      });
    });
  }

  async function loadAll({ keepTab = true, wbOverride = null } = {}) {
    try {
      const [wb, cfg] = wbOverride
        ? [wbOverride, await apiGet('/api/config')]
        : await Promise.all([apiGet('/api/workbench'), apiGet('/api/config')]);
      if (destroyed) return;
      wbData = wb;
      cfgData = cfg;
      buildFieldCatalog();
      baseEnv = { ...(cfg.env || {}), ...(wb.env || {}) };
      baseToml = { ...(wb.toml || {}) };
      draftEnv = { ...baseEnv };
      draftToml = { ...baseToml };
      selectedPresetId = '';
      if (!keepTab) activeTab = 'presets';
      renderBody();
      updateTabDirtyDots();
    } catch (err) {
      if (destroyed) return;
      const body = container.querySelector('#set-body');
      if (body) {
        body.innerHTML = `
          <div class="card"><div class="empty-state">
            <div class="empty-state-icon">${createIcon('alert', 20)}</div>
            <div class="empty-state-title">加载配置失败</div>
            <div class="text-sm text-muted">${escapeHtml(err.message)}</div>
            <button type="button" class="btn btn-primary btn-sm mt-8" id="set-retry-btn">重试</button>
          </div></div>
        `;
        body.querySelector('#set-retry-btn')?.addEventListener('click', () => loadAll());
      }
    }
  }

  // Top tabs & IME-safe search
  container.querySelectorAll('#set-tabs [data-tab]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const next = btn.dataset.tab;
      if (next === activeTab && !searchQuery) return;
      activeTab = next;
      if (searchQuery) {
        searchQuery = '';
        const inp = container.querySelector('#set-search-input');
        if (inp) inp.value = '';
        const clr = container.querySelector('#set-search-clear');
        if (clr) clr.style.display = 'none';
      }
      container.querySelectorAll('#set-tabs [data-tab]').forEach((b) => b.classList.toggle('active', b.dataset.tab === activeTab));
      renderBody();
    });
  });

  const searchInput = container.querySelector('#set-search-input');
  const searchClear = container.querySelector('#set-search-clear');
  searchInput?.addEventListener('input', () => {
    searchQuery = searchInput.value;
    if (searchClear) searchClear.style.display = searchQuery ? 'inline-flex' : 'none';
    renderBody();
  });
  searchClear?.addEventListener('click', () => {
    searchQuery = '';
    if (searchInput) { searchInput.value = ''; searchInput.focus(); }
    searchClear.style.display = 'none';
    renderBody();
  });

  const onKeyDown = (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 's') {
      const diff = computeDiff();
      if (diff.total > 0) {
        e.preventDefault();
        saveChanges('none');
      }
    }
  };
  document.addEventListener('keydown', onKeyDown);

  await loadAll();

  return {
    cleanup() {
      destroyed = true;
      document.removeEventListener('keydown', onKeyDown);
    },
    async refresh() {
      await loadAll({ keepTab: true });
    },
  };
}
