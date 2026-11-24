// ==========================================================================
// 哈基米 Console — First-visit Welcome Modal
// ==========================================================================

import { copyText, createIcon, escapeHtml } from './api.js';

export const WELCOME_STORAGE_KEY = 'hajimi_welcome_seen';
export const AUTHOR_BILIBILI_URL = 'https://b23.tv/xhgSb2L';

const BRAND_CAT_SVG = `
  <svg viewBox="0 0 32 32" width="24" height="24" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
    <path d="M6.5 13.2 L8.2 6.4 C8.4 5.7 9.2 5.5 9.7 6 L13.6 9.8 Q16 9.3 18.4 9.8 L22.3 6 C22.8 5.5 23.6 5.7 23.8 6.4 L25.5 13.2 C26.7 15.8 26.6 19 25.1 21.6 C23.4 24.6 20.2 26.2 16 26.2 C11.8 26.2 8.6 24.6 6.9 21.6 C5.4 19 5.3 15.8 6.5 13.2 Z" fill="currentColor"/>
    <circle cx="12.2" cy="17.4" r="1.5" fill="var(--brand-eye)"/>
    <circle cx="19.8" cy="17.4" r="1.5" fill="var(--brand-eye)"/>
  </svg>
`;

const BILIBILI_ICON_SVG = `
  <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
    <path d="M8 3.5l2.5 3M16 3.5l-2.5 3"/>
    <rect x="3" y="6.5" width="18" height="13.5" rx="3.5"/>
    <line x1="9" y1="11.75" x2="9" y2="14.75"/>
    <line x1="15" y1="11.75" x2="15" y2="14.75"/>
  </svg>
`;

function hasSeenWelcome() {
  try {
    return localStorage.getItem(WELCOME_STORAGE_KEY) === '1';
  } catch (_) {
    return false;
  }
}

function markWelcomeSeen() {
  try {
    localStorage.setItem(WELCOME_STORAGE_KEY, '1');
  } catch (_) {}
}

function isForcedByQuery() {
  try {
    const sp = new URLSearchParams(window.location.search || '');
    return sp.get('welcome') === '1';
  } catch (_) {
    return false;
  }
}

export function showWelcomeModal() {
  if (document.getElementById('welcome-modal-backdrop')) {
    return Promise.resolve();
  }

  return new Promise((resolve) => {
    const root = document.getElementById('modal-root') || document.body;
    const prevFocus = document.activeElement;
    const prevOverflow = document.body.style.overflow;

    const backdrop = document.createElement('div');
    backdrop.id = 'welcome-modal-backdrop';
    backdrop.className = 'modal-backdrop welcome-backdrop';
    backdrop.setAttribute('role', 'dialog');
    backdrop.setAttribute('aria-modal', 'true');
    backdrop.setAttribute('aria-labelledby', 'welcome-modal-title');
    backdrop.setAttribute('aria-describedby', 'welcome-modal-desc');

    const safeUrl = escapeHtml(AUTHOR_BILIBILI_URL);

    backdrop.innerHTML = `
      <div class="modal-dialog welcome-dialog" tabindex="-1">
        <div class="welcome-hero">
          <div class="welcome-hero-top">
            <span class="welcome-brand-mark" aria-hidden="true">${BRAND_CAT_SVG}</span>
            <button type="button" class="btn btn-ghost btn-icon btn-sm welcome-close-btn" data-welcome-close aria-label="关闭欢迎页">
              ${createIcon('x', 16)}
            </button>
          </div>
          <div class="welcome-eyebrow">哈基米 Console · 本地 Web 控制台</div>
          <h2 class="welcome-title" id="welcome-modal-title">欢迎使用哈基米</h2>
          <p class="welcome-desc" id="welcome-modal-desc">
            感谢使用 QQ 机器人「哈基米」本地控制台，可一站式管理运行状态、人设、长期记忆、插件与权限。
          </p>
        </div>

        <div class="welcome-body">
          <div class="welcome-card">
            <div class="welcome-card-icon welcome-card-icon-free" aria-hidden="true">
              ${createIcon('checkCircle', 18)}
            </div>
            <div class="welcome-card-content">
              <div class="welcome-card-head">
                <span class="welcome-card-title">完全免费使用</span>
                <span class="badge badge-success badge-sm">免费</span>
              </div>
              <p class="welcome-card-text">
                本项目完全免费提供给所有用户使用，不含任何收费门槛或隐藏付费项。如从付费渠道获取，请注意甄别以防受骗。
              </p>
            </div>
          </div>

          <div class="welcome-card welcome-card-bili">
            <div class="welcome-card-icon welcome-card-icon-bili" aria-hidden="true">
              ${BILIBILI_ICON_SVG}
            </div>
            <div class="welcome-card-content">
              <div class="welcome-card-head">
                <span class="welcome-card-title">关注作者 B 站账号</span>
                <span class="badge badge-accent badge-sm">持续更新</span>
              </div>
              <p class="welcome-card-text">
                欢迎关注作者的 B 站账号，后续版本迭代、新功能与使用教程都会在里面持续更新。
              </p>
              <div class="welcome-link-bar">
                <a class="welcome-link-url mono truncate" href="${safeUrl}" target="_blank" rel="noopener noreferrer" title="${safeUrl}">
                  ${safeUrl}
                </a>
                <div class="welcome-link-actions">
                  <button type="button" class="btn btn-ghost btn-xs" data-welcome-copy title="复制链接">
                    ${createIcon('copy', 13)} 复制
                  </button>
                  <a class="btn btn-xs welcome-bili-cta" href="${safeUrl}" target="_blank" rel="noopener noreferrer">
                    前往关注 ${createIcon('arrowUpRight', 13)}
                  </a>
                </div>
              </div>
            </div>
          </div>
        </div>

        <div class="welcome-footer">
          <span class="welcome-foot-note">仅在首次打开时提示</span>
          <button type="button" class="btn btn-primary welcome-start-btn" data-welcome-close>
            开始使用 ${createIcon('arrowRight', 15)}
          </button>
        </div>
      </div>
    `;

    let closed = false;
    const finish = () => {
      if (closed) return;
      closed = true;
      markWelcomeSeen();
      document.removeEventListener('keydown', onKeyDown);
      backdrop.classList.add('is-closing');
      window.setTimeout(() => {
        backdrop.remove();
        document.body.style.overflow = prevOverflow;
        if (prevFocus && typeof prevFocus.focus === 'function') {
          try {
            prevFocus.focus();
          } catch (_) {}
        }
        resolve();
      }, 160);
    };

    const onKeyDown = (e) => {
      if (e.key === 'Escape') {
        e.preventDefault();
        finish();
      }
    };

    backdrop.addEventListener('click', (e) => {
      if (e.target === backdrop) finish();
    });

    backdrop.querySelectorAll('[data-welcome-close]').forEach((btn) => {
      btn.addEventListener('click', finish);
    });

    backdrop.querySelector('[data-welcome-copy]')?.addEventListener('click', () => {
      copyText(AUTHOR_BILIBILI_URL, '已复制作者 B 站链接');
    });

    document.addEventListener('keydown', onKeyDown);
    document.body.style.overflow = 'hidden';
    root.appendChild(backdrop);

    const dialogEl = backdrop.querySelector('.welcome-dialog');
    dialogEl?.focus({ preventScroll: true });
  });
}

export function maybeShowWelcome() {
  if (isForcedByQuery() || !hasSeenWelcome()) {
    return showWelcomeModal();
  }
  return Promise.resolve();
}
