/**
 * NotebookLM은 로그인한 사용자의 노트북 안에서만 웹 소스 검색을 실행한다.
 * 이 페이지는 주식 학습에 맞춘 검색 프롬프트를 모아 복사·실행 흐름을 제공한다.
 */
const NOTEBOOKLM_URL = 'https://notebooklm.google/';
const NOTEBOOKLM_NEW_NOTEBOOK_URL = 'https://notebooklm.google.com/notebook/new?hl=ko';

const STOCK_RESEARCH_TOPICS = [
  ['주식 투자 기초', '주식 투자 입문', '한국과 미국 주식 투자의 기초를 설명하는 신뢰할 수 있는 교육 자료를 찾아줘. 주식의 소유권, 수익률, 위험, 장기투자 원칙을 중심으로 정리해줘.'],
  ['기업 분석', '재무제표로 기업 분석', '주식 투자자를 위한 재무제표 분석 자료를 찾아줘. 손익계산서, 재무상태표, 현금흐름표를 연결해 매출 성장성·수익성·재무안정성을 판단하는 방법을 중심으로 해줘.'],
  ['기업 분석', '산업·경쟁력 분석', '주식 투자 관점의 산업 분석 자료를 찾아줘. 산업 구조, 경쟁우위, 시장점유율, 성장 동력과 위험 요인을 분석하는 교육용 자료를 우선으로 모아줘.'],
  ['가치 평가', 'PER·PBR·ROE 가치평가', '주식 가치평가를 위한 PER, PBR, ROE, EPS의 의미와 활용 한계를 설명하는 신뢰할 수 있는 자료를 찾아줘. 업종 비교 시 유의점도 포함해줘.'],
  ['가치 평가', '현금흐름·기업가치', '주식 투자용 기업가치 평가 자료를 찾아줘. 영업현금흐름, 잉여현금흐름, DCF, 할인율의 개념과 실무적인 해석을 중심으로 해줘.'],
  ['기술적 분석', '이동평균·추세 분석', '주식 차트의 이동평균선과 추세 분석을 학습할 수 있는 교육 자료를 찾아줘. 지지·저항, 거래량과 함께 해석하는 방법 및 한계를 포함해줘.'],
  ['기술적 분석', 'RSI·MACD 보조지표', '주식 차트의 RSI와 MACD 보조지표를 설명하는 교육 자료를 찾아줘. 계산 원리, 과매수·과매도 해석, 단독 신호 사용 시 위험을 중심으로 해줘.'],
  ['ETF·배당', 'ETF 투자 기초', '한국과 미국 주식 ETF 투자 기초 자료를 찾아줘. 지수 추종, 총보수, 추적오차, 거래량, 분산투자와 ETF 선택 기준을 중심으로 해줘.'],
  ['ETF·배당', '배당주·배당 ETF', '배당주와 배당 ETF 투자 학습 자료를 찾아줘. 배당수익률, 배당성향, 배당 성장, 커버드콜 ETF의 구조와 유의사항을 객관적으로 설명해줘.'],
  ['포트폴리오·리스크', '분산투자와 리스크 관리', '주식 투자 포트폴리오와 리스크 관리 교육 자료를 찾아줘. 자산·산업 분산, 변동성, 최대낙폭, 리밸런싱, 투자 기간과 위험 감수 수준을 중심으로 해줘.'],
].map(([category, title, prompt]) => ({ category, title, prompt }));

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    const area = document.createElement('textarea');
    area.value = text;
    area.style.position = 'fixed';
    area.style.opacity = '0';
    document.body.append(area);
    area.select();
    const copied = document.execCommand('copy');
    area.remove();
    return copied;
  }
}

function topicCard(topic, index) {
  return `
    <article class="card notebook-search-card">
      <div class="resource-category">${topic.category}</div>
      <h3>${index + 1}. ${topic.title}</h3>
      <p>${topic.prompt}</p>
      <div class="notebook-card-actions">
        <button class="ghost-btn" type="button" data-copy-topic="${index}">
          <i class="fa-regular fa-copy"></i> 검색어 복사
        </button>
        <a class="resource-cta" href="${NOTEBOOKLM_NEW_NOTEBOOK_URL}" target="_blank" rel="noopener noreferrer" data-open-topic="${index}">
          <i class="fa-solid fa-magnifying-glass"></i> 새 노트북에서 소스 검색
        </a>
      </div>
    </article>`;
}

