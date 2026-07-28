/**
 * sidebarUI.js — 사이드바 열기/닫기, 섹션 토글 공통 로직.
 * index.html(SPA)과 pages/*.html(외부 자료 MPA 페이지) 양쪽에서 공유한다.
 */
const DESKTOP_BREAKPOINT = 1024;
let _sidebarOpen = window.innerWidth > DESKTOP_BREAKPOINT;
const MENU_SECTION_ORDER = ['learn', 'quiz', 'external', 'aitools', 'practice'];

// SPA와 정적 외부 자료 페이지가 같은 메뉴 순서를 유지하도록 실제 DOM 순서를 맞춘다.
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
  const sidebar = document.getElementById('sidebar');
  if (!sidebar || sidebar.querySelector('#sidebar-chatbot')) return;

  sidebar.insertAdjacentHTML('beforeend', `
    <section class="sidebar-chatbot" id="sidebar-chatbot" aria-label="AI 투자 도우미">
      <div class="sidebar-chatbot-head">
        <span><i class="fa-solid fa-robot"></i> AI 투자 도우미</span>
        <em>Enterprise</em>
      </div>
      <div class="sidebar-chatbot-messages" id="sidebar-chatbot-messages" aria-live="polite">
        <p class="sidebar-chatbot-welcome">궁금한 내용을 입력해 보세요.</p>
      </div>
      <form class="sidebar-chatbot-form" id="sidebar-chatbot-form">
        <input id="sidebar-chatbot-input" type="text" maxlength="300" placeholder="질문을 입력하세요" aria-label="챗봇 질문" />
        <button type="submit" aria-label="질문 보내기"><i class="fa-solid fa-paper-plane"></i></button>
      </form>
    </section>`);

  const form = document.getElementById('sidebar-chatbot-form');
  const input = document.getElementById('sidebar-chatbot-input');
  const messages = document.getElementById('sidebar-chatbot-messages');
  form?.addEventListener('submit', (event) => {
    event.preventDefault();
    const question = input?.value.trim();
    if (!question || !messages) return;

    const userMessage = document.createElement('p');
    userMessage.className = 'sidebar-chatbot-message is-user';
    userMessage.textContent = question;
    const enterpriseMessage = document.createElement('p');
    enterpriseMessage.className = 'sidebar-chatbot-message is-enterprise';
    enterpriseMessage.textContent = 'Enterprise 버전입니다.';
    messages.replaceChildren(userMessage, enterpriseMessage);
    input.value = '';
  });
}
window._ensureSidebarChatbot = ensureSidebarChatbot;

// ── Enterprise 안내 모달 (회원가입/로그인, 에이전트, 증시뉴스 클릭 시) ──
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
