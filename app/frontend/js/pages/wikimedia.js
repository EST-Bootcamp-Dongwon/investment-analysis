/**
 * pages/wikimedia.js — 저장소 학습 주제에 맞춘 위키백과(한국어) 검색 바로가기.
 * 정확한 문서 제목을 확신할 수 없는 경우가 많아, 존재를 보장할 수 없는 특정
 * /wiki/문서명 링크 대신 항상 유효한 위키백과 검색 결과 URL로 안내한다.
 */
import { RESOURCE_TOPICS } from '../data/resourceTopics.js';

function wikipediaSearchUrl(query) {
  return `https://ko.wikipedia.org/w/index.php?search=${encodeURIComponent(query)}&title=Special:검색`;
}

function render() {
  const el = document.getElementById('page-content');
  if (!el) return;

  const cards = RESOURCE_TOPICS.map((t) => `
    <div class="card resource-card">
      <div class="resource-category">${t.category}</div>
      <h3>${t.label}</h3>
      <p>"${t.query}" 관련 개념을 위키백과에서 찾아봅니다.</p>
      <a class="resource-link" target="_blank" rel="noopener noreferrer" href="${wikipediaSearchUrl(t.query)}">
        <i class="fa-brands fa-wikipedia-w"></i> 위키백과에서 검색
      </a>
    </div>
  `).join('');

  el.innerHTML = `
    <div class="resource-hero">
      <h1><i class="fa-brands fa-wikipedia-w"></i> 위키백과 참고자료</h1>
      <p>
        이 저장소의 학습 주제와 연결된 한국어 위키백과 검색 바로가기입니다.
        용어 정의, 배경 이론을 빠르게 확인하는 참고용이며, 투자 판단의 근거 자료로는
        <code>docs/*.md</code> 학습 문서와 DART 등 1차 데이터를 우선하세요.
      </p>
    </div>
    <div class="grid-3">${cards}</div>
  `;
}

render();