function render() {
  const el = document.getElementById('page-content');
  if (!el) return;

  el.innerHTML = `
    <div class="resource-hero">
      <h1><i class="fa-solid fa-book-open"></i> NotebookLM 주식 리서치</h1>
      <p>
        아래 주제를 선택하면 주식 투자 자료를 NotebookLM에서 찾아 모을 수 있습니다.
      </p>
      <div class="notebook-quick-steps" aria-label="NotebookLM 검색 방법">
        <div><b>1</b><span>새 노트북</span></div>
        <i class="fa-solid fa-arrow-right" aria-hidden="true"></i>
        <div><b>2</b><span>소스 추가</span></div>
        <i class="fa-solid fa-arrow-right" aria-hidden="true"></i>
        <div><b>3</b><span>웹 검색</span></div>
      </div>
      <p class="notebook-quick-tip"><strong>카드의 “새 노트북에서 소스 검색”을 누르세요.</strong> 검색어가 자동으로 복사됩니다. 마지막에 <strong>웹 검색</strong> 또는 <strong>Deep Research</strong> 칸에 붙여넣으면 됩니다.</p>
      <div class="notebook-hero-actions">
        <button class="ghost-btn" id="copy-all-notebook-topics" type="button"><i class="fa-regular fa-copy"></i> 전체 검색어 복사</button>
        <a class="resource-cta" href="${NOTEBOOKLM_URL}" target="_blank" rel="noopener noreferrer"><i class="fa-solid fa-arrow-up-right-from-square"></i> NotebookLM 열기</a>
      </div>
    </div>

    <div class="notebook-search-guide">
      <i class="fa-solid fa-circle-info"></i>
      <span>검색 결과에서 출처와 발행일을 확인한 뒤 필요한 자료만 추가하세요. 수집한 자료는 로그인한 내 NotebookLM 노트북에 저장됩니다.</span>
    </div>

    <section class="notebook-topic-grid">
      ${STOCK_RESEARCH_TOPICS.map(topicCard).join('')}
    </section>
  `;

  const setCopied = (button, label) => {
    const original = button.innerHTML;
    button.innerHTML = `<i class="fa-solid fa-check"></i> ${label}`;
    setTimeout(() => { button.innerHTML = original; }, 1600);
  };

  el.querySelectorAll('[data-copy-topic]').forEach((button) => {
    button.addEventListener('click', async () => {
      const topic = STOCK_RESEARCH_TOPICS[Number(button.dataset.copyTopic)];
      if (await copyText(topic.prompt)) setCopied(button, '복사됨');
    });
  });
  el.querySelectorAll('[data-open-topic]').forEach((link) => {
    link.addEventListener('click', async (event) => {
      event.preventDefault();
      const topic = STOCK_RESEARCH_TOPICS[Number(link.dataset.openTopic)];
      // 팝업 차단을 피하기 위해 사용자 클릭 중에 먼저 새 탭을 연다.
      window.open(NOTEBOOKLM_NEW_NOTEBOOK_URL, '_blank', 'noopener');
      if (await copyText(topic.prompt)) {
        const original = link.innerHTML;
        link.innerHTML = '<i class="fa-solid fa-check"></i> 검색어 복사됨';
        setTimeout(() => { link.innerHTML = original; }, 1600);
      }
    });
  });
  el.querySelector('#copy-all-notebook-topics')?.addEventListener('click', async (event) => {
    const prompts = STOCK_RESEARCH_TOPICS.map((topic, index) => `${index + 1}. ${topic.title}\n${topic.prompt}`).join('\n\n');
    if (await copyText(prompts)) setCopied(event.currentTarget, '전체 검색어 복사됨');
  });
}

render();
