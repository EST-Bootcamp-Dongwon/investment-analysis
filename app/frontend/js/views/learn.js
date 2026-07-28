/**
 * learn.js — MD 파일 학습 뷰 (marked.js CDN 렌더링)
 * /api/learn/doc/{docId} 엔드포인트에서 markdown 텍스트를 받아 렌더링
 */

function ensureMarked() {
  if (window.marked) return Promise.resolve();
  return new Promise((resolve, reject) => {
    const s = document.createElement('script');
    s.src = 'https://cdn.jsdelivr.net/npm/marked@11/marked.min.js';
    s.onload  = resolve;
    s.onerror = reject;
    document.head.appendChild(s);
  });
}

let mermaidLoader;
let mermaidInitialized = false;

function initializeMermaid() {
  if (!mermaidInitialized) {
    window.mermaid.initialize({ startOnLoad: false, theme: 'default', securityLevel: 'loose' });
    mermaidInitialized = true;
  }
  return window.mermaid;
}

function ensureMermaid() {
  if (window.mermaid) return Promise.resolve(initializeMermaid());
  if (mermaidLoader) return mermaidLoader;

  // 인라인 module 스크립트는 import 실패를 안정적으로 reject하지 못해 Mermaid
  // 렌더링이 대기 상태에 남을 수 있다. 전역 번들을 명시적으로 로드한다.
  mermaidLoader = new Promise((resolve, reject) => {
    const s = document.createElement('script');
    s.src = 'vendor/mermaid.min.js?v=11.16.0';
    s.async = true;
    s.onload = () => {
      if (!window.mermaid) {
        reject(new Error('Mermaid 라이브러리를 초기화하지 못했습니다.'));
        return;
      }
      resolve(initializeMermaid());
    };
    s.onerror = () => reject(new Error('Mermaid CDN을 불러오지 못했습니다.'));
    document.head.appendChild(s);
  }).catch((err) => {
    mermaidLoader = null;
    throw err;
  });

  return mermaidLoader;
}

/** Mermaid 소스는 VIEW 배지로 대체하고, 클릭할 때만 모달에서 렌더링한다. */
async function renderMermaidBlocks(root) {
  const blocks = [...root.querySelectorAll('code.language-mermaid')];
  if (!blocks.length) return;

  const modal = document.createElement('div');
  modal.className = 'mermaid-modal-backdrop';
  modal.setAttribute('role', 'dialog');
  modal.setAttribute('aria-modal', 'true');
  modal.setAttribute('aria-label', 'Mermaid 차트');
  modal.innerHTML = `
    <section class="mermaid-modal" role="document">
      <header class="mermaid-modal-header">
        <div><i class="fa-solid fa-diagram-project"></i> Mermaid 차트</div>
        <button type="button" class="mermaid-modal-close" aria-label="차트 닫기"><i class="fa-solid fa-xmark"></i></button>
      </header>
      <div class="mermaid-modal-chart" aria-live="polite"></div>
    </section>`;
  document.body.appendChild(modal);

  const chart = modal.querySelector('.mermaid-modal-chart');
  const closeButton = modal.querySelector('.mermaid-modal-close');
  let lastFocused = null;
  const closeModal = () => {
    modal.classList.remove('show');
    chart.replaceChildren();
    lastFocused?.focus();
  };
  const onKeydown = (event) => {
    if (event.key === 'Escape' && modal.classList.contains('show')) closeModal();
  };
  closeButton.addEventListener('click', closeModal);
  modal.addEventListener('click', (event) => {
    if (event.target === modal) closeModal();
  });
  document.addEventListener('keydown', onKeydown);

  const diagrams = blocks.map((code, index) => {
    const pre = code.closest('pre');
    const graphDef = code.textContent;
    const badge = document.createElement('button');
    badge.type = 'button';
    badge.className = 'mermaid-view-badge';
    badge.innerHTML = '<i class="fa-solid fa-eye"></i><span>VIEW</span><small>Mermaid 차트</small>';
    pre.replaceWith(badge);
    return { graphDef, badge, index };
  });

  diagrams.forEach(({ graphDef, badge, index }) => {
    badge.addEventListener('click', async () => {
      lastFocused = badge;
      modal.classList.add('show');
      chart.innerHTML = '<div class="mermaid-modal-loading"><i class="fa-solid fa-spinner fa-spin"></i> 차트를 렌더링하는 중…</div>';
      closeButton.focus();
      try {
        await ensureMermaid();
        const id = `mermaid-modal-${Date.now()}-${index}`;
        const { svg, bindFunctions } = await window.mermaid.render(id, graphDef);
        chart.innerHTML = svg;
        bindFunctions?.(chart);
      } catch (err) {
        chart.innerHTML = '<p class="mermaid-modal-error">차트를 렌더링하지 못했습니다. 문서의 Mermaid 문법을 확인하세요.</p>';
        console.error('mermaid 렌더링 실패:', err);
      }
    });
  });

  const previousCleanup = window._viewCleanup;
  window._viewCleanup = () => {
    previousCleanup?.();
    document.removeEventListener('keydown', onKeydown);
    modal.remove();
  };
}

