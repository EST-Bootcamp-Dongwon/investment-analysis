const HOME_MARKETS = [
  { id: 'kospi', name: 'KOSPI', ticker: '^KS11', color: '#0078d4' },
  { id: 'kosdaq', name: 'KOSDAQ', ticker: '^KQ11', color: '#8b5cf6' },
  { id: 'nasdaq', name: 'NASDAQ', ticker: '^IXIC', color: '#0f766e' },
  { id: 'sp500', name: 'S&P 500', ticker: '^GSPC', color: '#d97706' },
];

const PERIODS = [['1mo', '1M'], ['3mo', '3M'], ['6mo', '6M'], ['1y', '1Y']];
const UPWARD_COLOR = '#e11d48';
const DOWNWARD_COLOR = '#2563eb';

const US_MEGA_CAPS = [
  { ticker: 'AAPL',  name: 'Apple' },
  { ticker: 'MSFT',  name: 'Microsoft' },
  { ticker: 'GOOGL', name: 'Alphabet' },
  { ticker: 'AMZN',  name: 'Amazon' },
  { ticker: 'NVDA',  name: 'NVIDIA' },
  { ticker: 'META',  name: 'Meta' },
  { ticker: 'TSLA',  name: 'Tesla' },
];

const KOREAN_BLUE_CHIPS = [
  { ticker: '005930.KS', name: '삼성전자' },
  { ticker: '000660.KS', name: 'SK하이닉스' },
  { ticker: '373220.KS', name: 'LG에너지솔루션' },
  { ticker: '207940.KS', name: '삼성바이오로직스' },
  { ticker: '005380.KS', name: '현대차' },
];

function chartCard(market) {
  return `
    <section class="home-market-card" data-market-card="${market.id}">
      <header class="home-market-card-head">
        <div>
          <div class="home-market-name">${market.name} <span>· ${market.ticker}</span></div>
          <div class="home-market-price-row">
            <strong data-price="${market.id}">--</strong>
            <em data-change="${market.id}">--</em>
          </div>
        </div>
        <div class="home-market-periods" data-periods="${market.id}">
          ${PERIODS.map(([value, label]) => `<button type="button" data-period="${value}" class="${value === '3mo' ? 'active' : ''}">${label}</button>`).join('')}
        </div>
      </header>
      <div class="home-market-chart-wrap">
        <div class="home-market-chart" data-chart="${market.id}"></div>
        <div class="home-market-loading" data-loading="${market.id}"><i class="fa-solid fa-spinner fa-spin"></i> 데이터 불러오는 중…</div>
      </div>
      <footer class="home-market-foot">
        <span><i class="fa-solid fa-chart-line"></i> 일봉 · MA20 · 거래량</span>
        <span data-source="${market.id}"></span>
      </footer>
    </section>`;
}

function quotePanel(title, subtitle, items, region) {
  return `
    <section class="home-quote-panel" aria-label="${title} 대표 종목 시세">
      <header class="home-quote-panel-head">
        <div>
          <h2>${title}</h2>
          <p>${subtitle}</p>
        </div>
        <span class="home-quote-count">${items.length}종목</span>
      </header>
      <div class="home-quote-list" data-quote-list="${region}">
        ${items.map((item) => `
          <div class="home-quote-row is-loading" data-quote="${item.ticker}">
            <div class="home-quote-name"><strong>${item.name}</strong><span>${item.ticker}</span></div>
            <div class="home-quote-value"><b>--</b><em>조회 중</em></div>
          </div>`).join('')}
      </div>
    </section>`;
}

