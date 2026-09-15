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

function installFinancialStatementSamplesModal(root, docId) {
  const trigger = root.querySelector('[data-financial-statement-samples]');
  if (docId !== '04' || !trigger) return;

  const samples = [
    { id: 'income', label: '손익계산서', src: 'images/income-statement.png', alt: '가상 예시 손익계산서. 매출액, 매출총이익, 영업이익, 당기순이익을 보여준다.', caption: '손익계산서: 매출에서 비용을 빼고 이익이 남는 흐름을 봅니다.' },
    { id: 'balance', label: '재무상태표', src: 'images/balance-sheet.png', alt: '가상 예시 재무상태표. 자산, 부채, 자본의 구성을 보여준다.', caption: '재무상태표: 기준일에 회사가 가진 자산과 갚아야 할 부채, 주주의 몫인 자본을 함께 봅니다.' },
    { id: 'cashflow', label: '현금흐름표', src: 'images/cash-flow.png', alt: '가상 예시 현금흐름표. 영업, 투자, 재무 활동의 현금 흐름을 보여준다.', caption: '현금흐름표: 실제 현금이 영업·투자·재무 활동에서 어떻게 들어오고 나갔는지 봅니다.' },
  ];

  const modal = document.createElement('div');
  modal.className = 'financial-statement-modal-backdrop';
  modal.setAttribute('role', 'dialog');
  modal.setAttribute('aria-modal', 'true');
  modal.setAttribute('aria-labelledby', 'financial-statement-modal-title');
  modal.innerHTML = `
    <section class="financial-statement-modal">
      <header class="financial-statement-modal-header">
        <div><span class="financial-statement-modal-icon"><i class="fa-solid fa-file-invoice-dollar"></i></span><div><h2 id="financial-statement-modal-title">표본 재무제표</h2><p>가상의 단순 예시로 항목의 위치와 흐름을 살펴보세요.</p></div></div>
        <button type="button" class="financial-statement-modal-close" aria-label="표본 재무제표 닫기"><i class="fa-solid fa-xmark"></i></button>
      </header>
      <div class="financial-statement-tabs" role="tablist" aria-label="재무제표 종류">
        ${samples.map((sample, index) => `<button type="button" role="tab" id="statement-tab-${sample.id}" aria-selected="${index === 0}" aria-controls="statement-sample-panel" data-statement-sample="${sample.id}">${sample.label}</button>`).join('')}
      </div>
      <figure class="financial-statement-sample" id="statement-sample-panel" role="tabpanel" aria-labelledby="statement-tab-income">
        <div class="financial-statement-image-wrap"><img src="${samples[0].src}" alt="${samples[0].alt}"></div>
        <figcaption>${samples[0].caption}</figcaption>
      </figure>
      <p class="financial-statement-modal-note"><i class="fa-solid fa-circle-info"></i> 항목의 뜻을 익힌 뒤 실제 공시에서는 기간·단위·연결/별도 기준을 함께 확인하세요.</p>
    </section>`;
  document.body.appendChild(modal);

  const closeButton = modal.querySelector('.financial-statement-modal-close');
  const image = modal.querySelector('.financial-statement-sample img');
  const caption = modal.querySelector('.financial-statement-sample figcaption');
  const panel = modal.querySelector('#statement-sample-panel');
  let lastFocused = null;

  const closeModal = () => {
    modal.classList.remove('show');
    document.body.classList.remove('modal-open');
    lastFocused?.focus();
  };
  const selectSample = (sample) => {
    image.src = sample.src;
    image.alt = sample.alt;
    caption.textContent = sample.caption;
    modal.querySelectorAll('[data-statement-sample]').forEach((tab) => {
      const selected = tab.dataset.statementSample === sample.id;
      tab.setAttribute('aria-selected', String(selected));
      if (selected) panel.setAttribute('aria-labelledby', tab.id);
    });
  };
  const onKeydown = (event) => {
    if (event.key === 'Escape' && modal.classList.contains('show')) closeModal();
  };

  trigger.addEventListener('click', () => {
    lastFocused = trigger;
    modal.classList.add('show');
    document.body.classList.add('modal-open');
    closeButton.focus();
  });
  closeButton.addEventListener('click', closeModal);
  modal.addEventListener('click', (event) => { if (event.target === modal) closeModal(); });
  modal.querySelectorAll('[data-statement-sample]').forEach((tab) => {
    tab.addEventListener('click', () => selectSample(samples.find((sample) => sample.id === tab.dataset.statementSample)));
  });
  document.addEventListener('keydown', onKeydown);
  const previousCleanup = window._viewCleanup;
  window._viewCleanup = () => {
    previousCleanup?.();
    document.removeEventListener('keydown', onKeydown);
    modal.remove();
  };
}

