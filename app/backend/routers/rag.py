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
    pass


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


@router.post("/api/rag/search")
def rag_search(req: RagSearchRequest) -> dict[str, object]:
    """Qdrant에서 관련 문서 청크를 검색합니다."""
    _require_qdrant()
    chunks = _search(req.query, req.top_k, req.score_threshold)
    return {"query": req.query, "embed_method": "hash", "count": len(chunks), "results": chunks}


@router.post("/api/rag/ask")
def rag_ask(req: RagAskRequest) -> dict[str, object]:
    """생성 모델 없이 관련 문서 검색 결과를 반환합니다."""
    _require_qdrant()
    chunks = _search(req.query, req.top_k, req.score_threshold)
    return {
        "query": req.query,
        "answer": "관련 학습 문서를 찾았습니다. 아래 출처와 내용을 확인하세요.",
        "embed_method": "hash",
        "sources": chunks,
        "source_count": len(chunks),
    }


@router.get("/api/rag/status")
def rag_status() -> dict[str, object]:
    """Qdrant 연결 상태와 컬렉션 정보를 반환합니다."""
    available = _qdrant_available()
    info: dict[str, object] = {}
    if available:
        try:
            result = _qdrant_request("GET", f"/collections/{_QDRANT_COLLECTION}").get("result", {})
            info = {
                "points_count": result.get("points_count", 0),
                "vector_size": result.get("config", {}).get("params", {}).get("vectors", {}).get("size"),
                "status": result.get("status", "unknown"),
            }
        except Exception:
            info = {"error": "컬렉션이 없거나 조회 실패"}
    return {"qdrant": {"available": available, "url": _QDRANT_URL, "collection": _QDRANT_COLLECTION, **info}, "embed_method": "hash"}
