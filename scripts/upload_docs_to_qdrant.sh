#!/usr/bin/env bash
# docs/*.md 문서를 청크/벡터화하여 Qdrant 컬렉션에 업로드합니다.
# 외부 생성형 AI 없이 해시 임베딩(384차원)을 사용합니다.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DOCS_DIR="${DOCS_DIR:-$ROOT_DIR/docs}"
VECTORDB_PORT="${VECTORDB_PORT:-6333}"
QDRANT_URL="${QDRANT_URL:-http://localhost:${VECTORDB_PORT}}"
QDRANT_COLLECTION="${QDRANT_COLLECTION:-investment_docs}"
CHUNK_SIZE="${CHUNK_SIZE:-1200}"
CHUNK_OVERLAP="${CHUNK_OVERLAP:-200}"
BATCH_SIZE="${BATCH_SIZE:-64}"

usage() {
  cat <<EOF
Usage: $(basename "$0")

환경 변수:
  DOCS_DIR            Markdown 문서 폴더 (기본: ./docs)
  QDRANT_URL          Qdrant HTTP URL (기본: http://localhost:6333)
  QDRANT_COLLECTION   컬렉션 이름 (기본: investment_docs)
  CHUNK_SIZE          문서 청크 크기(문자 수, 기본: 1200)
  CHUNK_OVERLAP       청크 오버랩(문자 수, 기본: 200)
  BATCH_SIZE          업로드 배치 크기(기본: 64)
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

for cmd in curl python3; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "[ERROR] $cmd 이(가) 설치되어 있지 않습니다."
    exit 1
  fi
done

if [[ ! -d "$DOCS_DIR" ]]; then
  echo "[ERROR] DOCS_DIR 폴더가 없습니다: $DOCS_DIR"
  exit 1
fi

echo "[STEP] Vector DB 연결/컬렉션 조회: $QDRANT_URL"
if ! curl -fsS "$QDRANT_URL/collections" >/dev/null; then
  echo "[ERROR] Qdrant 조회 실패: $QDRANT_URL/collections"
  exit 1
fi
echo "[OK] Vector DB 조회 성공"

python3 - \
  "$DOCS_DIR" "$QDRANT_URL" "$QDRANT_COLLECTION" \
  "$CHUNK_SIZE" "$CHUNK_OVERLAP" "$BATCH_SIZE" <<'PY'
import hashlib
import json
import math
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path


class QdrantHTTPError(RuntimeError):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


def http_json(method: str, url: str, payload: dict | None = None) -> dict:
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="ignore")
        raise QdrantHTTPError(exc.code, f"Qdrant API 오류({exc.code}): {method} {url} :: {err_body[:300]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Qdrant 연결 실패: {method} {url} :: {exc.reason}") from exc


def chunk_text(text: str, chunk_size: int, chunk_overlap: int) -> list[str]:
    text = text.strip()
    if not text:
        return []
    chunks: list[str] = []
    step = chunk_size - chunk_overlap
    start = 0
    while start < len(text):
        end = min(len(text), start + chunk_size)
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(text):
            break
        start += step
    return chunks


TOKEN_PATTERN = re.compile(r"[0-9A-Za-z가-힣_]+")


def hash_embed(text: str, dim: int = 384) -> list[float]:
    """의존성 없는 해시 기반 임베딩 (384차원)."""
    vec = [0.0] * dim
    tokens = TOKEN_PATTERN.findall(text.lower())
    if not tokens:
        return vec
    for token in tokens:
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        idx = int.from_bytes(digest[:4], "big") % dim
        sign = 1.0 if (digest[4] & 1) == 0 else -1.0
        vec[idx] += sign
    norm = math.sqrt(sum(v * v for v in vec))
    if norm > 0:
        vec = [v / norm for v in vec]
    return vec


# ── 인수 파싱 ─────────────────────────────────────────────────────────────────
docs_dir        = Path(sys.argv[1])
base_url        = sys.argv[2].rstrip("/")
collection      = sys.argv[3]
chunk_size      = int(sys.argv[4])
chunk_overlap   = int(sys.argv[5])
batch_size      = int(sys.argv[6])

# ── 문서 청킹 ─────────────────────────────────────────────────────────────────
files = sorted(docs_dir.glob("*.md"))
if not files:
    raise SystemExit(f"[ERROR] markdown 파일이 없습니다: {docs_dir}")

records: list[dict] = []
for file_path in files:
    content = file_path.read_text(encoding="utf-8")
    chunks = chunk_text(content, chunk_size, chunk_overlap)
    for idx, chunk in enumerate(chunks):
        records.append({"source_doc": file_path.name, "chunk_index": idx, "text": chunk})

if not records:
    raise SystemExit("[ERROR] 업로드할 문서 청크가 없습니다.")

print(f"[INFO] docs={len(files)}, chunks={len(records)}")

# ── 임베딩 ───────────────────────────────────────────────────────────────────
print("[INFO] 해시 임베딩 생성 중 (dim=384)...")
matrix = [hash_embed(r["text"]) for r in records]
embed_type = "hash/384"

dim = len(matrix[0])
print(f"[INFO] 임베딩 타입={embed_type}, dim={dim}")

# ── Qdrant 업로드 ─────────────────────────────────────────────────────────────
try:
    http_json("DELETE", f"{base_url}/collections/{collection}")
    print(f"[INFO] 기존 컬렉션 삭제: {collection}")
except QdrantHTTPError as exc:
    if exc.code != 404:
        raise

http_json(
    "PUT",
    f"{base_url}/collections/{collection}",
    {
        "vectors": {"size": dim, "distance": "Cosine"},
        "payload_schema": {},
    },
)

# 컬렉션 메타데이터에 임베딩 타입 기록 (벡터 검색에 사용할 타입 식별용)
try:
    http_json(
        "PUT",
        f"{base_url}/collections/{collection}/payload",
        {},
    )
except Exception:
    pass

for start in range(0, len(records), batch_size):
    end = min(len(records), start + batch_size)
    points = [
        {
            "id": i + 1,
            "vector": matrix[i],
            "payload": {**records[i], "embed_type": embed_type},
        }
        for i in range(start, end)
    ]
    http_json(
        "PUT",
        f"{base_url}/collections/{collection}/points?wait=true",
        {"points": points},
    )
    print(f"[INFO] upserted {end}/{len(records)}")

result = http_json("GET", f"{base_url}/collections/{collection}")
count = result.get("result", {}).get("points_count")
print(f"[OK] 업로드 완료: collection={collection}, embed_type={embed_type}, points_count={count}")
PY
