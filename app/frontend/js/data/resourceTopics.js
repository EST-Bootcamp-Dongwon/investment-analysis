/**
 * resourceTopics.js — 리포지토리 docs/*.md 학습 주제를 기반으로 한 공통 토픽 목록.
 * 유튜브 검색, 위키백과 검색 등 외부 자료 페이지에서 공유해서 사용한다.
 */
export const RESOURCE_TOPICS = [
  { category: '세무·회계',   label: '법인세·소득세 기초',            query: '법인세 소득세 기초 회계' },
  { category: '거시경제',    label: '금리와 경제지표 분석',          query: '기준금리 경제지표 분석' },
  { category: '거시경제',    label: '산업 분석 방법론',              query: '산업분석 방법론 주식투자' },
  { category: '재무제표',    label: '손익계산서·대차대조표 읽는 법', query: '손익계산서 대차대조표 읽는법' },
  { category: '재무제표',    label: '현금흐름표와 기업가치평가',     query: '현금흐름표 기업가치평가' },
  { category: '밸류에이션',  label: 'PER·PBR 등 상대가치 평가',      query: 'PER PBR 밸류에이션 멀티플' },
  { category: '기술적분석',  label: '추세·보조지표 분석',            query: '기술적분석 이동평균 RSI' },
  { category: '기술적분석',  label: '엘리어트 파동·차트 패턴',        query: '엘리어트 파동이론 차트패턴' },
  { category: '금융상품',    label: '주식·배당·ETF 기초',            query: '주식 배당 ETF 기초' },
  { category: '금융상품',    label: '자본시장법과 금융상품 분류',     query: '자본시장법 금융상품 분류' },
  { category: '포트폴리오',  label: '포트폴리오 이론과 자산배분',     query: '포트폴리오 이론 자산배분 마코위츠' },
  { category: '리스크관리',  label: 'VaR와 리스크 관리',              query: 'Value at Risk 리스크관리' },
  { category: '퀀트/ML',     label: '퀀트 트레이딩과 백테스트',       query: '퀀트 트레이딩 백테스트 전략' },
  { category: '퀀트/ML',     label: 'LSTM·Transformer 시계열 예측',   query: 'LSTM Transformer 주가 시계열 예측' },
];

/** docs/*.md에서 자동 생성한 학습 자료 목록 (NotebookLM 안내와 학습 메뉴가 공유). */
export { LEARN_DOCS } from './learnDocs.js';