function installMacroNewsSimulator(root, docId) {
  const trigger = root.querySelector('[data-macro-news-simulator]');
  if (docId !== '03' || !trigger) return;

  const modal = document.createElement('div');
  modal.className = 'macro-news-modal-backdrop';
  modal.setAttribute('role', 'dialog');
  modal.setAttribute('aria-modal', 'true');
  modal.setAttribute('aria-labelledby', 'macro-news-modal-title');
  modal.innerHTML = `
    <section class="macro-news-modal">
      <header class="macro-news-modal-header">
        <div><span class="macro-news-modal-icon"><i class="fa-solid fa-chart-line"></i></span><div><h2 id="macro-news-modal-title">경제 뉴스 미니 시뮬레이션</h2><p>숫자를 움직여 업종별 영향을 비교해 보세요.</p></div></div>
        <button type="button" class="macro-news-modal-close" aria-label="시뮬레이션 닫기"><i class="fa-solid fa-xmark"></i></button>
      </header>
      <div class="macro-news-controls">
        <label>금리 <output data-output="rate">0</output><input type="range" data-factor="rate" min="-2" max="2" step="1" value="0" aria-label="금리 변화"></label>
        <label>물가 <output data-output="inflation">0</output><input type="range" data-factor="inflation" min="-2" max="2" step="1" value="0" aria-label="물가 변화"></label>
        <label>환율 <output data-output="fx">0</output><input type="range" data-factor="fx" min="-2" max="2" step="1" value="0" aria-label="환율 변화"></label>
        <label>수출 <output data-output="exports">0</output><input type="range" data-factor="exports" min="-2" max="2" step="1" value="0" aria-label="수출 변화"></label>
      </div>
      <div class="macro-news-canvas-wrap"><canvas class="macro-news-canvas" height="270" aria-label="업종별 예상 영향 그래프"></canvas></div>
      <p class="macro-news-insight" aria-live="polite"></p>
      <p class="macro-news-disclaimer">학습용 단순 모델입니다. 실제 주가·수익률을 예측하거나 투자 판단을 제시하지 않습니다.</p>
    </section>`;
  document.body.appendChild(modal);

  const canvas = modal.querySelector('.macro-news-canvas');
  const insight = modal.querySelector('.macro-news-insight');
  const closeButton = modal.querySelector('.macro-news-modal-close');
  const state = { rate: 0, inflation: 0, fx: 0, exports: 0 };
  let lastFocused = null;

  const impacts = () => [
    { label: '수출 제조', value: -0.35 * state.rate - 0.25 * state.inflation + 0.85 * state.fx + 1.05 * state.exports },
    { label: '금융', value: 0.9 * state.rate - 0.35 * state.inflation - 0.1 * state.fx + 0.15 * state.exports },
    { label: '내수 소비', value: -0.75 * state.rate - 0.9 * state.inflation - 0.1 * state.fx + 0.35 * state.exports },
    { label: '수입 원가', value: -0.2 * state.rate - 0.95 * state.inflation - 1.0 * state.fx + 0.15 * state.exports },
  ];

  const draw = () => {
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const width = Math.max(300, canvas.parentElement.clientWidth - 2);
    const height = 270;
    canvas.width = width * dpr;
    canvas.height = height * dpr;
    canvas.style.width = `${width}px`;
    canvas.style.height = `${height}px`;
    const ctx = canvas.getContext('2d');
    ctx.scale(dpr, dpr);
    ctx.fillStyle = '#f8fafc';
    ctx.fillRect(0, 0, width, height);
    const pad = { left: 76, right: 26, top: 35, bottom: 26 };
    const chartWidth = width - pad.left - pad.right;
    const rows = impacts();
    const zeroX = pad.left + chartWidth / 2;
    ctx.strokeStyle = '#cbd5e1';
    ctx.lineWidth = 1;
    ctx.setLineDash([4, 4]);
    ctx.beginPath(); ctx.moveTo(zeroX, pad.top - 10); ctx.lineTo(zeroX, height - pad.bottom); ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = '#64748b';
    ctx.font = '600 11px Pretendard, sans-serif';
    ctx.textAlign = 'center';
    ctx.fillText('부정적 영향', pad.left + chartWidth * .24, 18);
    ctx.fillText('긍정적 영향', pad.left + chartWidth * .76, 18);
    rows.forEach((row, index) => {
      const y = pad.top + index * 52;
      const amount = Math.max(-2.8, Math.min(2.8, row.value));
      const barWidth = Math.abs(amount) / 2.8 * (chartWidth / 2 - 8);
      const positive = amount >= 0;
      ctx.fillStyle = positive ? '#ef476f' : '#3979dc';
      ctx.fillRect(positive ? zeroX : zeroX - barWidth, y, barWidth, 28);
      ctx.fillStyle = '#334155';
      ctx.textAlign = 'right';
      ctx.font = '700 12px Pretendard, sans-serif';
      ctx.fillText(row.label, pad.left - 12, y + 19);
      ctx.fillStyle = positive ? '#be123c' : '#1d4ed8';
      ctx.textAlign = positive ? 'left' : 'right';
      ctx.font = '700 11px Pretendard, sans-serif';
      const score = `${positive ? '+' : ''}${amount.toFixed(1)}`;
      ctx.fillText(score, positive ? zeroX + barWidth + 7 : zeroX - barWidth - 7, y + 19);
    });
    const best = [...rows].sort((a, b) => b.value - a.value)[0];
    const worst = [...rows].sort((a, b) => a.value - b.value)[0];
    insight.innerHTML = `<strong>${best.label}</strong>이(가) 상대적으로 유리하고, <strong>${worst.label}</strong>은(는) 부담이 큰 시나리오입니다. 영향 점수는 ${best.value.toFixed(1)} ~ ${worst.value.toFixed(1)}입니다.`;
  };

  const closeModal = () => {
    modal.classList.remove('show');
    document.body.classList.remove('modal-open');
    lastFocused?.focus();
  };
  const onKeydown = (event) => {
    if (event.key === 'Escape' && modal.classList.contains('show')) closeModal();
  };
  trigger.addEventListener('click', () => {
    lastFocused = trigger;
    modal.classList.add('show');
    document.body.classList.add('modal-open');
    draw();
    closeButton.focus();
  });
  closeButton.addEventListener('click', closeModal);
  modal.addEventListener('click', (event) => { if (event.target === modal) closeModal(); });
  modal.querySelectorAll('input[data-factor]').forEach((input) => {
    input.addEventListener('input', () => {
      state[input.dataset.factor] = Number(input.value);
      modal.querySelector(`[data-output="${input.dataset.factor}"]`).value = input.value > 0 ? `+${input.value}` : input.value;
      draw();
    });
  });
  window.addEventListener('resize', draw);
  document.addEventListener('keydown', onKeydown);
  const previousCleanup = window._viewCleanup;
  window._viewCleanup = () => {
    previousCleanup?.();
    window.removeEventListener('resize', draw);
    document.removeEventListener('keydown', onKeydown);
    modal.remove();
  };
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
    installMacroNewsSimulator(mdContent, docId);
    installFinancialStatementSamplesModal(mdContent, docId);

    // docs/*.md의 외부 홈페이지 링크는 학습 화면을 유지한 채 별도 창에서 연다.
    mdContent.querySelectorAll('a[href^="http://"], a[href^="https://"]').forEach((a) => {
      a.target = '_new';
      a.rel = 'noopener noreferrer';
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
