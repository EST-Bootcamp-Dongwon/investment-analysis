const EXAMPLES = ['PER과 PBR의 차이를 알려줘', 'ETF 괴리율은 왜 생기나요?', '지정가 주문과 시장가 주문의 차이는?', '분산투자의 목적은 무엇인가요?'];

function escapeHtml(value) {
  return String(value).replace(/[&<>'"]/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[char]));
}

function formatText(value) {
  return escapeHtml(value).replace(/\n/g, '<br>');
}

export function ragChatView(app) {
  const messages = [];
  let sources = [];
  let provider = 'rag';
  let externalAiAvailable = false;

  function renderSources() {
    if (!sources.length) {
      return '<div class="rag-source-empty"><i class="fa-solid fa-file-lines"></i><p>질문을 보내면 RAG가 찾은 원문 문서 조각이 여기에 표시됩니다.</p></div>';
    }
    return sources.map((source, index) => `
      <article class="rag-source-card">
        <div class="rag-source-head"><strong>출처 ${index + 1}</strong><span>유사도 ${Number(source.score).toFixed(3)}</span></div>
        <p class="rag-source-name">${escapeHtml(source.source_doc)} · 문서 조각 ${Number(source.chunk_index) + 1}</p>
        <p class="rag-source-text">${formatText(source.text)}</p>
      </article>`).join('');
  }

  function render() {
    app.innerHTML = `
      <section class="card rag-chat-heading">
        <div class="rag-chat-heading-copy">
          <h2><i class="fa-solid fa-comments"></i>문서 검색 채팅</h2>
          <p>RAG가 학습 문서에서 관련 조각을 찾습니다. 답변은 검색된 원문만 정리하며, 원문은 오른쪽에서 확인할 수 있습니다.</p>
        </div>
        <div class="rag-chat-controls">
          <label for="rag-provider">답변 다듬기</label>
          <select id="rag-provider" class="param-input" aria-label="외부 AI 사용 모듈 선택">
            <option value="rag" ${provider === 'rag' ? 'selected' : ''}>사용 안 함 · RAG만</option>
            <option value="openai_compatible" ${provider === 'openai_compatible' ? 'selected' : ''} ${externalAiAvailable ? '' : 'disabled'}>외부 AI · OpenAI 호환</option>
          </select>
          <small id="rag-provider-note">${externalAiAvailable ? '외부 AI를 선택해도 검색 원문만 전달해 문장을 다듬습니다.' : '외부 AI가 설정되지 않아 RAG 검색 결과만 사용합니다.'}</small>
          <span id="rag-status" class="badge badge-gray">연결 확인 중</span>
        </div>
      </section>
      <section class="rag-chat-layout">
        <section class="card rag-chat-panel">
          <div id="rag-messages" class="rag-messages" aria-live="polite">
            ${messages.length ? messages.map((message) => `<div class="rag-message is-${message.role}">${formatText(message.text)}</div>`).join('') : '<div class="rag-welcome"><strong>무엇이 궁금한가요?</strong><br>예: “ETF 괴리율은 왜 생기나요?”</div>'}
          </div>
          <div class="rag-chat-compose">
            <div class="rag-examples">${EXAMPLES.map((example) => `<button type="button" class="btn btn-secondary btn-sm rag-example" data-query="${escapeHtml(example)}">${escapeHtml(example)}</button>`).join('')}</div>
            <form id="rag-form" class="rag-form">
              <input id="rag-input" maxlength="500" placeholder="문서에서 찾을 질문을 입력하세요" aria-label="문서 검색 질문" />
              <button class="btn btn-primary" type="submit"><i class="fa-solid fa-paper-plane"></i> 질문</button>
            </form>
          </div>
        </section>
        <aside class="card rag-source-panel" aria-label="RAG 검색 원문">
          <div class="rag-source-title"><div><h3><i class="fa-solid fa-book-open"></i>RAG 검색 원문</h3><p>채팅 답변의 근거가 된 문서 조각입니다.</p></div><span>${sources.length ? `${sources.length}개` : '대기 중'}</span></div>
          <div id="rag-sources" class="rag-sources">${renderSources()}</div>
        </aside>
      </section>`;

    const messagesEl = app.querySelector('#rag-messages');
    messagesEl.scrollTop = messagesEl.scrollHeight;
    app.querySelector('#rag-provider').addEventListener('change', (event) => { provider = event.target.value; });
    app.querySelectorAll('.rag-example').forEach((button) => button.addEventListener('click', () => ask(button.dataset.query)));
    app.querySelector('#rag-form').addEventListener('submit', (event) => {
      event.preventDefault();
      const input = app.querySelector('#rag-input');
      ask(input.value.trim());
    });
    updateStatus();
  }

  async function updateStatus() {
    try {
      const response = await fetch('/api/rag/status');
      const data = await response.json();
      const status = app.querySelector('#rag-status');
      const providerSelect = app.querySelector('#rag-provider');
      const providerNote = app.querySelector('#rag-provider-note');
      if (!status || !providerSelect || !providerNote) return;
      externalAiAvailable = Boolean(data.external_ai?.openai_compatible_available);
      const externalOption = providerSelect.querySelector('option[value="openai_compatible"]');
      externalOption.disabled = !externalAiAvailable;
      if (!externalAiAvailable && provider === 'openai_compatible') {
        provider = 'rag';
        providerSelect.value = 'rag';
      }
      providerNote.textContent = externalAiAvailable
        ? '외부 AI를 선택해도 검색 원문만 전달해 문장을 다듬습니다.'
        : '외부 AI가 설정되지 않아 RAG 검색 결과만 사용합니다.';
      if (data.qdrant?.collection_available) {
        status.className = 'badge badge-green';
        status.textContent = `문서 ${Number(data.qdrant.points_count || 0).toLocaleString()}개 청크 연결됨`;
      } else if (data.qdrant?.available) {
        status.className = 'badge badge-gray';
        status.textContent = '문서 색인 필요';
        providerNote.textContent = '학습 문서가 아직 색인되지 않았습니다. 관리자에게 문서 색인을 요청하세요.';
      } else status.textContent = '벡터 DB 연결 안 됨';
    } catch {
      const status = app.querySelector('#rag-status');
      if (status) status.textContent = 'RAG 상태 확인 실패';
    }
  }

  async function ask(query) {
    if (!query) return;
    messages.push({ role: 'user', text: query }, { role: 'assistant loading', text: '문서에서 찾는 중…' });
    render();
    try {
      const response = await fetch('/api/rag/ask', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ query, top_k: 5, provider }),
      });
      const data = await response.json();
      messages.pop();
      if (!response.ok) throw new Error(data.detail || '문서 검색에 실패했습니다.');
      sources = data.sources || [];
      messages.push({ role: 'assistant', text: data.answer || '관련 문서를 찾지 못했습니다.' });
    } catch (error) {
      messages.pop();
      messages.push({ role: 'error', text: error.message || '문서 검색에 실패했습니다.' });
    }
    render();
  }

  render();
}