function buildToc(container) {
  const heads = [...container.querySelectorAll('h2, h3')];
  if (!heads.length) return '';
  return `<aside class="learn-toc" id="learn-toc" aria-hidden="true" aria-label="문서 목차">
    <div class="learn-toc-hdr">
      <div class="learn-toc-title"><i class="fa-solid fa-list-ul"></i> 목차</div>
      <button class="learn-toc-close" id="learn-toc-close" type="button" aria-label="목차 닫기">
        <i class="fa-solid fa-xmark"></i>
      </button>
    </div>
    <ul class="toc-list">
      ${heads.map(h => {
        const cls = h.tagName === 'H3' ? 'toc-item h3' : 'toc-item';
        return `<li class="${cls}" data-id="${h.id}">${h.textContent.replace(/^[#\s]+/,'')}</li>`;
      }).join('')}
    </ul>
  </aside>`;
}

export function learnView(app, docId) {
  app.innerHTML = `
    <div class="loading-wrap">
      <div class="spinner"></div>
      <div class="loading-text">문서 로딩 중…</div>
    </div>`;

  Promise.all([
    ensureMarked(),
    fetch(`/api/learn/doc/${encodeURIComponent(docId)}`).then(r => {
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      return r.json();
    })
  ]).then(([, data]) => {
    const html = window.marked.parse(data.content || '');

    app.innerHTML = `
      <div class="learn-container" id="learn-wrap">
        <div class="learn-body">
          <div class="md-body" id="md-content">${html}</div>
        </div>
        <button class="toc-toggle-btn" id="toc-toggle" type="button" aria-controls="learn-toc" aria-expanded="false">
          <i class="fa-solid fa-list-ul"></i> 목차
        </button>
        <div class="toc-overlay" id="toc-overlay"></div>
        <div id="toc-placeholder"></div>
      </div>`;

    // add heading IDs for TOC navigation
    const mdContent = app.querySelector('#md-content');
    mdContent.querySelectorAll('h2, h3').forEach((h, i) => {
      if (!h.id) h.id = `heading-${i}`;
    });

    // Mermaid 소스는 VIEW 배지로 표시하고 클릭 시 모달에서 렌더링한다.
    renderMermaidBlocks(mdContent).catch((err) => console.error('Mermaid 로드 실패:', err));

    // 문서 안의 youtube.com 링크는 외부로 바로 나가지 않고, 자체 뷰어가 있는
    // "외부 자료 > 유튜브 학습 영상" 페이지로 연결한다.
    mdContent.querySelectorAll('a[href*="youtube.com"], a[href*="youtu.be"]').forEach((a) => {
      a.href = 'pages/youtube.html';
      a.removeAttribute('target');
      a.title = '유튜브 학습 영상 목록으로 이동';
    });

    // inject TOC (문서에 소제목이 없으면 목차 버튼도 숨김)
    const tocEl = app.querySelector('#toc-placeholder');
    const tocHtml = buildToc(mdContent);
    if (tocEl) tocEl.outerHTML = tocHtml;
    if (!tocHtml) app.querySelector('#toc-toggle')?.style.setProperty('display', 'none');

    // 목차 offcanvas 열기/닫기
    function openToc() {
      const toc = app.querySelector('#learn-toc');
      toc?.classList.add('open');
      toc?.setAttribute('aria-hidden', 'false');
      app.querySelector('#toc-overlay')?.classList.add('show');
      app.querySelector('#toc-toggle')?.setAttribute('aria-expanded', 'true');
    }
    function closeToc() {
      const toc = app.querySelector('#learn-toc');
      toc?.classList.remove('open');
      toc?.setAttribute('aria-hidden', 'true');
      app.querySelector('#toc-overlay')?.classList.remove('show');
      app.querySelector('#toc-toggle')?.setAttribute('aria-expanded', 'false');
    }
    const onKeydown = (event) => {
      if (event.key === 'Escape') closeToc();
    };
    document.addEventListener('keydown', onKeydown);

    // 화면 전환 시 열려 있던 목차와 observer/event listener를 정리한다.
    const previousCleanup = window._viewCleanup;
    window._viewCleanup = () => {
      previousCleanup?.();
      closeToc();
      observer.disconnect();
      document.removeEventListener('keydown', onKeydown);
    };
    app.querySelector('#toc-toggle')?.addEventListener('click', openToc);
    app.querySelector('#toc-overlay')?.addEventListener('click', closeToc);
    app.querySelector('#learn-toc-close')?.addEventListener('click', closeToc);

    // wire TOC clicks
    app.querySelectorAll('.toc-item').forEach(li => {
      li.addEventListener('click', () => {
        const target = document.getElementById(li.dataset.id);
        if (target) target.scrollIntoView({ behavior: 'smooth', block: 'start' });
        if (window.innerWidth <= 1024) closeToc();
      });
    });

    // TOC active tracking
    const observer = new IntersectionObserver(entries => {
      entries.forEach(e => {
        if (e.isIntersecting) {
          app.querySelectorAll('.toc-item').forEach(li => li.classList.remove('active'));
          const li = app.querySelector(`.toc-item[data-id="${e.target.id}"]`);
          if (li) li.classList.add('active');
        }
      });
    }, { rootMargin: '-20% 0px -70% 0px' });
    mdContent.querySelectorAll('h2, h3').forEach(h => observer.observe(h));

  }).catch(err => {
    app.innerHTML = `<div class="card">
      <p style="color:var(--red)">문서를 불러오지 못했습니다: ${err.message}</p>
      <p style="font-size:.82rem;color:var(--text-muted)">백엔드 서버가 실행 중인지 확인하세요.</p>
    </div>`;
  });
}
