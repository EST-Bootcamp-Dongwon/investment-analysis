from __future__ import annotations

import hashlib
import json
import math
import os
import re
import urllib.error
import urllib.request

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter()

_QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
_QDRANT_COLLECTION = os.getenv("QDRANT_COLLECTION", "investment_docs")


def _qdrant_request(method: str, path: str, payload: dict | None = None) -> dict:
    url = _QDRANT_URL.rstrip("/") + path
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data, method=method, headers=headers), timeout=10) as response:
            body = response.read().decode("utf-8")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        raise HTTPException(502, f"Qdrant 오류({exc.code}): {detail[:200]}") from exc
    except urllib.error.URLError as exc:
        raise HTTPException(503, f"Qdrant 연결 실패 ({_QDRANT_URL}): {exc.reason}") from exc


def _qdrant_available() -> bool:
    try:
        with urllib.request.urlopen(_QDRANT_URL.rstrip("/") + "/collections", timeout=3) as response:
            return response.status == 200
    except Exception:
        return False


def _qdrant_collection_available() -> bool:
    """Qdrant 연결과 컬렉션 생성 여부를 분리해 확인한다."""
    try:
        with urllib.request.urlopen(f"{_QDRANT_URL.rstrip('/')}/collections/{_QDRANT_COLLECTION}", timeout=3) as response:
            return response.status == 200
    except Exception:
        return False


def _hash_embed(text: str, dim: int = 384) -> list[float]:
    """문서 색인 스크립트와 동일한 의존성 없는 384차원 해시 임베딩."""
    vector = [0.0] * dim
    for token in re.findall(r"[0-9A-Za-z가-힣_]+", text.lower()):
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        vector[int.from_bytes(digest[:4], "big") % dim] += 1.0 if digest[4] & 1 == 0 else -1.0
    norm = math.sqrt(sum(value * value for value in vector))
    return [value / norm for value in vector] if norm else vector


def _embed_query(text: str) -> list[float]:
    try:
        info = _qdrant_request("GET", f"/collections/{_QDRANT_COLLECTION}")
        dim = int(info.get("result", {}).get("config", {}).get("params", {}).get("vectors", {}).get("size", 384))
    except Exception:
        dim = 384
    return _hash_embed(text, dim)


class RagSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000, description="검색 질문")
    top_k: int = Field(default=5, ge=1, le=20, description="반환할 최대 청크 수")
    score_threshold: float = Field(default=0.0, ge=0.0, le=1.0, description="최소 유사도 점수")


class RagAskRequest(RagSearchRequest):
    provider: str = Field(default="rag", pattern="^(rag|openai_compatible)$", description="답변 다듬기에 사용할 외부 AI 모듈")


def _search(query: str, top_k: int, score_threshold: float) -> list[dict[str, object]]:
    payload: dict[str, object] = {"vector": _embed_query(query), "limit": top_k, "with_payload": True, "with_vector": False}
    if score_threshold > 0:
        payload["score_threshold"] = score_threshold
    result = _qdrant_request("POST", f"/collections/{_QDRANT_COLLECTION}/points/search", payload)
    return [{
        "score": round(hit.get("score", 0), 4),
        "source_doc": hit.get("payload", {}).get("source_doc", ""),
        "chunk_index": hit.get("payload", {}).get("chunk_index", 0),
        "text": hit.get("payload", {}).get("text", ""),
    } for hit in result.get("result", [])]


def _require_qdrant() -> None:
    if not _qdrant_available():
        raise HTTPException(503, f"Qdrant 서버에 연결할 수 없습니다 ({_QDRANT_URL}). 서버를 실행한 뒤 문서를 색인하세요.")
    if not _qdrant_collection_available():
        raise HTTPException(
            503,
            "문서 검색용 컬렉션이 아직 없습니다. Docker Compose 환경에서는 "
            "`docker compose --profile tools run --rm docs-index`로 학습 문서를 먼저 색인하세요.",
        )


def _rag_only_answer(chunks: list[dict[str, object]]) -> str:
    """검색된 원문만 잘라 정리한다. 생성 모델이나 외부 지식은 사용하지 않는다."""
    if not chunks:
        return "관련 문서를 찾지 못했습니다. 다른 표현으로 질문해 보세요."
    excerpts = []
    for chunk in chunks[:3]:
        text = " ".join(str(chunk.get("text", "")).split())
        if text:
            excerpts.append(f"• {text[:500]}{'…' if len(text) > 500 else ''}")
    return "문서에서 찾은 관련 내용입니다. 오른쪽 원문과 함께 확인하세요.\n\n" + "\n\n".join(excerpts)


