import { api } from '../api.js';

const MARKETS = [
  { id: 'us', country: '미국', exchange: 'NYSE · Nasdaq', cap: 62.2, share: 42.7, index: 'S&P 500', ticker: '^GSPC', x: 24, y: 39, color: '#2563eb' },
  { id: 'ca', country: '캐나다', exchange: 'TMX', cap: 3.2, share: 2.2, x: 19, y: 25, color: '#0ea5e9' },
  { id: 'uk', country: '영국', exchange: 'LSE', cap: 3.5, share: 2.4, index: 'FTSE 100', ticker: '^FTSE', x: 47, y: 32, color: '#7c3aed' },
  { id: 'de', country: '독일', exchange: 'Deutsche Börse', cap: 2.4, share: 1.6, index: 'DAX', ticker: '^GDAXI', x: 51, y: 38, color: '#8b5cf6' },
  { id: 'sa', country: '사우디', exchange: 'Tadawul', cap: 2.8, share: 1.9, index: 'TASI', ticker: '^TASI.SR', x: 57, y: 54, color: '#d97706' },
  { id: 'in', country: '인도', exchange: 'NSE · BSE', cap: 5.3, share: 3.6, index: 'Nifty 50', ticker: '^NSEI', x: 67, y: 56, color: '#ea580c' },
  { id: 'cn', country: '중국', exchange: 'SSE · SZSE', cap: 11.5, share: 7.9, index: '상하이종합', ticker: '000001.SS', x: 72, y: 43, color: '#dc2626' },
  { id: 'jp', country: '일본', exchange: 'JPX', cap: 6.5, share: 4.5, index: 'Nikkei 225', ticker: '^N225', x: 83, y: 40, color: '#db2777' },
  { id: 'kr', country: '한국', exchange: 'KRX', cap: 1.9, share: 1.3, index: 'KOSPI', ticker: '^KS11', x: 79, y: 47, color: '#0891b2' },
  { id: 'tw', country: '대만', exchange: 'TWSE', cap: 2.0, share: 1.4, index: 'TAIEX', ticker: '^TWII', x: 78, y: 51, color: '#059669' },
  { id: 'hk', country: '홍콩', exchange: 'HKEX', cap: 4.1, share: 2.8, index: 'Hang Seng', ticker: '^HSI', x: 75, y: 52, color: '#e11d48' },
  { id: 'au', country: '호주', exchange: 'ASX', cap: 1.9, share: 1.3, index: 'S&P/ASX 200', ticker: '^AXJO', x: 84, y: 75, color: '#0284c7' },
];

const QUOTED_MARKETS = MARKETS.filter((market) => ['us', 'uk', 'in', 'cn', 'jp', 'kr', 'tw', 'hk'].includes(market.id));

function marketCap(value) {
  return `$${value.toFixed(1)}조`;
}

function mapMarker(market) {
  const size = Math.max(20, Math.min(52, 14 + Math.sqrt(market.cap) * 10));
  return `<button class="world-market-marker" type="button" data-market-id="${market.id}" style="--x:${market.x}%;--y:${market.y}%;--size:${size}px;--market-color:${market.color}" aria-label="${market.country} ${marketCap(market.cap)}">
    <span>${market.country}</span><b>${marketCap(market.cap)}</b>
  </button>`;
}

function barChartOptions() {
  const data = [...MARKETS].sort((a, b) => b.cap - a.cap).slice(0, 10);
  return {
    chart: { type: 'bar', height: 400, toolbar: { show: false }, fontFamily: 'Pretendard, -apple-system, "Malgun Gothic", sans-serif' },
    series: [{ name: '시가총액', data: data.map((market) => market.cap) }],
    colors: data.map((market) => market.color),
    plotOptions: { bar: { horizontal: true, borderRadius: 4, barHeight: '58%', distributed: true } },
    dataLabels: { enabled: true, formatter: (value) => `$${Number(value).toFixed(1)}조`, style: { colors: ['#334155'], fontSize: '11px', fontWeight: 750 }, offsetX: 8 },
    xaxis: { categories: data.map((market) => market.country), max: 70, labels: { formatter: (value) => `$${value}조`, style: { colors: '#94a3b8', fontSize: '10px' } }, axisBorder: { show: false }, axisTicks: { show: false } },
    yaxis: { labels: { style: { colors: '#475569', fontSize: '11px', fontWeight: 700 } } },
    grid: { borderColor: '#eef2f7', strokeDashArray: 3, padding: { left: 4, right: 38 } },
    legend: { show: false }, tooltip: { y: { formatter: (value) => marketCap(Number(value)) } },
  };
}

function quoteMarkup(market, quote) {
  if (!quote || quote.status !== 'ok') return '<span class="world-market-quote is-empty">시세 확인 중</span>';
  const change = Number(quote.change_pct) || 0;
  return `<span class="world-market-quote ${change >= 0 ? 'is-up' : 'is-down'}">${quote.value.toLocaleString('ko-KR', { maximumFractionDigits: 2 })} <b>${change >= 0 ? '▲' : '▼'} ${Math.abs(change).toFixed(2)}%</b></span>`;
}