function formatQuoteValue(value, region) {
  if (!Number.isFinite(value)) return '--';
  return region === 'kr'
    ? `${Math.round(value).toLocaleString()}원`
    : `$${value.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

export function homeView(container) {
  container.innerHTML = `
    <div class="home-dashboard" id="home-dashboard">
      <div class="home-market-grid">${HOME_MARKETS.map(chartCard).join('')}</div>
      <section class="home-quote-dashboard" aria-labelledby="home-quote-title">
        <header class="home-quote-dashboard-head">
          <div>
            <h1 id="home-quote-title">대표 기업 주가</h1>
            <p>미국 매그니피센트 7과 한국 대표 코스피 5종목의 현재가·전일 대비입니다.</p>
          </div>
          <div class="home-quote-actions">
            <span id="home-quote-stamp">조회 전</span>
            <button type="button" id="home-quote-refresh"><i class="fa-solid fa-rotate-right"></i> 새로고침</button>
          </div>
        </header>
        <div class="home-quote-grid">
          ${quotePanel('미국', 'Magnificent Seven · USD', US_MEGA_CAPS, 'us')}
          ${quotePanel('한국', '대표 KOSPI 기업 · KRW', KOREAN_BLUE_CHIPS, 'kr')}
        </div>
        <p class="home-quote-note">Yahoo Finance 기준이며, 장중 시세는 지연되거나 시장이 닫힌 경우 마지막 거래 가격일 수 있습니다.</p>
      </section>
    </div>`;

  const charts = new Map();
  const periods = new Map(HOME_MARKETS.map((market) => [market.id, '3mo']));
  let quoteAbortController = null;

  function destroyChart(id) {
    const chart = charts.get(id);
    if (chart) {
      try { chart.destroy(); } catch {}
      charts.delete(id);
    }
  }

  async function loadChart(market) {
    const id = market.id;
    const card = container.querySelector(`[data-market-card="${id}"]`);
    const chartEl = card.querySelector(`[data-chart="${id}"]`);
    const loading = card.querySelector(`[data-loading="${id}"]`);
    const price = card.querySelector(`[data-price="${id}"]`);
    const change = card.querySelector(`[data-change="${id}"]`);
    const source = card.querySelector(`[data-source="${id}"]`);
    loading.style.display = 'flex';
    destroyChart(id);

    try {
      const response = await fetch(`/api/home/market-candle?market=${encodeURIComponent(id)}&period=${periods.get(id)}`);
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      const ohlcv = data.ohlcv || [];
      if (!ohlcv.length) throw new Error('데이터 없음');

      const last = ohlcv.at(-1);
      const previousClose = ohlcv.length > 1 ? ohlcv.at(-2).c : last.o;
      const changePercent = ((last.c / previousClose) - 1) * 100;
      const isUp = changePercent >= 0;
      price.textContent = last.c.toLocaleString(undefined, { maximumFractionDigits: 2 });
      change.textContent = `${isUp ? '▲' : '▼'} ${Math.abs(changePercent).toFixed(2)}%`;
      change.className = isUp ? 'is-up' : 'is-down';
      source.textContent = data.is_simulated ? '시뮬레이션 데이터' : 'Yahoo Finance · 15분 지연';
      source.classList.toggle('is-simulated', Boolean(data.is_simulated));

      const candles = ohlcv.map((point) => ({
        x: new Date(point.date).getTime(), y: [point.o, point.h, point.l, point.c],
      }));
      const ma20 = ohlcv.map((point, index) => ({
        x: new Date(point.date).getTime(),
        y: index < 19 ? null : ohlcv.slice(index - 19, index + 1).reduce((sum, item) => sum + item.c, 0) / 20,
      }));
      const volume = ohlcv.map((point) => ({
        x: new Date(point.date).getTime(), y: point.v || 0,
        fillColor: point.c >= point.o ? UPWARD_COLOR : DOWNWARD_COLOR,
      }));

      const chart = new ApexCharts(chartEl, {
        chart: { type: 'candlestick', height: 250, toolbar: { show: false }, zoom: { enabled: false }, animations: { enabled: false }, background: '#fff', fontFamily: 'Pretendard, -apple-system, "Malgun Gothic", sans-serif' },
        series: [
          { name: market.name, type: 'candlestick', data: candles },
          { name: 'MA20', type: 'line', data: ma20 },
          { name: '거래량', type: 'bar', data: volume },
        ],
        plotOptions: { candlestick: { colors: { upward: UPWARD_COLOR, downward: DOWNWARD_COLOR }, wick: { useFillColor: true } }, bar: { columnWidth: '65%' } },
        colors: [UPWARD_COLOR, market.color, '#94a3b8'],
        stroke: { curve: 'smooth', width: [1, 1.7, 0] },
        xaxis: { type: 'datetime', labels: { format: 'MM-dd', style: { fontSize: '10px', colors: '#94a3b8' }, hideOverlappingLabels: true, datetimeUTC: false }, axisBorder: { show: false }, axisTicks: { show: false } },
        yaxis: [{ labels: { formatter: (value) => value ? Math.round(value).toLocaleString() : '', style: { fontSize: '10px', colors: '#94a3b8' } } }, { show: false }, { show: false }],
        grid: { borderColor: '#eef2f7', strokeDashArray: 3, padding: { right: 10, left: 4 } },
        tooltip: { shared: false, x: { format: 'yyyy-MM-dd' }, y: { formatter: (value) => value == null ? '' : Number(value).toLocaleString(undefined, { maximumFractionDigits: 2 }) } },
        legend: { show: false },
      });
      charts.set(id, chart);
      await chart.render();
      loading.style.display = 'none';
    } catch (error) {
      loading.innerHTML = `<span class="home-market-error">데이터 오류: ${error.message}</span>`;
    }
  }

  HOME_MARKETS.forEach((market) => {
    container.querySelector(`[data-periods="${market.id}"]`).addEventListener('click', (event) => {
      const button = event.target.closest('[data-period]');
      if (!button) return;
      periods.set(market.id, button.dataset.period);
      container.querySelectorAll(`[data-periods="${market.id}"] [data-period]`).forEach((item) => item.classList.toggle('active', item === button));
      loadChart(market);
    });
    loadChart(market);
  });

  async function loadQuotes() {
    const refreshButton = container.querySelector('#home-quote-refresh');
    const stamp = container.querySelector('#home-quote-stamp');
    quoteAbortController?.abort();
    quoteAbortController = new AbortController();
    refreshButton.disabled = true;
    refreshButton.classList.add('is-loading');
    stamp.textContent = '시세 조회 중…';

    try {
      const tickers = [...US_MEGA_CAPS, ...KOREAN_BLUE_CHIPS].map((item) => item.ticker);
      const response = await fetch('/api/market/snapshot', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ tickers }),
        signal: quoteAbortController.signal,
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      const quoteByTicker = new Map((data.items || []).map((item) => [item.ticker, item]));

      [['us', US_MEGA_CAPS], ['kr', KOREAN_BLUE_CHIPS]].forEach(([region, stocks]) => {
        stocks.forEach((stock) => {
          const row = container.querySelector(`[data-quote="${stock.ticker}"]`);
          const quote = quoteByTicker.get(stock.ticker);
          const value = row.querySelector('.home-quote-value b');
          const change = row.querySelector('.home-quote-value em');
          row.classList.remove('is-loading');
          if (!quote || quote.status !== 'ok') {
            row.classList.add('is-error');
            value.textContent = '조회 불가';
            change.textContent = '잠시 후 재시도';
            return;
          }
          const changePct = Number(quote.change_pct) || 0;
          const isUp = changePct >= 0;
          row.classList.remove('is-error');
          value.textContent = formatQuoteValue(Number(quote.value), region);
          change.textContent = `${isUp ? '▲' : '▼'} ${Math.abs(changePct).toFixed(2)}%`;
          change.className = isUp ? 'is-up' : 'is-down';
        });
      });
      const fetchedAt = data.fetched_at ? new Date(data.fetched_at) : null;
      stamp.textContent = fetchedAt && !Number.isNaN(fetchedAt.valueOf())
        ? `조회 ${fetchedAt.toLocaleTimeString('ko-KR', { hour: '2-digit', minute: '2-digit' })}`
        : '조회 완료';
    } catch (error) {
      if (error.name !== 'AbortError') stamp.textContent = '시세 조회 실패';
    } finally {
      refreshButton.disabled = false;
      refreshButton.classList.remove('is-loading');
    }
  }

  container.querySelector('#home-quote-refresh').addEventListener('click', loadQuotes);
  loadQuotes();

  window._viewCleanup = () => {
    quoteAbortController?.abort();
    charts.forEach((chart) => {
      try { chart.destroy(); } catch {}
    });
  };
}
