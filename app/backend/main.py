from __future__ import annotations

import base64
from datetime import datetime, timezone
import io
import json
import os
import re
import time
from threading import Lock
from time import monotonic
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

try:
    from .openapi_docs import install_openapi
except ImportError:
    from openapi_docs import install_openapi

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
ACTIVE_VISITOR_TTL_SECONDS = 90
_active_visitors: dict[str, float] = {}
_active_visitors_lock = Lock()

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
        "주식 투자 입문 가이드"
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


try:
    from .routers.ml import router as ml_router
    from .routers.quant import router as quant_router
    from .routers.quiz import router as quiz_router
    from .routers.vocabulary_exam import EXAM_OPEN_AT, router as vocabulary_exam_router
    from .routers.tax import router as tax_router
    from .routers.rag import router as rag_router
except ImportError:  # Allows `uvicorn main:app` from app/backend.
    from routers.ml import router as ml_router  # type: ignore
    from routers.quant import router as quant_router  # type: ignore
    from routers.quiz import router as quiz_router  # type: ignore
    from routers.vocabulary_exam import EXAM_OPEN_AT, router as vocabulary_exam_router  # type: ignore
    from routers.tax import router as tax_router  # type: ignore
    from routers.rag import router as rag_router  # type: ignore
app.include_router(ml_router)
app.include_router(quant_router)
app.include_router(vocabulary_exam_router)
# Routers registered below are also included before the schema is first requested;
# the OpenAPI factory is installed at the bottom of this module.

@app.middleware("http")
async def no_cache_static_assets(request, call_next):
    """Always fetch the latest local HTML, JavaScript, and CSS on page load.

    StaticFiles normally uses ETag/Last-Modified revalidation. The learning
    app is deployed as a single image, so an old shell or module can otherwise
    survive a deployment in a browser cache. ``no-store`` plus legacy
    revalidation headers makes local frontend assets non-cacheable for both
    browsers and intermediary proxies.
    """
    response = await call_next(request)
    path = request.url.path
    if path == "/" or path.endswith((".html", ".js", ".css")):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


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


def _read_cpu_times() -> tuple[int, int]:
    """Return total and idle CPU ticks from Linux procfs."""
    with open("/proc/stat", encoding="utf-8") as proc_stat:
        first_line = proc_stat.readline().split()
    values = [int(value) for value in first_line[1:]]
    total = sum(values)
    # Linux reports idle and iowait separately; both mean the CPU was not
    # executing application work for this interval.
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    return total, idle


def _memory_usage() -> tuple[int, int]:
    """Return total and used memory bytes, preferring MemAvailable on Linux."""
    meminfo: dict[str, int] = {}
    try:
        with open("/proc/meminfo", encoding="utf-8") as proc_meminfo:
            for line in proc_meminfo:
                key, raw_value, *_ = line.split()
                meminfo[key.rstrip(":")] = int(raw_value) * 1024
        total = meminfo["MemTotal"]
        available = meminfo.get("MemAvailable", meminfo.get("MemFree", 0))
        return total, max(total - available, 0)
    except (FileNotFoundError, KeyError, ValueError):
        page_size = os.sysconf("SC_PAGE_SIZE")
        total = os.sysconf("SC_PHYS_PAGES") * page_size
        available = os.sysconf("SC_AVPHYS_PAGES") * page_size
        return total, max(total - available, 0)


