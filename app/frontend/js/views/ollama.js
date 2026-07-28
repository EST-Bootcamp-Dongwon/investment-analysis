import { api } from '../api.js';

function esc(v) {
  return String(v ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function fmtSize(gb) {
  if (gb < 0.1) return '<0.1 GB';
  return gb.toFixed(1) + ' GB';
}

function modelCard(m) {
  const isDefault = m.is_default;
  return `
    <div style="background:#0f172a; border:1px solid ${isDefault ? '#6366f1' : '#1e293b'};
        border-radius:10px; padding:14px 16px; display:flex; align-items:center; gap:14px;">
      <div style="flex:1; min-width:0;">
        <div style="display:flex; align-items:center; gap:8px; margin-bottom:4px; flex-wrap:wrap;">
          <span style="font-size:0.9rem; font-weight:700; color:#e2e8f0; word-break:break-all;">${esc(m.name)}</span>
          ${isDefault ? '<span style="font-size:0.67rem; background:#6366f1; color:#e0e7ff; padding:2px 7px; border-radius:4px; font-weight:700; flex-shrink:0;">기본</span>' : ''}
        </div>
        <div style="font-size:0.78rem; color:#64748b;">
          ${m.param_size ? `파라미터: <b style="color:#94a3b8;">${esc(m.param_size)}</b>` : ''}
          ${m.quantize  ? ` · 양자화: <b style="color:#94a3b8;">${esc(m.quantize)}</b>` : ''}
          ${m.size_gb   ? ` · 크기: <b style="color:#94a3b8;">${fmtSize(m.size_gb)}</b>` : ''}
          ${m.modified  ? ` · 갱신: <b style="color:#94a3b8;">${esc(m.modified)}</b>` : ''}
        </div>
      </div>
      <button class="btn-chat-model" data-model="${esc(m.name)}"
          style="background:#1e3a5f; color:#93c5fd; border:none; border-radius:6px;
          padding:6px 14px; font-size:0.78rem; cursor:pointer; white-space:nowrap;">
        <i class="fa-solid fa-message" style="margin-right:5px;"></i>테스트
      </button>
    </div>`;
}

function suggestedCard(s) {
  return `
    <div style="background:#0a1628; border:1px solid #1e293b; border-radius:10px;
        padding:12px 16px; display:flex; align-items:center; gap:12px;">
      <div style="flex:1;">
        <div style="font-size:0.85rem; font-weight:700; color:#94a3b8;">${esc(s.name)}</div>
        <div style="font-size:0.76rem; color:#475569; margin-top:2px;">${esc(s.desc)} · ${esc(s.size)}</div>
      </div>
      <button class="btn-pull-model" data-model="${esc(s.name)}"
          style="background:#052e16; color:#4ade80; border:1px solid #16a34a;
          border-radius:6px; padding:6px 14px; font-size:0.78rem; cursor:pointer; white-space:nowrap;">
        <i class="fa-solid fa-download" style="margin-right:5px;"></i>다운로드
      </button>
    </div>`;
}

export function ollamaView(container) {
  container.innerHTML = `
    <div style="padding:0 4px;">
      <h2 style="color:#e2e8f0; font-size:1.2rem; font-weight:800; margin-bottom:4px; display:flex; align-items:center; gap:10px;">
        <i class="fa-solid fa-microchip" style="color:#6366f1;"></i>Ollama 로컬 AI 엔진 관리
      </h2>
      <p style="color:#64748b; font-size:0.82rem; margin-bottom:20px;">
        Windows PC Ollama 서버 연결 상태 확인, 설치된 모델 조회, 모델 다운로드 및 채팅 테스트
      </p>

      <!-- Status card -->
      <div id="ol-status-card" style="background:#1e293b; border:1px solid #334155; border-radius:12px; padding:18px 22px; margin-bottom:20px;">
        <div style="display:flex; align-items:center; gap:10px; margin-bottom:4px;">
          <i class="fa-solid fa-spinner fa-spin" style="color:#6366f1;"></i>
          <span style="font-size:0.88rem; color:#94a3b8;">연결 확인 중…</span>
        </div>
      </div>

      <!-- Installed models -->
      <div id="ol-models-section" style="display:none; margin-bottom:24px;">
        <div style="font-size:0.92rem; font-weight:700; color:#e2e8f0; margin-bottom:12px; display:flex; align-items:center; gap:8px;">
          <i class="fa-solid fa-cube" style="color:#3b82f6;"></i>설치된 모델
        </div>
        <div id="ol-models-list" style="display:flex; flex-direction:column; gap:10px;"></div>
      </div>

      <!-- Suggested downloads -->
      <div id="ol-suggest-section" style="display:none; margin-bottom:24px;">
        <div style="font-size:0.92rem; font-weight:700; color:#e2e8f0; margin-bottom:12px; display:flex; align-items:center; gap:8px;">
          <i class="fa-solid fa-star" style="color:#f59e0b;"></i>추천 모델 다운로드
        </div>
        <div id="ol-suggest-list" style="display:flex; flex-direction:column; gap:10px;"></div>
        <p style="font-size:0.72rem; color:#475569; margin-top:10px;">
          ※ 다운로드는 서버측에서 실행됩니다 (Ollama pull). 용량에 따라 수 분 소요될 수 있습니다.
        </p>
      </div>

      <!-- Chat test panel -->
      <div id="ol-chat-section" style="display:none;">
        <div style="background:#1e293b; border:1px solid #334155; border-radius:12px; padding:18px 22px;">
          <div style="font-size:0.92rem; font-weight:700; color:#e2e8f0; margin-bottom:14px; display:flex; align-items:center; gap:8px;">
            <i class="fa-solid fa-comments" style="color:#6366f1;"></i>채팅 테스트
            <span id="ol-chat-model-badge" style="font-size:0.72rem; background:#1e3a5f; color:#93c5fd; padding:2px 8px; border-radius:4px;"></span>
          </div>
          <div style="display:flex; gap:10px; margin-bottom:12px; flex-wrap:wrap;">
            <select id="ol-chat-model-sel" style="background:#0f172a; color:#e2e8f0; border:1px solid #334155; border-radius:6px; padding:6px 10px; font-size:0.82rem; flex:1; min-width:160px;"></select>
            <input id="ol-system-input" type="text" placeholder="시스템 프롬프트 (선택)"
              style="background:#0f172a; color:#e2e8f0; border:1px solid #334155; border-radius:6px;
              padding:6px 12px; font-size:0.82rem; flex:2; min-width:200px;"
              value="당신은 한국 금융·투자 전문가입니다. 한국어로 답변하세요.">
          </div>
          <div style="display:flex; gap:10px; align-items:flex-end;">
            <textarea id="ol-prompt-input" rows="3" placeholder="프롬프트를 입력하세요…"
              style="background:#0f172a; color:#e2e8f0; border:1px solid #334155; border-radius:8px;
              padding:10px 14px; font-size:0.85rem; flex:1; resize:vertical; line-height:1.6;
              min-height:72px;"></textarea>
            <button id="ol-send-btn"
              style="background:#6366f1; color:#fff; border:none; border-radius:8px;
              padding:10px 20px; font-size:0.85rem; cursor:pointer; height:fit-content; white-space:nowrap;">
              <i class="fa-solid fa-paper-plane" style="margin-right:5px;"></i>전송
            </button>
          </div>
          <div id="ol-chat-response" style="margin-top:14px; display:none; background:#0a1628;
              border:1px solid #1e3a5f; border-radius:8px; padding:14px 16px; font-size:0.85rem;
              color:#cbd5e1; line-height:1.8; white-space:pre-wrap;"></div>
        </div>
      </div>
    </div>
  `;

  const statusCard   = container.querySelector('#ol-status-card');
  const modelsSection = container.querySelector('#ol-models-section');
  const modelsList   = container.querySelector('#ol-models-list');
  const suggestSection = container.querySelector('#ol-suggest-section');
  const suggestList  = container.querySelector('#ol-suggest-list');
  const chatSection  = container.querySelector('#ol-chat-section');
  const chatModelSel = container.querySelector('#ol-chat-model-sel');
  const systemInput  = container.querySelector('#ol-system-input');
  const promptInput  = container.querySelector('#ol-prompt-input');
  const sendBtn      = container.querySelector('#ol-send-btn');
  const chatResponse = container.querySelector('#ol-chat-response');
  const chatBadge    = container.querySelector('#ol-chat-model-badge');

  let _statusData = null;

  async function loadStatus() {
    try {
      const data = await api.ollamaStatus();
      _statusData = data;

      if (data.available) {
        statusCard.innerHTML = `
          <div style="display:flex; align-items:center; gap:12px; flex-wrap:wrap;">
            <div style="display:flex; align-items:center; gap:8px;">
              <span style="width:10px; height:10px; border-radius:50%; background:#4ade80; display:inline-block; flex-shrink:0;"></span>
              <span style="font-size:0.92rem; font-weight:700; color:#4ade80;">연결됨</span>
            </div>
            <div style="font-size:0.82rem; color:#64748b;">
              <b style="color:#94a3b8;">${esc(data.host)}</b>
              · 모델 ${data.model_count}개 설치
              · 기본 모델: <b style="color:#93c5fd;">${esc(data.default_model)}</b>
            </div>
          </div>`;

        // Installed models
        if (data.models.length) {
          modelsSection.style.display = 'block';
          modelsList.innerHTML = data.models.map(modelCard).join('');
          chatSection.style.display = 'block';
          chatModelSel.innerHTML = data.models.map(m =>
            `<option value="${esc(m.name)}"${m.is_default ? ' selected' : ''}>${esc(m.name)} (${fmtSize(m.size_gb)})</option>`
          ).join('');
          chatBadge.textContent = data.default_model;
        }

        // Suggested models (skip already installed)
        const installed = new Set(data.models.map(m => m.name));
        const suggestions = (data.suggested_pull || []).filter(s => !installed.has(s.name));
        if (suggestions.length) {
          suggestSection.style.display = 'block';
          suggestList.innerHTML = suggestions.map(suggestedCard).join('');
        }

      } else {
        statusCard.innerHTML = `
          <div style="display:flex; align-items:center; gap:8px;">
            <span style="width:10px; height:10px; border-radius:50%; background:#ef4444; display:inline-block;"></span>
            <span style="font-size:0.92rem; font-weight:700; color:#ef4444;">연결 실패</span>
            <span style="font-size:0.82rem; color:#64748b;">— ${esc(data.host)}</span>
          </div>
          <p style="font-size:0.8rem; color:#7f1d1d; margin:8px 0 0; padding-top:8px; border-top:1px solid #2d1a1a;">
            Ollama가 Windows PC에서 실행 중인지 확인하세요. 포트(${esc(data.host?.split(':').pop() || '11435')})가 방화벽에서 허용되어야 합니다.
          </p>`;
      }
    } catch (e) {
      statusCard.innerHTML = `
        <div style="color:#ef4444; font-size:0.88rem;">
          <i class="fa-solid fa-circle-exclamation" style="margin-right:6px;"></i>상태 조회 실패: ${esc(e.message)}
        </div>`;
    }
  }

  // Pull model click
  container.addEventListener('click', async (e) => {
    const pullBtn = e.target.closest('.btn-pull-model');
    if (pullBtn) {
      const model = pullBtn.dataset.model;
      pullBtn.disabled = true;
      pullBtn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> 다운로드 중…';
      try {
        const res = await api.ollamaPull({ model });
        pullBtn.innerHTML = `<i class="fa-solid fa-check"></i> 완료`;
        pullBtn.style.background = '#052e16';
        pullBtn.style.color = '#4ade80';
        setTimeout(() => loadStatus(), 2000);
      } catch (err) {
        pullBtn.disabled = false;
        pullBtn.innerHTML = `<i class="fa-solid fa-circle-exclamation"></i> 실패`;
        pullBtn.style.color = '#f87171';
      }
    }

    const chatBtn = e.target.closest('.btn-chat-model');
    if (chatBtn) {
      const model = chatBtn.dataset.model;
      chatModelSel.value = model;
      chatBadge.textContent = model;
      chatSection.style.display = 'block';
      chatSection.scrollIntoView({ behavior: 'smooth', block: 'start' });
      promptInput.focus();
    }
  });

  chatModelSel.addEventListener('change', () => {
    chatBadge.textContent = chatModelSel.value;
  });

  sendBtn.addEventListener('click', async () => {
    const prompt = promptInput.value.trim();
    if (!prompt) return;

    sendBtn.disabled = true;
    sendBtn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i>';
    chatResponse.style.display = 'block';
    chatResponse.textContent = '생성 중…';

    try {
      const data = await api.ollamaChat({
        model:  chatModelSel.value,
        system: systemInput.value.trim(),
        prompt,
      });
      chatResponse.textContent = data.response || '(응답 없음)';
    } catch (err) {
      chatResponse.style.color = '#f87171';
      chatResponse.textContent = '오류: ' + err.message;
    } finally {
      sendBtn.disabled = false;
      sendBtn.innerHTML = '<i class="fa-solid fa-paper-plane" style="margin-right:5px;"></i>전송';
    }
  });

  promptInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) sendBtn.click();
  });

  loadStatus();
}
