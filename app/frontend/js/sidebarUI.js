/**
 * sidebarUI.js — 사이드바 열기/닫기, 섹션 토글 공통 로직.
 * index.html(SPA)과 pages/*.html 정적 페이지 양쪽에서 공유한다.
 */
const DESKTOP_BREAKPOINT = 1024;
let _sidebarOpen = window.innerWidth > DESKTOP_BREAKPOINT;
const MENU_SECTION_ORDER = ['learn', 'review', 'quiz', 'visualization'];

// SPA와 정적 페이지가 같은 메뉴 순서를 유지하도록 실제 DOM 순서를 맞춘다.
function orderSidebarSections() {
  const nav = document.querySelector('.sidebar-nav');
  if (!nav) return;

  const sections = new Map(
    [...nav.querySelectorAll(':scope > .nav-section')].map((section) => {
      const id = MENU_SECTION_ORDER.find((item) => section.querySelector(`#nav-${item}`));
      return [id, section];
    }),
  );
  MENU_SECTION_ORDER.forEach((id) => {
    const section = sections.get(id);
    if (section) nav.append(section);
  });
}
window._orderSidebarSections = orderSidebarSections;

function ensureSidebarChatbot() {
  if (document.getElementById('floating-chatbot')) return;

  document.body.insertAdjacentHTML('beforeend', `
    <section class="floating-chatbot" id="floating-chatbot" aria-label="AI 투자 도우미">
      <div class="floating-chatbot-panel" id="floating-chatbot-panel" hidden>
        <header class="floating-chatbot-head">
          <span><i class="fa-solid fa-robot"></i> AI 투자 도우미</span>
          <button type="button" id="floating-chatbot-close" aria-label="대화창 닫기"><i class="fa-solid fa-xmark"></i></button>
        </header>
        <div class="floating-chatbot-messages" id="floating-chatbot-messages" aria-live="polite">
          <p class="floating-chatbot-welcome">투자와 종목에 관한 궁금한 내용을 입력해 보세요.</p>
        </div>
        <form class="floating-chatbot-form" id="floating-chatbot-form">
          <input id="floating-chatbot-input" type="text" maxlength="300" placeholder="질문을 입력하세요" aria-label="챗봇 질문" />
          <button type="submit" aria-label="질문 보내기"><i class="fa-solid fa-paper-plane"></i></button>
        </form>
      </div>
      <button type="button" class="floating-chatbot-trigger" id="floating-chatbot-trigger" aria-label="AI 투자 도우미 열기" aria-expanded="false">
        <i class="fa-solid fa-comment-dots"></i><span>AI 투자 도우미</span>
      </button>
    </section>`);

  const panel = document.getElementById('floating-chatbot-panel');
  const trigger = document.getElementById('floating-chatbot-trigger');
  const close = document.getElementById('floating-chatbot-close');
  const form = document.getElementById('floating-chatbot-form');
  const input = document.getElementById('floating-chatbot-input');
  const messages = document.getElementById('floating-chatbot-messages');
  const setOpen = (open) => {
    panel.hidden = !open;
    trigger.setAttribute('aria-expanded', String(open));
    trigger.setAttribute('aria-label', open ? 'AI 투자 도우미 닫기' : 'AI 투자 도우미 열기');
    if (open) input?.focus();
  };
  trigger?.addEventListener('click', () => setOpen(panel.hidden));
  close?.addEventListener('click', () => setOpen(false));

  form?.addEventListener('submit', (event) => {
    event.preventDefault();
    const question = input?.value.trim();
    if (!question || !messages) return;

    const userMessage = document.createElement('p');
    userMessage.className = 'floating-chatbot-message is-user';
    userMessage.textContent = question;
    const enterpriseMessage = document.createElement('p');
    enterpriseMessage.className = 'floating-chatbot-message is-enterprise';
    enterpriseMessage.textContent = 'Enterprise 버전입니다.';
    messages.replaceChildren(userMessage, enterpriseMessage);
    input.value = '';
  });
}
window._ensureSidebarChatbot = ensureSidebarChatbot;