def _openai_compatible_answer(query: str, chunks: list[dict[str, object]]) -> str:
    """RAG 원문만 근거로 외부 OpenAI 호환 모델이 답변을 다듬도록 한다."""
    api_key = os.getenv("RAG_LLM_API_KEY")
    model = os.getenv("RAG_LLM_MODEL")
    base_url = os.getenv("RAG_LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    if not api_key or not model:
        raise HTTPException(503, "외부 AI를 사용하려면 RAG_LLM_API_KEY와 RAG_LLM_MODEL을 설정하세요.")

    context = "\n\n".join(
        f"[출처 {index + 1}: {chunk.get('source_doc', '')} / 조각 {int(chunk.get('chunk_index', 0)) + 1}]\n{chunk.get('text', '')}"
        for index, chunk in enumerate(chunks)
    )[:14000]
    prompt = (
        "아래 '검색 원문'만 근거로 사용자의 질문에 한국어로 간결하게 답하세요. "
        "원문에 없는 사실·숫자·투자 조언을 추가하지 말고, 정보가 부족하면 부족하다고 밝히세요. "
        "출처 번호를 [출처 1]처럼 표시하고 3개 이내의 짧은 문단 또는 목록으로 정리하세요.\n\n"
        f"사용자 질문: {query}\n\n검색 원문:\n{context}"
    )
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "당신은 제공된 RAG 문서만 다듬어 설명하는 도우미입니다."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.1,
    }
    request = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
        answer = str(result.get("choices", [{}])[0].get("message", {}).get("content", "")).strip()
        if not answer:
            raise ValueError("빈 응답")
        return answer
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        raise HTTPException(502, f"외부 AI 응답 오류({exc.code}): {detail[:200]}") from exc
    except (urllib.error.URLError, ValueError, KeyError, IndexError) as exc:
        raise HTTPException(502, f"외부 AI 응답을 받지 못했습니다: {exc}") from exc


@router.post("/api/rag/search")
def rag_search(req: RagSearchRequest) -> dict[str, object]:
    """Qdrant에서 관련 문서 청크를 검색합니다."""
    _require_qdrant()
    chunks = _search(req.query, req.top_k, req.score_threshold)
    return {"query": req.query, "embed_method": "hash", "count": len(chunks), "results": chunks}


@router.post("/api/rag/ask")
def rag_ask(req: RagAskRequest) -> dict[str, object]:
    """RAG 검색 결과만으로 답변을 만들고, 선택 시 외부 AI로 문장만 다듬습니다."""
    _require_qdrant()
    chunks = _search(req.query, req.top_k, req.score_threshold)
    answer = _rag_only_answer(chunks) if req.provider == "rag" else _openai_compatible_answer(req.query, chunks)
    return {
        "query": req.query,
        "answer": answer,
        "provider": req.provider,
        "embed_method": "hash",
        "sources": chunks,
        "source_count": len(chunks),
    }


@router.get("/api/rag/status")
def rag_status() -> dict[str, object]:
    """Qdrant 연결 상태와 컬렉션 정보를 반환합니다."""
    available = _qdrant_available()
    collection_available = available and _qdrant_collection_available()
    info: dict[str, object] = {}
    if collection_available:
        try:
            result = _qdrant_request("GET", f"/collections/{_QDRANT_COLLECTION}").get("result", {})
            info = {
                "points_count": result.get("points_count", 0),
                "vector_size": result.get("config", {}).get("params", {}).get("vectors", {}).get("size"),
                "status": result.get("status", "unknown"),
            }
        except Exception:
            info = {"error": "컬렉션이 없거나 조회 실패"}
    return {
        "qdrant": {"available": available, "collection_available": collection_available, "url": _QDRANT_URL, "collection": _QDRANT_COLLECTION, **info},
        "external_ai": {"openai_compatible_available": bool(os.getenv("RAG_LLM_API_KEY") and os.getenv("RAG_LLM_MODEL"))},
        "embed_method": "hash",
    }