@app.get("/api/system/resources")
def system_resources() -> dict[str, object]:
    """Return host CPU, memory and root-disk utilization for the admin view."""
    try:
        total_before, idle_before = _read_cpu_times()
        time.sleep(0.1)
        total_after, idle_after = _read_cpu_times()
        total_delta = total_after - total_before
        idle_delta = idle_after - idle_before
        cpu_used = 0.0 if total_delta <= 0 else (1 - idle_delta / total_delta) * 100

        memory_total, memory_used = _memory_usage()
        disk = os.statvfs("/")
        disk_total = disk.f_frsize * disk.f_blocks
        disk_free = disk.f_frsize * disk.f_bavail
        disk_used = max(disk_total - disk_free, 0)

        def usage(total: int, used: int) -> dict[str, int | float]:
            percent = (used / total * 100) if total else 0.0
            return {"total_bytes": total, "used_bytes": used,
                    "free_bytes": max(total - used, 0), "used_percent": round(percent, 1)}

        return {
            "cpu": {"used_percent": round(max(0.0, min(cpu_used, 100.0)), 1),
                    "logical_cores": os.cpu_count() or 1},
            "memory": usage(memory_total, memory_used),
            "disk": usage(disk_total, disk_used),
            "disk_mount": "/",
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
    except OSError as exc:
        raise HTTPException(status_code=503, detail=f"서버 리소스 정보를 읽을 수 없습니다: {exc}") from exc


class VisitorHeartbeatRequest(BaseModel):
    visitor_id: str = Field(min_length=16, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")


@app.post("/api/visitors/heartbeat")
def visitor_heartbeat(payload: VisitorHeartbeatRequest) -> dict[str, int]:
    """Register one browser briefly and return the current active-browser count.

    This is an in-memory presence indicator, not an analytics counter. A browser
    remains active for 90 seconds after its latest heartbeat, so closed tabs
    disappear without retaining personally identifiable visit history.
    """
    now = monotonic()
    with _active_visitors_lock:
        expired = [
            visitor_id
            for visitor_id, last_seen in _active_visitors.items()
            if now - last_seen > ACTIVE_VISITOR_TTL_SECONDS
        ]
        for visitor_id in expired:
            _active_visitors.pop(visitor_id, None)
        _active_visitors[payload.visitor_id] = now
        count = len(_active_visitors)
    return {"active_visitors": count}


@app.get("/api/learn/doc/{doc_id}")
def get_learn_doc(doc_id: str) -> dict[str, str]:
    # 시험지는 /api/vocabulary-exam에서 개시 시각을 통과한 뒤에만 제공한다.
    # 문서 API로 직접 접근해도 문제를 미리 볼 수 없게 한다.
    if doc_id == "voca-exam" and datetime.now(EXAM_OPEN_AT.tzinfo) < EXAM_OPEN_AT:
        raise HTTPException(status_code=403, detail="단어장 시험은 2026년 8월 10일 14:00(한국시간)에 공개됩니다.")
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


class PortfolioCombinationRequest(BaseModel):
    ticker_a: str = Field(default="AAPL", min_length=1, max_length=20)
    ticker_b: str = Field(default="JNJ", min_length=1, max_length=20)
    period: str = Field(default="1y", pattern=r"^(3mo|6mo|1y|2y)$")


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

VOLUME_CLOUD_MARKETS = {
    "us": [
        {"ticker": "AAPL", "name": "Apple"}, {"ticker": "MSFT", "name": "Microsoft"},
        {"ticker": "GOOGL", "name": "Alphabet"}, {"ticker": "AMZN", "name": "Amazon"},
        {"ticker": "NVDA", "name": "NVIDIA"}, {"ticker": "META", "name": "Meta"},
        {"ticker": "TSLA", "name": "Tesla"},
    ],
    "kr": [
        {"ticker": "005930.KS", "name": "삼성전자"},
        {"ticker": "000660.KS", "name": "SK하이닉스"},
        {"ticker": "373220.KS", "name": "LG에너지솔루션"},
        {"ticker": "207940.KS", "name": "삼성바이오로직스"},
        {"ticker": "005380.KS", "name": "현대차"},
        {"ticker": "000270.KS", "name": "기아"},
        {"ticker": "068270.KS", "name": "셀트리온"},
        {"ticker": "105560.KS", "name": "KB금융"},
        {"ticker": "055550.KS", "name": "신한지주"},
        {"ticker": "012330.KS", "name": "현대모비스"},
        {"ticker": "035420.KS", "name": "NAVER"},
        {"ticker": "028260.KS", "name": "삼성물산"},
        {"ticker": "006400.KS", "name": "삼성SDI"},
        {"ticker": "051910.KS", "name": "LG화학"},
        {"ticker": "003670.KS", "name": "포스코홀딩스"},
        {"ticker": "035720.KS", "name": "카카오"},
        {"ticker": "096770.KS", "name": "SK이노베이션"},
        {"ticker": "034730.KS", "name": "SK"},
        {"ticker": "086790.KS", "name": "하나금융지주"},
        {"ticker": "032830.KS", "name": "삼성생명"},
    ],
}


def _extract_close_series(frame):
    close = frame["Close"]
    if hasattr(close, "columns"):
        close = close.iloc[:, 0]
    return close.dropna()


@app.post("/api/market/portfolio-combination")
def portfolio_combination(req: PortfolioCombinationRequest) -> dict[str, object]:
    """Compare two real tickers and return a plain-language diversification signal."""
    import pandas as pd
    import yfinance as yf

    def clean_ticker(raw: str) -> str:
        ticker = raw.strip().upper()
        if not re.fullmatch(r"[A-Z0-9.^=\-]{1,20}", ticker):
            raise HTTPException(status_code=422, detail="올바른 종목 코드를 입력해 주세요.")
        return ticker

    ticker_a = clean_ticker(req.ticker_a)
    ticker_b = clean_ticker(req.ticker_b)
    if ticker_a == ticker_b:
        raise HTTPException(status_code=422, detail="서로 다른 두 종목을 선택해 주세요.")

    prices: dict[str, pd.Series] = {}
    unavailable: list[str] = []
    for ticker in (ticker_a, ticker_b):
        try:
            frame = yf.download(ticker, period=req.period, interval="1d", progress=False,
                                auto_adjust=True, threads=False)
            close = _extract_close_series(frame)
            if len(close) < 21:
                unavailable.append(ticker)
            else:
                prices[ticker] = close
        except Exception:
            unavailable.append(ticker)

    if unavailable:
        names = ", ".join(unavailable)
        raise HTTPException(status_code=422, detail=f"{names}의 충분한 가격 데이터를 찾지 못했습니다.")

    # 거래일이 겹치는 구간의 일간 변화만 이용한다. 화면에는 수식 대신 신호와 문장만 노출한다.
    aligned_prices = pd.concat(prices, axis=1, join="inner").dropna()
    daily_moves = aligned_prices.pct_change(fill_method=None).dropna()
    if len(daily_moves) < 20:
        raise HTTPException(status_code=422, detail="두 종목의 함께 비교할 수 있는 거래일이 부족합니다.")
    relationship = float(daily_moves.corr().iloc[0, 1])

    if relationship < 0.30:
        signal = "green"
        summary = "최근 흐름이 비교적 다르게 나타났습니다. 함께 담을 때 한 종목에만 의존하는 정도를 낮추는 데 도움이 될 수 있습니다."
        hint = "두 종목의 움직임이 겹치는 정도가 낮은 편입니다. 업종과 보유 비중도 함께 확인해 보세요."
    elif relationship < 0.70:
        signal = "yellow"
        summary = "최근에는 일부 구간에서 함께 움직였습니다. 분산 효과는 기대할 수 있지만 크기는 제한적일 수 있습니다."
        hint = "조합의 균형은 보통 수준입니다. 다른 업종이나 자산을 더하면 포트폴리오 폭을 넓힐 수 있습니다."
    else:
        signal = "red"
        summary = "최근 가격 흐름이 자주 같은 방향으로 움직였습니다. 두 종목을 함께 담아도 분산 효과가 작을 수 있습니다."
        hint = "한 종목의 영향이 다른 종목에도 이어질 수 있습니다. 업종이 다른 종목이나 다른 자산을 함께 검토해 보세요."

    period_labels = {"3mo": "최근 3개월", "6mo": "최근 6개월", "1y": "최근 1년", "2y": "최근 2년"}
    latest_data_at = pd.Timestamp(daily_moves.index[-1]).isoformat()
    # 두 종목의 가격 단위가 달라도 흐름을 한 차트에서 비교할 수 있도록 출발선을 맞춘다.
    chart_base = aligned_prices / aligned_prices.iloc[0] * 100
    chart_points = [
        {
            "date": pd.Timestamp(index).date().isoformat(),
            "a": round(float(row[ticker_a]), 4),
            "b": round(float(row[ticker_b]), 4),
        }
        for index, row in chart_base.iterrows()
    ]
    return {
        "ticker_a": ticker_a,
        "ticker_b": ticker_b,
        "period_label": period_labels[req.period],
        "signal": signal,
        "summary": summary,
        "portfolio_hint": hint,
        "latest_data_at": latest_data_at,
        "chart_points": chart_points,
    }


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


@app.get("/api/market/volume-cloud")
def market_volume_cloud(market: str = "us") -> dict[str, object]:
    """Return recent volume and price changes for a compact market bubble cloud."""
    import pandas as pd
    import yfinance as yf

    market_key = market.lower()
    companies = VOLUME_CLOUD_MARKETS.get(market_key)
    if not companies:
        raise HTTPException(status_code=400, detail="market은 us 또는 kr만 선택할 수 있습니다.")

    tickers = [company["ticker"] for company in companies]
    try:
        data = yf.download(tickers, period="2mo", interval="1d", group_by="ticker",
                           progress=False, auto_adjust=False, threads=True)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"거래량 데이터를 가져오지 못했습니다: {exc}") from exc

    items: list[dict[str, object]] = []
    latest_dates: list[pd.Timestamp] = []
    for company in companies:
        ticker = company["ticker"]
        try:
            frame = data[ticker] if len(tickers) > 1 else data
            close = _extract_close_series(frame)
            volume = frame["Volume"]
            if hasattr(volume, "columns"):
                volume = volume.iloc[:, 0]
            volume = volume.dropna()
            if len(close) < 2 or volume.empty:
                raise ValueError("가격 또는 거래량 이력이 부족합니다.")

            last_close = float(close.iloc[-1])
            previous_close = float(close.iloc[-2])
            last_volume = float(volume.iloc[-1])
            prior_volume = volume.iloc[-21:-1] if len(volume) > 1 else volume
            average_volume = float(prior_volume.mean()) if not prior_volume.empty else last_volume
            items.append({
                "ticker": ticker,
                "name": company["name"],
                "price": round(last_close, 4),
                "change_pct": round((last_close / previous_close - 1) * 100, 2),
                "volume": int(last_volume),
                "average_volume_20d": int(average_volume),
                "volume_ratio": round(last_volume / average_volume, 2) if average_volume else 0.0,
                "latest_data_at": pd.Timestamp(close.index[-1]).isoformat(),
                "status": "ok",
            })
            latest_dates.append(pd.Timestamp(close.index[-1]))
        except Exception as exc:
            items.append({"ticker": ticker, "name": company["name"], "status": "error", "error": str(exc)})

    return {
        "market": market_key,
        "items": items,
        "fetched_at": pd.Timestamp.utcnow().isoformat(),
        "latest_data_at": max(latest_dates).isoformat() if latest_dates else None,
        "source": "Yahoo Finance",
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


app.include_router(quiz_router)

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
        "bsns_year":  req.bsns_year,
    }


app.include_router(tax_router)
app.include_router(rag_router)
install_openapi(app)

# ─────────────────────────────────────────────────────────────────────────────

app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