// ── Enterprise 안내 모달 (회원가입/로그인 클릭 시) ──
function openEnterpriseModal() {
  const overlay = document.getElementById('enterprise-modal-overlay');
  if (!overlay) return;
  overlay.hidden = false;
  document.body.classList.add('modal-open');
}
function closeEnterpriseModal() {
  const overlay = document.getElementById('enterprise-modal-overlay');
  if (!overlay) return;
  overlay.hidden = true;
  document.body.classList.remove('modal-open');
}
window.closeEnterpriseModal = closeEnterpriseModal;

document.querySelectorAll('.js-enterprise-gate').forEach((el) => {
  el.addEventListener('click', (event) => {
    event.preventDefault();
    openEnterpriseModal();
  });
});
document.getElementById('enterprise-modal-overlay')?.addEventListener('click', (event) => {
  if (event.target.id === 'enterprise-modal-overlay') closeEnterpriseModal();
});
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') closeEnterpriseModal();
});

function syncSidebarToggle() {
  const toggle = document.getElementById('sidebar-toggle');
  if (!toggle) return;
  toggle.setAttribute('aria-expanded', String(_sidebarOpen));
  toggle.setAttribute('aria-label', _sidebarOpen ? '메뉴 닫기' : '메뉴 열기');
}

function toggleSidebar() {
  _sidebarOpen ? closeSidebar() : openSidebar();
}
function openSidebar() {
  document.getElementById('sidebar').classList.add('open');
  document.body.classList.remove('sidebar-collapsed');
  if (window.innerWidth <= DESKTOP_BREAKPOINT) {
    document.getElementById('overlay').classList.add('show');
  }
  _sidebarOpen = true;
  syncSidebarToggle();
}
function closeSidebar() {
  document.getElementById('sidebar').classList.remove('open');
  document.getElementById('overlay').classList.remove('show');
  if (window.innerWidth > DESKTOP_BREAKPOINT) {
    document.body.classList.add('sidebar-collapsed');
  }
  _sidebarOpen = false;
  syncSidebarToggle();
}
function toggleNav(id) {
  const el = document.getElementById('nav-' + id);
  const chev = document.getElementById('chev-' + id);
  const open = el.classList.toggle('open');
  if (chev) chev.style.transform = open ? 'rotate(180deg)' : '';
}
// auto-open a section (e.g. quiz/learn while that view is active)
window._openNavSection = function(id) {
  const el = document.getElementById('nav-' + id);
  const chev = document.getElementById('chev-' + id);
  if (el && !el.classList.contains('open')) {
    el.classList.add('open');
    if (chev) chev.style.transform = 'rotate(180deg)';
  }
};
// close every section (used before opening only the section(s) currently in use)
function closeAllNavSections() {
  document.querySelectorAll('.nav-children').forEach((el) => {
    el.classList.remove('open');
    const chev = document.getElementById('chev-' + el.id.replace(/^nav-/, ''));
    if (chev) chev.style.transform = '';
  });
}
// close everything, then open only the section(s) relevant to the current view
window._setActiveNavSections = function(ids) {
  closeAllNavSections();
  (ids || []).forEach((id) => window._openNavSection(id));
};

window.addEventListener('resize', () => {
  const isMobile = window.innerWidth <= DESKTOP_BREAKPOINT;
  document.getElementById('overlay').classList.remove('show');

  if (isMobile) {
    document.body.classList.remove('sidebar-collapsed');
    document.getElementById('sidebar').classList.remove('open');
    _sidebarOpen = false;
  } else {
    document.getElementById('sidebar').classList.toggle('open', !document.body.classList.contains('sidebar-collapsed'));
    _sidebarOpen = !document.body.classList.contains('sidebar-collapsed');
  }

  syncSidebarToggle();
});

if (window.innerWidth > DESKTOP_BREAKPOINT) {
  document.getElementById('sidebar').classList.add('open');
}
orderSidebarSections();
ensureSidebarChatbot();
syncSidebarToggle();