export function worldMarketsView(container) {
  container.innerHTML = `
    <section class="world-markets-page">
      <header class="world-markets-head">
        <div><h1><i class="fa-solid fa-earth-americas"></i> 세계증시현황</h1><p>주요 국가 거래소의 상장주식 시가총액 규모와 대표 지수를 한눈에 비교합니다.</p></div>
        <div class="world-markets-actions"><span id="world-markets-stamp">시세 조회 전</span><button type="button" id="world-markets-refresh"><i class="fa-solid fa-rotate-right"></i> 새로고침</button></div>
      </header>
      <div class="world-markets-kpis">
        <article><span>표시 시장 합계</span><strong>$${MARKETS.reduce((sum, market) => sum + market.cap, 0).toFixed(1)}조</strong><small>상위 ${MARKETS.length}개 시장</small></article>
        <article><span>최대 시장</span><strong>미국 $62.2조</strong><small>NYSE · Nasdaq 기준</small></article>
        <article><span>아시아 주요 시장</span><strong>$31.3조</strong><small>중국·일본·인도·한국·대만·홍콩</small></article>
      </div>
      <div class="world-markets-grid">
        <section class="world-market-panel world-market-map-panel">
          <div class="world-market-panel-head"><div><h2>글로벌 증시 지도</h2><p>원 크기는 시장 규모를 나타냅니다.</p></div><span>단위: USD 조</span></div>
          <div class="world-market-map" aria-label="주요 국가 증시 규모 지도">
            <svg class="world-market-land" viewBox="0 0 1000 510" aria-hidden="true"><path d="M45 93l84-43 106 15 49 50-18 49-78 19-39 73-80-10-42-67zM278 257l60 24 34 91-24 104-43-7-20-85-42-69zM430 97l80-42 130 17 47 46-29 42-75-2-37 35-103-17-31-42zM535 204l95-12 92 55-24 104-80 38-71-50-42-73zM719 97l112-25 120 43 11 70-83 23-82-27-62-12zM764 297l105 21 70 68-35 76-115-8-48-69z"/></svg>
            ${MARKETS.map(mapMarker).join('')}
          </div>
          <p class="world-market-map-note"><i class="fa-solid fa-circle"></i> 국가를 선택하면 아래 대표지수 목록에서 해당 시장을 확인할 수 있습니다.</p>
        </section>
        <section class="world-market-panel world-market-chart-panel">
          <div class="world-market-panel-head"><div><h2>주요국 증시 규모</h2><p>상위 10개 시장 비교</p></div><span>상장주식 시가총액</span></div>
          <div id="world-market-cap-chart"></div>
        </section>
      </div>
      <section class="world-market-panel world-market-list-panel">
        <div class="world-market-panel-head"><div><h2>대표 지수 현황</h2><p>지수는 외부 시세 공급자 기준이며 시장이 닫힌 경우 마지막 거래가를 표시합니다.</p></div><span id="world-markets-quote-count">시세 연결 중</span></div>
        <div class="world-market-list">${QUOTED_MARKETS.map((market) => `<article class="world-market-row" id="world-market-row-${market.id}"><span class="world-market-dot" style="--market-color:${market.color}"></span><div><strong>${market.country}</strong><small>${market.exchange} · ${market.index}</small></div><b>${marketCap(market.cap)}</b><div id="world-market-quote-${market.id}">${quoteMarkup(market)}</div></article>`).join('')}</div>
      </section>
      <p class="world-markets-note">시장 규모는 비교 편의를 위해 USD로 환산한 최근 연간 공개 통계의 반올림 참고값입니다. 서로 다른 거래소의 중복상장·환율 변동에 따라 실제 합계와 차이가 날 수 있으며, 투자 권유 목적의 정보가 아닙니다.</p>
    </section>`;

  const chart = new ApexCharts(container.querySelector('#world-market-cap-chart'), barChartOptions());
  let disposed = false;
  let loading = false;
  chart.render();

  function highlightMarket(id) {
    container.querySelectorAll('.world-market-row, .world-market-marker').forEach((element) => element.classList.remove('is-selected'));
    container.querySelector(`#world-market-row-${id}`)?.classList.add('is-selected');
    container.querySelector(`[data-market-id="${id}"]`)?.classList.add('is-selected');
  }

  async function loadQuotes() {
    if (loading || disposed) return;
    loading = true;
    const button = container.querySelector('#world-markets-refresh');
    const stamp = container.querySelector('#world-markets-stamp');
    button.disabled = true;
    button.classList.add('is-loading');
    try {
      const data = await api.marketSnapshot({ tickers: QUOTED_MARKETS.map((market) => market.ticker) });
      if (disposed) return;
      const quotes = new Map((data.items || []).map((item) => [item.ticker, item]));
      let count = 0;
      QUOTED_MARKETS.forEach((market) => {
        const quote = quotes.get(market.ticker);
        if (quote?.status === 'ok') count += 1;
        const element = container.querySelector(`#world-market-quote-${market.id}`);
        if (element) element.innerHTML = quoteMarkup(market, quote);
      });
      container.querySelector('#world-markets-quote-count').textContent = `${count}/${QUOTED_MARKETS.length}개 지수 연결`;
      const fetchedAt = new Date(data.fetched_at);
      stamp.textContent = Number.isNaN(fetchedAt.valueOf()) ? '시세 조회 완료' : `조회 ${fetchedAt.toLocaleString('ko-KR', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' })}`;
    } catch (error) {
      if (!disposed) stamp.textContent = `시세 조회 실패: ${error.message}`;
    } finally {
      loading = false;
      if (!disposed) { button.disabled = false; button.classList.remove('is-loading'); }
    }
  }

  container.querySelectorAll('[data-market-id]').forEach((element) => element.addEventListener('click', () => highlightMarket(element.dataset.marketId)));
  container.querySelector('#world-markets-refresh').addEventListener('click', loadQuotes);
  loadQuotes();
  const refreshTimer = window.setInterval(loadQuotes, 60_000);
  window._viewCleanup = () => { disposed = true; window.clearInterval(refreshTimer); try { chart.destroy(); } catch {} };
}
