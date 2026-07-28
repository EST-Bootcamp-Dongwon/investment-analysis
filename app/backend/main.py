from __future__ import annotations

import base64
import io
import json
import os
import re
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")

# ─── Ollama Configuration ─────────────────────────────────────────────────────
OLLAMA_HOST  = os.getenv("OLLAMA_HOST",  "http://172.29.32.1:11435")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3:latest")
_OLLAMA_TIMEOUT = 120  # seconds


def _ollama_request(endpoint: str, payload: dict, timeout: int = _OLLAMA_TIMEOUT) -> dict:
    """Send a JSON request to the Ollama API and return parsed response."""
    url  = OLLAMA_HOST.rstrip("/") + endpoint
    data = json.dumps(payload).encode()
    req  = urllib.request.Request(url, data=data,
                                  headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.URLError as exc:
        raise HTTPException(status_code=503,
                            detail=f"Ollama 연결 실패 ({OLLAMA_HOST}): {exc.reason}") from exc
    except Exception as exc:
        raise HTTPException(status_code=503,
                            detail=f"Ollama 오류: {exc}") from exc


def _ollama_available() -> bool:
    """Quick connectivity check — returns False instead of raising."""
    try:
        url = OLLAMA_HOST.rstrip("/") + "/api/tags"
        with urllib.request.urlopen(url, timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def _ollama_models() -> list[dict]:
    """Return list of locally installed Ollama models."""
    try:
        url = OLLAMA_HOST.rstrip("/") + "/api/tags"
        with urllib.request.urlopen(url, timeout=5) as r:
            return json.loads(r.read()).get("models", [])
    except Exception:
        return []


def _ollama_chat(
    model: str,
    system_prompt: str,
    user_prompt: str,
    temperature: float = 0.3,
    num_predict: int = 800,
) -> str:
    """Call Ollama /api/chat and return assistant text content."""
    result = _ollama_request(
        "/api/chat",
        {
            "model":  model,
            "stream": False,
            "options": {"temperature": temperature, "num_predict": num_predict},
            "messages": [
                {"role": "system",  "content": system_prompt},
                {"role": "user",    "content": user_prompt},
            ],
        },
    )
    return (result.get("message") or {}).get("content", "").strip()


def _build_financial_analysis_prompt(
    corp_name: str, market: str, bsns_year: str,
    financials: dict, ratios: dict, score: float, grade: str,
) -> tuple[str, str]:
    """Return (system_prompt, user_prompt) for Ollama financial analysis."""
    system_prompt = (
        "당신은 한국 상장기업 재무분석 전문가입니다. "
        "DART 전자공시 데이터를 기반으로 투자자가 이해하기 쉬운 한국어로 "
        "재무 건전성 분석과 투자 의견을 제시합니다. "
        "답변은 반드시 한국어로, 명확하고 구체적으로 작성하세요."
    )

    def fmt(v: object, unit: str = "억원") -> str:
        if v is None:
            return "N/A"
        return f"{float(v):,.1f}{unit}"

    def fpct(v: object) -> str:
        if v is None:
            return "N/A"
        return f"{float(v):.1f}%"

    user_prompt = f"""다음 기업의 재무 데이터를 분석해주세요.

【기업 정보】
- 기업명: {corp_name}
- 상장시장: {market}
- 분석 연도: {bsns_year}년

【재무 현황】 (단위: 억원)
- 매출액: {fmt(financials.get('revenue'))} (전기: {fmt(financials.get('prev_revenue'))}, YoY {fpct(ratios.get('revenue_growth'))})
- 영업이익: {fmt(financials.get('op_income'))} (영업이익률 {fpct(ratios.get('op_margin'))})
- 당기순이익: {fmt(financials.get('net_income'))} (순이익률 {fpct(ratios.get('net_margin'))})
- 자산총계: {fmt(financials.get('total_assets'))}
- 부채총계: {fmt(financials.get('total_liabilities'))} (부채비율 {fpct(ratios.get('debt_equity_ratio'))})
- 자본총계: {fmt(financials.get('total_equity'))}
- 유동비율: {fpct(ratios.get('current_ratio'))}
- ROE: {fpct(ratios.get('roe'))} / ROA: {fpct(ratios.get('roa'))}
- 재무 건전성 점수: {score:.0f}/100점 (등급: {grade})

다음 5개 항목으로 나누어 분석해주세요:
① 재무 건전성 종합 평가 (2~3문장)
② 수익성 분석 (1~2문장)
③ 안정성 분석 (1~2문장)
④ 성장성 분석 (1~2문장)
⑤ 투자 의견: 반드시 "매수", "중립", "매도" 중 하나를 명시하고 근거를 1~2문장으로 작성"""

    return system_prompt, user_prompt

from bson import ObjectId
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from pymongo.errors import DuplicateKeyError

try:
    from .db import get_db
except ImportError:
    from db import get_db

try:
    import orjson
except ImportError:
    DEFAULT_RESPONSE_CLASS = JSONResponse
else:
    class FastORJSONResponse(Response):
        media_type = "application/json"

        def render(self, content: object) -> bytes:
            return orjson.dumps(content)

    DEFAULT_RESPONSE_CLASS = FastORJSONResponse

ROOT_DIR = Path(__file__).resolve().parents[2]
FRONTEND_DIR = ROOT_DIR / "app" / "frontend"
GENERATED_DIR = ROOT_DIR / "app" / "generated"
QUIZ_SQL_PATH = ROOT_DIR / "app" / "backend" / "quiz_seed.sql"
DOCS_DIR = ROOT_DIR / "docs"
GENERATED_DIR.mkdir(parents=True, exist_ok=True)
_MATPLOTLIB_FONT_CONFIGURED = False

def _learn_document_map() -> dict[str, Path]:
    """Expose exactly the Markdown files shipped in docs/, without traversal."""
    return {
        path.stem: path
        for path in DOCS_DIR.glob("*.md")
        if re.fullmatch(r"[A-Za-z0-9_-]+", path.stem)
    }


def configure_matplotlib_korean_font(plt) -> None:
    """Use an installed Korean font when Matplotlib renders Korean labels."""
    global _MATPLOTLIB_FONT_CONFIGURED
    if _MATPLOTLIB_FONT_CONFIGURED:
        return

    import matplotlib.font_manager as fm

    candidates = [
        Path("/usr/share/fonts/truetype/nanum/NanumGothic.ttf"),
        Path("/usr/share/fonts/truetype/nanum/NanumBarunGothic.ttf"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
    ]
    for font_path in candidates:
        if font_path.exists():
            fm.fontManager.addfont(str(font_path))
            font_name = fm.FontProperties(fname=str(font_path)).get_name()
            plt.rcParams["font.family"] = font_name
            break
    plt.rcParams["axes.unicode_minus"] = False
    _MATPLOTLIB_FONT_CONFIGURED = True


app = FastAPI(
    title="Python Education Cloud API",
    version="2.0.0",
    description=(
        "교육용 ML/DL API 서버 | Educational ML/DL API server. "
        "Supports: Cross-Validation, Decision Boundary, Random Forest, "
        "KMeans Clustering, SVM, MLP Neural Network, Linear/Polynomial Regression, "
        "Text Classification (NLP), OpenCV Animation, HuggingFace Diffusion, "
        "1D CNN Time Series, LSTM Predictor, Transformer Time Series."
    ),
    default_response_class=DEFAULT_RESPONSE_CLASS,
)

app.add_middleware(GZipMiddleware, minimum_size=1024)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def no_cache_static_assets(request, call_next):
    """StaticFiles only sets ETag/Last-Modified, so browsers could otherwise
    keep serving a pre-deploy JS/CSS file after a redeploy. no-store forbids
    the browser from caching these responses at all, so every load fetches
    the current deploy instead of depending on cache revalidation."""
    response = await call_next(request)
    if not request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


class CrossValidationRequest(BaseModel):
    n_samples: int = Field(default=1000, ge=200, le=20000)
    n_features: int = Field(default=10, ge=2, le=100)
    cv: int = Field(default=5, ge=3, le=10)


class RandomForestRequest(BaseModel):
    test_size: float = Field(default=0.3, ge=0.1, le=0.5)


class CircleAnimationRequest(BaseModel):
    width: int = Field(default=512, ge=128, le=1920)
    height: int = Field(default=512, ge=128, le=1080)
    fps: int = Field(default=30, ge=10, le=60)


class KMeansRequest(BaseModel):
    n_samples: int = Field(default=400, ge=100, le=5000)
    n_clusters: int = Field(default=4, ge=2, le=10)
    cluster_std: float = Field(default=0.8, ge=0.1, le=3.0)


class SVMRequest(BaseModel):
    kernel: str = Field(default="rbf", pattern="^(rbf|linear|poly)$")
    C: float = Field(default=1.0, ge=0.01, le=100.0)


class MLPRequest(BaseModel):
    hidden_layers: str = Field(default="128,64,32", pattern=r"^\d+(,\d+)*$")
    max_iter: int = Field(default=300, ge=50, le=1000)
    n_samples: int = Field(default=1000, ge=200, le=10000)


class LinearRegressionRequest(BaseModel):
    degree: int = Field(default=1, ge=1, le=5)
    n_samples: int = Field(default=200, ge=50, le=2000)
    noise: float = Field(default=3.0, ge=0.0, le=20.0)


class TextClassifyRequest(BaseModel):
    texts: list[str] = Field(default=["The team played amazing hockey tonight!"])
    max_features: int = Field(default=5000, ge=500, le=20000)


class DiffusionRequest(BaseModel):
    prompt: str = Field(default="A futuristic city skyline at sunset")
    height: int = Field(default=512, ge=256, le=1024)
    width: int = Field(default=512, ge=256, le=1024)
    guidance_scale: float = Field(default=8.0, ge=1.0, le=15.0)


class BacktestRequest(BaseModel):
    fast: int = Field(default=5, ge=2, le=50, description="단기 이동평균 기간")
    slow: int = Field(default=20, ge=5, le=200, description="장기 이동평균 기간")
    n_days: int = Field(default=1260, ge=252, le=3780, description="시뮬레이션 일수 (252=1년)")
    annual_return: float = Field(default=0.08, ge=-0.2, le=0.5)
    annual_vol: float = Field(default=0.20, ge=0.05, le=1.0)
    risk_free: float = Field(default=0.03, ge=0.0, le=0.1)


class PortfolioRequest(BaseModel):
    n_portfolios: int = Field(default=3000, ge=500, le=10000)
    risk_free: float = Field(default=0.03, ge=0.0, le=0.1)


class RiskRequest(BaseModel):
    annual_vol: float = Field(default=0.25, ge=0.05, le=1.0)
    capital: float = Field(default=10_000_000, ge=1_000_000, le=1_000_000_000)
    risk_pct: float = Field(default=0.01, ge=0.001, le=0.05)
    confidence: float = Field(default=0.95, ge=0.90, le=0.99)
    atr_multiplier: float = Field(default=2.0, ge=1.0, le=5.0)


class TVAlertRequest(BaseModel):
    action: str = Field(default="buy", pattern="^(buy|sell)$")
    ticker: str = Field(default="AAPL", max_length=20)
    price: float = Field(default=0.0, ge=0.0)
    rsi: float | None = Field(default=None)


class CNNTimeSeriesRequest(BaseModel):
    window: int = Field(default=20, ge=10, le=60, description="입력 창 길이 (거래일)")
    n_samples: int = Field(default=2000, ge=500, le=10000, description="합성 데이터 샘플 수")
    epochs: int = Field(default=20, ge=5, le=50, description="학습 에폭 수")


class LSTMRequest(BaseModel):
    seq_len: int = Field(default=30, ge=10, le=60, description="LSTM 입력 시퀀스 길이")
    n_days: int = Field(default=2000, ge=500, le=5000, description="시뮬레이션 일수")
    hidden_size: int = Field(default=64, ge=16, le=256, description="LSTM 은닉 유닛 수")
    epochs: int = Field(default=30, ge=10, le=80, description="학습 에폭 수")


class TransformerTSRequest(BaseModel):
    seq_len: int = Field(default=40, ge=20, le=80, description="인코더 입력 창 길이")
    pred_steps: int = Field(default=5, ge=1, le=10, description="예측 스텝 수")
    d_model: int = Field(default=32, ge=16, le=128, description="Transformer d_model 차원")
    epochs: int = Field(default=30, ge=10, le=80, description="학습 에폭 수")


class DartCompanySearchRequest(BaseModel):
    company_name: str = Field(default="삼성전자", min_length=1, max_length=80)
    limit: int = Field(default=10, ge=1, le=30)


class GroupNetworkRequest(BaseModel):
    group_name: str = Field(default="삼성", min_length=1, max_length=50)
    limit: int = Field(default=80, ge=1, le=100)


class DartCompanyListRequest(BaseModel):
    region: str = Field(default="서울특별시", max_length=30)
    emp_min: int | None = Field(default=None, ge=0, le=1_000_000)
    emp_max: int | None = Field(default=None, ge=0, le=1_000_000)
    bsns_year: str = Field(default="2024", pattern=r"^\d{4}$")
    limit: int = Field(default=50, ge=1, le=200)


@app.get("/api/health")
def health_check() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/learn/doc/{doc_id}")
def get_learn_doc(doc_id: str) -> dict[str, str]:
    target = _learn_document_map().get(doc_id)
    if not target:
        raise HTTPException(status_code=404, detail="지원하지 않는 학습 문서입니다.")
    return {"doc_id": doc_id, "file": target.name, "content": target.read_text(encoding="utf-8")}


def _dart_api_key() -> str:
    key = os.getenv("DART_API_KEY") or os.getenv("OPENDART_API_KEY")
    if not key:
        raise HTTPException(
            status_code=503,
            detail="DART_API_KEY 또는 OPENDART_API_KEY 환경변수를 설정하세요.",
        )
    return key


@lru_cache(maxsize=1)
def _load_dart_corp_codes() -> list[dict[str, str]]:
    key = _dart_api_key()
    url = "https://opendart.fss.or.kr/api/corpCode.xml?" + urllib.parse.urlencode({"crtfc_key": key})
    try:
        with urllib.request.urlopen(url, timeout=20) as response:
            payload = response.read()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"DART 회사코드 목록 수신 실패: {exc}") from exc

    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as zf:
            xml_name = zf.namelist()[0]
            xml_bytes = zf.read(xml_name)
    except zipfile.BadZipFile as exc:
        message = payload[:200].decode("utf-8", errors="ignore")
        raise HTTPException(status_code=502, detail=f"DART 응답을 해석할 수 없습니다: {message}") from exc

    root = ET.fromstring(xml_bytes)
    rows: list[dict[str, str]] = []
    for item in root.findall("list"):
        corp_code = (item.findtext("corp_code") or "").strip()
        corp_name = (item.findtext("corp_name") or "").strip()
        stock_code = (item.findtext("stock_code") or "").strip()
        modify_date = (item.findtext("modify_date") or "").strip()
        if corp_code and corp_name:
            rows.append({
                "corp_code": corp_code,
                "corp_name": corp_name,
                "stock_code": stock_code,
                "modify_date": modify_date,
            })
    return rows


@lru_cache(maxsize=4096)
def _resolve_krx_yahoo_ticker(stock_code: str) -> dict[str, object]:
    if not stock_code:
        return {"ticker": None, "candidates": []}

    candidates = [f"{stock_code}.KS", f"{stock_code}.KQ"]
    found: list[str] = []
    for ticker in candidates:
        chart_url = (
            "https://query1.finance.yahoo.com/v8/finance/chart/"
            + urllib.parse.quote(ticker)
            + "?range=5d&interval=1d"
        )
        try:
            with urllib.request.urlopen(chart_url, timeout=4) as response:
                text = response.read(5000).decode("utf-8", errors="ignore")
            if '"regularMarketPrice"' in text or '"timestamp"' in text:
                found.append(ticker)
        except Exception:
            continue

    return {"ticker": found[0] if found else f"{stock_code}.KS", "candidates": found or candidates}


@app.post("/api/dart/company-search")
def dart_company_search(req: DartCompanySearchRequest) -> dict[str, object]:
    query = req.company_name.strip()
    normalized = query.replace(" ", "").lower()
    if not normalized:
        raise HTTPException(status_code=400, detail="회사명을 입력하세요.")

    rows = _load_dart_corp_codes()
    listed = [row for row in rows if row["stock_code"]]
    exact = [row for row in listed if row["corp_name"].replace(" ", "").lower() == normalized]
    partial = [row for row in listed if normalized in row["corp_name"].replace(" ", "").lower()]
    matches = (exact + [row for row in partial if row not in exact])[: req.limit]

    results = []
    for row in matches:
        ticker_info = _resolve_krx_yahoo_ticker(row["stock_code"])
        results.append({
            **row,
            "ticker": ticker_info["ticker"],
            "ticker_candidates": ticker_info["candidates"],
            "display": f'{ticker_info["ticker"] or row["stock_code"]}, {row["corp_name"]}',
        })

    return {
        "query": query,
        "count": len(results),
        "results": results,
        "source": "OpenDART corpCode.xml",
        "notes": [
            "DART 회사코드 목록에서 상장기업(stock_code 보유 기업)만 검색합니다.",
            "DART는 .KS/.KQ suffix를 제공하지 않아 조회 가능한 Yahoo ticker 후보로 보완 표시합니다.",
        ],
    }


@lru_cache(maxsize=6)
def _pykrx_market_data(ref_date: str) -> dict[str, dict]:
    """Return {stock_code: {market, market_cap, close}} using pykrx.

    ref_date is "YYYYMMDD" and acts as the LRU cache key so that data is
    refreshed automatically each new trading day.  Failures are silently
    swallowed so that group-network still returns results even when pykrx
    cannot reach KRX servers.
    """
    try:
        from pykrx import stock as krx

        result: dict[str, dict] = {}

        for mkt in ("KOSPI", "KOSDAQ"):
            try:
                cap_df = krx.get_market_cap_by_ticker(ref_date, market=mkt)
                for ticker, row in cap_df.iterrows():
                    result[ticker] = {
                        "market":     mkt,
                        "market_cap": int(row.get("시가총액", 0)),
                        "close":      int(row.get("종가", 0)),
                    }
            except Exception:
                pass

        return result
    except Exception:
        return {}


@app.post("/api/dart/group-network")
def dart_group_network(req: GroupNetworkRequest) -> dict[str, object]:
    """Search DART listed companies by group/conglomerate keyword and enrich
    each match with live market-cap data from pykrx."""
    import datetime

    query = req.group_name.strip()
    normalized = query.replace(" ", "").lower()
    if not normalized:
        raise HTTPException(status_code=400, detail="그룹명을 입력하세요.")

    rows = _load_dart_corp_codes()
    listed = [row for row in rows if row["stock_code"]]

    # Exact name prefix matches first, then partial matches
    exact_codes = {r["corp_code"] for r in listed if r["corp_name"].replace(" ", "").lower().startswith(normalized)}
    exact = [r for r in listed if r["corp_code"] in exact_codes]
    partial = [r for r in listed if normalized in r["corp_name"].replace(" ", "").lower()
               and r["corp_code"] not in exact_codes]
    matches = (exact + partial)[: req.limit]

    # Try today first; fall back to yesterday for weekends / holidays
    today = datetime.date.today()
    ref_date = today.strftime("%Y%m%d")
    market_data = _pykrx_market_data(ref_date)
    if not market_data:
        yesterday = (today - datetime.timedelta(days=1)).strftime("%Y%m%d")
        market_data = _pykrx_market_data(yesterday)

    results = []
    for row in matches:
        sc = row["stock_code"]
        mkt_info = market_data.get(sc, {})
        market_label = mkt_info.get("market", "기타")
        results.append({
            "corp_code":   row["corp_code"],
            "corp_name":   row["corp_name"],
            "stock_code":  sc,
            "modify_date": row["modify_date"],
            "market":      market_label,
            "market_cap":  mkt_info.get("market_cap", 0),
            "close":       mkt_info.get("close", 0),
            "dart_url":    f"https://dart.fss.or.kr/corp/main.do?corp_code={row['corp_code']}",
        })

    # Sort by market cap descending so flagship companies appear first
    results.sort(key=lambda x: x["market_cap"], reverse=True)

    total_market_cap = sum(r["market_cap"] for r in results)
    kospi_count  = sum(1 for r in results if r["market"] == "KOSPI")
    kosdaq_count = sum(1 for r in results if r["market"] == "KOSDAQ")

    return {
        "query":            query,
        "count":            len(results),
        "total_market_cap": total_market_cap,
        "kospi_count":      kospi_count,
        "kosdaq_count":     kosdaq_count,
        "results":          results,
        "source":           "OpenDART corpCode.xml + pykrx KRX 시장데이터",
    }


@lru_cache(maxsize=10000)
def _fetch_company_detail(corp_code: str) -> dict:
    """Fetch company overview (address, bizr_no) from DART company.json."""
    key = _dart_api_key()
    url = ("https://opendart.fss.or.kr/api/company.json?"
           + urllib.parse.urlencode({"crtfc_key": key, "corp_code": corp_code}))
    try:
        with urllib.request.urlopen(url, timeout=8) as response:
            data = json.loads(response.read())
        return data if data.get("status") == "000" else {}
    except Exception:
        return {}


@lru_cache(maxsize=1)
def _load_all_listed_details() -> list[dict]:
    """Batch-fetch company detail for all listed companies. Cached in-process.

    First call may take ~30 s; subsequent calls are instant.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    rows = _load_dart_corp_codes()
    listed = [r for r in rows if r["stock_code"]]

    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=30) as executor:
        future_to_row = {
            executor.submit(_fetch_company_detail, r["corp_code"]): r
            for r in listed
        }
        for future in as_completed(future_to_row):
            row = future_to_row[future]
            detail = future.result()
            if detail:
                results.append({
                    "corp_code":   row["corp_code"],
                    "corp_name":   detail.get("corp_name") or row["corp_name"],
                    "stock_code":  row["stock_code"],
                    "modify_date": row["modify_date"],
                    "bizr_no":     detail.get("bizr_no", ""),
                    "jurir_no":    detail.get("jurir_no", ""),
                    "adres":       detail.get("adres", ""),
                    "ceo_nm":      detail.get("ceo_nm", ""),
                    "corp_cls":    detail.get("corp_cls", ""),
                    "est_dt":      detail.get("est_dt", ""),
                })
    return results


@lru_cache(maxsize=20000)
def _fetch_emp_count(corp_code: str, bsns_year: str) -> int:
    """Return total employee count from DART empSttus.json. Returns -1 on failure."""
    key = _dart_api_key()
    url = ("https://opendart.fss.or.kr/api/empSttus.json?"
           + urllib.parse.urlencode({
               "crtfc_key":  key,
               "corp_code":  corp_code,
               "bsns_year":  bsns_year,
               "reprt_code": "11011",  # 사업보고서
           }))
    try:
        with urllib.request.urlopen(url, timeout=8) as response:
            data = json.loads(response.read())
        if data.get("status") != "000":
            return -1

        def to_int(v: object) -> int:
            try:
                return int(str(v).replace(",", "").strip() or "0")
            except (ValueError, TypeError):
                return 0

        items = data.get("list", [])
        if not items:
            return -1

        total_rows = [x for x in items if "합계" in str(x.get("nm", ""))]
        target = total_rows or items[:1]
        total = sum(to_int(x.get("rgllbr_co", 0)) + to_int(x.get("cnttk_co", 0)) for x in target)
        if total > 0:
            return total
        return to_int(items[0].get("jan_blyy_empcnt", -1)) or -1
    except Exception:
        return -1


@app.post("/api/dart/company-list")
def dart_company_list(req: DartCompanyListRequest) -> dict[str, object]:
    """Search listed companies by region (address substring) and/or employee count."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    region = req.region.strip()
    use_emp = req.emp_min is not None or req.emp_max is not None

    all_companies = _load_all_listed_details()

    candidates = (
        [c for c in all_companies if region in c.get("adres", "")]
        if region else list(all_companies)
    )

    if use_emp:
        with ThreadPoolExecutor(max_workers=20) as executor:
            futures = {
                executor.submit(_fetch_emp_count, c["corp_code"], req.bsns_year): c
                for c in candidates
            }
            filtered: list[dict] = []
            for future in as_completed(futures):
                company = futures[future]
                emp = future.result()
                if emp < 0:
                    continue
                if req.emp_min is not None and emp < req.emp_min:
                    continue
                if req.emp_max is not None and emp > req.emp_max:
                    continue
                filtered.append({**company, "emp_count": emp})
    else:
        filtered = [{**c, "emp_count": None} for c in candidates]

    results = sorted(filtered, key=lambda x: x["corp_name"])[: req.limit]

    return {
        "region":        region or None,
        "emp_min":       req.emp_min,
        "emp_max":       req.emp_max,
        "bsns_year":     req.bsns_year,
        "total_matched": len(filtered),
        "count":         len(results),
        "results":       results,
        "source":        "OpenDART company.json" + (" + empSttus.json" if use_emp else ""),
    }


@app.post("/api/ml/cross-validation")
def cross_validation(req: CrossValidationRequest) -> dict[str, object]:
    import numpy as np
    from sklearn.datasets import make_classification
    from sklearn.linear_model import LogisticRegression
    from sklearn.model_selection import cross_val_score

    X, y = make_classification(
        n_samples=req.n_samples,
        n_features=req.n_features,
        n_informative=max(2, req.n_features // 2),
        n_redundant=max(1, req.n_features // 5),
        n_classes=2,
        random_state=42,
    )

    model = LogisticRegression(max_iter=1000)
    scores = cross_val_score(model, X, y, cv=req.cv)
    return {
        "fold_scores": [float(s) for s in scores],
        "mean_accuracy": float(np.mean(scores)),
        "std_accuracy": float(np.std(scores)),
    }


@app.get("/api/ml/decision-boundary")
def decision_boundary_image() -> dict[str, str]:
    import matplotlib
    import numpy as np
    from sklearn.datasets import make_classification
    from sklearn.linear_model import LogisticRegression
    from sklearn.model_selection import train_test_split

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    X, y = make_classification(
        n_samples=240,
        n_features=2,
        n_redundant=0,
        n_informative=2,
        n_clusters_per_class=1,
        random_state=42,
    )
    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.3, random_state=42)

    model = LogisticRegression()
    model.fit(X_train, y_train)

    x_min, x_max = X[:, 0].min() - 1, X[:, 0].max() + 1
    y_min, y_max = X[:, 1].min() - 1, X[:, 1].max() + 1
    xx, yy = np.meshgrid(np.linspace(x_min, x_max, 400), np.linspace(y_min, y_max, 400))
    Z = model.predict(np.c_[xx.ravel(), yy.ravel()]).reshape(xx.shape)

    fig, ax = plt.subplots(figsize=(7, 5))
    ax.contourf(xx, yy, Z, alpha=0.3, cmap=plt.cm.coolwarm)
    ax.scatter(X_train[:, 0], X_train[:, 1], c=y_train, marker="o", edgecolors="k", label="Train")
    ax.scatter(X_test[:, 0], X_test[:, 1], c=y_test, marker="x", label="Test")
    ax.set_title("Decision Boundary")
    ax.legend()
    ax.grid(True)

    buffer = io.BytesIO()
    fig.tight_layout()
    fig.savefig(buffer, format="png", dpi=140)
    plt.close(fig)
    image_b64 = base64.b64encode(buffer.getvalue()).decode("utf-8")

    return {"image_base64": image_b64}


@app.post("/api/ml/random-forest")
def random_forest(req: RandomForestRequest) -> dict[str, object]:
    import pandas as pd
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.metrics import accuracy_score, classification_report
    from sklearn.model_selection import train_test_split

    data = {
        "age": [22, 45, 33, 35, 52, 23, 43, 56, 48, 29, 33, 53, 56, 58, 29],
        "monthly_spend": [10, 200, 100, 150, 300, 15, 180, 400, 250, 35, 150, 300, 15, 180, 99],
        "months_active": [1, 36, 24, 30, 60, 2, 33, 72, 50, 5, 33, 72, 50, 5, 12],
        "churn": [1, 0, 0, 0, 0, 1, 0, 0, 0, 1, 1, 0, 0, 0, 1],
    }

    df = pd.DataFrame(data)
    X = df[["age", "monthly_spend", "months_active"]]
    y = df["churn"]

    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=req.test_size, random_state=42)

    model = RandomForestClassifier(n_estimators=100, random_state=42)
    model.fit(X_train, y_train)
    y_pred = model.predict(X_test)

    report = classification_report(y_test, y_pred, output_dict=True)
    return {
        "accuracy": float(accuracy_score(y_test, y_pred)),
        "report": report,
    }


@app.post("/api/cv/circle-animation")
def circle_animation(req: CircleAnimationRequest) -> dict[str, str]:
    import cv2
    import numpy as np

    output_path = GENERATED_DIR / "circle_animation.mp4"

    writer = cv2.VideoWriter(
        str(output_path), cv2.VideoWriter_fourcc(*"mp4v"), req.fps, (req.width, req.height)
    )
    for radius in list(np.linspace(20, min(req.width, req.height) // 3, 30)) + list(
        np.linspace(min(req.width, req.height) // 3, 20, 30)
    ):
        image = np.zeros((req.height, req.width, 3), dtype=np.uint8)
        image[:] = (255, 0, 0)
        cv2.circle(image, (req.width // 2, req.height // 2), int(radius), (255, 255, 255), -1)
        writer.write(image)

    writer.release()
    return {"video_url": "/files/circle_animation.mp4"}


@app.post("/api/ml/kmeans")
def kmeans_clustering(req: KMeansRequest) -> dict[str, object]:
    import matplotlib
    import numpy as np
    from sklearn.cluster import KMeans
    from sklearn.datasets import make_blobs
    from sklearn.metrics import silhouette_score
    from sklearn.preprocessing import StandardScaler

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    X, _ = make_blobs(
        n_samples=req.n_samples,
        centers=req.n_clusters,
        cluster_std=req.cluster_std,
        random_state=42,
    )
    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X)

    kmeans = KMeans(n_clusters=req.n_clusters, random_state=42, n_init=10)
    labels = kmeans.fit_predict(X_scaled)
    centers = kmeans.cluster_centers_
    sil = float(silhouette_score(X_scaled, labels))

    # Elbow data
    inertias = []
    ks = list(range(2, min(req.n_clusters + 4, 10)))
    for ki in ks:
        km = KMeans(n_clusters=ki, random_state=42, n_init=10)
        km.fit(X_scaled)
        inertias.append(float(km.inertia_))

    fig, axes = plt.subplots(1, 2, figsize=(12, 5))
    axes[0].scatter(X_scaled[:, 0], X_scaled[:, 1], c=labels, cmap="tab10", alpha=0.7, s=12)
    axes[0].scatter(centers[:, 0], centers[:, 1], c="red", marker="X", s=200, zorder=5)
    axes[0].set_title(f"KMeans (k={req.n_clusters})  Silhouette={sil:.3f}")
    axes[0].grid(True)
    axes[1].plot(ks, inertias, "bo-")
    axes[1].set_xlabel("k")
    axes[1].set_ylabel("Inertia")
    axes[1].set_title("Elbow Method")
    axes[1].grid(True)
    plt.tight_layout()

    buffer = io.BytesIO()
    fig.savefig(buffer, format="png", dpi=120)
    plt.close(fig)

    return {
        "image_base64": base64.b64encode(buffer.getvalue()).decode(),
        "silhouette_score": sil,
        "inertia": float(kmeans.inertia_),
        "elbow": {"ks": ks, "inertias": inertias},
    }


@app.post("/api/ml/svm")
def svm_classifier(req: SVMRequest) -> dict[str, object]:
    import matplotlib
    import numpy as np
    from sklearn.datasets import make_classification
    from sklearn.inspection import DecisionBoundaryDisplay
    from sklearn.metrics import accuracy_score, classification_report
    from sklearn.model_selection import train_test_split
    from sklearn.preprocessing import StandardScaler
    from sklearn.svm import SVC

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    X, y = make_classification(
        n_samples=300, n_features=2, n_redundant=0, n_informative=2,
        n_clusters_per_class=1, random_state=42,
    )
    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X)
    X_train, X_test, y_train, y_test = train_test_split(X_scaled, y, test_size=0.3, random_state=42)

    svm = SVC(kernel=req.kernel, C=req.C, gamma="scale", random_state=42)
    svm.fit(X_train, y_train)
    y_pred = svm.predict(X_test)
    report = classification_report(y_test, y_pred, output_dict=True)
    accuracy = float(accuracy_score(y_test, y_pred))
    n_support = int(np.sum(svm.n_support_))

    fig, ax = plt.subplots(figsize=(7, 5))
    DecisionBoundaryDisplay.from_estimator(
        svm, X_scaled, ax=ax, alpha=0.3, cmap=plt.cm.coolwarm, response_method="predict"
    )
    ax.scatter(X_train[:, 0], X_train[:, 1], c=y_train, cmap=plt.cm.coolwarm, edgecolors="k", s=25, label="Train")
    ax.scatter(X_test[:, 0], X_test[:, 1], c=y_test, cmap=plt.cm.coolwarm, marker="^", edgecolors="k", s=25, label="Test")
    ax.scatter(
        svm.support_vectors_[:, 0], svm.support_vectors_[:, 1],
        s=100, linewidth=1.5, facecolors="none", edgecolors="k", label="Support Vectors",
    )
    ax.set_title(f"SVM ({req.kernel} kernel, C={req.C})  Acc={accuracy:.3f}")
    ax.legend(fontsize=8)
    ax.grid(True)
    plt.tight_layout()

    buffer = io.BytesIO()
    fig.savefig(buffer, format="png", dpi=140)
    plt.close(fig)

    return {
        "image_base64": base64.b64encode(buffer.getvalue()).decode(),
        "accuracy": accuracy,
        "n_support_vectors": n_support,
        "report": report,
    }


@app.post("/api/ml/mlp")
def mlp_classifier(req: MLPRequest) -> dict[str, object]:
    import matplotlib
    from sklearn.datasets import make_classification
    from sklearn.metrics import accuracy_score, classification_report
    from sklearn.model_selection import train_test_split
    from sklearn.neural_network import MLPClassifier
    from sklearn.preprocessing import StandardScaler

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    hidden_layers = tuple(int(x) for x in req.hidden_layers.split(","))

    X, y = make_classification(
        n_samples=req.n_samples, n_features=10, n_informative=6,
        n_redundant=2, n_classes=2, random_state=42,
    )
    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X)
    X_train, X_test, y_train, y_test = train_test_split(X_scaled, y, test_size=0.2, random_state=42)

    mlp = MLPClassifier(
        hidden_layer_sizes=hidden_layers, activation="relu", solver="adam",
        max_iter=req.max_iter, random_state=42,
    )
    mlp.fit(X_train, y_train)
    y_pred = mlp.predict(X_test)
    accuracy = float(accuracy_score(y_test, y_pred))
    report = classification_report(y_test, y_pred, output_dict=True)

    fig, ax = plt.subplots(figsize=(8, 4))
    ax.plot(mlp.loss_curve_, linewidth=2, color="#2563eb")
    ax.set_xlabel("Iteration")
    ax.set_ylabel("Loss")
    ax.set_title(f"MLP Loss Curve  (layers={hidden_layers})  Acc={accuracy:.3f}")
    ax.grid(True)
    plt.tight_layout()

    buffer = io.BytesIO()
    fig.savefig(buffer, format="png", dpi=140)
    plt.close(fig)

    return {
        "image_base64": base64.b64encode(buffer.getvalue()).decode(),
        "accuracy": accuracy,
        "n_iterations": mlp.n_iter_,
        "report": report,
    }


@app.post("/api/ml/linear-regression")
def linear_regression(req: LinearRegressionRequest) -> dict[str, object]:
    import matplotlib
    import numpy as np
    from sklearn.linear_model import LinearRegression as LR
    from sklearn.metrics import mean_squared_error, r2_score
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import PolynomialFeatures

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    rng = np.random.default_rng(42)
    X = rng.uniform(0, 10, size=(req.n_samples, 1))
    # True underlying function: mixture so poly can shine
    y = sum(
        c * X.ravel() ** i
        for i, c in enumerate([10, -4, 0.5][: req.degree + 1])
    ) + rng.normal(0, req.noise, req.n_samples)

    model = make_pipeline(
        PolynomialFeatures(degree=req.degree, include_bias=False),
        LR(),
    )
    model.fit(X, y)
    y_pred = model.predict(X)
    r2 = float(r2_score(y, y_pred))
    mse = float(mean_squared_error(y, y_pred))

    X_plot = np.linspace(0, 10, 200).reshape(-1, 1)
    y_plot = model.predict(X_plot)

    fig, ax = plt.subplots(figsize=(8, 5))
    ax.scatter(X, y, alpha=0.4, s=15, label="Data")
    ax.plot(X_plot, y_plot, "r-", linewidth=2, label=f"Poly degree={req.degree}")
    ax.set_title(f"Regression (degree={req.degree})  R²={r2:.3f}  MSE={mse:.2f}")
    ax.set_xlabel("X")
    ax.set_ylabel("y")
    ax.legend()
    ax.grid(True)
    plt.tight_layout()

    buffer = io.BytesIO()
    fig.savefig(buffer, format="png", dpi=140)
    plt.close(fig)

    return {
        "image_base64": base64.b64encode(buffer.getvalue()).decode(),
        "r2_score": r2,
        "mse": mse,
        "degree": req.degree,
    }


@app.post("/api/nlp/text-classify")
def text_classify(req: TextClassifyRequest) -> dict[str, object]:
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline

    # Inline bilingual training corpus — sports vs. politics
    _TRAIN_TEXTS = [
        # Sports (label 0)
        "The hockey team scored three goals in the final period to win the championship.",
        "Basketball playoffs begin next week with exciting matchups across the league.",
        "The pitcher threw a no-hitter in yesterday's baseball game.",
        "Football season kicks off with record-breaking ticket sales.",
        "The swimmer broke the world record in the 100m freestyle at the Olympics.",
        "Tennis star wins Grand Slam after coming back from injury.",
        "Ice hockey league announces expansion teams in new cities.",
        "The marathon runner finished in record time despite difficult conditions.",
        "Soccer World Cup qualifying matches start this weekend.",
        "Gold medals were awarded in gymnastics and rowing at the games.",
        # Politics (label 1)
        "The senator proposed a new budget policy for healthcare reform.",
        "Parliament voted on the controversial immigration bill last night.",
        "The president signed an executive order on environmental regulations.",
        "Political debates are heating up ahead of the upcoming election.",
        "The government announced a new economic stimulus package.",
        "Opposition leaders called for an emergency session of parliament.",
        "Tax reform legislation passed the Senate with bipartisan support.",
        "Foreign policy discussions dominated the United Nations summit.",
        "The prime minister addressed the nation about the economic crisis.",
        "Congressional hearings on data privacy began on Monday.",
    ]
    _TRAIN_LABELS = [0] * 10 + [1] * 10
    _LABEL_NAMES = ["rec.sport", "politics"]

    pipe = make_pipeline(
        TfidfVectorizer(max_features=req.max_features, ngram_range=(1, 2), stop_words="english"),
        LogisticRegression(max_iter=1000),
    )
    pipe.fit(_TRAIN_TEXTS, _TRAIN_LABELS)

    predictions = []
    for text in req.texts:
        pred_idx = int(pipe.predict([text])[0])
        prob = pipe.predict_proba([text])[0]
        predictions.append({
            "text": text[:200],
            "label": _LABEL_NAMES[pred_idx],
            "confidence": float(prob[pred_idx]),
        })

    return {"predictions": predictions, "categories": _LABEL_NAMES}


@app.post("/api/genai/text-to-image")
def text_to_image(req: DiffusionRequest) -> dict[str, str]:
    try:
        import torch
        from diffusers import StableDiffusionPipeline
    except Exception as exc:  # pragma: no cover
        raise HTTPException(status_code=503, detail=f"Missing dependency: {exc}") from exc

    if not torch.cuda.is_available():
        raise HTTPException(status_code=503, detail="CUDA GPU is not available in this environment.")

    model_id = os.getenv("DIFFUSERS_MODEL_ID", "runwayml/stable-diffusion-v1-5")
    pipe = StableDiffusionPipeline.from_pretrained(model_id, torch_dtype=torch.float16).to("cuda")

    image = pipe(
        req.prompt,
        guidance_scale=req.guidance_scale,
        height=req.height,
        width=req.width,
    ).images[0]

    output_path = GENERATED_DIR / "diffusion_result.png"
    image.save(output_path)
    return {"image_url": "/files/diffusion_result.png"}


@app.get("/files/{file_name}")
def get_generated_file(file_name: str) -> FileResponse:
    target = GENERATED_DIR / file_name
    if not target.exists():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(path=target)


@app.post("/api/dl/cnn-timeseries")
def cnn_timeseries(req: CNNTimeSeriesRequest) -> dict[str, object]:
    """
    1D CNN으로 주가 패턴(상승·횡보·하락)을 분류합니다. (Day036 대응)
    """
    try:
        import torch
        import torch.nn as nn
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Missing dependency: {exc}") from exc

    import numpy as np
    from sklearn.model_selection import train_test_split
    from sklearn.preprocessing import StandardScaler

    SEED = 42
    rng_ = np.random.default_rng(SEED)

    windows, labels = [], []
    for _ in range(req.n_samples):
        mu_ = rng_.uniform(-0.0003, 0.0003)
        sigma_ = rng_.uniform(0.01, 0.025)
        returns = rng_.normal(mu_, sigma_, req.window + 1)
        prices = 100 * np.exp(np.cumsum(returns))
        next_ret = (prices[req.window] - prices[req.window - 1]) / prices[req.window - 1]
        windows.append(prices[: req.window])
        labels.append(2 if next_ret > 0.01 else (0 if next_ret < -0.01 else 1))

    X_raw = np.array(windows, dtype=np.float32)
    y_arr = np.array(labels, dtype=np.int64)
    X_norm = StandardScaler().fit_transform(X_raw).astype(np.float32)
    X_tr, X_te, y_tr, y_te = train_test_split(X_norm, y_arr, test_size=0.2, stratify=y_arr, random_state=SEED)

    Xtr = torch.tensor(X_tr).unsqueeze(1)
    Xte = torch.tensor(X_te).unsqueeze(1)
    ytr = torch.tensor(y_tr)
    yte = torch.tensor(y_te)

    class _CNN1D(nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.net = nn.Sequential(
                nn.Conv1d(1, 32, 3, padding=1), nn.ReLU(),
                nn.Conv1d(32, 64, 3, padding=1), nn.ReLU(),
                nn.AdaptiveAvgPool1d(1), nn.Flatten(),
                nn.Linear(64, 32), nn.ReLU(), nn.Dropout(0.3), nn.Linear(32, 3),
            )
        def forward(self, x: torch.Tensor) -> torch.Tensor:
            return self.net(x)

    torch.manual_seed(SEED)
    model = _CNN1D()
    opt = torch.optim.Adam(model.parameters(), lr=1e-3)
    crit = nn.CrossEntropyLoss()
    ds = torch.utils.data.TensorDataset(Xtr, ytr)
    dl = torch.utils.data.DataLoader(ds, batch_size=64, shuffle=True)

    train_acc_hist = []
    for _ in range(req.epochs):
        model.train()
        correct, total = 0, 0
        for xb, yb in dl:
            opt.zero_grad()
            out = model(xb)
            loss = crit(out, yb)
            loss.backward()
            opt.step()
            correct += (out.argmax(1) == yb).sum().item()
            total += len(yb)
        train_acc_hist.append(correct / total)

    model.eval()
    with torch.no_grad():
        val_preds = model(Xte).argmax(1).numpy()
    val_acc = float(np.mean(val_preds == y_te))

    label_names = ["하락", "횡보", "상승"]
    class_counts = {label_names[i]: int(np.sum(val_preds == i)) for i in range(3)}

    return {
        "val_accuracy": val_acc,
        "train_accuracy_final": float(train_acc_hist[-1]),
        "predicted_class_counts": class_counts,
        "train_accuracy_history": [round(a, 4) for a in train_acc_hist],
    }


@app.post("/api/dl/lstm-predictor")
def lstm_predictor(req: LSTMRequest) -> dict[str, object]:
    """
    LSTM으로 다음 날 주가 수익률을 예측합니다. (Day037 대응)
    """
    try:
        import torch
        import torch.nn as nn
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Missing dependency: {exc}") from exc

    import numpy as np
    from sklearn.preprocessing import StandardScaler

    SEED = 42
    rng_ = np.random.default_rng(SEED)
    daily_ret = rng_.normal(0.0002, 0.015, req.n_days)
    prices = 1000 * np.exp(np.cumsum(daily_ret))
    returns = (np.diff(prices) / prices[:-1]).astype(np.float32)

    def _make_seq(series: np.ndarray, seq_len: int):
        X_, y_ = [], []
        for i in range(len(series) - seq_len):
            X_.append(series[i : i + seq_len])
            y_.append(series[i + seq_len])
        return np.array(X_, dtype=np.float32), np.array(y_, dtype=np.float32)

    scaler_X = StandardScaler()
    scaler_y = StandardScaler()
    X_raw, y_raw = _make_seq(returns, req.seq_len)
    X_norm = scaler_X.fit_transform(X_raw).astype(np.float32)
    y_norm = scaler_y.fit_transform(y_raw.reshape(-1, 1)).ravel().astype(np.float32)

    split = int(len(X_norm) * 0.8)
    Xtr = torch.tensor(X_norm[:split]).unsqueeze(-1)
    Xte = torch.tensor(X_norm[split:]).unsqueeze(-1)
    ytr = torch.tensor(y_norm[:split])
    yte_raw = y_raw[split:]

    class _LSTM(nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.lstm = nn.LSTM(1, req.hidden_size, 2, batch_first=True, dropout=0.2)
            self.fc = nn.Sequential(nn.Linear(req.hidden_size, 32), nn.ReLU(), nn.Linear(32, 1))
        def forward(self, x: torch.Tensor) -> torch.Tensor:
            out, _ = self.lstm(x)
            return self.fc(out[:, -1, :]).squeeze(-1)

    torch.manual_seed(SEED)
    model = _LSTM()
    opt = torch.optim.Adam(model.parameters(), lr=1e-3)
    crit = nn.MSELoss()
    ds = torch.utils.data.TensorDataset(Xtr, ytr)
    dl = torch.utils.data.DataLoader(ds, batch_size=64, shuffle=True)
    val_losses = []

    for _ in range(req.epochs):
        model.train()
        for xb, yb in dl:
            opt.zero_grad()
            loss = crit(model(xb), yb)
            loss.backward()
            nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            opt.step()
        model.eval()
        with torch.no_grad():
            vl = crit(model(Xte), torch.tensor(y_norm[split:])).item()
        val_losses.append(float(vl))

    model.eval()
    with torch.no_grad():
        y_pred_norm = model(Xte).numpy()
    y_pred_raw = scaler_y.inverse_transform(y_pred_norm.reshape(-1, 1)).ravel()
    dir_acc = float(np.mean(np.sign(y_pred_raw) == np.sign(yte_raw)))

    return {
        "direction_accuracy": dir_acc,
        "val_loss_final": val_losses[-1],
        "val_loss_history": [round(v, 6) for v in val_losses],
    }


@app.post("/api/dl/transformer-timeseries")
def transformer_timeseries(req: TransformerTSRequest) -> dict[str, object]:
    """
    Transformer Encoder로 멀티스텝 주가를 예측합니다. (Day038-039 대응)
    """
    try:
        import math
        import torch
        import torch.nn as nn
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Missing dependency: {exc}") from exc

    import numpy as np
    from sklearn.preprocessing import MinMaxScaler

    SEED = 42
    rng_ = np.random.default_rng(SEED)
    t = np.arange(2500)
    seasonal = 0.02 * np.sin(2 * np.pi * t / 252)
    log_returns = 0.0001 + seasonal * 0.005 + rng_.normal(0, 0.012, 2500)
    prices = 1000 * np.exp(np.cumsum(log_returns))
    scaler_ = MinMaxScaler()
    prices_norm = scaler_.fit_transform(prices.reshape(-1, 1)).ravel().astype(np.float32)

    X_list, y_list = [], []
    for i in range(len(prices_norm) - req.seq_len - req.pred_steps):
        X_list.append(prices_norm[i : i + req.seq_len])
        y_list.append(prices_norm[i + req.seq_len : i + req.seq_len + req.pred_steps])
    X_all = np.array(X_list, dtype=np.float32)
    y_all = np.array(y_list, dtype=np.float32)

    split = int(len(X_all) * 0.8)
    Xtr = torch.tensor(X_all[:split]).unsqueeze(-1)
    Xte = torch.tensor(X_all[split:]).unsqueeze(-1)
    ytr = torch.tensor(y_all[:split])
    yte = torch.tensor(y_all[split:])

    class _PE(nn.Module):
        def __init__(self, d: int, max_len: int = 200) -> None:
            super().__init__()
            pe = torch.zeros(max_len, d)
            pos = torch.arange(0, max_len, dtype=torch.float).unsqueeze(1)
            div = torch.exp(torch.arange(0, d, 2, dtype=torch.float) * (-math.log(10000.0) / d))
            pe[:, 0::2] = torch.sin(pos * div)
            pe[:, 1::2] = torch.cos(pos * div)
            self.register_buffer("pe", pe.unsqueeze(0))
        def forward(self, x: torch.Tensor) -> torch.Tensor:
            return x + self.pe[:, : x.size(1), :]

    class _TransformerModel(nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.proj = nn.Linear(1, req.d_model)
            self.pe = _PE(req.d_model)
            nhead = max(1, req.d_model // 8)
            enc_layer = nn.TransformerEncoderLayer(
                d_model=req.d_model, nhead=nhead,
                dim_feedforward=req.d_model * 4, dropout=0.1, batch_first=True,
            )
            self.enc = nn.TransformerEncoder(enc_layer, num_layers=2)
            self.head = nn.Linear(req.d_model, req.pred_steps)
        def forward(self, x: torch.Tensor) -> torch.Tensor:
            x = self.pe(self.proj(x))
            return self.head(self.enc(x)[:, -1, :])

    torch.manual_seed(SEED)
    model = _TransformerModel()
    opt = torch.optim.AdamW(model.parameters(), lr=5e-4)
    crit = nn.MSELoss()
    ds = torch.utils.data.TensorDataset(Xtr, ytr)
    dl = torch.utils.data.DataLoader(ds, batch_size=64, shuffle=True)
    val_losses = []

    for _ in range(req.epochs):
        model.train()
        for xb, yb in dl:
            opt.zero_grad()
            loss = crit(model(xb), yb)
            loss.backward()
            nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            opt.step()
        model.eval()
        with torch.no_grad():
            vl = crit(model(Xte), yte).item()
        val_losses.append(float(vl))

    model.eval()
    with torch.no_grad():
        y_pred = model(Xte).numpy()

    y_pred_orig = scaler_.inverse_transform(y_pred[:, 0].reshape(-1, 1)).ravel()
    y_true_orig = scaler_.inverse_transform(yte[:, 0].numpy().reshape(-1, 1)).ravel()
    last_input = scaler_.inverse_transform(X_all[split:, -1].reshape(-1, 1)).ravel()
    dir_acc = float(np.mean(np.sign(y_pred_orig - last_input) == np.sign(y_true_orig - last_input)))

    return {
        "direction_accuracy_step1": dir_acc,
        "val_loss_final": val_losses[-1],
        "val_loss_history": [round(v, 6) for v in val_losses],
    }


# ─── Quant Strategy Models ────────────────────────────────────────────────────

class BacktestRequest(BaseModel):
    fast_ma: int = Field(default=20, ge=5, le=60)
    slow_ma: int = Field(default=60, ge=20, le=200)
    n_days: int = Field(default=1260, ge=252, le=5040)


class PortfolioRequest(BaseModel):
    n_simulations: int = Field(default=3000, ge=500, le=10000)
    risk_free: float = Field(default=0.03, ge=0.0, le=0.1)


class RiskRequest(BaseModel):
    confidence: float = Field(default=0.95, ge=0.90, le=0.99)
    n_scenarios: int = Field(default=10000, ge=1000, le=100000)
    portfolio_value: float = Field(default=100_000_000, ge=1_000_000)


class PipelineRequest(BaseModel):
    ticker: str = Field(default="SPY")
    fast_ma: int = Field(default=20, ge=5, le=60)
    slow_ma: int = Field(default=60, ge=20, le=200)


class FinancialKnowledgeRequest(BaseModel):
    focus: str = Field(default="balanced", pattern="^(balanced|products|allocation)$")
    n_simulations: int = Field(default=3000, ge=500, le=10000)
    risk_free: float = Field(default=0.03, ge=0.0, le=0.1)


# ─── Quant Endpoints ──────────────────────────────────────────────────────────

@app.post("/api/quant/backtest")
def quant_backtest(req: BacktestRequest) -> dict[str, object]:
    """MA 크로스오버 전략 백테스트 (Day041·57 대응)"""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.gridspec as gridspec
    import numpy as np
    import pandas as pd
    configure_matplotlib_korean_font(plt)

    rng = np.random.default_rng(42)
    dt = 1 / 252
    mu, sigma = 0.08, 0.20
    daily_r = rng.normal((mu - 0.5 * sigma**2) * dt, sigma * np.sqrt(dt), req.n_days)
    prices = pd.Series(
        100 * np.exp(np.cumsum(daily_r)),
        index=pd.date_range("2020-01-01", periods=req.n_days, freq="B"),
        name="Close",
    )

    df = pd.DataFrame({"Close": prices})
    df["MA_fast"] = df["Close"].rolling(req.fast_ma).mean()
    df["MA_slow"] = df["Close"].rolling(req.slow_ma).mean()
    df["Signal"] = (df["MA_fast"] > df["MA_slow"]).astype(float)
    df["Position"] = df["Signal"].shift(1).fillna(0)
    df["Ret"] = df["Close"].pct_change()
    df["Strat_Ret"] = df["Position"] * df["Ret"]
    df["BH_Ret"] = df["Ret"]
    df["Strat_Cum"] = (1 + df["Strat_Ret"]).cumprod()
    df["BH_Cum"] = (1 + df["BH_Ret"]).cumprod()
    df = df.dropna()

    ret = df["Strat_Ret"]
    n_years = len(ret) / 252
    cum = df["Strat_Cum"]
    cagr = float(cum.iloc[-1] ** (1 / n_years) - 1) if n_years > 0 else 0
    excess = ret - 0.03 / 252
    sharpe = float(excess.mean() / excess.std() * np.sqrt(252)) if excess.std() > 0 else 0
    rolling_max = cum.cummax()
    dd = (cum - rolling_max) / rolling_max
    mdd = float(dd.min())
    wins = ret[ret > 0]
    losses = ret[ret < 0]
    win_rate = len(wins) / max(len(ret[ret != 0]), 1)
    pf = float(wins.sum() / abs(losses.sum())) if len(losses) > 0 and losses.sum() != 0 else 9.99
    n_trades = int(df["Position"].diff().abs()[lambda x: x > 0].count())
    total_ret = float(cum.iloc[-1] - 1)
    bh_ret = float(df["BH_Cum"].iloc[-1] - 1)

    fig = plt.figure(figsize=(14, 9), facecolor="#0f172a")
    gs = gridspec.GridSpec(3, 2, figure=fig, hspace=0.45, wspace=0.3)
    text_c = "#e2e8f0"
    grid_c = "#1e293b"

    for ax in [fig.add_subplot(gs[r, c]) for r in range(3) for c in range(2)]:
        ax.set_facecolor("#1e293b")
    plt.clf()

    ax1 = fig.add_subplot(gs[0, :])
    ax1.set_facecolor("#1e293b")
    ax1.plot(df.index, df["Close"], color="#64748b", lw=0.8, label="주가")
    ax1.plot(df.index, df["MA_fast"], color="#3b82f6", lw=1.5, label=f"MA{req.fast_ma}")
    ax1.plot(df.index, df["MA_slow"], color="#f97316", lw=1.5, label=f"MA{req.slow_ma}")
    buy_m = (df["Position"] == 1) & (df["Position"].shift(1) == 0)
    sell_m = (df["Position"] == 0) & (df["Position"].shift(1) == 1)
    ax1.scatter(df.index[buy_m], df["Close"][buy_m], marker="^", color="#22c55e", s=50, zorder=5, label="매수")
    ax1.scatter(df.index[sell_m], df["Close"][sell_m], marker="v", color="#ef4444", s=50, zorder=5, label="매도")
    ax1.set_title(f"MA 크로스오버 전략 (MA{req.fast_ma}/MA{req.slow_ma})", color=text_c, fontsize=11, fontweight="bold")
    ax1.legend(fontsize=8, ncol=5, labelcolor=text_c, facecolor="#0f172a")
    ax1.tick_params(colors=text_c); ax1.spines[:].set_color(grid_c)
    ax1.grid(True, alpha=0.2, color=grid_c)

    ax2 = fig.add_subplot(gs[1, :])
    ax2.set_facecolor("#1e293b")
    ax2.plot(df.index, df["Strat_Cum"], color="#3b82f6", lw=2, label=f"전략 ({total_ret:+.1%})")
    ax2.plot(df.index, df["BH_Cum"], color="#94a3b8", lw=2, ls="--", label=f"Buy & Hold ({bh_ret:+.1%})")
    ax2.axhline(1.0, color="#475569", lw=0.6)
    ax2.set_title("누적 수익률 비교", color=text_c, fontsize=11)
    ax2.legend(fontsize=9, labelcolor=text_c, facecolor="#0f172a")
    ax2.tick_params(colors=text_c); ax2.spines[:].set_color(grid_c)
    ax2.grid(True, alpha=0.2, color=grid_c)

    ax3 = fig.add_subplot(gs[2, 0])
    ax3.set_facecolor("#1e293b")
    ax3.fill_between(df.index, dd * 100, 0, color="#ef4444", alpha=0.5)
    ax3.set_title("낙폭 Drawdown (%)", color=text_c, fontsize=11)
    ax3.tick_params(colors=text_c); ax3.spines[:].set_color(grid_c)
    ax3.grid(True, alpha=0.2, color=grid_c)

    ax4 = fig.add_subplot(gs[2, 1])
    ax4.set_facecolor("#1e293b")
    ax4.axis("off")
    rows = [
        ["전략 총수익률", f"{total_ret:+.1%}"],
        ["B&H 수익률", f"{bh_ret:+.1%}"],
        ["CAGR", f"{cagr:+.2%}"],
        ["Sharpe", f"{sharpe:.2f}"],
        ["MDD", f"{mdd:.1%}"],
        ["승률", f"{win_rate:.1%}"],
        ["손익비", f"{pf:.2f}"],
        ["거래횟수", f"{n_trades}회"],
    ]
    tbl = ax4.table(cellText=rows, colLabels=["지표", "값"], loc="center", bbox=[0, 0, 1, 1])
    tbl.auto_set_font_size(False); tbl.set_fontsize(9)
    for (r, c), cell in tbl.get_celld().items():
        cell.set_facecolor("#0f172a" if r == 0 else "#1e293b")
        cell.set_text_props(color=text_c)
        cell.set_edgecolor(grid_c)
    ax4.set_title("성과 요약", color=text_c, fontsize=11)

    fig.patch.set_facecolor("#0f172a")
    plt.suptitle("백테스트 결과 — MA 크로스오버 전략", color=text_c, fontsize=13, fontweight="bold", y=1.01)

    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=130, bbox_inches="tight", facecolor="#0f172a")
    plt.close(fig)
    return {
        "image": "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode(),
        "metrics": {"cagr": round(cagr, 4), "sharpe": round(sharpe, 2), "mdd": round(mdd, 4),
                    "win_rate": round(win_rate, 4), "profit_factor": round(pf, 2),
                    "n_trades": n_trades, "total_return": round(total_ret, 4), "bh_return": round(bh_ret, 4)},
    }


@app.post("/api/quant/portfolio")
def quant_portfolio(req: PortfolioRequest) -> dict[str, object]:
    """포트폴리오 최적화 — 효율적 프론티어 + Sharpe 극대화 (Day57·76·77 대응)"""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    configure_matplotlib_korean_font(plt)

    tickers = ["KOSPI", "S&P500", "국채10Y", "금(Gold)", "BTC"]
    mu_ann = np.array([0.10, 0.12, 0.04, 0.07, 0.30])
    vol_ann = np.array([0.18, 0.17, 0.06, 0.15, 0.70])
    corr = np.array([
        [1.00, 0.75, 0.10, 0.10, 0.20],
        [0.75, 1.00, 0.05, 0.05, 0.25],
        [0.10, 0.05, 1.00, 0.20, 0.00],
        [0.10, 0.05, 0.20, 1.00, 0.05],
        [0.20, 0.25, 0.00, 0.05, 1.00],
    ])
    cov = np.outer(vol_ann, vol_ann) * corr
    n = len(tickers)
    rng = np.random.default_rng(42)
    rf = req.risk_free

    port_rets, port_vols, port_sharpes = [], [], []
    all_weights = []
    for _ in range(req.n_simulations):
        w = rng.random(n); w /= w.sum()
        r = float(w @ mu_ann)
        v = float(np.sqrt(w @ cov @ w))
        port_rets.append(r); port_vols.append(v)
        port_sharpes.append((r - rf) / v)
        all_weights.append(w)

    port_rets = np.array(port_rets)
    port_vols = np.array(port_vols)
    port_sharpes = np.array(port_sharpes)
    all_weights = np.array(all_weights)

    best_i = int(np.argmax(port_sharpes))
    best_w = all_weights[best_i]

    # Risk-parity weights (equal risk contribution approx)
    inv_vol = 1 / vol_ann; rp_w = inv_vol / inv_vol.sum()
    rp_r = float(rp_w @ mu_ann); rp_v = float(np.sqrt(rp_w @ cov @ rp_w))

    fig, axes = plt.subplots(1, 2, figsize=(14, 6), facecolor="#0f172a")
    text_c = "#e2e8f0"; grid_c = "#1e293b"

    ax = axes[0]; ax.set_facecolor("#1e293b")
    sc = ax.scatter(port_vols * 100, port_rets * 100, c=port_sharpes, cmap="RdYlGn",
                    s=4, alpha=0.6)
    ax.scatter(port_vols[best_i] * 100, port_rets[best_i] * 100,
               marker="*", color="#fbbf24", s=300, zorder=10, label=f"최적(Sharpe={port_sharpes[best_i]:.2f})")
    ax.scatter(rp_v * 100, rp_r * 100, marker="D", color="#22d3ee", s=120, zorder=10, label="Risk-Parity")
    for i, tk in enumerate(tickers):
        ax.scatter(vol_ann[i] * 100, mu_ann[i] * 100, marker="o", s=80, zorder=10)
        ax.annotate(tk, (vol_ann[i] * 100, mu_ann[i] * 100), textcoords="offset points",
                    xytext=(5, 3), color=text_c, fontsize=8)
    cbar = plt.colorbar(sc, ax=ax); cbar.set_label("Sharpe Ratio", color=text_c)
    cbar.ax.yaxis.set_tick_params(color=text_c)
    plt.setp(cbar.ax.yaxis.get_ticklabels(), color=text_c)
    ax.set_xlabel("리스크 (변동성 %)", color=text_c); ax.set_ylabel("기대수익률 (%)", color=text_c)
    ax.set_title("효율적 프론티어", color=text_c, fontsize=12, fontweight="bold")
    ax.legend(fontsize=8, labelcolor=text_c, facecolor="#0f172a")
    ax.tick_params(colors=text_c); ax.spines[:].set_color(grid_c)
    ax.grid(True, alpha=0.2, color=grid_c)

    ax2 = axes[1]; ax2.set_facecolor("#1e293b")
    colors = ["#3b82f6", "#22c55e", "#f97316", "#fbbf24", "#a78bfa"]
    bars = ax2.bar(tickers, best_w * 100, color=colors, alpha=0.85, edgecolor=grid_c)
    for bar, val in zip(bars, best_w):
        ax2.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.5,
                 f"{val:.1%}", ha="center", va="bottom", color=text_c, fontsize=9, fontweight="bold")
    ax2.set_title(f"최적 포트폴리오 비중 (Sharpe={port_sharpes[best_i]:.2f})", color=text_c, fontsize=12, fontweight="bold")
    ax2.set_ylabel("비중 (%)", color=text_c)
    ax2.tick_params(colors=text_c); ax2.spines[:].set_color(grid_c)
    ax2.set_facecolor("#1e293b"); ax2.grid(True, alpha=0.2, color=grid_c, axis="y")

    fig.patch.set_facecolor("#0f172a")
    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=130, bbox_inches="tight", facecolor="#0f172a")
    plt.close(fig)

    return {
        "image": "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode(),
        "optimal_weights": {tk: round(float(w), 4) for tk, w in zip(tickers, best_w)},
        "optimal_return": round(float(port_rets[best_i]), 4),
        "optimal_vol": round(float(port_vols[best_i]), 4),
        "optimal_sharpe": round(float(port_sharpes[best_i]), 4),
        "riskparity_weights": {tk: round(float(w), 4) for tk, w in zip(tickers, rp_w)},
    }


@app.post("/api/quant/financial-knowledge")
def quant_financial_knowledge(req: FinancialKnowledgeRequest) -> dict[str, object]:
    """모듈 8 — 금융상품 이해와 자산배분방법론 5일 커리큘럼 점검/실습."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    configure_matplotlib_korean_font(plt)

    coverage = [
        {
            "day": "Day 052",
            "topic": "주식/ETF 상품",
            "document": "docs/37.md",
            "coverage": 0.96,
            "web_status": "보완됨",
            "items": ["주식/ETF 개요", "ETF 운용 전략", "성과 비교"],
        },
        {
            "day": "Day 053",
            "topic": "채권 상품",
            "document": "docs/37.md",
            "coverage": 0.88,
            "web_status": "보완됨",
            "items": ["채권 개요", "듀레이션", "수익률 곡선", "운용 전략"],
        },
        {
            "day": "Day 054",
            "topic": "파생상품",
            "document": "docs/38.md",
            "coverage": 0.86,
            "web_status": "보완됨",
            "items": ["선물", "옵션", "스왑", "헤징 전략"],
        },
        {
            "day": "Day 055",
            "topic": "포트폴리오 이론",
            "document": "docs/39.md",
            "coverage": 0.94,
            "web_status": "기존+보완",
            "items": ["MPT", "성과분석", "MDD", "Sharpe", "Sortino"],
        },
        {
            "day": "Day 056",
            "topic": "자산배분 모델",
            "document": "docs/40.md",
            "coverage": 0.92,
            "web_status": "기존+보완",
            "items": ["평균분산", "블랙-리터만", "Risk-Parity", "사례 분석"],
        },
    ]

    rng = np.random.default_rng(7)
    n_days = 252
    asset_names = ["주식/ETF", "채권", "원자재", "현금"]
    mu = np.array([0.10, 0.04, 0.06, 0.025])
    vol = np.array([0.19, 0.07, 0.16, 0.01])
    corr = np.array([
        [1.00, -0.10, 0.25, 0.00],
        [-0.10, 1.00, 0.05, 0.00],
        [0.25, 0.05, 1.00, 0.00],
        [0.00, 0.00, 0.00, 1.00],
    ])
    cov = np.outer(vol, vol) * corr
    daily_mean = mu / 252
    daily_cov = cov / 252
    returns = rng.multivariate_normal(daily_mean, daily_cov, n_days)
    curves = np.cumprod(1 + returns, axis=0)

    inv_vol = 1 / vol
    risk_parity_w = inv_vol / inv_vol.sum()
    sixty_forty_w = np.array([0.60, 0.35, 0.00, 0.05])
    market_w = np.array([0.50, 0.30, 0.15, 0.05])
    investor_view = np.array([0.005, 0.000, 0.006, 0.000])
    black_litterman_return = (mu * 0.75) + ((mu + investor_view) * 0.25)

    port_rets, port_vols, sharpes, weights = [], [], [], []
    for _ in range(req.n_simulations):
        w = rng.random(len(asset_names))
        w = w / w.sum()
        r = float(w @ mu)
        v = float(np.sqrt(w.T @ cov @ w))
        s = (r - req.risk_free) / v
        port_rets.append(r)
        port_vols.append(v)
        sharpes.append(s)
        weights.append(w)

    port_rets = np.array(port_rets)
    port_vols = np.array(port_vols)
    sharpes = np.array(sharpes)
    weights = np.array(weights)
    best_i = int(np.argmax(sharpes))
    mean_variance_w = weights[best_i]
    black_litterman_w = black_litterman_return / black_litterman_return.sum()

    def metrics(w: np.ndarray) -> dict[str, float]:
        portfolio_daily = returns @ w
        cumulative = np.cumprod(1 + portfolio_daily)
        cagr = float(cumulative[-1] ** (252 / len(cumulative)) - 1)
        annual_vol = float(np.std(portfolio_daily) * np.sqrt(252))
        mdd = float(np.min(cumulative / np.maximum.accumulate(cumulative) - 1))
        downside = portfolio_daily[portfolio_daily < 0]
        downside_vol = float(np.std(downside) * np.sqrt(252)) if len(downside) else annual_vol
        sharpe = float((cagr - req.risk_free) / annual_vol) if annual_vol else 0.0
        sortino = float((cagr - req.risk_free) / downside_vol) if downside_vol else 0.0
        return {
            "cagr": round(cagr, 4),
            "volatility": round(annual_vol, 4),
            "mdd": round(mdd, 4),
            "sharpe": round(sharpe, 3),
            "sortino": round(sortino, 3),
        }

    strategies = {
        "60/40 사례": sixty_forty_w,
        "평균분산": mean_variance_w,
        "블랙-리터만": black_litterman_w,
        "Risk-Parity": risk_parity_w,
    }

    strategy_payload = {
        name: {
            "weights": {asset: round(float(weight), 4) for asset, weight in zip(asset_names, w)},
            "metrics": metrics(w),
        }
        for name, w in strategies.items()
    }

    spots = np.linspace(70, 130, 121)
    call = np.maximum(spots - 100, 0) - 5
    put = np.maximum(100 - spots, 0) - 4
    straddle = call + put
    tenors = ["3M", "2Y", "5Y", "10Y", "30Y"]
    yields = np.array([4.6, 4.3, 4.0, 4.1, 4.25])

    text_c = "#e2e8f0"; grid_c = "#334155"
    fig, axes = plt.subplots(2, 2, figsize=(14, 9), facecolor="#0f172a")

    ax = axes[0, 0]; ax.set_facecolor("#1e293b")
    for i, name in enumerate(asset_names):
        ax.plot(curves[:, i] * 100, label=name, linewidth=1.4)
    ax.set_title("금융상품 이해: 자산군별 누적 성과", color=text_c, fontweight="bold")
    ax.set_ylabel("기준가", color=text_c)
    ax.legend(fontsize=8, labelcolor=text_c, facecolor="#0f172a")
    ax.tick_params(colors=text_c); ax.grid(True, alpha=0.2, color=grid_c)
    ax.spines[:].set_color(grid_c)

    ax = axes[0, 1]; ax.set_facecolor("#1e293b")
    sc = ax.scatter(port_vols * 100, port_rets * 100, c=sharpes, cmap="viridis", s=5, alpha=0.55)
    ax.scatter(port_vols[best_i] * 100, port_rets[best_i] * 100, marker="*", s=260, color="#fbbf24", label="평균분산")
    rp_r = float(risk_parity_w @ mu); rp_v = float(np.sqrt(risk_parity_w.T @ cov @ risk_parity_w))
    ax.scatter(rp_v * 100, rp_r * 100, marker="D", s=110, color="#22c55e", label="Risk-Parity")
    ax.set_title("자산배분방법론: 효율적 투자선", color=text_c, fontweight="bold")
    ax.set_xlabel("변동성 (%)", color=text_c); ax.set_ylabel("기대수익률 (%)", color=text_c)
    ax.legend(fontsize=8, labelcolor=text_c, facecolor="#0f172a")
    ax.tick_params(colors=text_c); ax.grid(True, alpha=0.2, color=grid_c)
    ax.spines[:].set_color(grid_c)
    cbar = plt.colorbar(sc, ax=ax); cbar.set_label("Sharpe", color=text_c)
    plt.setp(cbar.ax.yaxis.get_ticklabels(), color=text_c)

    ax = axes[1, 0]; ax.set_facecolor("#1e293b")
    x = np.arange(len(asset_names))
    width = 0.2
    for offset, (name, w) in zip([-1.5, -0.5, 0.5, 1.5], strategies.items()):
        ax.bar(x + offset * width, w * 100, width=width, label=name)
    ax.set_xticks(x); ax.set_xticklabels(asset_names, color=text_c)
    ax.set_title("자산배분 모델별 비중 비교", color=text_c, fontweight="bold")
    ax.set_ylabel("비중 (%)", color=text_c)
    ax.legend(fontsize=8, labelcolor=text_c, facecolor="#0f172a")
    ax.tick_params(colors=text_c); ax.grid(True, alpha=0.2, color=grid_c, axis="y")
    ax.spines[:].set_color(grid_c)

    ax = axes[1, 1]; ax.set_facecolor("#1e293b")
    ax.plot(spots, call, label="콜 매수", color="#3b82f6")
    ax.plot(spots, put, label="풋 매수", color="#ef4444")
    ax.plot(spots, straddle, label="스트래들", color="#a855f7")
    ax2 = ax.twinx()
    ax2.plot(np.arange(len(tenors)), yields, marker="o", color="#22c55e", label="채권 수익률곡선")
    ax2.set_ylabel("금리 (%)", color="#22c55e")
    ax2.tick_params(colors="#22c55e")
    ax2.set_xticks(np.arange(len(tenors)))
    ax2.set_xticklabels(tenors, color=text_c)
    ax.axhline(0, color="#94a3b8", linewidth=0.8)
    ax.set_title("파생상품 손익 + 채권 곡선 예시", color=text_c, fontweight="bold")
    ax.set_xlabel("기초자산 가격 / 만기", color=text_c); ax.set_ylabel("옵션 손익", color=text_c)
    ax.legend(fontsize=8, labelcolor=text_c, facecolor="#0f172a", loc="upper left")
    ax.tick_params(colors=text_c); ax.grid(True, alpha=0.2, color=grid_c)
    ax.spines[:].set_color(grid_c)

    fig.suptitle("퀀트를 위한 금융 필수 지식 — 웹앱 반영 점검", color=text_c, fontsize=15, fontweight="bold")
    fig.tight_layout(rect=[0, 0, 1, 0.97])
    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=130, bbox_inches="tight", facecolor="#0f172a")
    plt.close(fig)

    diagnostics = [
        {"area": "문서 커리큘럼", "status": "충분", "note": "37~40.md가 Day 052~056의 5일 과정을 모두 포함합니다."},
        {"area": "기존 웹앱", "status": "부분 반영", "note": "포트폴리오 최적화와 리스크 분석은 있었지만 금융상품별 통합 화면은 부족했습니다."},
        {"area": "보완 웹앱", "status": "반영", "note": "주식/ETF, 채권, 파생상품, 포트폴리오 이론, 자산배분 모델을 한 화면에서 확인합니다."},
    ]

    return {
        "image": "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode(),
        "coverage": coverage,
        "diagnostics": diagnostics,
        "strategies": strategy_payload,
        "curriculum": [
            {"day": "Day 052", "title": "주식/ETF 상품 이해", "practice": "ETF 성과 비교"},
            {"day": "Day 053", "title": "채권 상품 이해", "practice": "수익률 곡선·듀레이션"},
            {"day": "Day 054", "title": "파생상품 이해", "practice": "옵션 손익 시뮬레이션"},
            {"day": "Day 055", "title": "포트폴리오 이론 및 성과 분석", "practice": "CAGR·MDD·Sharpe"},
            {"day": "Day 056", "title": "자산배분 모델 및 사례 분석", "practice": "평균분산·블랙리터만·Risk-Parity 비교"},
        ],
    }


@app.post("/api/quant/risk")
def quant_risk(req: RiskRequest) -> dict[str, object]:
    """VaR / CVaR 리스크 분석 (Day39·55 대응)"""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    configure_matplotlib_korean_font(plt)

    rng = np.random.default_rng(42)
    mu, sigma = 0.0004, 0.012
    daily_ret = rng.normal(mu, sigma, req.n_scenarios).astype(float)

    alpha = 1 - req.confidence
    var_pct = float(np.percentile(daily_ret, alpha * 100))
    cvar_pct = float(daily_ret[daily_ret <= var_pct].mean())
    var_amt = abs(var_pct) * req.portfolio_value
    cvar_amt = abs(cvar_pct) * req.portfolio_value

    fig, axes = plt.subplots(1, 2, figsize=(14, 5), facecolor="#0f172a")
    text_c = "#e2e8f0"; grid_c = "#1e293b"

    ax = axes[0]; ax.set_facecolor("#1e293b")
    ax.hist(daily_ret * 100, bins=80, color="#3b82f6", alpha=0.75, edgecolor="none", label="수익률 분포")
    ax.axvline(var_pct * 100, color="#f97316", lw=2, linestyle="--", label=f"VaR ({req.confidence:.0%}): {var_pct:.2%}")
    ax.axvline(cvar_pct * 100, color="#ef4444", lw=2, linestyle="-", label=f"CVaR: {cvar_pct:.2%}")
    ax.fill_betweenx([0, ax.get_ylim()[1] if ax.get_ylim()[1] > 0 else 500],
                     daily_ret.min() * 100, var_pct * 100, color="#ef4444", alpha=0.15)
    ax.set_xlabel("일간 수익률 (%)", color=text_c); ax.set_ylabel("빈도", color=text_c)
    ax.set_title(f"수익률 분포 & VaR/CVaR ({req.confidence:.0%} 신뢰수준)", color=text_c, fontsize=11, fontweight="bold")
    ax.legend(fontsize=8, labelcolor=text_c, facecolor="#0f172a")
    ax.tick_params(colors=text_c); ax.spines[:].set_color(grid_c)
    ax.grid(True, alpha=0.2, color=grid_c)

    ax2 = axes[1]; ax2.set_facecolor("#1e293b")
    labels = ["VaR 예상 손실", "CVaR 예상 손실", "포트폴리오 가치"]
    values = [var_amt / 1e6, cvar_amt / 1e6, req.portfolio_value / 1e6]
    colors2 = ["#f97316", "#ef4444", "#22c55e"]
    bars = ax2.barh(labels, values, color=colors2, alpha=0.85, edgecolor=grid_c)
    for bar, val in zip(bars, values):
        ax2.text(val + req.portfolio_value / 1e6 * 0.01, bar.get_y() + bar.get_height() / 2,
                 f"{val:.1f}M", va="center", color=text_c, fontsize=10, fontweight="bold")
    ax2.set_xlabel("금액 (백만원)", color=text_c)
    ax2.set_title("리스크 금액 비교", color=text_c, fontsize=11, fontweight="bold")
    ax2.tick_params(colors=text_c); ax2.spines[:].set_color(grid_c)
    ax2.set_facecolor("#1e293b"); ax2.grid(True, alpha=0.2, color=grid_c, axis="x")

    fig.patch.set_facecolor("#0f172a")
    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=130, bbox_inches="tight", facecolor="#0f172a")
    plt.close(fig)

    return {
        "image": "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode(),
        "var_pct": round(var_pct, 6),
        "cvar_pct": round(cvar_pct, 6),
        "var_amount": round(var_amt, 0),
        "cvar_amount": round(cvar_amt, 0),
        "confidence": req.confidence,
        "portfolio_value": req.portfolio_value,
    }


@app.post("/api/quant/pipeline")
def quant_pipeline(req: PipelineRequest) -> dict[str, object]:
    """퀀트 실전 4단계 파이프라인 시각화 (Day43·61 대응)"""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.gridspec as gridspec
    import numpy as np
    import pandas as pd
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.model_selection import TimeSeriesSplit
    configure_matplotlib_korean_font(plt)
    from sklearn.metrics import accuracy_score

    rng = np.random.default_rng(42)
    n = 1260
    daily_r = rng.normal(0.0003, 0.015, n)
    prices = pd.Series(
        100 * np.exp(np.cumsum(daily_r)),
        index=pd.date_range("2020-01-01", periods=n, freq="B"),
    )

    df = pd.DataFrame({"Close": prices})
    df["MA_fast"] = df["Close"].rolling(req.fast_ma).mean()
    df["MA_slow"] = df["Close"].rolling(req.slow_ma).mean()
    df["RSI"] = _calc_rsi(df["Close"])
    df["BB_upper"] = df["Close"].rolling(20).mean() + 2 * df["Close"].rolling(20).std()
    df["BB_lower"] = df["Close"].rolling(20).mean() - 2 * df["Close"].rolling(20).std()
    df["BB_pct"] = (df["Close"] - df["BB_lower"]) / (df["BB_upper"] - df["BB_lower"])
    df["MACD"] = df["Close"].ewm(span=12).mean() - df["Close"].ewm(span=26).mean()
    df["ATR"] = (df["Close"].rolling(14).max() - df["Close"].rolling(14).min())
    df["Signal"] = (df["MA_fast"] > df["MA_slow"]).astype(float)
    df["Position"] = df["Signal"].shift(1).fillna(0)
    df["Ret"] = df["Close"].pct_change()
    df["Strat_Ret"] = df["Position"] * df["Ret"]
    df["Strat_Cum"] = (1 + df["Strat_Ret"]).cumprod()
    df["BH_Cum"] = (1 + df["Ret"]).cumprod()
    df = df.dropna()

    features = ["MA_fast", "MA_slow", "RSI", "BB_pct", "MACD", "ATR"]
    target = (df["Ret"].shift(-1) > 0).astype(int)
    feat_df = df[features].iloc[:-1]
    tgt = target.iloc[:-1]
    tscv = TimeSeriesSplit(n_splits=3)
    accs = []
    for tr_i, te_i in tscv.split(feat_df):
        rf = RandomForestClassifier(n_estimators=50, random_state=42, n_jobs=-1)
        rf.fit(feat_df.iloc[tr_i], tgt.iloc[tr_i])
        accs.append(accuracy_score(tgt.iloc[te_i], rf.predict(feat_df.iloc[te_i])))
    ml_acc = float(np.mean(accs))

    ret = df["Strat_Ret"]
    n_years = len(ret) / 252
    cum = df["Strat_Cum"]
    cagr = float(cum.iloc[-1] ** (1 / n_years) - 1) if n_years > 0 else 0
    excess = ret - 0.03 / 252
    sharpe = float(excess.mean() / excess.std() * np.sqrt(252)) if excess.std() > 0 else 0
    rolling_max = cum.cummax()
    mdd = float(((cum - rolling_max) / rolling_max).min())

    fig = plt.figure(figsize=(15, 10), facecolor="#0f172a")
    gs = gridspec.GridSpec(2, 2, figure=fig, hspace=0.4, wspace=0.3)
    text_c = "#e2e8f0"; grid_c = "#1e293b"

    ax1 = fig.add_subplot(gs[0, :])
    ax1.set_facecolor("#1e293b")
    ax1.plot(df.index, df["Close"], color="#64748b", lw=0.8, label="주가")
    ax1.plot(df.index, df["MA_fast"], color="#3b82f6", lw=1.5, label=f"MA{req.fast_ma}")
    ax1.plot(df.index, df["MA_slow"], color="#f97316", lw=1.5, label=f"MA{req.slow_ma}")
    ax1.fill_between(df.index, df["BB_upper"], df["BB_lower"], alpha=0.07, color="#8b5cf6")
    ax1.set_title(f"1단계+2단계: 주가 & 기술지표 — {req.ticker}", color=text_c, fontsize=11, fontweight="bold")
    ax1.legend(fontsize=8, ncol=4, labelcolor=text_c, facecolor="#0f172a")
    ax1.tick_params(colors=text_c); ax1.spines[:].set_color(grid_c)
    ax1.grid(True, alpha=0.2, color=grid_c)

    ax2 = fig.add_subplot(gs[1, 0])
    ax2.set_facecolor("#1e293b")
    ax2.plot(df.index, df["Strat_Cum"], color="#3b82f6", lw=2, label=f"전략")
    ax2.plot(df.index, df["BH_Cum"], color="#94a3b8", lw=2, ls="--", label="Buy&Hold")
    ax2.set_title(f"3단계: 백테스트 | CAGR {cagr:+.1%} | Sharpe {sharpe:.2f} | MDD {mdd:.1%}",
                  color=text_c, fontsize=10, fontweight="bold")
    ax2.legend(fontsize=8, labelcolor=text_c, facecolor="#0f172a")
    ax2.tick_params(colors=text_c); ax2.spines[:].set_color(grid_c)
    ax2.grid(True, alpha=0.2, color=grid_c)

    ax3 = fig.add_subplot(gs[1, 1])
    ax3.set_facecolor("#1e293b")
    fi = rf.feature_importances_
    sorted_idx = np.argsort(fi)
    bars = ax3.barh([features[i] for i in sorted_idx], fi[sorted_idx],
                    color=["#3b82f6", "#22c55e", "#f97316", "#a78bfa", "#f472b6", "#fbbf24"][::-1],
                    alpha=0.85, edgecolor=grid_c)
    ax3.set_title(f"4단계: ML 특징 중요도 | 방향 정확도 {ml_acc:.1%}", color=text_c, fontsize=10, fontweight="bold")
    ax3.tick_params(colors=text_c); ax3.spines[:].set_color(grid_c)
    ax3.grid(True, alpha=0.2, color=grid_c, axis="x")

    fig.patch.set_facecolor("#0f172a")
    plt.suptitle(f"퀀트 실전 4단계 파이프라인 — {req.ticker}", color=text_c, fontsize=13, fontweight="bold", y=1.01)
    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=130, bbox_inches="tight", facecolor="#0f172a")
    plt.close(fig)

    return {
        "image": "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode(),
        "metrics": {"cagr": round(cagr, 4), "sharpe": round(sharpe, 2), "mdd": round(mdd, 4), "ml_accuracy": round(ml_acc, 4)},
        "ticker": req.ticker,
    }


def _calc_rsi(series: "pd.Series", period: int = 14) -> "pd.Series":
    import pandas as pd
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, 1e-9)
    return 100 - (100 / (1 + rs))


# ── 산업 경쟁력 분석 ─────────────────────────────────────────────────────────

class PorterRequest(BaseModel):
    industry: str = "반도체"
    scores: dict[str, float] = {
        "경쟁강도":       8.0,
        "신규진입 위협":  6.0,
        "대체재 위협":    4.0,
        "구매자 교섭력":  5.0,
        "공급자 교섭력":  7.0,
    }

class SectorRequest(BaseModel):
    tickers: list[str] = ["SOXX", "XLE", "XLF", "XLV", "XLK", "XLI"]
    period:  str       = "1y"

class PeerRequest(BaseModel):
    tickers: dict[str, str] = {
        "삼성전자": "005930.KS",
        "SK하이닉스": "000660.KS",
        "엔비디아": "NVDA",
        "인텔": "INTC",
    }

class LifecycleRequest(BaseModel):
    stage:    str = "성장기"   # 도입기 성장기 성숙기 쇠퇴기
    industry: str = "전기차"


@app.post("/api/industry/porter")
def industry_porter(req: PorterRequest) -> dict[str, object]:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.gridspec as gridspec
    import numpy as np
    import io, base64
    configure_matplotlib_korean_font(plt)

    DARK, SURF, BORDER, TEXT, MUTED = "#0f172a","#1e293b","#334155","#e2e8f0","#64748b"
    ACCENT = "#3b82f6"
    C_HIGH  = "#ef4444"   # 위협 강함
    C_MED   = "#f59e0b"
    C_LOW   = "#22c55e"   # 위협 약함

    forces = list(req.scores.keys())
    values = [max(0.0, min(10.0, float(v))) for v in req.scores.values()]
    N = len(forces)

    def force_color(v):
        if v >= 7: return C_HIGH
        if v >= 4: return C_MED
        return C_LOW

    fig = plt.figure(figsize=(14, 8), facecolor=DARK)
    gs  = gridspec.GridSpec(1, 2, figure=fig, wspace=0.35,
                            left=0.05, right=0.97, top=0.88, bottom=0.1)

    # ── Panel 1: Radar (polar) ──────────────────────────────────────────────
    ax_r = fig.add_subplot(gs[0, 0], polar=True, facecolor=SURF)
    angles = [n / N * 2 * np.pi for n in range(N)]
    angles += angles[:1]
    vals_plot = values + values[:1]

    ax_r.set_theta_offset(np.pi / 2)
    ax_r.set_theta_direction(-1)
    ax_r.set_ylim(0, 10)
    ax_r.set_yticks([2, 4, 6, 8, 10])
    ax_r.set_yticklabels(["2","4","6","8","10"], color=MUTED, fontsize=7)
    ax_r.set_xticks(angles[:-1])
    ax_r.set_xticklabels(forces, color=TEXT, fontsize=9)
    ax_r.spines["polar"].set_color(BORDER)
    ax_r.tick_params(colors=MUTED)
    ax_r.grid(color=BORDER, linewidth=0.8)

    # 배경 zone 색칠 (위험 등급)
    for thresh, col in [(10, "#ef444411"), (7, "#f59e0b11"), (4, "#22c55e11")]:
        zone = [thresh] * N + [thresh]
        ax_r.fill(angles, zone, color=col)

    ax_r.plot(angles, vals_plot, color=ACCENT, lw=2.2, zorder=3)
    ax_r.fill(angles, vals_plot, color=ACCENT, alpha=0.25, zorder=2)
    for a, v in zip(angles[:-1], values):
        ax_r.plot(a, v, "o", color=force_color(v), ms=8, zorder=4)
        ax_r.text(a, v + 0.8, f"{v:.1f}", ha="center", va="center",
                  fontsize=8, color=TEXT, fontweight="bold")

    ax_r.set_title(f"Porter 5 Forces\n{req.industry} 산업",
                   color=TEXT, fontsize=11, fontweight="bold", pad=18)

    # ── Panel 2: 수평 바 + 해석 ─────────────────────────────────────────────
    ax_b = fig.add_subplot(gs[0, 1], facecolor=SURF)
    bars = ax_b.barh(forces, values, color=[force_color(v) for v in values],
                     height=0.5, zorder=2)
    ax_b.set_xlim(0, 10)
    ax_b.axvline(4, color=MUTED, lw=0.8, ls="--", alpha=0.5)
    ax_b.axvline(7, color=MUTED, lw=0.8, ls="--", alpha=0.5)
    ax_b.text(2, -0.8, "약함", ha="center", color=C_LOW, fontsize=8)
    ax_b.text(5.5, -0.8, "보통", ha="center", color=C_MED, fontsize=8)
    ax_b.text(8.5, -0.8, "강함", ha="center", color=C_HIGH, fontsize=8)

    INTERP = {
        "경쟁강도":      {(7,10):"경쟁사 많음 → 가격경쟁↑·수익성↓", (4,7):"과점 구조 → 안정적", (0,4):"독점적 지위"},
        "신규진입 위협": {(7,10):"진입장벽 낮음 → 점유율 위협", (4,7):"중간 진입장벽", (0,4):"특허·규제·규모 장벽↑"},
        "대체재 위협":   {(7,10):"대체재 다수 → 가격결정력↓", (4,7):"부분 대체 가능", (0,4):"대체재 없음"},
        "구매자 교섭력": {(7,10):"구매자 협상력↑ → 마진압박", (4,7):"균형 협상", (0,4):"공급자 우위"},
        "공급자 교섭력": {(7,10):"원재료 공급 불안정·비용↑", (4,7):"복수 공급선 확보", (0,4):"공급 안정"},
    }

    for i, (bar, force, val) in enumerate(zip(bars, forces, values)):
        ax_b.text(val + 0.15, bar.get_y() + bar.get_height()/2,
                  f"{val:.1f}", va="center", fontsize=9,
                  color=TEXT, fontweight="bold")
        for (lo, hi), msg in INTERP.get(force, {}).items():
            if lo <= val <= hi:
                ax_b.text(10.2, bar.get_y() + bar.get_height()/2,
                          msg, va="center", fontsize=7, color=MUTED)
                break

    ax_b.set_xlabel("위협 강도 (0 = 낮음, 10 = 높음)", color=MUTED, fontsize=8)
    ax_b.tick_params(colors=TEXT, labelsize=9)
    ax_b.spines[:].set_color(BORDER)
    ax_b.set_title("5 Forces 위협 강도 분석", color=TEXT, fontsize=11,
                   fontweight="bold", pad=10)
    ax_b.set_xlim(0, 16)   # 오른쪽 텍스트 공간

    # 종합 점수
    avg = sum(values) / N
    grade = "고위험" if avg >= 7 else "중위험" if avg >= 4 else "저위험"
    grade_col = C_HIGH if avg >= 7 else C_MED if avg >= 4 else C_LOW
    fig.suptitle(
        f"산업 경쟁력 분석  |  {req.industry}  |  종합 위협 지수: {avg:.1f}/10  [{grade}]",
        color=TEXT, fontsize=12, fontweight="bold", y=0.97)
    fig.text(0.5, 0.01,
             "■ 녹색: 약함(0-4)  ■ 주황: 보통(4-7)  ■ 빨강: 강함(7-10)",
             ha="center", fontsize=8, color=MUTED)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, facecolor=DARK, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    img = "data:image/png;base64," + base64.b64encode(buf.read()).decode()

    result = {f: {"score": v, "level": "강함" if v>=7 else "보통" if v>=4 else "약함"}
              for f, v in zip(forces, values)}
    return {"image": img, "industry": req.industry,
            "avg_score": round(avg, 2), "grade": grade, "forces": result}


SECTOR_LABELS = {
    "SOXX": "반도체 (SOXX)", "XLE": "에너지 (XLE)", "XLF": "금융 (XLF)",
    "XLV":  "헬스케어 (XLV)", "XLK": "기술 (XLK)",  "XLI": "산업재 (XLI)",
    "XLY":  "소비재경기 (XLY)","XLP": "소비재필수 (XLP)","XLB": "소재 (XLB)",
    "XLRE": "부동산 (XLRE)",  "XLU": "유틸리티 (XLU)","IBB": "바이오 (IBB)",
}

@app.post("/api/industry/sector")
def industry_sector(req: SectorRequest) -> dict[str, object]:
    import yfinance as yf
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.gridspec as gridspec
    import numpy as np
    import pandas as pd
    import io, base64
    configure_matplotlib_korean_font(plt)

    DARK, SURF, BORDER, TEXT, MUTED = "#0f172a","#1e293b","#334155","#e2e8f0","#64748b"
    COLORS = ["#3b82f6","#22c55e","#f59e0b","#ef4444","#a855f7",
              "#06b6d4","#f97316","#84cc16","#ec4899","#14b8a6","#8b5cf6","#fb923c"]

    raw: dict[str, pd.Series] = {}
    for t in req.tickers:
        try:
            df = yf.download(t, period=req.period, progress=False, auto_adjust=True)
            if df.empty: continue
            s = df["Close"]
            if isinstance(s, pd.DataFrame): s = s.iloc[:, 0]
            s = s.dropna()
            if len(s) > 5: raw[t] = s
        except Exception:
            pass

    if not raw:
        raise HTTPException(status_code=503, detail="데이터를 가져올 수 없습니다.")

    fig = plt.figure(figsize=(14, 10), facecolor=DARK)
    gs  = gridspec.GridSpec(2, 2, figure=fig, hspace=0.42, wspace=0.32,
                            left=0.07, right=0.97, top=0.92, bottom=0.07)

    # Panel 1: 정규화 수익률
    ax1 = fig.add_subplot(gs[0, :], facecolor=SURF)
    for i, (t, s) in enumerate(raw.items()):
        norm = (s / s.iloc[0] - 1) * 100
        ax1.plot(norm.index, norm.values, color=COLORS[i % len(COLORS)],
                 lw=1.8, label=SECTOR_LABELS.get(t, t))
    ax1.axhline(0, color=MUTED, lw=0.8, ls="--")
    ax1.set_title("섹터 ETF 정규화 누적 수익률 (%)", color=TEXT, fontsize=10, pad=6)
    ax1.tick_params(colors=TEXT, labelsize=7)
    ax1.spines[:].set_color(BORDER)
    ax1.legend(fontsize=7, facecolor=SURF, labelcolor=TEXT,
               ncol=4, loc="upper left", framealpha=0.7)
    ax1.tick_params(axis="x", rotation=30)
    for lbl in ax1.get_xticklabels(): lbl.set_fontsize(6)

    # Panel 2: 기간 수익률 바
    ax2 = fig.add_subplot(gs[1, 0], facecolor=SURF)
    names, returns, cols = [], [], []
    for i, (t, s) in enumerate(raw.items()):
        r = (s.iloc[-1] / s.iloc[0] - 1) * 100
        names.append(SECTOR_LABELS.get(t, t))
        returns.append(r)
        cols.append(COLORS[i % len(COLORS)])
    order = sorted(range(len(returns)), key=lambda x: returns[x], reverse=True)
    names_s  = [names[i] for i in order]
    returns_s = [returns[i] for i in order]
    cols_s   = [cols[i] for i in order]
    bars = ax2.barh(names_s, returns_s, color=cols_s, height=0.6)
    ax2.axvline(0, color=MUTED, lw=0.8)
    for bar, v in zip(bars, returns_s):
        ax2.text(v + (0.3 if v >= 0 else -0.3), bar.get_y() + bar.get_height()/2,
                 f"{v:+.1f}%", va="center", ha="left" if v >= 0 else "right",
                 fontsize=7, color=TEXT)
    ax2.set_title(f"기간 수익률 순위 ({req.period})", color=TEXT, fontsize=10, pad=6)
    ax2.tick_params(colors=TEXT, labelsize=7)
    ax2.spines[:].set_color(BORDER)

    # Panel 3: 변동성 vs 수익률 (리스크-리턴 산점도)
    ax3 = fig.add_subplot(gs[1, 1], facecolor=SURF)
    for i, (t, s) in enumerate(raw.items()):
        ret  = (s.iloc[-1] / s.iloc[0] - 1) * 100
        vol  = s.pct_change().std() * (252**0.5) * 100
        ax3.scatter(vol, ret, color=COLORS[i % len(COLORS)], s=100, zorder=3)
        ax3.text(vol + 0.3, ret, SECTOR_LABELS.get(t, t).split("(")[0].strip(),
                 fontsize=6.5, color=TEXT)
    ax3.axhline(0, color=MUTED, lw=0.8, ls="--")
    ax3.set_xlabel("연환산 변동성 (%)", color=MUTED, fontsize=8)
    ax3.set_ylabel(f"수익률 ({req.period}) %", color=MUTED, fontsize=8)
    ax3.set_title("리스크-리턴 산점도", color=TEXT, fontsize=10, pad=6)
    ax3.tick_params(colors=TEXT, labelsize=7)
    ax3.spines[:].set_color(BORDER)
    ax3.grid(color=BORDER, lw=0.5, alpha=0.5)

    fig.suptitle(f"섹터 주가 비교 분석  |  {req.period}",
                 color=TEXT, fontsize=12, fontweight="bold", y=0.97)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, facecolor=DARK, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    img = "data:image/png;base64," + base64.b64encode(buf.read()).decode()

    summary = {}
    for t, s in raw.items():
        summary[SECTOR_LABELS.get(t, t)] = {
            "return_pct": round((s.iloc[-1]/s.iloc[0]-1)*100, 2),
            "annual_vol":  round(s.pct_change().std()*(252**0.5)*100, 2),
        }
    return {"image": img, "summary": summary}


@app.post("/api/industry/peer")
def industry_peer(req: PeerRequest) -> dict[str, object]:
    import math

    import yfinance as yf

    if not req.tickers:
        raise HTTPException(status_code=400, detail="비교할 종목을 1개 이상 입력하세요.")
    if len(req.tickers) > 12:
        raise HTTPException(status_code=400, detail="Peer Comparison은 최대 12개 종목까지 지원합니다.")

    def as_float(value):
        if value is None:
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if math.isnan(number) or math.isinf(number):
            return None
        return number

    rows: list[dict[str, object]] = []
    for name, ticker in req.tickers.items():
        label = (name or ticker).strip()[:40]
        symbol = (ticker or "").strip().upper()
        if not symbol:
            continue
        try:
            info = yf.Ticker(symbol).info
        except Exception as exc:
            rows.append({
                "company": label,
                "ticker": symbol,
                "error": f"데이터 수신 실패: {exc}",
            })
            continue

        market_cap = as_float(info.get("marketCap"))
        revenue_growth = as_float(info.get("revenueGrowth"))
        operating_margin = as_float(info.get("operatingMargins"))
        debt_to_equity = as_float(info.get("debtToEquity"))
        roe = as_float(info.get("returnOnEquity"))
        per = as_float(info.get("trailingPE"))
        pbr = as_float(info.get("priceToBook"))

        rows.append({
            "company": label,
            "ticker": symbol,
            "market_cap_krw_100m": round(market_cap / 1e8, 0) if market_cap is not None else None,
            "revenue_growth_pct": round(revenue_growth * 100, 1) if revenue_growth is not None else None,
            "operating_margin_pct": round(operating_margin * 100, 1) if operating_margin is not None else None,
            "per": round(per, 1) if per is not None else None,
            "pbr": round(pbr, 2) if pbr is not None else None,
            "debt_to_equity_pct": round(debt_to_equity, 1) if debt_to_equity is not None else None,
            "roe_pct": round(roe * 100, 1) if roe is not None else None,
            "currency": info.get("currency"),
            "sector": info.get("sector"),
        })

    if not rows:
        raise HTTPException(status_code=400, detail="유효한 종목 코드가 없습니다.")

    valid_rows = [r for r in rows if not r.get("error")]
    leader = None
    if valid_rows:
        leader = max(
            valid_rows,
            key=lambda r: (
                r.get("operating_margin_pct") if r.get("operating_margin_pct") is not None else -999,
                r.get("roe_pct") if r.get("roe_pct") is not None else -999,
            ),
        ).get("company")

    return {
        "rows": rows,
        "leader": leader,
        "notes": [
            "동종 기업 여부를 먼저 확인한 뒤 멀티플 차이를 해석하세요.",
            "PER/PBR은 성장률, 수익성, 재무건전성과 함께 봐야 합니다.",
            "Yahoo Finance 항목 누락 시 일부 값은 빈칸으로 표시됩니다.",
        ],
    }


LIFECYCLE_DATA = {
    "도입기": {
        "idx": 0, "color": "#3b82f6",
        "chars": ["매출 낮음·손실 가능", "높은 R&D 비용", "선도자 이점 확보 기회"],
        "strategy": ["성장주 투자", "VC/초기 투자", "기술 모멘텀 추종"],
        "examples": ["양자컴퓨터", "뇌-컴퓨터 인터페이스", "핵융합"],
    },
    "성장기": {
        "idx": 1, "color": "#22c55e",
        "chars": ["매출 급증", "경쟁자 진입 시작", "규모의 경제 달성"],
        "strategy": ["성장주 비중 확대", "시장점유율 1위 기업 주목", "PEG 지표 활용"],
        "examples": ["AI 반도체", "전기차", "클라우드"],
    },
    "성숙기": {
        "idx": 2, "color": "#f59e0b",
        "chars": ["성장 둔화", "가격경쟁 심화", "배당·자사주 매입 증가"],
        "strategy": ["가치주·배당주 투자", "PER·PBR 저평가 선별", "FCF 중심 분석"],
        "examples": ["스마트폰", "자동차", "은행"],
    },
    "쇠퇴기": {
        "idx": 3, "color": "#ef4444",
        "chars": ["매출 감소", "구조조정·M&A", "대체재에 시장 잠식"],
        "strategy": ["Short 전략 고려", "방어주 비중 축소", "Exit 타이밍 관리"],
        "examples": ["인쇄매체", "유선전화", "DVD 렌탈"],
    },
}

@app.post("/api/industry/lifecycle")
def industry_lifecycle(req: LifecycleRequest) -> dict[str, object]:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.patches as mpatches
    import numpy as np
    import io, base64
    configure_matplotlib_korean_font(plt)

    DARK, SURF, BORDER, TEXT, MUTED = "#0f172a","#1e293b","#334155","#e2e8f0","#64748b"

    stage_info = LIFECYCLE_DATA.get(req.stage, LIFECYCLE_DATA["성장기"])
    cur_idx    = stage_info["idx"]

    # S-curve (logistic)
    x = np.linspace(-6, 10, 400)
    y = 100 / (1 + np.exp(-x * 0.9))          # 도입~성숙
    decline = np.linspace(0, 1, 100)
    x_full  = np.concatenate([x, x[-1] + decline * 4])
    y_full  = np.concatenate([y, y[-1] - decline * 35])  # 쇠퇴

    # 각 단계 x 범위
    stage_x = [(-6, -1.5), (-1.5, 3), (3, 7), (7, x_full[-1])]
    stage_colors = [d["color"] for d in LIFECYCLE_DATA.values()]
    stage_names  = list(LIFECYCLE_DATA.keys())

    fig, (ax_main, ax_info) = plt.subplots(1, 2, figsize=(14, 7),
                                           gridspec_kw={"width_ratios": [3, 2]},
                                           facecolor=DARK)

    # ── 메인: S-curve ────────────────────────────────────────────────────────
    ax_main.set_facecolor(SURF)
    for i, ((x0, x1), col, name) in enumerate(zip(stage_x, stage_colors, stage_names)):
        mask = (x_full >= x0) & (x_full <= x1)
        alpha = 0.9 if i == cur_idx else 0.35
        lw    = 3.5 if i == cur_idx else 1.5
        ax_main.plot(x_full[mask], y_full[mask], color=col, lw=lw, alpha=alpha, zorder=3)
        mid_x = (x0 + x1) / 2
        mid_y = np.interp(mid_x, x_full, y_full)
        ax_main.text(mid_x, mid_y + (8 if i != 3 else -8), name,
                     ha="center", fontsize=10, color=col,
                     fontweight="bold" if i == cur_idx else "normal",
                     bbox=dict(boxstyle="round,pad=0.3",
                               facecolor=SURF if i != cur_idx else col + "33",
                               edgecolor=col, linewidth=1.5 if i == cur_idx else 0.8))
        # 단계 구분선
        if i < 3:
            ax_main.axvline(x1, color=BORDER, lw=1, ls=":", alpha=0.7)

    # 현재 위치 표시
    cur_x0, cur_x1 = stage_x[cur_idx]
    cur_mid = (cur_x0 + cur_x1) / 2
    cur_y   = np.interp(cur_mid, x_full, y_full)
    ax_main.scatter([cur_mid], [cur_y], color=stage_info["color"],
                    s=200, zorder=5, edgecolors=TEXT, linewidths=1.5)
    ax_main.annotate(f"▶ {req.industry}\n({req.stage})",
                     xy=(cur_mid, cur_y), xytext=(cur_mid + 0.5, cur_y - 18),
                     fontsize=9, color=stage_info["color"], fontweight="bold",
                     arrowprops=dict(arrowstyle="->", color=stage_info["color"], lw=1.5))

    ax_main.set_xlabel("시간 →", color=MUTED, fontsize=10)
    ax_main.set_ylabel("시장 규모 / 매출", color=MUTED, fontsize=10)
    ax_main.set_title("산업 생애주기 (Industry Life Cycle)", color=TEXT,
                      fontsize=11, fontweight="bold", pad=10)
    ax_main.tick_params(colors=MUTED, labelsize=7)
    ax_main.set_xticklabels([])
    ax_main.spines[:].set_color(BORDER)
    ax_main.set_ylim(-5, 115)

    # ── 사이드: 단계별 특성표 ────────────────────────────────────────────────
    ax_info.set_facecolor(DARK)
    ax_info.axis("off")

    y_pos = 0.97
    ax_info.text(0.5, y_pos, f"{req.industry}  |  {req.stage}", ha="center",
                 fontsize=12, fontweight="bold", color=stage_info["color"],
                 transform=ax_info.transAxes)
    y_pos -= 0.08

    sections = [
        ("특징", stage_info["chars"], "#e2e8f0"),
        ("투자 전략", stage_info["strategy"], "#3b82f6"),
        ("예시 산업", stage_info["examples"], "#a855f7"),
    ]
    for title, items, col in sections:
        ax_info.text(0.05, y_pos, title, fontsize=9, fontweight="bold",
                     color=col, transform=ax_info.transAxes)
        y_pos -= 0.06
        for item in items:
            ax_info.text(0.08, y_pos, f"• {item}", fontsize=8.5, color=TEXT,
                         transform=ax_info.transAxes)
            y_pos -= 0.065
        y_pos -= 0.02

    # 4단계 요약 타임라인
    y_pos -= 0.02
    ax_info.text(0.5, y_pos, "── 4단계 흐름 ──", ha="center",
                 fontsize=8, color=MUTED, transform=ax_info.transAxes)
    y_pos -= 0.065
    for i, (name, data) in enumerate(LIFECYCLE_DATA.items()):
        marker = "●" if i == cur_idx else "○"
        weight = "bold" if i == cur_idx else "normal"
        ax_info.text(0.1 + i * 0.22, y_pos, f"{marker}\n{name}", ha="center",
                     fontsize=8, color=data["color"], fontweight=weight,
                     transform=ax_info.transAxes)

    fig.suptitle("산업 생애주기 분석  |  단계별 투자 전략",
                 color=TEXT, fontsize=12, fontweight="bold", y=0.99)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, facecolor=DARK, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    img = "data:image/png;base64," + base64.b64encode(buf.read()).decode()

    return {"image": img, "stage": req.stage, "industry": req.industry,
            "characteristics": stage_info["chars"],
            "strategies": stage_info["strategy"]}


# ── 거시경제현황 1: yfinance 실시간 ──────────────────────────────────────────

class MacroRealtimeRequest(BaseModel):
    tickers: list[str] = ["^TNX", "CL=F", "^GSPC", "^KS11", "GC=F", "EURUSD=X"]
    period:  str       = "1y"   # 1mo 3mo 6mo 1y 2y 5y


class MarketSnapshotRequest(BaseModel):
    tickers: list[str] = ["^KS11", "^IXIC", "KRW=X"]


MARKET_SNAPSHOT_LABELS = {
    "^KS11": "KOSPI",
    "^IXIC": "NASDAQ",
    "KRW=X": "USD/KRW",
}

TICKER_LABELS = {
    "^TNX":     "미국 10년물 금리",
    "CL=F":     "WTI 유가",
    "^GSPC":    "S&P 500",
    "^KS11":    "KOSPI",
    "GC=F":     "금 (Gold)",
    "EURUSD=X": "EUR/USD",
    "BTC-USD":  "Bitcoin",
    "^IRX":     "미국 단기금리(3M)",
    "^VIX":     "VIX 공포지수",
    "DX-Y.NYB": "달러 인덱스",
}


def _extract_close_series(frame):
    close = frame["Close"]
    if hasattr(close, "columns"):
        close = close.iloc[:, 0]
    return close.dropna()


@app.post("/api/market/snapshot")
def market_snapshot(req: MarketSnapshotRequest) -> dict[str, object]:
    import pandas as pd
    import yfinance as yf

    if not req.tickers:
        raise HTTPException(status_code=400, detail="최소 1개 종목을 선택하세요.")

    fetched_at = pd.Timestamp.utcnow()
    items: list[dict[str, object]] = []

    for ticker in req.tickers:
        label = MARKET_SNAPSHOT_LABELS.get(ticker, ticker)
        try:
            tk = yf.Ticker(ticker)
            fi = tk.fast_info

            # fast_info provides near-realtime last_price (15-min delayed for most exchanges)
            current  = float(fi.last_price)
            previous = float(fi.previous_close) if fi.previous_close else current
            change_pct = ((current / previous) - 1) * 100 if previous else 0.0

            items.append({
                "ticker": ticker,
                "label": label,
                "value": round(current, 4),
                "change_pct": round(change_pct, 2),
                "latest_data_at": fetched_at.isoformat(),
                "status": "ok",
            })
        except Exception as exc:
            # fallback: last daily close
            try:
                df = yf.download(ticker, period="5d", interval="1d",
                                 progress=False, auto_adjust=False, threads=False)
                close = _extract_close_series(df)
                current  = float(close.iloc[-1])
                previous = float(close.iloc[-2]) if len(close) > 1 else current
                change_pct = ((current / previous) - 1) * 100 if previous else 0.0
                items.append({
                    "ticker": ticker, "label": label,
                    "value": round(current, 4),
                    "change_pct": round(change_pct, 2),
                    "latest_data_at": pd.Timestamp(close.index[-1]).isoformat(),
                    "status": "ok",
                })
            except Exception as exc2:
                items.append({"ticker": ticker, "label": label,
                              "status": "error", "error": str(exc2)})

    return {
        "items": items,
        "fetched_at": fetched_at.isoformat(),
    }

@app.post("/api/macro/realtime")
def macro_realtime(req: MacroRealtimeRequest) -> dict[str, object]:
    import yfinance as yf
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.gridspec as gridspec
    import numpy as np
    import pandas as pd
    import io, base64
    configure_matplotlib_korean_font(plt)

    DARK   = "#0f172a"
    SURF   = "#1e293b"
    BORDER = "#334155"
    TEXT   = "#e2e8f0"
    MUTED  = "#64748b"
    COLORS = ["#3b82f6","#22c55e","#f59e0b","#ef4444","#a855f7","#06b6d4","#f97316","#84cc16"]

    if not req.tickers:
        raise HTTPException(status_code=400, detail="최소 1개 종목을 선택하세요.")

    # ── 데이터 fetch ──────────────────────────────────────────────────────────
    raw: dict[str, pd.Series] = {}
    fetch_error: str | None = None
    fetched_at = pd.Timestamp.utcnow()

    for t in req.tickers:
        try:
            df = yf.download(t, period=req.period, progress=False, auto_adjust=True)
            if df.empty:
                continue
            close = _extract_close_series(df)
            if len(close) > 0:
                raw[t] = close
        except Exception as e:
            fetch_error = str(e)

    # ── 실시간 데이터 없을 때 GBM 시뮬레이션으로 폴백 ──────────────────────────
    is_simulated = False
    if not raw:
        is_simulated = True
        rng_fb = np.random.default_rng(42)
        n_days = {"1mo": 22, "3mo": 66, "6mo": 132, "1y": 252,
                  "2y": 504, "5y": 1260}.get(req.period, 252)
        BASE = {
            "^TNX": (4.20, 0.0, 0.40), "CL=F": (78.0, 0.03, 0.35),
            "^GSPC": (4800, 0.08, 0.17), "^KS11": (2650, 0.06, 0.18),
            "GC=F": (2000, 0.05, 0.14), "EURUSD=X": (1.08, -0.01, 0.07),
            "BTC-USD": (45000, 0.20, 0.70), "^IRX": (5.25, 0.0, 0.15),
            "^VIX": (18.0, 0.0, 0.80), "DX-Y.NYB": (104.0, 0.01, 0.06),
        }
        dt = 1 / 252
        for t in req.tickers:
            s0, mu, sigma = BASE.get(t, (100, 0.05, 0.20))
            shocks = rng_fb.standard_normal(n_days)
            log_r  = (mu - 0.5 * sigma**2) * dt + sigma * np.sqrt(dt) * shocks
            vals   = s0 * np.exp(np.cumsum(log_r))
            idx    = pd.date_range(end=pd.Timestamp.today(), periods=n_days, freq="B")
            raw[t] = pd.Series(vals, index=idx)

    labels_used = [TICKER_LABELS.get(t, t) for t in raw]

    # ── 정규화 수익률 ─────────────────────────────────────────────────────────
    norm: dict[str, pd.Series] = {}
    for t, s in raw.items():
        norm[t] = (s / s.iloc[0] - 1) * 100

    # ── 공통 날짜로 상관관계 DataFrame ────────────────────────────────────────
    combined = pd.DataFrame({TICKER_LABELS.get(t, t): s for t, s in raw.items()})
    combined = combined.dropna()
    corr = combined.pct_change().dropna().corr()

    # ── Figure ────────────────────────────────────────────────────────────────
    n = len(raw)
    fig = plt.figure(figsize=(14, 11), facecolor=DARK)
    gs  = gridspec.GridSpec(2, 2, figure=fig, hspace=0.4, wspace=0.35,
                            left=0.07, right=0.97, top=0.93, bottom=0.07)

    # Panel 1: 원시 가격 추세
    ax1 = fig.add_subplot(gs[0, 0])
    ax1.set_facecolor(SURF)
    for i, (t, s) in enumerate(raw.items()):
        ax2_ = ax1.twinx() if i > 0 else ax1
        col  = COLORS[i % len(COLORS)]
        lbl  = TICKER_LABELS.get(t, t)
        if i == 0:
            ax1.plot(s.index, s.values, color=col, lw=1.5, label=lbl)
        # 정규화 차트가 더 유용하므로 여기선 첫 종목만 왼쪽 축에 표시
    ax1.tick_params(colors=TEXT, labelsize=7)
    ax1.set_title("가격 추이 (첫 번째 종목 기준)", color=TEXT, fontsize=9, pad=6)
    ax1.spines[:].set_color(BORDER)
    ax1.set_xlabel("")
    ax1.tick_params(axis='x', rotation=30)
    for label in ax1.get_xticklabels(): label.set_fontsize(6)

    # Panel 2: 정규화 수익률 (누적 %)
    ax2 = fig.add_subplot(gs[0, 1])
    ax2.set_facecolor(SURF)
    for i, (t, s) in enumerate(norm.items()):
        ax2.plot(s.index, s.values, color=COLORS[i % len(COLORS)],
                 lw=1.5, label=TICKER_LABELS.get(t, t))
    ax2.axhline(0, color=MUTED, lw=0.8, ls="--")
    ax2.set_title("정규화 누적 수익률 (%)", color=TEXT, fontsize=9, pad=6)
    ax2.tick_params(colors=TEXT, labelsize=7)
    ax2.spines[:].set_color(BORDER)
    ax2.legend(fontsize=6, facecolor=SURF, labelcolor=TEXT,
               loc="upper left", framealpha=0.7)
    ax2.tick_params(axis='x', rotation=30)
    for label in ax2.get_xticklabels(): label.set_fontsize(6)

    # Panel 3: 상관관계 히트맵
    ax3 = fig.add_subplot(gs[1, 0])
    ax3.set_facecolor(SURF)
    if len(corr) > 1:
        cmat = corr.values
        im = ax3.imshow(cmat, cmap="RdYlGn", vmin=-1, vmax=1, aspect="auto")
        ax3.set_xticks(range(len(corr.columns)))
        ax3.set_yticks(range(len(corr.columns)))
        ax3.set_xticklabels(corr.columns, rotation=45, ha="right",
                            fontsize=7, color=TEXT)
        ax3.set_yticklabels(corr.columns, fontsize=7, color=TEXT)
        for ii in range(len(cmat)):
            for jj in range(len(cmat)):
                v = cmat[ii, jj]
                ax3.text(jj, ii, f"{v:.2f}", ha="center", va="center",
                         fontsize=7, color="white" if abs(v) > 0.5 else TEXT)
        plt.colorbar(im, ax=ax3, fraction=0.04, pad=0.02).ax.tick_params(
            labelcolor=TEXT, labelsize=7)
    else:
        ax3.text(0.5, 0.5, "2개 이상 선택 시\n상관관계 표시", ha="center",
                 va="center", color=MUTED, transform=ax3.transAxes, fontsize=9)
    ax3.set_title("수익률 상관관계 히트맵", color=TEXT, fontsize=9, pad=6)
    ax3.spines[:].set_color(BORDER)

    # Panel 4: 최근 수익률 바 차트 (1M / 3M / 기간 전체)
    ax4 = fig.add_subplot(gs[1, 1])
    ax4.set_facecolor(SURF)
    period_returns = {}
    for t, s in raw.items():
        lbl = TICKER_LABELS.get(t, t)
        period_returns[lbl] = (s.iloc[-1] / s.iloc[0] - 1) * 100
    names  = list(period_returns.keys())
    values = list(period_returns.values())
    bar_colors = [COLORS[i % len(COLORS)] for i in range(len(names))]
    bars = ax4.barh(names, values, color=bar_colors, height=0.55)
    ax4.axvline(0, color=MUTED, lw=0.8)
    for bar, v in zip(bars, values):
        ax4.text(v + (0.5 if v >= 0 else -0.5), bar.get_y() + bar.get_height()/2,
                 f"{v:+.1f}%", va="center", ha="left" if v >= 0 else "right",
                 fontsize=7, color=TEXT)
    ax4.set_title(f"기간 전체 수익률 ({req.period})", color=TEXT, fontsize=9, pad=6)
    ax4.tick_params(colors=TEXT, labelsize=7)
    ax4.spines[:].set_color(BORDER)

    title_suffix = "  [시뮬레이션 — 실시간 연결 불가]" if is_simulated else "  (Yahoo Finance)"
    fig.suptitle(f"거시경제현황 — 실시간 데이터{title_suffix}", color=TEXT,
                 fontsize=12, fontweight="bold", y=0.97)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, facecolor=DARK)
    plt.close(fig)
    buf.seek(0)
    img_b64 = "data:image/png;base64," + base64.b64encode(buf.read()).decode()

    # ── 요약 통계 ─────────────────────────────────────────────────────────────
    summary = {}
    for t, s in raw.items():
        lbl = TICKER_LABELS.get(t, t)
        ret  = (s.iloc[-1] / s.iloc[0] - 1) * 100
        vol  = s.pct_change().std() * (252 ** 0.5) * 100
        summary[lbl] = {
            "current": round(float(s.iloc[-1]), 4),
            "return_pct": round(ret, 2),
            "annual_vol_pct": round(vol, 2),
            "latest_data_at": pd.Timestamp(s.index[-1]).isoformat(),
        }

    return {"image": img_b64, "summary": summary, "period": req.period,
            "n_tickers": len(raw),
            "is_simulated": is_simulated,
            "fetched_at": fetched_at.isoformat(),
            "warning": "Yahoo Finance 요청 한도 초과로 시뮬레이션 데이터를 표시합니다. 잠시 후 다시 시도하세요." if is_simulated else None}


# ── KOSPI 섹터/종목 제외 지수 ──────────────────────────────────────────────────

KOSPI_COMPONENTS = [
    {"ticker": "005930.KS", "name": "삼성전자",        "sector": "반도체",    "weight": 0.210},
    {"ticker": "000660.KS", "name": "SK하이닉스",      "sector": "반도체",    "weight": 0.075},
    {"ticker": "373220.KS", "name": "LG에너지솔루션",  "sector": "배터리",    "weight": 0.035},
    {"ticker": "207940.KS", "name": "삼성바이오로직스", "sector": "바이오",    "weight": 0.028},
    {"ticker": "005380.KS", "name": "현대차",          "sector": "자동차",    "weight": 0.025},
    {"ticker": "000270.KS", "name": "기아",            "sector": "자동차",    "weight": 0.022},
    {"ticker": "105560.KS", "name": "KB금융",          "sector": "금융",      "weight": 0.018},
    {"ticker": "035420.KS", "name": "NAVER",           "sector": "IT/플랫폼", "weight": 0.013},
    {"ticker": "055550.KS", "name": "신한지주",        "sector": "금융",      "weight": 0.015},
    {"ticker": "006400.KS", "name": "삼성SDI",         "sector": "배터리",    "weight": 0.012},
    {"ticker": "086790.KS", "name": "하나금융지주",    "sector": "금융",      "weight": 0.012},
    {"ticker": "012330.KS", "name": "현대모비스",      "sector": "자동차",    "weight": 0.009},
    {"ticker": "051910.KS", "name": "LG화학",          "sector": "화학",      "weight": 0.010},
    {"ticker": "032830.KS", "name": "삼성생명",        "sector": "금융",      "weight": 0.008},
    {"ticker": "035720.KS", "name": "카카오",          "sector": "IT/플랫폼", "weight": 0.008},
    {"ticker": "316140.KS", "name": "우리금융지주",    "sector": "금융",      "weight": 0.007},
    {"ticker": "068270.KS", "name": "셀트리온",        "sector": "바이오",    "weight": 0.010},
    {"ticker": "005490.KS", "name": "POSCO홀딩스",     "sector": "철강",      "weight": 0.015},
    {"ticker": "017670.KS", "name": "SK텔레콤",        "sector": "통신",      "weight": 0.010},
    {"ticker": "030200.KS", "name": "KT",              "sector": "통신",      "weight": 0.008},
    {"ticker": "018260.KS", "name": "삼성SDS",         "sector": "IT/플랫폼", "weight": 0.005},
    {"ticker": "096770.KS", "name": "SK이노베이션",    "sector": "에너지",    "weight": 0.006},
    {"ticker": "034730.KS", "name": "SK",              "sector": "에너지",    "weight": 0.006},
    {"ticker": "003550.KS", "name": "LG",              "sector": "지주회사",  "weight": 0.005},
    {"ticker": "090430.KS", "name": "아모레퍼시픽",    "sector": "소비재",    "weight": 0.004},
    {"ticker": "034220.KS", "name": "LG디스플레이",    "sector": "디스플레이","weight": 0.004},
    {"ticker": "011170.KS", "name": "롯데케미칼",      "sector": "화학",      "weight": 0.003},
    {"ticker": "000120.KS", "name": "CJ대한통운",      "sector": "물류",      "weight": 0.003},
]

KOSPI_SECTORS = sorted({c["sector"] for c in KOSPI_COMPONENTS})


class MacroKospiExRequest(BaseModel):
    exclude_tickers: list[str] = []
    exclude_sectors: list[str] = []
    period: str = "1y"


@app.post("/api/macro/kospi-ex")
def macro_kospi_ex(req: MacroKospiExRequest) -> dict[str, object]:
    import yfinance as yf
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.gridspec as gridspec
    import numpy as np
    import pandas as pd
    import io, base64
    from datetime import datetime, timezone
    configure_matplotlib_korean_font(plt)

    # 제외 대상 결정
    excl_ticker_codes = {t.replace(".KS", "").replace(".KQ", "") for t in req.exclude_tickers}
    excl_sectors      = set(req.exclude_sectors)

    excluded: list[dict] = []
    included: list[dict] = []
    for comp in KOSPI_COMPONENTS:
        code = comp["ticker"].replace(".KS", "").replace(".KQ", "")
        if code in excl_ticker_codes or comp["sector"] in excl_sectors:
            excluded.append(comp)
        else:
            included.append(comp)

    total_excl_weight = sum(c["weight"] for c in excluded)
    if total_excl_weight >= 0.95:
        raise HTTPException(status_code=400, detail="제외 비중이 너무 커서 지수를 계산할 수 없습니다.")

    # 다운로드
    tickers_needed = ["^KS11"] + [c["ticker"] for c in excluded]
    raw: dict[str, pd.Series] = {}
    is_simulated = False
    fetched_at = datetime.now(timezone.utc)

    for t in tickers_needed:
        try:
            df = yf.download(t, period=req.period, progress=False, auto_adjust=True)
            if df.empty:
                raise ValueError("empty")
            s = df["Close"]
            if isinstance(s, pd.DataFrame):
                s = s.iloc[:, 0]
            s = s.dropna()
            if len(s) > 5:
                raw[t] = s
        except Exception:
            is_simulated = True

    if "^KS11" not in raw or is_simulated:
        # 시뮬레이션 대체 데이터
        import math
        rng = 12345
        def _rnd():
            nonlocal rng
            rng = (rng * 1664525 + 1013904223) % 2**32
            return rng / 2**32
        def _randn():
            u, v = max(_rnd(), 1e-10), _rnd()
            return math.sqrt(-2*math.log(u)) * math.cos(2*math.pi*v)
        period_days = {"1mo":30,"3mo":90,"6mo":180,"1y":365,"2y":730,"3y":1095}
        days = period_days.get(req.period, 365)
        n_bars = int(days * 0.72)
        base_date = pd.Timestamp("today") - pd.Timedelta(days=days)
        dates = [base_date + pd.Timedelta(days=i+1) for i in range(n_bars)]
        price = 2650.0
        prices = []
        for _ in range(n_bars):
            price = max(price * (1 + _randn() * 0.012), 100)
            prices.append(price)
        raw["^KS11"] = pd.Series(prices, index=dates)
        # 시뮬레이션된 종목 데이터
        for comp in excluded:
            price2 = 50000.0
            p2 = []
            for _ in range(n_bars):
                price2 = max(price2 * (1 + _randn() * 0.015), 100)
                p2.append(price2)
            raw[comp["ticker"]] = pd.Series(p2, index=dates)
        is_simulated = True

    kospi_s = raw["^KS11"]
    # 공통 날짜 인덱스 정렬
    common_idx = kospi_s.index
    for comp in excluded:
        if comp["ticker"] in raw:
            common_idx = common_idx.intersection(raw[comp["ticker"]].index)
    kospi_s = kospi_s.loc[common_idx]

    # 일별 수익률
    kospi_ret = kospi_s.pct_change().fillna(0)

    # 제외 종목 기여도 계산
    contrib = pd.Series(0.0, index=common_idx)
    for comp in excluded:
        if comp["ticker"] in raw:
            s = raw[comp["ticker"]].reindex(common_idx).ffill()
            ret = s.pct_change().fillna(0)
            contrib += comp["weight"] * ret

    # 조정 수익률: r_adj = (r_KOSPI - contrib_excl) / (1 - total_excl_weight)
    adj_ret = (kospi_ret - contrib) / (1 - total_excl_weight)

    # 누적 가격 지수 (100 기준)
    kospi_norm  = (1 + kospi_ret).cumprod() * 100
    adj_norm    = (1 + adj_ret).cumprod() * 100
    kospi_norm.iloc[0] = 100.0
    adj_norm.iloc[0]   = 100.0

    # 통계
    def _stats(s: pd.Series) -> dict:
        ret_pct = float((s.iloc[-1] / s.iloc[0] - 1) * 100)
        vol_pct = float(s.pct_change().std() * (252**0.5) * 100)
        return {"return_pct": round(ret_pct, 2), "annual_vol_pct": round(vol_pct, 2)}

    stats = {
        "kospi":    _stats(kospi_s),
        "adjusted": _stats(adj_norm),
        "total_excl_weight": round(total_excl_weight * 100, 1),
    }

    # 차트 그리기 (화이트 테마)
    BG    = "#ffffff"
    SURF  = "#f8f9fa"
    GRID  = "#e8e8e8"
    TEXT  = "#1a1a1a"
    MUTED = "#666666"
    C1    = "#0078d4"   # KOSPI
    C2    = "#e63946"   # 제외 후

    fig = plt.figure(figsize=(13, 8), facecolor=BG)
    gs  = gridspec.GridSpec(2, 2, figure=fig, hspace=0.44, wspace=0.30,
                            left=0.07, right=0.97, top=0.90, bottom=0.08)

    # Panel 1: 누적 수익률 비교 (상단 전체)
    ax1 = fig.add_subplot(gs[0, :], facecolor=SURF)
    excl_label = _build_excl_label(excluded, excl_sectors, total_excl_weight)
    ax1.plot(kospi_norm.index, kospi_norm.values, color=C1, lw=2.0,
             label="KOSPI (실제)", zorder=3)
    ax1.plot(adj_norm.index, adj_norm.values, color=C2, lw=2.0, ls="--",
             label=f"KOSPI 제외 후 ({excl_label})", zorder=3)
    ax1.axhline(100, color=MUTED, lw=0.8, ls=":")
    ax1.fill_between(adj_norm.index, kospi_norm.values, adj_norm.values,
                     where=(adj_norm.values > kospi_norm.values),
                     alpha=0.12, color=C2, label="제외 후 > KOSPI")
    ax1.fill_between(adj_norm.index, kospi_norm.values, adj_norm.values,
                     where=(adj_norm.values <= kospi_norm.values),
                     alpha=0.12, color=C1, label="KOSPI > 제외 후")
    ax1.set_title(f"KOSPI vs KOSPI 제외 후 비교  |  {req.period}", color=TEXT, fontsize=11, pad=8, fontweight="bold")
    ax1.tick_params(colors=MUTED, labelsize=7)
    for sp in ax1.spines.values(): sp.set_color(GRID)
    ax1.grid(color=GRID, lw=0.6, alpha=0.8)
    ax1.tick_params(axis="x", rotation=20)
    ax1.legend(fontsize=8, facecolor=BG, labelcolor=TEXT, framealpha=0.9, loc="upper left")
    ax1.set_facecolor(BG)

    # Panel 2: 수익률 차이 (하단 좌)
    ax2 = fig.add_subplot(gs[1, 0], facecolor=BG)
    diff = adj_norm.values - kospi_norm.values
    colors_diff = [C2 if d > 0 else C1 for d in diff]
    ax2.bar(range(len(diff)), diff, color=colors_diff, alpha=0.7, width=1.0)
    ax2.axhline(0, color=MUTED, lw=0.8)
    ax2.set_title("KOSPI 대비 초과 성과 (제외 후 - 실제)", color=TEXT, fontsize=9, pad=6)
    ax2.tick_params(colors=MUTED, labelsize=7)
    for sp in ax2.spines.values(): sp.set_color(GRID)
    ax2.grid(color=GRID, lw=0.6, axis="y", alpha=0.8)
    ax2.set_xticks([])
    ax2.set_facecolor(BG)

    # Panel 3: 제외 종목 기여 비중 파이 (하단 우)
    ax3 = fig.add_subplot(gs[1, 1], facecolor=BG)
    if excluded:
        pie_labels = [c["name"] for c in excluded]
        pie_sizes  = [c["weight"] for c in excluded]
        SECTOR_COLORS = ["#0078d4","#e63946","#2dc653","#f59e0b","#a855f7",
                         "#06b6d4","#f97316","#ec4899","#14b8a6","#8b5cf6"]
        wedge_colors = [SECTOR_COLORS[i % len(SECTOR_COLORS)] for i in range(len(pie_sizes))]
        wedges, texts, autotexts = ax3.pie(
            pie_sizes, labels=pie_labels, colors=wedge_colors,
            autopct=lambda p: f"{p:.1f}%" if p > 3 else "",
            pctdistance=0.78, startangle=90,
            wedgeprops={"edgecolor": BG, "linewidth": 1.5},
            textprops={"fontsize": 7, "color": TEXT},
        )
        for at in autotexts: at.set_fontsize(6.5)
        ax3.set_title(f"제외 종목 구성  (총 비중 {total_excl_weight*100:.1f}%)", color=TEXT, fontsize=9, pad=6)
    else:
        ax3.text(0.5, 0.5, "제외 종목 없음", ha="center", va="center", color=MUTED, fontsize=10)
        ax3.set_title("제외 종목 구성", color=TEXT, fontsize=9, pad=6)
    ax3.set_facecolor(BG)

    title_excl = excl_label if excl_label else "없음"
    fig.suptitle(f"KOSPI 제외 지수 분석  |  제외: {title_excl}",
                 color=TEXT, fontsize=12, fontweight="bold", y=0.96)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, facecolor=BG, bbox_inches="tight")
    plt.close(fig)
    img_b64 = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()

    return {
        "image": img_b64,
        "stats": stats,
        "excluded": [{"name": c["name"], "ticker": c["ticker"].replace(".KS","").replace(".KQ",""),
                      "sector": c["sector"], "weight_pct": round(c["weight"]*100,1)} for c in excluded],
        "period": req.period,
        "is_simulated": is_simulated,
        "fetched_at": fetched_at.isoformat(),
        "warning": "Yahoo Finance 데이터 수신 실패로 시뮬레이션 데이터를 표시합니다." if is_simulated else None,
    }


def _build_excl_label(excluded: list, excl_sectors: set, total_weight: float) -> str:
    if not excluded:
        return "없음"
    sector_names = sorted(excl_sectors) if excl_sectors else []
    stock_names  = [c["name"] for c in excluded if c["sector"] not in excl_sectors]
    parts = sector_names + stock_names
    label = ", ".join(parts[:3])
    if len(parts) > 3:
        label += f" 외 {len(parts)-3}개"
    return label


@app.get("/api/macro/kospi-ex/meta")
def macro_kospi_ex_meta() -> dict[str, object]:
    sectors = sorted({c["sector"] for c in KOSPI_COMPONENTS})
    components = [
        {"ticker": c["ticker"].replace(".KS","").replace(".KQ",""),
         "name": c["name"], "sector": c["sector"],
         "weight_pct": round(c["weight"]*100, 1)}
        for c in KOSPI_COMPONENTS
    ]
    return {"sectors": sectors, "components": components}


# ── 거시경제현황 2: GBM 시뮬레이션 대시보드 ──────────────────────────────────

class MacroSimRequest(BaseModel):
    n_days:    int   = 252
    seed:      int   = 42

@app.post("/api/macro/simulation")
def macro_simulation(req: MacroSimRequest) -> dict[str, object]:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.gridspec as gridspec
    import numpy as np
    import io, base64
    configure_matplotlib_korean_font(plt)

    DARK   = "#0f172a"
    SURF   = "#1e293b"
    BORDER = "#334155"
    TEXT   = "#e2e8f0"
    MUTED  = "#64748b"
    COLORS = ["#3b82f6","#f59e0b","#ef4444","#22c55e","#a855f7","#06b6d4"]

    rng = np.random.default_rng(req.seed)
    T   = max(60, min(req.n_days, 1260))
    dt  = 1 / 252

    def gbm(s0, mu, sigma, n, rng):
        shocks = rng.standard_normal(n)
        log_r  = (mu - 0.5 * sigma**2) * dt + sigma * np.sqrt(dt) * shocks
        return s0 * np.exp(np.cumsum(log_r))

    indicators = {
        "기준금리 (%)" :   {"s0": 3.50,  "mu":  0.05, "sigma": 0.08,  "fmt": ".2f"},
        "CPI (전년비 %)":  {"s0": 3.20,  "mu":  0.02, "sigma": 0.12,  "fmt": ".2f"},
        "WTI 유가 ($)":    {"s0": 78.0,  "mu":  0.03, "sigma": 0.30,  "fmt": ".1f"},
        "USD/KRW":         {"s0": 1320,  "mu": -0.01, "sigma": 0.07,  "fmt": ".0f"},
        "KOSPI":           {"s0": 2650,  "mu":  0.06, "sigma": 0.18,  "fmt": ".0f"},
        "S&P 500":         {"s0": 5200,  "mu":  0.08, "sigma": 0.16,  "fmt": ".0f"},
    }

    # macro regime: 경기 사이클 phase 추가 (상승/둔화/침체/회복)
    phase_len  = T // 4
    phases     = ["상승기", "과열기", "침체기", "회복기"]
    phase_muls = [1.0, 0.5, -0.5, 1.2]

    series_dict = {}
    for name, cfg in indicators.items():
        mu_adj = cfg["mu"]
        vals = []
        for ph_i, mul in enumerate(phase_muls):
            seg = gbm(cfg["s0"] if not vals else vals[-1],
                      mu_adj * mul, cfg["sigma"],
                      min(phase_len, T - len(vals)), rng)
            vals.extend(seg.tolist())
            if len(vals) >= T:
                break
        series_dict[name] = np.array(vals[:T])

    days = np.arange(T)

    # ── Figure ────────────────────────────────────────────────────────────────
    fig = plt.figure(figsize=(14, 12), facecolor=DARK)
    gs  = gridspec.GridSpec(3, 2, figure=fig, hspace=0.45, wspace=0.35,
                            left=0.08, right=0.97, top=0.93, bottom=0.05)

    names_list = list(series_dict.keys())
    for idx, (name, vals) in enumerate(series_dict.items()):
        row, col = divmod(idx, 2)
        ax = fig.add_subplot(gs[row, col])
        ax.set_facecolor(SURF)
        color = COLORS[idx]
        cfg   = indicators[name]

        ax.plot(days, vals, color=color, lw=1.5)
        ax.fill_between(days, vals, vals[0], alpha=0.12, color=color)

        # 경기국면 배경
        for ph_i, (ph_name, mul) in enumerate(zip(phases, phase_muls)):
            x0 = ph_i * phase_len
            x1 = min((ph_i + 1) * phase_len, T)
            bg = "#22c55e22" if mul > 0.8 else "#f59e0b22" if mul > 0 else "#ef444422"
            ax.axvspan(x0, x1, color=bg, alpha=0.4)
            ax.text((x0 + x1) / 2, ax.get_ylim()[0], ph_name,
                    ha="center", va="bottom", fontsize=6, color=MUTED)

        cur  = vals[-1]
        chg  = (cur / vals[0] - 1) * 100
        sign = "+" if chg >= 0 else ""
        ax.set_title(f"{name}  현재: {cur:{cfg['fmt']}}  ({sign}{chg:.1f}%)",
                     color=TEXT, fontsize=8.5, pad=5)
        ax.tick_params(colors=TEXT, labelsize=7)
        ax.spines[:].set_color(BORDER)
        ax.set_xlim(0, T)

        # 최고/최저 표시
        hi, lo = np.argmax(vals), np.argmin(vals)
        ax.annotate(f"고: {vals[hi]:{cfg['fmt']}}",
                    xy=(hi, vals[hi]), xytext=(5, 5), textcoords="offset points",
                    fontsize=6, color="#22c55e", arrowprops=dict(arrowstyle="->", color=MUTED, lw=0.5))
        ax.annotate(f"저: {vals[lo]:{cfg['fmt']}}",
                    xy=(lo, vals[lo]), xytext=(5, -12), textcoords="offset points",
                    fontsize=6, color="#ef4444", arrowprops=dict(arrowstyle="->", color=MUTED, lw=0.5))

    # 경기국면 범례 (우측 상단)
    from matplotlib.patches import Patch
    legend_els = [
        Patch(facecolor="#22c55e44", label="상승기"),
        Patch(facecolor="#f59e0b44", label="과열기"),
        Patch(facecolor="#ef444444", label="침체기"),
        Patch(facecolor="#22c55e44", label="회복기"),
    ]
    fig.legend(handles=legend_els, loc="upper right", fontsize=7,
               facecolor=SURF, labelcolor=TEXT, framealpha=0.8, ncol=4,
               bbox_to_anchor=(0.97, 0.995))

    fig.suptitle(f"거시경제 시뮬레이션 대시보드 — {T}거래일 GBM 시뮬레이션",
                 color=TEXT, fontsize=12, fontweight="bold", y=0.975)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, facecolor=DARK)
    plt.close(fig)
    buf.seek(0)
    img_b64 = "data:image/png;base64," + base64.b64encode(buf.read()).decode()

    summary = {name: {"start": round(float(v[0]), 2),
                      "end":   round(float(v[-1]), 2),
                      "chg_pct": round((v[-1]/v[0]-1)*100, 2)}
               for name, v in series_dict.items()}

    return {"image": img_b64, "summary": summary, "n_days": T}


class QuizQuestionUpdate(BaseModel):
    question: str = Field(min_length=5, max_length=500)
    choices: list[str] = Field(min_length=4, max_length=4)
    answer: int = Field(ge=0, le=3)
    explanation: str = Field(default="", max_length=2000)


QUIZ_SEED_01_02: list[dict[str, object]] = [
    {
        "day": 1, "question_no": 1, "source_doc": "01", "topic": "법적 주체",
        "question": "법인이 자연인과 구별되는 핵심 특징으로 가장 적절한 것은?",
        "choices": ["출생으로 성립한다", "법이 권리·의무 능력을 부여한 인위적 존재다", "세금을 내지 않는다", "해산해도 권리능력이 유지된다"],
        "answer": 1,
        "explanation": "법인은 설립 등기를 통해 성립하며, 법이 권리·의무의 주체성을 부여한 인위적 존재입니다.",
    },
    {
        "day": 1, "question_no": 2, "source_doc": "01", "topic": "사업자 형태",
        "question": "개인사업자와 법인사업자의 비교로 옳은 것은?",
        "choices": ["개인사업자는 대표자 급여를 비용 처리할 수 있다", "법인사업자는 무한책임이 원칙이다", "법인사업자는 주식 발행 등 외부 투자 유치가 상대적으로 용이하다", "개인사업자는 법인세를 낸다"],
        "answer": 2,
        "explanation": "법인사업자는 별도 법인격을 바탕으로 지분 구조 설계와 외부 투자 유치가 상대적으로 유리합니다.",
    },
    {
        "day": 1, "question_no": 3, "source_doc": "01", "topic": "책임 구조",
        "question": "유한책임의 의미로 가장 적절한 것은?",
        "choices": ["사업채무를 국가가 모두 대신 갚아준다", "출자액 한도 내에서만 책임을 부담한다", "대표이사가 모든 채무를 개인 재산으로 갚는다", "모든 주주는 무한책임을 진다"],
        "answer": 1,
        "explanation": "유한책임은 투자자가 출자한 금액 범위 내에서만 손실을 부담하는 구조입니다.",
    },
    {
        "day": 1, "question_no": 4, "source_doc": "01", "topic": "회사 형태",
        "question": "상법상 회사 형태 중 실제로 가장 보편적으로 사용되는 형태는?",
        "choices": ["합명회사", "합자회사", "유한회사", "주식회사"],
        "answer": 3,
        "explanation": "문서에서는 주식회사가 공개·상장 및 자본조달 측면에서 가장 보편적 형태로 제시됩니다.",
    },
    {
        "day": 1, "question_no": 5, "source_doc": "01", "topic": "주식회사 구조",
        "question": "주식회사의 최고 의사결정 기관은?",
        "choices": ["대표이사", "이사회", "주주총회", "감사"],
        "answer": 2,
        "explanation": "주주총회가 이사 선임, 정관 변경 등 핵심 사항을 결의하는 최고 의사결정 기관입니다.",
    },
    {
        "day": 1, "question_no": 6, "source_doc": "01", "topic": "주식 개념",
        "question": "보통주에 대한 설명으로 가장 적절한 것은?",
        "choices": ["의결권이 없다", "배당을 절대 받을 수 없다", "일반적으로 1주 1의결권을 가진다", "시가가 항상 액면가와 같다"],
        "answer": 2,
        "explanation": "문서 기준 보통주는 의결권이 있고 일반적으로 1주당 1표를 행사합니다.",
    },
    {
        "day": 1, "question_no": 7, "source_doc": "01", "topic": "소득세",
        "question": "소득세(개인)와 법인세(법인)의 비교로 옳은 것은?",
        "choices": ["소득세 최고세율이 법인세 최고세율보다 낮다", "법인세는 누진구조가 없다", "소득세는 누진세율 구조를 갖고 최고세율이 상대적으로 높다", "법인세는 지방소득세가 없다"],
        "answer": 2,
        "explanation": "문서에서 소득세는 6~45%, 법인세는 9~24%로 제시되어 개인 소득세 누진이 더 가파릅니다.",
    },
    {
        "day": 1, "question_no": 8, "source_doc": "01", "topic": "부가가치세",
        "question": "부가가치세(VAT)에 대한 설명으로 옳은 것은?",
        "choices": ["세율은 5%가 기본이다", "납부세액은 매출세액-매입세액으로 계산한다", "지방세만 해당된다", "직접세에 해당한다"],
        "answer": 1,
        "explanation": "문서에서는 부가가치세 기본세율 10%, 납부액은 매출세액에서 매입세액을 차감하는 구조로 설명합니다.",
    },
    {
        "day": 1, "question_no": 9, "source_doc": "01", "topic": "원천징수",
        "question": "금융소득(이자·배당)의 기본 원천징수 세율로 문서에 제시된 값은?",
        "choices": ["3.3%", "10.0%", "15.4%", "22.0%"],
        "answer": 2,
        "explanation": "이자·배당소득은 14% + 지방소득세 1.4%로 총 15.4% 원천징수로 제시됩니다.",
    },
    {
        "day": 1, "question_no": 10, "source_doc": "01", "topic": "직접세/간접세",
        "question": "직접세에 해당하는 항목은?",
        "choices": ["부가가치세", "주세", "소득세", "담배소비세"],
        "answer": 2,
        "explanation": "소득세는 납세자와 담세자가 동일한 직접세로 분류됩니다.",
    },
    {
        "day": 1, "question_no": 11, "source_doc": "01", "topic": "간접세",
        "question": "간접세의 일반적 특징으로 가장 적절한 것은?",
        "choices": ["누진세만 가능하다", "납세자와 담세자가 동일하다", "판매자가 납부하지만 소비자가 실질 부담할 수 있다", "지방정부만 부과할 수 있다"],
        "answer": 2,
        "explanation": "부가가치세처럼 판매자가 신고·납부하나 최종 부담은 소비자에게 전가되는 구조가 대표적입니다.",
    },
    {
        "day": 1, "question_no": 12, "source_doc": "01", "topic": "금융소득 종합과세",
        "question": "연간 금융소득(이자+배당)이 얼마를 초과하면 종합소득에 합산 과세되는가?",
        "choices": ["1,000만원", "2,000만원", "3,000만원", "5,000만원"],
        "answer": 1,
        "explanation": "문서에는 금융소득 합계 2,000만원 초과 시 종합과세로 설명되어 있습니다.",
    },
    {
        "day": 1, "question_no": 13, "source_doc": "01", "topic": "지방소득세",
        "question": "지방소득세에 대한 설명으로 옳은 것은?",
        "choices": ["소득세·법인세와 무관하다", "소득세·법인세 산출세액의 10%를 별도로 납부한다", "국세청이 아닌 거래소에 납부한다", "법인에게만 부과된다"],
        "answer": 1,
        "explanation": "문서에서는 지방소득세가 소득세·법인세 산출세액의 10%로 제시됩니다.",
    },
    {
        "day": 1, "question_no": 14, "source_doc": "01", "topic": "금융소비자보호법",
        "question": "금융소비자보호법의 핵심 체계로 문서에서 제시한 것은?",
        "choices": ["3대 판매 원칙", "6대 판매 원칙", "10대 내부통제 원칙", "위법계약 자동 무효"],
        "answer": 1,
        "explanation": "문서에서는 금소법 핵심 체계로 6대 판매원칙과 소비자 권리를 강조합니다.",
    },
    {
        "day": 1, "question_no": 15, "source_doc": "01", "topic": "예금자보호",
        "question": "예금자보호법 관련 설명으로 옳은 것은?",
        "choices": ["주식·펀드도 동일하게 전액 보호된다", "같은 금융기관 내 계좌는 1인당 5,000만원 한도로 합산 보호된다", "법인은 보호 대상에서 제외된다", "외화예금은 항상 전액 보호된다"],
        "answer": 1,
        "explanation": "문서에서는 1인당 5,000만원 한도, 동일 금융기관 내 합산 보호 원칙을 설명합니다.",
    },
    {
        "day": 1, "question_no": 16, "source_doc": "02", "topic": "업종/업태",
        "question": "업종과 업태의 차이를 가장 잘 설명한 것은?",
        "choices": ["업종=어떻게 팔아, 업태=무엇을 팔아", "업종=무엇을 팔아, 업태=어떻게 팔아", "둘은 완전히 동일하다", "업종은 세무와 무관하다"],
        "answer": 1,
        "explanation": "문서에서 업종은 재화·서비스 종류, 업태는 영업·유통 방식으로 구분합니다.",
    },
    {
        "day": 1, "question_no": 17, "source_doc": "02", "topic": "산업 분류",
        "question": "GICS에 대한 설명으로 가장 적절한 것은?",
        "choices": ["한국 통계청이 만든 분류다", "글로벌 ETF·지수 분석에 쓰이는 표준 산업 분류다", "세금 신고 전용 분류다", "2단계 구조만 가진다"],
        "answer": 1,
        "explanation": "GICS는 MSCI와 S&P가 만든 글로벌 산업 분류 체계로 포트폴리오 분석에 널리 활용됩니다.",
    },
    {
        "day": 1, "question_no": 18, "source_doc": "02", "topic": "가치사슬",
        "question": "가치사슬에서 소비자와 가장 가까운 단계는?",
        "choices": ["업스트림", "미드스트림", "다운스트림", "백오피스"],
        "answer": 2,
        "explanation": "다운스트림은 유통·소매·서비스 등 최종 소비자 접점에 가까운 단계입니다.",
    },
    {
        "day": 1, "question_no": 19, "source_doc": "02", "topic": "경기 사이클",
        "question": "경기 확장기(Expansion)의 특징으로 문서에 제시된 것은?",
        "choices": ["실업 급등, GDP 감소", "소비·투자 증가와 물가 상승", "금리 급인하", "필수소비재만 강세"],
        "answer": 1,
        "explanation": "확장기는 소비·투자 증가, 성장 가속과 함께 에너지·소재·IT 등이 강세로 제시됩니다.",
    },
    {
        "day": 1, "question_no": 20, "source_doc": "02", "topic": "경기 침체",
        "question": "경기 침체기(Recession)에서 상대적 강세 섹터로 제시된 것은?",
        "choices": ["에너지·산업재", "금융·부동산", "필수소비재·채권·금", "경기소비재"],
        "answer": 2,
        "explanation": "문서에서는 침체기에 필수소비재, 금, 국채 같은 방어적 자산이 강세라고 설명합니다.",
    },
    {
        "day": 1, "question_no": 21, "source_doc": "02", "topic": "핵심 지표",
        "question": "거시지표와 설명의 연결이 옳은 것은?",
        "choices": ["CPI: 노동시장", "실업률: 인플레이션 수준", "GDP 성장률: 경제 규모 성장 속도", "환율: 기업 내부 회계기준"],
        "answer": 2,
        "explanation": "GDP 성장률은 경제 규모 성장 속도를 나타내는 대표 거시지표입니다.",
    },
    {
        "day": 1, "question_no": 22, "source_doc": "02", "topic": "기준금리/시장금리",
        "question": "다음 중 시장금리에 해당하는 것은?",
        "choices": ["한국은행 기준금리", "연준 정책금리 목표범위", "국채 10년물 금리", "법정 최고금리"],
        "answer": 2,
        "explanation": "국채 10년물 금리처럼 시장 수요·공급으로 형성되는 금리가 시장금리입니다.",
    },
    {
        "day": 1, "question_no": 23, "source_doc": "02", "topic": "금리 구조",
        "question": "문서의 금리 구조 설명에서 단기 시장금리 예시는?",
        "choices": ["국고채 30년", "CD금리(91일)", "회사채 영구채", "우선주 배당수익률"],
        "answer": 1,
        "explanation": "CD금리(91일), 콜금리 등이 단기 시장금리 예시로 제시됩니다.",
    },
    {
        "day": 1, "question_no": 24, "source_doc": "02", "topic": "금리-주가 관계",
        "question": "일반적으로 금리 상승 시 주가에 하방 압력이 커지는 이유로 옳지 않은 것은?",
        "choices": ["기업 차입비용 증가", "미래이익 할인율 상승", "채권의 상대적 매력 증가", "모든 기업의 매출이 즉시 증가"],
        "answer": 3,
        "explanation": "금리 상승은 보통 자금조달 부담을 키워 주가에 부담이며, 매출 즉시 증가가 일반적 결과는 아닙니다.",
    },
    {
        "day": 1, "question_no": 25, "source_doc": "02", "topic": "금리-채권 관계",
        "question": "채권 가격과 금리의 관계로 가장 적절한 것은?",
        "choices": ["같은 방향으로 움직인다", "금리와 무관하다", "금리 상승 시 기존 채권 가격은 하락하는 경향이 있다", "쿠폰이 고정이면 가격도 항상 고정된다"],
        "answer": 2,
        "explanation": "금리 상승 시 기존 저쿠폰 채권 매력이 낮아져 채권 가격이 하락하는 역관계가 일반적입니다.",
    },
    {
        "day": 1, "question_no": 26, "source_doc": "02", "topic": "통화정책 기관",
        "question": "한국은행 금통위에 대한 설명으로 문서와 일치하는 것은?",
        "choices": ["연 12회 정기회의", "총재 포함 7명 구성, 연 8회 정기회의", "정책금리 결정 권한이 없다", "오직 환율만 보고 결정한다"],
        "answer": 1,
        "explanation": "문서에서 금통위는 총재 포함 7명, 연 8회 정기회의로 기준금리를 결정한다고 설명합니다.",
    },
    {
        "day": 1, "question_no": 27, "source_doc": "02", "topic": "FOMC",
        "question": "FOMC의 정책 신호 중 '매파(Hawkish)'가 의미하는 것은?",
        "choices": ["금리 인하 선호", "경기 부양 우선", "금리 인상 선호 및 인플레이션 억제 우선", "양적완화 무조건 확대"],
        "answer": 2,
        "explanation": "매파적 스탠스는 물가 안정 우선, 상대적으로 긴축적 금리 정책 선호를 의미합니다.",
    },
    {
        "day": 1, "question_no": 28, "source_doc": "02", "topic": "데이터 소스",
        "question": "국내 기준금리·국고채·CD금리 데이터를 공식적으로 조회할 때 문서에서 우선 권장한 출처는?",
        "choices": ["네이버 금융 기사", "한국은행 ECOS", "개인 블로그", "임의 커뮤니티 엑셀 파일"],
        "answer": 1,
        "explanation": "문서에서는 국내 금리 데이터의 1차 신뢰 원천으로 한국은행 ECOS를 강조합니다.",
    },
    {
        "day": 1, "question_no": 29, "source_doc": "02", "topic": "FRED 시리즈",
        "question": "문서에서 금리 실습 시 자주 사용하는 FRED 시리즈로 언급된 것은?",
        "choices": ["FEDFUNDS, DGS2, DGS10", "BTCUSD, ETHUSD, DOGEUSD", "KOSPI, KOSDAQ, KRX300", "GDPDEF, CPIAUCSL만 단독 사용"],
        "answer": 0,
        "explanation": "문서에 FEDFUNDS, DGS2, DGS10, DGS30, T10Y2Y 등이 대표 시리즈로 제시됩니다.",
    },
    {
        "day": 1, "question_no": 30, "source_doc": "02", "topic": "스프레드 해석",
        "question": "장단기 금리 스프레드를 볼 때 주의할 점으로 가장 적절한 것은?",
        "choices": ["장단기 금리 차이는 의미가 없다", "정책금리와 시장금리를 항상 동일 지표로 취급한다", "국가·통화·지표 정의 차이를 확인하고 방향성과 함께 해석한다", "스프레드가 0이면 무조건 강세장이다"],
        "answer": 2,
        "explanation": "문서에서는 지표 정의(정책/시장금리), 국가 차이를 확인하고 스프레드의 방향성을 함께 해석하라고 설명합니다.",
    },
]


def _quiz_collection():
    return get_db()["quiz_questions"]


def _serialize_quiz_question(doc: dict) -> dict:
    data = dict(doc)
    data["_id"] = str(data["_id"])
    return data


@lru_cache(maxsize=1)
def _quiz_seed_from_sql() -> list[dict[str, object]]:
    if not QUIZ_SQL_PATH.exists():
        return []

    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(QUIZ_SQL_PATH.read_text(encoding="utf-8"))
        rows = conn.execute(
            """
            SELECT day, question_no, source_doc, topic, question,
                   choice_1, choice_2, choice_3, choice_4,
                   answer, explanation
              FROM quiz_questions
             ORDER BY day, question_no
            """
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()

    seeds: list[dict[str, object]] = []
    for (
        day,
        question_no,
        source_doc,
        topic,
        question,
        choice_1,
        choice_2,
        choice_3,
        choice_4,
        answer,
        explanation,
    ) in rows:
        seeds.append(
            {
                "day": int(day),
                "question_no": int(question_no),
                "source_doc": source_doc,
                "topic": topic,
                "question": question,
                "choices": [choice_1, choice_2, choice_3, choice_4],
                "answer": int(answer),
                "explanation": explanation,
            }
        )

    return seeds


async def _seed_quiz_questions() -> int:
    coll = _quiz_collection()
    await coll.create_index([("day", 1), ("question_no", 1)], unique=True)
    quiz_seed = _quiz_seed_from_sql() or QUIZ_SEED_01_02
    inserted = 0
    for q in quiz_seed:
        payload = dict(q)
        try:
            result = await coll.update_one(
                {"day": payload["day"], "question_no": payload["question_no"]},
                {"$setOnInsert": payload},
                upsert=True,
            )
        except DuplicateKeyError:
            continue
        if result.upserted_id:
            inserted += 1
    return inserted


@app.get("/api/quiz/day/{day}")
async def get_quiz_by_day(day: int) -> list[dict]:
    coll = _quiz_collection()
    rows = await coll.find({"day": day}).sort("question_no", 1).to_list(length=100)
    return [_serialize_quiz_question(r) for r in rows]


@app.patch("/api/quiz/questions/{question_id}")
async def update_quiz_question(question_id: str, payload: QuizQuestionUpdate) -> dict:
    try:
        oid = ObjectId(question_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="유효하지 않은 문항 ID입니다.") from exc

    choices = [c.strip() for c in payload.choices]
    if any(not c for c in choices):
        raise HTTPException(status_code=400, detail="보기는 빈 문자열일 수 없습니다.")

    coll = _quiz_collection()
    result = await coll.update_one(
        {"_id": oid},
        {
            "$set": {
                "question": payload.question.strip(),
                "choices": choices,
                "answer": payload.answer,
                "explanation": payload.explanation.strip(),
            }
        },
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="문항을 찾을 수 없습니다.")

    updated = await coll.find_one({"_id": oid})
    return _serialize_quiz_question(updated) if updated else {}


@app.get("/api/quiz/days")
async def get_quiz_days() -> list[dict]:
    coll = _quiz_collection()
    pipeline = [
        {"$group": {"_id": "$day", "count": {"$sum": 1}}},
        {"$sort": {"_id": 1}},
        {"$project": {"day": "$_id", "count": 1, "_id": 0}},
    ]
    result = await coll.aggregate(pipeline).to_list(length=100)
    return result


@app.post("/api/quiz/seed")
async def seed_quiz_questions() -> dict[str, int]:
    inserted = await _seed_quiz_questions()
    coll = _quiz_collection()
    total = await coll.count_documents({})
    return {"inserted": inserted, "total_questions": total}


@app.get("/api/quiz/seed-script")
def get_quiz_seed_script() -> dict[str, str]:
    quiz_seed = _quiz_seed_from_sql() or QUIZ_SEED_01_02
    script = "db.quiz_questions.insertMany(" + json.dumps(quiz_seed, ensure_ascii=False, indent=2) + ");"
    return {"script": script}


class CompanyFinancialsRequest(BaseModel):
    ticker: str = Field(default="AAPL", min_length=1, max_length=30)
    period: str = Field(default="annual", pattern="^(annual|quarterly)$")


@app.post("/api/finance/company-financials")
def company_financials(req: CompanyFinancialsRequest) -> dict[str, object]:
    """Return structured financial data for a ticker using yfinance."""
    import math
    import yfinance as yf
    import pandas as pd

    def safe_float(v) -> float | None:
        if v is None:
            return None
        try:
            f = float(v)
            return None if (math.isnan(f) or math.isinf(f)) else f
        except (TypeError, ValueError):
            return None

    def row(df: "pd.DataFrame", *keys: str) -> "pd.Series":
        for key in keys:
            if key in df.index:
                return df.loc[key]
        return pd.Series(dtype=float)

    def series_to_list(series: "pd.Series") -> list[dict]:
        result = []
        for idx, val in series.items():
            label = str(idx)[:7] if hasattr(idx, "strftime") else str(idx)[:10]
            result.append({"period": label, "value": safe_float(val)})
        return list(reversed(result))

    ticker_sym = req.ticker.strip().upper()
    try:
        tk = yf.Ticker(ticker_sym)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"티커 오류: {exc}") from exc

    # Select annual vs quarterly statements
    if req.period == "annual":
        income  = tk.income_stmt
        balance = tk.balance_sheet
        cashflow = tk.cashflow
    else:
        income  = tk.quarterly_income_stmt
        balance = tk.quarterly_balance_sheet
        cashflow = tk.quarterly_cashflow

    if income is None or income.empty:
        raise HTTPException(status_code=404, detail=f"'{ticker_sym}' 재무데이터를 찾을 수 없습니다.")

    # ── Income statement rows ──────────────────────────────────────────────────
    revenue       = row(income, "Total Revenue")
    cogs          = row(income, "Cost Of Revenue")
    gross_profit  = row(income, "Gross Profit")
    op_expense    = row(income, "Operating Expense")
    op_income     = row(income, "Operating Income", "EBIT")
    other_income  = row(income, "Other Income Expense",
                        "Other Non Operating Income Expense",
                        "Non Operating Income")
    pretax        = row(income, "Pretax Income")
    tax           = row(income, "Tax Provision")
    net_income    = row(income, "Net Income")

    # ── Balance sheet rows ────────────────────────────────────────────────────
    total_debt = row(balance, "Total Debt", "Long Term Debt")
    cash       = row(balance,
                     "Cash And Cash Equivalents",
                     "Cash Cash Equivalents And Short Term Investments")

    # ── Cash flow rows ─────────────────────────────────────────────────────────
    op_cf  = row(cashflow, "Operating Cash Flow",
                 "Cash Flow From Continuing Operating Activities")
    capex  = row(cashflow, "Capital Expenditure")

    # Free Cash Flow = Operating CF + Capex (capex stored as negative)
    if not op_cf.empty and not capex.empty:
        shared_idx = op_cf.index.intersection(capex.index)
        fcf = op_cf.loc[shared_idx] + capex.loc[shared_idx]
    elif not op_cf.empty:
        fcf = op_cf
    else:
        fcf = pd.Series(dtype=float)

    # Net margin %
    margin_data: list[dict] = []
    for idx in revenue.index:
        r = safe_float(revenue.get(idx))
        n = safe_float(net_income.get(idx))
        label = str(idx)[:7]
        if r and n and r != 0:
            margin_data.append({"period": label, "value": round(n / r * 100, 2)})
    margin_data = list(reversed(margin_data))

    # ── Waterfall (most recent period) ────────────────────────────────────────
    def wf(series: "pd.Series") -> float | None:
        return safe_float(series.iloc[0]) if not series.empty else None

    waterfall = {
        "revenue":          wf(revenue),
        "cogs":             wf(cogs),
        "gross_profit":     wf(gross_profit),
        "operating_expense": wf(op_expense),
        "operating_income": wf(op_income),
        "other_income":     wf(other_income),
        "tax":              wf(tax),
        "net_income":       wf(net_income),
    }

    # ── Earnings history ──────────────────────────────────────────────────────
    earnings_data: list[dict] = []
    try:
        ed = tk.earnings_dates
        if ed is not None and not ed.empty:
            for idx, erow in list(ed.iterrows())[:20]:
                earnings_data.append({
                    "date":         str(idx)[:10],
                    "eps_estimate": safe_float(erow.get("EPS Estimate")),
                    "eps_actual":   safe_float(erow.get("Reported EPS")),
                    "surprise_pct": safe_float(erow.get("Surprise(%)")),
                })
            earnings_data.sort(key=lambda x: x["date"])
    except Exception:
        pass

    # ── Company info ─────────────────────────────────────────────────────────
    company_name = ticker_sym
    currency = "USD"
    try:
        info = tk.info or {}
        company_name = info.get("longName") or info.get("shortName") or ticker_sym
        currency = info.get("currency", "USD")
    except Exception:
        pass

    return {
        "ticker":   ticker_sym,
        "name":     company_name,
        "currency": currency,
        "period":   req.period,
        "performance": {
            "revenue":        series_to_list(revenue),
            "net_income":     series_to_list(net_income),
            "net_margin_pct": margin_data,
        },
        "waterfall": waterfall,
        "debt": {
            "total_debt": series_to_list(total_debt),
            "fcf":        series_to_list(fcf),
            "cash":       series_to_list(cash),
        },
        "earnings": earnings_data,
    }


PERIOD_DAYS = {"1mo": 30, "3mo": 90, "6mo": 180, "1y": 365}
HOME_MARKETS = {
    "kospi":  {"ticker": "^KS11", "name": "KOSPI", "base_price": 2650.0, "seed": 42},
    "kosdaq": {"ticker": "^KQ11", "name": "KOSDAQ", "base_price": 850.0, "seed": 73},
    "nasdaq": {"ticker": "^IXIC", "name": "NASDAQ", "base_price": 18000.0, "seed": 109},
    "sp500":  {"ticker": "^GSPC", "name": "S&P 500", "base_price": 5200.0, "seed": 151},
}


@app.get("/api/home/market-candle")
def home_market_candle(market: str = "kospi", period: str = "3mo") -> dict[str, object]:
    if period not in PERIOD_DAYS:
        period = "3mo"
    config = HOME_MARKETS.get(market, HOME_MARKETS["kospi"])
    import pandas as pd
    try:
        import yfinance as yf
        df = yf.download(config["ticker"], period=period, interval="1d", progress=False,
                         auto_adjust=True, threads=False)
        if df.empty:
            raise ValueError("empty")
        ohlcv = []
        for idx, row in df.iterrows():
            def _f(col):
                v = row.get(col)
                if v is None:
                    return None
                if hasattr(v, '__iter__') and not isinstance(v, (str, float, int)):
                    v = list(v)[0]
                return round(float(v), 2)
            ohlcv.append({
                "date": str(idx)[:10],
                "o": _f("Open"), "h": _f("High"),
                "l": _f("Low"),  "c": _f("Close"),
                "v": int(_f("Volume") or 0),
            })
        return {"market": market, "name": config["name"], "ticker": config["ticker"], "ohlcv": ohlcv, "is_simulated": False}
    except Exception:
        import numpy as np, math
        rng_state = config["seed"]
        def _rand():
            nonlocal rng_state
            rng_state = (rng_state * 1664525 + 1013904223) % 2**32
            return rng_state / 2**32
        def _randn():
            u, v = max(_rand(), 1e-10), _rand()
            return math.sqrt(-2 * math.log(u)) * math.cos(2 * math.pi * v)
        price = config["base_price"]
        ohlcv = []
        days = PERIOD_DAYS[period]
        n_bars = int(days * 0.72)
        base = pd.Timestamp("today") - pd.Timedelta(days=days)
        for i in range(n_bars):
            date = (base + pd.Timedelta(days=i + 1)).strftime("%Y-%m-%d")
            chg = _randn() * price * 0.012
            o = price
            c = max(o * 0.9, o + chg)
            h = max(o, c) * (1 + _rand() * 0.008)
            l = min(o, c) * (1 - _rand() * 0.008)
            ohlcv.append({"date": date, "o": round(o, 2), "h": round(h, 2),
                          "l": round(l, 2), "c": round(c, 2), "v": int(_rand() * 1e8)})
            price = c
        return {"market": market, "name": config["name"], "ticker": config["ticker"], "ohlcv": ohlcv, "is_simulated": True}


@app.get("/api/home/kospi-candle")
def home_kospi_candle(period: str = "3mo") -> dict[str, object]:
    """Backward-compatible KOSPI endpoint for older clients."""
    return home_market_candle("kospi", period)


@app.get("/api/home/box-range")
def home_box_range(market: str = "kospi", start: str = "", end: str = "") -> dict[str, object]:
    """지정한 from~to 기간의 박스권(최고가·최저가) 상단/하단 퍼센티지를 계산한다."""
    import datetime as _dt
    config = HOME_MARKETS.get(market, HOME_MARKETS["kospi"])

    today = _dt.date.today()
    try:
        end_date = _dt.date.fromisoformat(end) if end else today
    except ValueError:
        end_date = today
    try:
        start_date = _dt.date.fromisoformat(start) if start else end_date - _dt.timedelta(days=90)
    except ValueError:
        start_date = end_date - _dt.timedelta(days=90)
    if start_date >= end_date:
        start_date = end_date - _dt.timedelta(days=1)

    import pandas as pd
    ohlcv: list[dict] = []
    is_simulated = True
    try:
        import yfinance as yf
        df = yf.download(config["ticker"], start=start_date.isoformat(),
                         end=(end_date + _dt.timedelta(days=1)).isoformat(),
                         interval="1d", progress=False, auto_adjust=True, threads=False)
        if df.empty:
            raise ValueError("empty")
        for idx, row in df.iterrows():
            def _f(col):
                v = row.get(col)
                if v is None:
                    return None
                if hasattr(v, '__iter__') and not isinstance(v, (str, float, int)):
                    v = list(v)[0]
                return round(float(v), 2)
            ohlcv.append({
                "date": str(idx)[:10],
                "o": _f("Open"), "h": _f("High"),
                "l": _f("Low"),  "c": _f("Close"),
            })
        is_simulated = False
    except Exception:
        import math
        rng_state = config["seed"]
        def _rand():
            nonlocal rng_state
            rng_state = (rng_state * 1664525 + 1013904223) % 2**32
            return rng_state / 2**32
        def _randn():
            u, v = max(_rand(), 1e-10), _rand()
            return math.sqrt(-2 * math.log(u)) * math.cos(2 * math.pi * v)
        price = config["base_price"]
        n_days = max(1, (end_date - start_date).days)
        n_bars = max(1, int(n_days * 0.72))
        for i in range(n_bars):
            date = (start_date + _dt.timedelta(days=int(i / 0.72) + 1)).isoformat()
            chg = _randn() * price * 0.012
            o = price
            c = max(o * 0.9, o + chg)
            h = max(o, c) * (1 + _rand() * 0.008)
            l = min(o, c) * (1 - _rand() * 0.008)
            ohlcv.append({"date": date, "o": round(o, 2), "h": round(h, 2),
                          "l": round(l, 2), "c": round(c, 2)})
            price = c

    if not ohlcv:
        raise HTTPException(status_code=404, detail="해당 기간의 시세 데이터를 찾을 수 없습니다.")

    box_high = max(bar["h"] for bar in ohlcv)
    box_low  = min(bar["l"] for bar in ohlcv)
    last_close = ohlcv[-1]["c"]
    box_range = box_high - box_low
    upper_pct    = round((box_high - last_close) / last_close * 100, 2) if last_close else None
    lower_pct    = round((last_close - box_low) / last_close * 100, 2) if last_close else None
    position_pct = round((last_close - box_low) / box_range * 100, 2) if box_range else None

    return {
        "market": market, "name": config["name"], "ticker": config["ticker"],
        "start": start_date.isoformat(), "end": end_date.isoformat(),
        "ohlcv": ohlcv, "is_simulated": is_simulated,
        "box_high": round(box_high, 2), "box_low": round(box_low, 2),
        "last_close": last_close,
        "upper_pct": upper_pct, "lower_pct": lower_pct, "position_pct": position_pct,
    }


# ─── DART Financial Analysis ─────────────────────────────────────────────────

class DartFinancialAnalysisRequest(BaseModel):
    corp_code:    str       = Field(min_length=8, max_length=8, description="DART 고유번호 (8자리)")
    bsns_year:    str       = Field(default="2023", pattern=r"^\d{4}$")
    reprt_code:   str       = Field(default="11011", pattern=r"^1101[1-4]$",
                                    description="11011=사업보고서 11012=반기 11013=1분기 11014=3분기")
    ollama_model: str | None = Field(default=None, description="Ollama 모델명 (미지정 시 환경변수 기본값 사용)")


def _parse_dart_amounts(items: list[dict]) -> dict[str, dict[str, float]]:
    """Extract key financial line items from DART fnlttSinglAcnt response.

    Returns a dict mapping account_nm → {current, prior}.
    """
    ACCT_MAP: dict[str, list[str]] = {
        "current_assets":       ["유동자산"],
        "noncurrent_assets":    ["비유동자산"],
        "total_assets":         ["자산총계"],
        "current_liabilities":  ["유동부채"],
        "noncurrent_liabilities": ["비유동부채"],
        "total_liabilities":    ["부채총계"],
        "paid_in_capital":      ["자본금"],
        "retained_earnings":    ["이익잉여금"],
        "total_equity":         ["자본총계"],
        "revenue":              ["매출액", "영업수익", "수익(매출액)"],
        "op_income":            ["영업이익", "영업손익"],
        "pretax_income":        ["법인세차감전", "법인세비용차감전"],
        "net_income":           ["당기순이익(손실)", "당기순이익"],
        "comprehensive_income": ["총포괄손익"],
    }

    def _num(val: object) -> float:
        try:
            s = str(val or "").replace(",", "").strip()
            return float(s) if s and s not in ("-", "") else 0.0
        except (ValueError, TypeError):
            return 0.0

    result: dict[str, dict[str, float]] = {}
    for key, kws in ACCT_MAP.items():
        for item in items:
            nm = (item.get("account_nm") or "").strip()
            if any(kw in nm for kw in kws):
                result[key] = {
                    "current": _num(item.get("thstrm_amount")),
                    "prior":   _num(item.get("frmtrm_amount")),
                }
                break
        if key not in result:
            result[key] = {"current": 0.0, "prior": 0.0}
    return result


def _calc_dart_ratios(fin: dict[str, dict[str, float]]) -> dict[str, float | None]:
    """Compute financial ratios from parsed DART data."""

    def g(k: str, period: str = "current") -> float:
        return fin.get(k, {}).get(period, 0.0)

    def safe_r(a: float, b: float, mult: float = 100.0) -> float | None:
        return (a / b * mult) if b != 0 else None

    rev     = g("revenue")
    prev_rev = g("revenue", "prior")
    op_inc  = g("op_income")
    net_inc = g("net_income")
    assets  = g("total_assets")
    liab    = g("total_liabilities")
    equity  = g("total_equity")
    cur_a   = g("current_assets")
    cur_l   = g("current_liabilities")
    ret_e   = g("retained_earnings")
    prev_eq = g("total_equity", "prior")

    return {
        "debt_equity_ratio": safe_r(liab, equity),
        "op_margin":         safe_r(op_inc, rev),
        "net_margin":        safe_r(net_inc, rev),
        "roe":               safe_r(net_inc, equity),
        "roa":               safe_r(net_inc, assets),
        "current_ratio":     safe_r(cur_a, cur_l),
        "revenue_growth":    safe_r(rev - prev_rev, prev_rev) if prev_rev else None,
        "equity_growth":     safe_r(equity - prev_eq, prev_eq) if prev_eq else None,
        "retained_ratio":    safe_r(ret_e, equity),
        "debt_ratio":        safe_r(liab, assets),
    }


def _score_financial_health(ratios: dict) -> tuple[float, dict]:
    """Score financial health on 0-100 scale with breakdown."""
    breakdown: dict[str, dict] = {}
    total = 0.0

    def score_item(key: str, label: str, max_score: float,
                   thresholds: list[tuple[float, float]], value: float | None) -> float:
        if value is None:
            s = max_score * 0.5
        else:
            s = 0.0
            for limit, pts in thresholds:
                if value >= limit:
                    s = pts
                    break
        breakdown[label] = {"score": round(s, 1), "max": max_score, "value": value}
        return s

    # 부채비율 (20점) — 낮을수록 좋음 (역방향)
    dr = ratios.get("debt_equity_ratio")
    dr_inv = -dr if dr is not None else None  # invert so higher=better
    total += score_item("debt_equity_ratio", "부채비율", 20,
                        [(-50, 20), (-100, 16), (-200, 10), (-300, 5), (-1e9, 0)], dr_inv)

    # 영업이익률 (20점)
    total += score_item("op_margin", "영업이익률", 20,
                        [(20, 20), (10, 16), (5, 10), (0, 5), (-1e9, 0)],
                        ratios.get("op_margin"))

    # ROE (15점)
    total += score_item("roe", "자기자본이익률(ROE)", 15,
                        [(20, 15), (10, 12), (5, 8), (0, 4), (-1e9, 0)],
                        ratios.get("roe"))

    # 유동비율 (15점)
    total += score_item("current_ratio", "유동비율", 15,
                        [(200, 15), (150, 12), (100, 8), (50, 4), (-1e9, 0)],
                        ratios.get("current_ratio"))

    # 매출 성장률 (15점)
    total += score_item("revenue_growth", "매출 성장률", 15,
                        [(15, 15), (5, 12), (0, 7), (-10, 3), (-1e9, 0)],
                        ratios.get("revenue_growth"))

    # 이익잉여금 비율 (15점)
    total += score_item("retained_ratio", "이익잉여금 비율", 15,
                        [(70, 15), (50, 12), (30, 8), (10, 4), (-1e9, 0)],
                        ratios.get("retained_ratio"))

    return round(total, 1), breakdown


def _generate_dart_analysis(
    company: dict, market: str, ratios: dict,
    score: float, grade: str, bsns_year: str,
) -> dict:
    """Generate rule-based AI financial analysis narrative."""
    corp_name = company.get("corp_name", "동 기업")

    market_ctx = {
        "KOSPI":  "유가증권시장(KOSPI)에 상장된",
        "KOSDAQ": "코스닥(KOSDAQ)에 상장된",
        "KONEX":  "코넥스(KONEX)에 상장된",
    }.get(market, "상장된")

    paragraphs: list[str] = []

    # Overall verdict
    if score >= 85:
        paragraphs.append(
            f"{corp_name}의 {bsns_year}년 재무제표는 전반적으로 매우 우수한 건전성을 보입니다. "
            f"{market_ctx} 기업으로, 재무 안정성과 수익성 모두 업계 상위 수준입니다."
        )
    elif score >= 70:
        paragraphs.append(
            f"{corp_name}의 {bsns_year}년 재무 상태는 양호한 수준입니다. "
            f"{market_ctx} 기업으로, 핵심 재무지표들이 안정적으로 관리되고 있습니다."
        )
    elif score >= 55:
        paragraphs.append(
            f"{corp_name}의 {bsns_year}년 재무 상태는 보통 수준이며, 일부 지표에서 개선이 필요합니다. "
            f"{market_ctx} 기업으로, 선별적 모니터링이 권고됩니다."
        )
    else:
        paragraphs.append(
            f"{corp_name}의 {bsns_year}년 재무 상태는 취약한 것으로 분석됩니다. "
            f"{market_ctx} 기업이나, 재무 리스크가 높아 투자에 각별한 주의가 필요합니다."
        )

    # Debt structure
    dr = ratios.get("debt_equity_ratio")
    if dr is not None:
        if dr < 50:
            paragraphs.append(
                f"부채비율 {dr:.1f}%는 매우 낮은 수준으로, 무차입 또는 보수적 재무 구조를 유지하고 있습니다. "
                "금리 상승기에도 재무적 부담이 경미합니다."
            )
        elif dr < 100:
            paragraphs.append(
                f"부채비율 {dr:.1f}%는 안정적 수준으로, 재무 레버리지가 건전하게 관리되고 있습니다."
            )
        elif dr < 200:
            paragraphs.append(
                f"부채비율 {dr:.1f}%는 업계 평균 수준(100~200%)에 해당하며, 레버리지 관리가 중요합니다."
            )
        else:
            paragraphs.append(
                f"부채비율 {dr:.1f}%는 높은 편입니다. 이자 부담 및 유동성 리스크를 면밀히 점검해야 합니다."
            )

    # Profitability
    om = ratios.get("op_margin")
    nm = ratios.get("net_margin")
    if om is not None:
        if om > 20:
            paragraphs.append(
                f"영업이익률 {om:.1f}%는 매우 높은 수익성을 입증합니다. "
                "강력한 가격 결정력 또는 원가 경쟁력을 보유한 것으로 판단됩니다."
            )
        elif om > 10:
            paragraphs.append(
                f"영업이익률 {om:.1f}%는 안정적 수익성을 나타냅니다."
                + (f" 순이익률 {nm:.1f}%까지 고려할 때 전반적 수익 구조가 건전합니다." if nm and nm > 5 else "")
            )
        elif om > 0:
            paragraphs.append(
                f"영업이익률 {om:.1f}%는 낮은 편으로, 수익성 개선이 향후 핵심 과제입니다."
            )
        else:
            paragraphs.append(
                f"영업이익 적자(영업이익률 {om:.1f}%)는 핵심 영업 활동에서의 손실을 의미합니다. "
                "사업 구조 재편 또는 비용 절감이 시급합니다."
            )

    # Capital efficiency
    roe = ratios.get("roe")
    roa = ratios.get("roa")
    if roe is not None:
        if roe > 15:
            paragraphs.append(
                f"ROE {roe:.1f}%는 자본 효율성이 탁월함을 보여줍니다."
                + (f" ROA {roa:.1f}%도 양호해 자산 운용 효율이 높습니다." if roa and roa > 5 else "")
            )
        elif roe > 5:
            paragraphs.append(f"ROE {roe:.1f}%는 적정 수준의 자본 수익성을 나타냅니다.")
        else:
            paragraphs.append(
                f"ROE {roe:.1f}%는 낮은 자본 효율성을 시사합니다. "
                "수익 모델 개선 또는 자본 재구조화 여지를 검토할 필요가 있습니다."
            )

    # Growth
    rg = ratios.get("revenue_growth")
    if rg is not None:
        if rg > 20:
            paragraphs.append(f"전년 대비 매출이 {rg:.1f}% 급성장하며 강한 성장 모멘텀을 보여줍니다.")
        elif rg > 5:
            paragraphs.append(f"매출 성장률 {rg:.1f}%는 안정적 성장세를 나타냅니다.")
        elif rg >= 0:
            paragraphs.append(f"매출 성장률 {rg:.1f}%로 소폭 성장에 그쳤습니다. 성장 동력 강화가 필요합니다.")
        else:
            paragraphs.append(
                f"매출이 전년 대비 {abs(rg):.1f}% 감소했습니다. "
                "수요 약화 또는 경쟁 심화 여부를 면밀히 파악해야 합니다."
            )

    # Liquidity
    cr = ratios.get("current_ratio")
    if cr is not None:
        if cr > 200:
            paragraphs.append(f"유동비율 {cr:.0f}%는 단기 채무 상환 능력이 매우 충분함을 나타냅니다.")
        elif cr > 100:
            paragraphs.append(f"유동비율 {cr:.0f}%는 단기 유동성이 적정 수준입니다.")
        else:
            paragraphs.append(
                f"유동비율 {cr:.0f}%는 단기 유동성이 다소 취약합니다. "
                "단기 차입 의존도를 낮추는 전략이 필요합니다."
            )

    # Outlook
    if score >= 75:
        outlook       = "매수(Buy)"
        outlook_eng   = "BUY"
        outlook_color = "green"
        outlook_reason = (
            f"재무 건전성 종합점수 {score:.0f}점(등급: {grade}) — "
            "견실한 재무구조·수익성을 바탕으로 중장기 투자 매력이 높습니다."
        )
    elif score >= 55:
        outlook       = "중립(Hold)"
        outlook_eng   = "HOLD"
        outlook_color = "yellow"
        outlook_reason = (
            f"재무 건전성 종합점수 {score:.0f}점(등급: {grade}) — "
            "일부 지표의 개선 여부를 모니터링하면서 보유 또는 소규모 분할 접근이 권고됩니다."
        )
    else:
        outlook       = "관망(Sell/Wait)"
        outlook_eng   = "SELL"
        outlook_color = "red"
        outlook_reason = (
            f"재무 건전성 종합점수 {score:.0f}점(등급: {grade}) — "
            "재무 리스크가 높아 투자에 신중을 기하고 실적 개선 확인 후 재검토를 권고합니다."
        )

    return {
        "paragraphs":     paragraphs,
        "outlook":        outlook,
        "outlook_eng":    outlook_eng,
        "outlook_color":  outlook_color,
        "outlook_reason": outlook_reason,
        "disclaimer":     (
            "본 분석은 DART 공시 재무제표를 기반으로 한 자동화 AI 분석이며, "
            "투자 권유가 아닙니다. 실제 투자 판단은 전문가와 상담하시기 바랍니다."
        ),
    }


@app.post("/api/dart/financial-analysis")
def dart_financial_analysis(req: DartFinancialAnalysisRequest) -> dict:
    """Fetch DART financial statements and run AI-powered financial health analysis."""
    key = _dart_api_key()

    # ── 1. Company meta-data ─────────────────────────────────────────────────
    company = _fetch_company_detail(req.corp_code)
    if not company:
        raise HTTPException(status_code=404, detail="DART 기업 정보를 조회할 수 없습니다.")

    corp_cls = company.get("corp_cls", "")
    market   = {"Y": "KOSPI", "K": "KOSDAQ", "N": "KONEX"}.get(corp_cls, "비상장/기타")

    # ── 2. Financial statements ──────────────────────────────────────────────
    def _fetch_fin(fs_div: str) -> dict:
        url = ("https://opendart.fss.or.kr/api/fnlttSinglAcnt.json?"
               + urllib.parse.urlencode({
                   "crtfc_key":  key,
                   "corp_code":  req.corp_code,
                   "bsns_year":  req.bsns_year,
                   "reprt_code": req.reprt_code,
                   "fs_div":     fs_div,
               }))
        try:
            with urllib.request.urlopen(url, timeout=15) as resp:
                return json.loads(resp.read())
        except Exception:
            return {}

    fin_data = _fetch_fin("CFS")  # 연결재무제표 우선
    is_consolidated = True
    if fin_data.get("status") != "000" or not fin_data.get("list"):
        fin_data = _fetch_fin("OFS")  # 별도재무제표 fallback
        is_consolidated = False

    if fin_data.get("status") != "000" or not fin_data.get("list"):
        raise HTTPException(
            status_code=404,
            detail=f"{req.bsns_year}년 재무제표 데이터가 없습니다: {fin_data.get('message', '알 수 없음')}"
        )

    items = fin_data.get("list", [])
    fin   = _parse_dart_amounts(items)

    # ── 3. Ratios & scoring ──────────────────────────────────────────────────
    ratios       = _calc_dart_ratios(fin)
    score, breakdown = _score_financial_health(ratios)
    grade        = (
        "A+" if score >= 90 else "A" if score >= 80 else "B+" if score >= 75
        else "B" if score >= 70 else "C" if score >= 60 else "D" if score >= 50 else "F"
    )
    verdict      = "매우 견실" if score >= 85 else "견실" if score >= 70 else "보통" if score >= 55 else "취약"

    # ── 4. Friendly financial snapshot (unit: 억원) ──────────────────────────
    B = 100_000_000  # 1억

    def to_eok(v: float) -> float | None:
        return round(v / B, 1) if v else None

    snap = {
        "revenue":              to_eok(fin["revenue"]["current"]),
        "prev_revenue":         to_eok(fin["revenue"]["prior"]),
        "op_income":            to_eok(fin["op_income"]["current"]),
        "prev_op_income":       to_eok(fin["op_income"]["prior"]),
        "net_income":           to_eok(fin["net_income"]["current"]),
        "prev_net_income":      to_eok(fin["net_income"]["prior"]),
        "total_assets":         to_eok(fin["total_assets"]["current"]),
        "total_liabilities":    to_eok(fin["total_liabilities"]["current"]),
        "total_equity":         to_eok(fin["total_equity"]["current"]),
        "current_assets":       to_eok(fin["current_assets"]["current"]),
        "current_liabilities":  to_eok(fin["current_liabilities"]["current"]),
        "retained_earnings":    to_eok(fin["retained_earnings"]["current"]),
        "is_consolidated":      is_consolidated,
        "unit":                 "억원",
    }

    # ── 5. AI narrative (Ollama → rule-based fallback) ────────────────────────
    ollama_text: str | None = None
    ollama_model_used: str | None = None
    ollama_ok = _ollama_available()

    if ollama_ok:
        try:
            model_to_use = req.ollama_model or OLLAMA_MODEL
            sys_p, usr_p = _build_financial_analysis_prompt(
                company.get("corp_name", ""),
                market, req.bsns_year, snap, ratios, score, grade,
            )
            ollama_text       = _ollama_chat(model_to_use, sys_p, usr_p)
            ollama_model_used = model_to_use
        except Exception:
            ollama_ok = False

    analysis = _generate_dart_analysis(company, market, ratios, score, grade, req.bsns_year)

    return {
        "company":  {
            "corp_code":  req.corp_code,
            "corp_name":  company.get("corp_name", ""),
            "ceo_nm":     company.get("ceo_nm", ""),
            "adres":      company.get("adres", ""),
            "est_dt":     company.get("est_dt", ""),
            "stock_code": company.get("stock_code", ""),
            "corp_cls":   corp_cls,
            "market":     market,
        },
        "financials": snap,
        "ratios":     ratios,
        "health":     {
            "score":     score,
            "grade":     grade,
            "verdict":   verdict,
            "breakdown": breakdown,
        },
        "analysis":       analysis,
        "ollama": {
            "available":    ollama_ok,
            "host":         OLLAMA_HOST,
            "model_used":   ollama_model_used,
            "text":         ollama_text,
        },
        "bsns_year":  req.bsns_year,
    }


# ─── Ollama Endpoints ────────────────────────────────────────────────────────

class OllamaChatRequest(BaseModel):
    model:   str        = Field(default="")
    prompt:  str        = Field(min_length=1, max_length=8000)
    system:  str        = Field(default="당신은 한국 금융·투자 전문가입니다. 한국어로 답변하세요.")
    temperature: float  = Field(default=0.4, ge=0.0, le=2.0)
    num_predict: int    = Field(default=600, ge=50, le=2000)


class OllamaPullRequest(BaseModel):
    model: str = Field(min_length=1, max_length=100)


@app.get("/api/ollama/status")
def ollama_status() -> dict:
    """Return Ollama connection status and available models."""
    available = _ollama_available()
    models: list[dict] = []
    if available:
        raw = _ollama_models()
        for m in raw:
            details = m.get("details", {})
            models.append({
                "name":       m.get("name", ""),
                "size_gb":    round(m.get("size", 0) / 1e9, 2),
                "param_size": details.get("parameter_size", ""),
                "quantize":   details.get("quantization_level", ""),
                "modified":   (m.get("modified_at") or "")[:10],
                "is_default": m.get("name", "") == OLLAMA_MODEL,
            })
    return {
        "available":     available,
        "host":          OLLAMA_HOST,
        "default_model": OLLAMA_MODEL,
        "models":        models,
        "model_count":   len(models),
        "recommended": {
            "financial_analysis": "llama3:latest",
            "korean_text":        "ko-llama:latest",
            "embedding":          "nomic-embed-text:latest",
            "fast_coding":        "qwen2.5-coder:1.5b-base",
        },
        "suggested_pull": [
            {"name": "llama3.1:8b",    "desc": "한국어 지원 강화 버전 (추천)", "size": "~4.7GB"},
            {"name": "qwen2.5:7b",     "desc": "한국어·영어 다국어 최적화",   "size": "~4.4GB"},
            {"name": "exaone3.5:7.8b", "desc": "LG AI Research 한국어 전용", "size": "~4.9GB"},
        ],
    }


@app.post("/api/ollama/chat")
def ollama_chat(req: OllamaChatRequest) -> dict:
    """Send a prompt to Ollama and return the generated text."""
    model = req.model or OLLAMA_MODEL
    text  = _ollama_chat(model, req.system, req.prompt, req.temperature, req.num_predict)
    return {"model": model, "response": text, "host": OLLAMA_HOST}


@app.post("/api/ollama/pull")
def ollama_pull(req: OllamaPullRequest) -> dict:
    """Start pulling an Ollama model (non-streaming, may take several minutes)."""
    result = _ollama_request(
        "/api/pull",
        {"name": req.model, "stream": False},
        timeout=600,  # 10-min max for large models
    )
    status = result.get("status", "unknown")
    return {"model": req.model, "status": status, "detail": result}


# ─── Tax / Accounting Simulation ─────────────────────────────────────────────

class TaxSimulationRequest(BaseModel):
    transactions: list[dict]
    entity_type: str = Field(default="individual", pattern="^(individual|corporate)$")
    tax_year: int = Field(default=2024, ge=2020, le=2030)
    business_name: str = Field(default="")
    taxpayer_id: str = Field(default="")
    vat_registered: bool = Field(default=True)
    standard_deduction: float = Field(default=0.0, ge=0.0)


_INCOME_KEYWORDS: dict[str, list[str]] = {
    "상품매출":   ["매출", "판매대금", "카드매출", "판매입금", "상품판매"],
    "서비스매출": ["서비스", "용역", "컨설팅", "자문료", "강의료"],
    "임대수입":   ["임대", "월세", "부동산"],
    "이자수입":   ["이자수입", "예금이자", "이자"],
    "기타수입":   ["환급", "지원금", "보조금", "보상금", "배당"],
}

_EXPENSE_KEYWORDS: dict[str, list[str]] = {
    "급여":       ["급여", "월급", "임금", "상여", "인건비", "퇴직금"],
    "임차료":     ["임대료", "임차료", "월세", "관리비"],
    "접대비":     ["접대", "회식", "식대", "거래처식사"],
    "광고선전비": ["광고", "마케팅", "홍보"],
    "복리후생비": ["복리", "후생", "식권", "직원식대"],
    "통신비":     ["통신", "인터넷", "전화요금", "KT", "SKT", "LGU"],
    "수도광열비": ["전기요금", "가스요금", "수도요금", "한전", "한국전력", "가스공사"],
    "교통비":     ["교통비", "주유", "택시", "버스", "주차비", "고속도로"],
    "사무용품비": ["사무용품", "문구", "비품", "소모품", "사무기기"],
    "보험료":     ["보험료", "화재보험", "자동차보험", "단체보험"],
    "수수료비용": ["수수료", "카드수수료", "결제수수료", "플랫폼수수료"],
}


def _categorize_tx(desc: str, deposit: float, withdrawal: float) -> tuple[str, str]:
    if deposit > 0:
        for cat, kws in _INCOME_KEYWORDS.items():
            if any(k in desc for k in kws):
                return "income", cat
        return "income", "기타수입"
    for cat, kws in _EXPENSE_KEYWORDS.items():
        if any(k in desc for k in kws):
            return "expense", cat
    return "expense", "기타비용"


def _calc_income_tax(income: float) -> float:
    """Korean individual income tax brackets (2024)."""
    if income <= 0:
        return 0.0
    brackets = [
        (14_000_000,         0.06, 0),
        (50_000_000,         0.15, 1_260_000),
        (88_000_000,         0.24, 5_220_000),
        (150_000_000,        0.35, 14_900_000),
        (300_000_000,        0.38, 19_400_000),
        (500_000_000,        0.40, 25_400_000),
        (1_000_000_000,      0.42, 35_400_000),
        (float("inf"),       0.45, 65_400_000),
    ]
    for limit, rate, deduction in brackets:
        if income <= limit:
            return max(0.0, income * rate - deduction)
    return max(0.0, income * 0.45 - 65_400_000)


def _calc_corporate_tax(income: float) -> float:
    """Korean corporate tax brackets (2024)."""
    if income <= 0:
        return 0.0
    brackets = [
        (200_000_000,         0.09, 0),
        (20_000_000_000,      0.19, 20_000_000),
        (300_000_000_000,     0.21, 420_000_000),
        (float("inf"),        0.24, 9_420_000_000),
    ]
    for limit, rate, deduction in brackets:
        if income <= limit:
            return max(0.0, income * rate - deduction)
    return max(0.0, income * 0.24 - 9_420_000_000)


def _parse_df_to_transactions(df: "import pandas; pandas.DataFrame") -> list[dict]:
    import pandas as pd

    raw_cols = list(df.columns)
    col_map: dict[str, str | None] = {
        "date": None, "description": None,
        "withdrawal": None, "deposit": None, "balance": None,
    }

    DATE_KW   = ["날짜", "일자", "거래일", "일시", "거래시간", "date"]
    DESC_KW   = ["내용", "적요", "거래내용", "거래명", "메모", "description"]
    OUT_KW    = ["출금", "출금액", "지출", "출금금액", "debit", "withdrawal"]
    IN_KW     = ["입금", "입금액", "수입", "입금금액", "credit", "deposit"]
    BAL_KW    = ["잔액", "잔금", "balance"]

    for c in raw_cols:
        cs = str(c).strip()
        if col_map["date"]        is None and any(k in cs for k in DATE_KW):   col_map["date"]        = c
        if col_map["description"] is None and any(k in cs for k in DESC_KW):   col_map["description"] = c
        if col_map["withdrawal"]  is None and any(k in cs for k in OUT_KW):    col_map["withdrawal"]  = c
        if col_map["deposit"]     is None and any(k in cs for k in IN_KW):     col_map["deposit"]     = c
        if col_map["balance"]     is None and any(k in cs for k in BAL_KW):    col_map["balance"]     = c

    def _num(val: object) -> float:
        try:
            s = str(val).replace(",", "").strip()
            return float(s) if s and s.lower() not in ("nan", "") else 0.0
        except (ValueError, TypeError):
            return 0.0

    txs: list[dict] = []
    for _, row in df.iterrows():
        date_val = str(row[col_map["date"]]).strip()   if col_map["date"]        else ""
        desc_val = str(row[col_map["description"]]).strip() if col_map["description"] else ""
        out_val  = _num(row[col_map["withdrawal"]])    if col_map["withdrawal"]   else 0.0
        in_val   = _num(row[col_map["deposit"]])       if col_map["deposit"]      else 0.0
        bal_val  = _num(row[col_map["balance"]])       if col_map["balance"]      else 0.0

        if not date_val or date_val.lower() == "nan":
            continue

        txs.append({
            "date":        date_val,
            "description": desc_val,
            "withdrawal":  out_val,
            "deposit":     in_val,
            "balance":     bal_val,
        })
    return txs


@app.post("/api/tax/upload")
async def tax_upload(file: UploadFile = File(...)) -> dict:
    """Parse a bank-statement CSV or Excel file and return structured transactions."""
    import pandas as pd

    filename = (file.filename or "").lower()
    content  = await file.read()

    try:
        if filename.endswith((".xlsx", ".xls")):
            df = pd.read_excel(io.BytesIO(content), dtype=str)
        else:
            df = None
            for enc in ("utf-8-sig", "utf-8", "euc-kr", "cp949"):
                try:
                    text = content.decode(enc)
                    df   = pd.read_csv(io.StringIO(text), dtype=str)
                    break
                except (UnicodeDecodeError, Exception):
                    continue
            if df is None:
                raise HTTPException(status_code=400, detail="파일 인코딩을 감지할 수 없습니다.")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"파일 파싱 오류: {exc}") from exc

    df.dropna(how="all", inplace=True)
    txs = _parse_df_to_transactions(df)

    if not txs:
        raise HTTPException(status_code=400, detail="유효한 거래 데이터를 찾을 수 없습니다. 컬럼명을 확인하세요.")

    return {
        "status":   "ok",
        "rows":     len(txs),
        "columns":  list(df.columns),
        "transactions": txs[:500],  # max 500 rows for safety
    }


@app.get("/api/tax/sample")
def tax_sample() -> dict:
    """Return synthetic sample transaction data for demo purposes."""
    import datetime, math, random

    rng = random.Random(42)
    base_date = datetime.date(2024, 1, 2)
    balance   = 50_000_000.0
    txs: list[dict] = []

    INCOME_TEMPLATES = [
        ("상품 판매 대금 입금 (거래처A)",   3_000_000, 8_000_000, "상품매출"),
        ("컨설팅 용역료 입금",               2_000_000, 5_000_000, "서비스매출"),
        ("카드매출 정산",                    1_000_000, 4_000_000, "상품매출"),
        ("임대료 수입",                      1_500_000, 1_500_000, "임대수입"),
        ("이자수입",                          50_000,   200_000, "이자수입"),
    ]
    EXPENSE_TEMPLATES = [
        ("급여 지급",                        3_500_000, 6_000_000, "급여"),
        ("사무실 임대료 납부",               1_200_000, 1_200_000, "임차료"),
        ("광고비 지출",                       300_000,  800_000, "광고선전비"),
        ("사무용품 구매",                      50_000,  200_000, "사무용품비"),
        ("KT 통신비",                          80_000,  150_000, "통신비"),
        ("전기요금 납부 (한전)",              120_000,  300_000, "수도광열비"),
        ("교통비 (주유·택시)",               100_000,  400_000, "교통비"),
        ("거래처 회식 (접대비)",             300_000,  500_000, "접대비"),
        ("카드수수료",                         30_000,  100_000, "수수료비용"),
        ("복리후생비",                        200_000,  500_000, "복리후생비"),
        ("단체보험료",                        150_000,  250_000, "보험료"),
    ]

    for day_offset in range(0, 365, rng.randint(1, 4)):
        date = base_date + datetime.timedelta(days=day_offset)
        if date >= datetime.date(2025, 1, 1):
            break

        # 50% income, 50% expense
        if rng.random() < 0.45:
            tmpl = rng.choice(INCOME_TEMPLATES)
            amt  = rng.randint(int(tmpl[1] / 1000), int(tmpl[2] / 1000)) * 1000
            balance += amt
            txs.append({
                "date":        date.strftime("%Y-%m-%d"),
                "description": tmpl[0],
                "deposit":     float(amt),
                "withdrawal":  0.0,
                "balance":     balance,
            })
        else:
            tmpl = rng.choice(EXPENSE_TEMPLATES)
            amt  = rng.randint(int(tmpl[1] / 1000), int(tmpl[2] / 1000)) * 1000
            balance -= amt
            txs.append({
                "date":        date.strftime("%Y-%m-%d"),
                "description": tmpl[0],
                "deposit":     0.0,
                "withdrawal":  float(amt),
                "balance":     balance,
            })

    return {"status": "ok", "rows": len(txs), "transactions": txs}


@app.post("/api/tax/simulate")
def tax_simulate(req: TaxSimulationRequest) -> dict:
    """Run full Korean tax simulation on the provided transaction list."""
    import datetime

    categorized: list[dict] = []
    income_by_cat: dict[str, float] = {}
    expense_by_cat: dict[str, float] = {}
    monthly: dict[str, dict[str, float]] = {}

    for tx in req.transactions:
        deposit    = float(tx.get("deposit", 0) or 0)
        withdrawal = float(tx.get("withdrawal", 0) or 0)
        desc       = str(tx.get("description", ""))
        date_str   = str(tx.get("date", ""))
        balance    = float(tx.get("balance", 0) or 0)

        tx_type, category = _categorize_tx(desc, deposit, withdrawal)
        amount = deposit if tx_type == "income" else withdrawal

        categorized.append({
            "date":        date_str,
            "description": desc,
            "deposit":     deposit,
            "withdrawal":  withdrawal,
            "balance":     balance,
            "type":        tx_type,
            "category":    category,
            "amount":      amount,
        })

        month = date_str[:7]
        if month not in monthly:
            monthly[month] = {"income": 0.0, "expense": 0.0}
        monthly[month][tx_type] += amount

        if tx_type == "income":
            income_by_cat[category]  = income_by_cat.get(category, 0.0)  + amount
        else:
            expense_by_cat[category] = expense_by_cat.get(category, 0.0) + amount

    total_income  = sum(income_by_cat.values())
    total_expense = sum(expense_by_cat.values())
    net_income    = total_income - total_expense
    taxable_income = max(0.0, net_income - req.standard_deduction)

    # VAT (부가가치세) — 일반과세자 기준 10%
    vat_output  = total_income  * 0.10 if req.vat_registered else 0.0
    vat_input   = total_expense * 0.10 if req.vat_registered else 0.0
    vat_payable = max(0.0, vat_output - vat_input)

    # 소득세 또는 법인세
    if req.entity_type == "individual":
        base_tax = _calc_income_tax(taxable_income)
    else:
        base_tax = _calc_corporate_tax(taxable_income)

    local_tax  = base_tax * 0.10   # 지방소득세
    total_tax  = base_tax + local_tax
    effective_rate = (base_tax / taxable_income * 100) if taxable_income > 0 else 0.0

    # Income tax bracket breakdown (for audit report)
    bracket_info: list[dict] = []
    if req.entity_type == "individual":
        BRACKETS = [
            (14_000_000,   "6%",  14_000_000),
            (50_000_000,   "15%", 36_000_000),
            (88_000_000,   "24%", 38_000_000),
            (150_000_000,  "35%", 62_000_000),
            (300_000_000,  "38%", 150_000_000),
            (500_000_000,  "40%", 200_000_000),
            (1_000_000_000,"42%", 500_000_000),
            (float("inf"), "45%", 0),
        ]
        prev = 0.0
        for limit, rate_label, width in BRACKETS:
            if taxable_income > prev:
                in_bracket = min(taxable_income, limit if limit != float("inf") else taxable_income) - prev
                bracket_info.append({
                    "range": f"{int(prev):,}원 초과 ~ {int(limit):,}원 이하" if limit != float("inf") else f"{int(prev):,}원 초과",
                    "rate":  rate_label,
                    "amount": in_bracket,
                })
                prev = limit
            else:
                break

    monthly_sorted = dict(sorted(monthly.items()))

    return {
        "entity_type":    req.entity_type,
        "tax_year":       req.tax_year,
        "business_name":  req.business_name,
        "taxpayer_id":    req.taxpayer_id,
        "summary": {
            "total_income":    total_income,
            "total_expense":   total_expense,
            "net_income":      net_income,
            "taxable_income":  taxable_income,
            "standard_deduction": req.standard_deduction,
        },
        "income_by_category":  income_by_cat,
        "expense_by_category": expense_by_cat,
        "monthly":             monthly_sorted,
        "vat": {
            "output_tax":  vat_output,
            "input_tax":   vat_input,
            "payable":     vat_payable,
            "registered":  req.vat_registered,
        },
        "income_tax": {
            "amount":         base_tax,
            "local_tax":      local_tax,
            "total":          total_tax,
            "effective_rate": effective_rate,
            "brackets":       bracket_info,
        },
        "transactions": categorized,
    }


# ─── RAG (Retrieval-Augmented Generation) ────────────────────────────────────

_QDRANT_URL        = os.getenv("QDRANT_URL",        "http://localhost:6333")
_QDRANT_COLLECTION = os.getenv("QDRANT_COLLECTION", "investment_docs")
_OLLAMA_EMBED_MODEL = os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text")


def _qdrant_request(method: str, path: str, payload: dict | None = None) -> dict:
    url = _QDRANT_URL.rstrip("/") + path
    data = None
    headers: dict[str, str] = {}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="ignore")
        raise HTTPException(status_code=502, detail=f"Qdrant 오류({exc.code}): {err_body[:200]}") from exc
    except urllib.error.URLError as exc:
        raise HTTPException(status_code=503, detail=f"Qdrant 연결 실패 ({_QDRANT_URL}): {exc.reason}") from exc


def _qdrant_available() -> bool:
    try:
        req = urllib.request.Request(_QDRANT_URL.rstrip("/") + "/collections")
        with urllib.request.urlopen(req, timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def _ollama_embed(text: str, model: str = _OLLAMA_EMBED_MODEL) -> list[float] | None:
    """Ollama /api/embeddings 호출. 실패 시 None."""
    try:
        result = _ollama_request(
            "/api/embeddings",
            {"model": model, "prompt": text},
            timeout=30,
        )
        emb = result.get("embedding")
        if emb and len(emb) > 0:
            return [float(v) for v in emb]
        return None
    except Exception:
        return None


def _hash_embed(text: str, dim: int = 384) -> list[float]:
    """해시 기반 폴백 임베딩 (upload_docs_to_qdrant.sh 해시 임베딩과 동일)."""
    import hashlib, math as _math, re as _re
    TOKEN = _re.compile(r"[0-9A-Za-z가-힣_]+")
    vec = [0.0] * dim
    tokens = TOKEN.findall(text.lower())
    if not tokens:
        return vec
    for token in tokens:
        digest = hashlib.sha256(token.encode()).digest()
        idx = int.from_bytes(digest[:4], "big") % dim
        sign = 1.0 if (digest[4] & 1) == 0 else -1.0
        vec[idx] += sign
    norm = _math.sqrt(sum(v * v for v in vec))
    if norm > 0:
        vec = [v / norm for v in vec]
    return vec


def _embed_query(text: str) -> tuple[list[float], str]:
    """쿼리 텍스트를 임베딩. Ollama 성공 시 (vector, 'ollama'), 폴백 시 (vector, 'hash')."""
    if _ollama_available():
        emb = _ollama_embed(text)
        if emb:
            return emb, "ollama"
    # 컬렉션 벡터 크기 확인해서 hash dim 맞추기
    try:
        col_info = _qdrant_request("GET", f"/collections/{_QDRANT_COLLECTION}")
        dim = col_info.get("result", {}).get("config", {}).get("params", {}).get("vectors", {}).get("size", 384)
    except Exception:
        dim = 384
    return _hash_embed(text, dim=int(dim)), "hash"


class RagSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000, description="검색 질문")
    top_k: int = Field(default=5, ge=1, le=20, description="반환할 최대 청크 수")
    score_threshold: float = Field(default=0.0, ge=0.0, le=1.0, description="최소 유사도 점수 (0=필터 없음)")


class RagAskRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000, description="질문")
    top_k: int = Field(default=5, ge=1, le=20)
    score_threshold: float = Field(default=0.0, ge=0.0, le=1.0)
    ollama_model: str | None = Field(default=None, description="답변 생성 모델 (미지정 시 기본값)")


@app.post("/api/rag/search")
def rag_search(req: RagSearchRequest) -> dict[str, object]:
    """Qdrant에서 관련 문서 청크를 검색합니다."""
    if not _qdrant_available():
        raise HTTPException(
            status_code=503,
            detail=(
                f"Qdrant 서버에 연결할 수 없습니다 ({_QDRANT_URL}). "
                "서버를 실행하고 scripts/upload_docs_to_qdrant.sh 로 문서를 업로드하세요."
            ),
        )

    vector, embed_method = _embed_query(req.query)

    search_payload: dict[str, object] = {
        "vector": vector,
        "limit": req.top_k,
        "with_payload": True,
        "with_vector": False,
    }
    if req.score_threshold > 0:
        search_payload["score_threshold"] = req.score_threshold

    result = _qdrant_request(
        "POST",
        f"/collections/{_QDRANT_COLLECTION}/points/search",
        search_payload,
    )

    hits = result.get("result", [])
    chunks = [
        {
            "score":       round(h.get("score", 0), 4),
            "source_doc":  h.get("payload", {}).get("source_doc", ""),
            "chunk_index": h.get("payload", {}).get("chunk_index", 0),
            "text":        h.get("payload", {}).get("text", ""),
        }
        for h in hits
    ]

    return {
        "query":        req.query,
        "embed_method": embed_method,
        "count":        len(chunks),
        "results":      chunks,
    }


@app.post("/api/rag/ask")
def rag_ask(req: RagAskRequest) -> dict[str, object]:
    """RAG: Qdrant 검색 후 Ollama로 답변을 생성합니다."""
    if not _qdrant_available():
        raise HTTPException(
            status_code=503,
            detail=(
                f"Qdrant 서버에 연결할 수 없습니다 ({_QDRANT_URL}). "
                "서버를 실행하고 scripts/upload_docs_to_qdrant.sh 로 문서를 업로드하세요."
            ),
        )

    # 1. 벡터 검색
    vector, embed_method = _embed_query(req.query)
    search_payload: dict[str, object] = {
        "vector": vector,
        "limit": req.top_k,
        "with_payload": True,
        "with_vector": False,
    }
    if req.score_threshold > 0:
        search_payload["score_threshold"] = req.score_threshold

    result = _qdrant_request(
        "POST",
        f"/collections/{_QDRANT_COLLECTION}/points/search",
        search_payload,
    )
    hits = result.get("result", [])
    chunks = [
        {
            "score":      round(h.get("score", 0), 4),
            "source_doc": h.get("payload", {}).get("source_doc", ""),
            "text":       h.get("payload", {}).get("text", ""),
        }
        for h in hits
    ]

    # 2. 컨텍스트 구성
    context_parts = []
    for i, c in enumerate(chunks, 1):
        context_parts.append(f"[출처: {c['source_doc']} | 유사도: {c['score']}]\n{c['text']}")
    context = "\n\n---\n\n".join(context_parts)

    # 3. Ollama 답변 생성
    ollama_ok = _ollama_available()
    answer: str | None = None
    model_used: str | None = None

    if ollama_ok:
        model_to_use = req.ollama_model or OLLAMA_MODEL
        system_prompt = (
            "당신은 한국 금융·투자·경제 교육 전문가입니다. "
            "아래 참고 문서를 바탕으로 질문에 한국어로 정확하고 간결하게 답하세요. "
            "참고 문서에 없는 내용은 '제공된 문서에 해당 정보가 없습니다'라고 명시하세요."
        )
        user_prompt = (
            f"【참고 문서】\n{context}\n\n"
            f"【질문】\n{req.query}\n\n"
            "위 참고 문서를 기반으로 질문에 답해주세요."
        )
        try:
            answer = _ollama_chat(model_to_use, system_prompt, user_prompt, temperature=0.2, num_predict=800)
            model_used = model_to_use
        except Exception:
            ollama_ok = False

    if not ollama_ok or not answer:
        # Ollama 없을 때 검색 결과만 반환
        answer = (
            "Ollama 서버에 연결할 수 없어 검색 결과만 반환합니다. "
            "아래 참고 문서를 확인하세요."
        )

    return {
        "query":        req.query,
        "answer":       answer,
        "embed_method": embed_method,
        "ollama_model": model_used,
        "sources":      chunks,
        "source_count": len(chunks),
    }


@app.get("/api/rag/status")
def rag_status() -> dict[str, object]:
    """Qdrant 연결 상태 및 컬렉션 정보를 반환합니다."""
    qdrant_ok = _qdrant_available()
    collection_info: dict = {}
    if qdrant_ok:
        try:
            col = _qdrant_request("GET", f"/collections/{_QDRANT_COLLECTION}")
            res = col.get("result", {})
            collection_info = {
                "points_count": res.get("points_count", 0),
                "vector_size":  res.get("config", {}).get("params", {}).get("vectors", {}).get("size"),
                "status":       res.get("status", "unknown"),
            }
        except Exception:
            collection_info = {"error": "컬렉션이 없거나 조회 실패"}

    return {
        "qdrant": {
            "available":  qdrant_ok,
            "url":        _QDRANT_URL,
            "collection": _QDRANT_COLLECTION,
            **collection_info,
        },
        "ollama": {
            "available":    _ollama_available(),
            "host":         OLLAMA_HOST,
            "embed_model":  _OLLAMA_EMBED_MODEL,
            "chat_model":   OLLAMA_MODEL,
        },
        "upload_hint": "문서 업로드: bash scripts/upload_docs_to_qdrant.sh",
    }


# ─────────────────────────────────────────────────────────────────────────────

app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
