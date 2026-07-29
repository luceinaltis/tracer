"""DartAdapter — Fundamental, News (disclosures), and Alternative data via OpenDART.

OpenDART (금융감독원 전자공시, https://opendart.fss.or.kr) is Korea's electronic
disclosure system — the KRX equivalent of SEC EDGAR. This adapter provides three
capabilities for Korean listed companies, keyed by 6-digit KRX stock code (e.g.
``005930`` for Samsung Electronics):

- FundamentalProvider → revenue + net income from the latest annual report
- NewsProvider → recent disclosure filings (treated as a news feed)
- AlternativeProvider → major-holder (5%) and executive/insider shareholding reports

DART queries take an 8-digit ``corp_code``, not a stock code, so the adapter downloads
OpenDART's ``corpCode.xml`` once and caches a ``{stock_code: corp_code}`` map to
``~/.qracer/dart_corpcodes.json``.

Non-6-digit tickers (e.g. US symbols) fail fast with ``ValueError`` so the
DataRegistry falls straight through to the next adapter.
"""

from __future__ import annotations

import io
import json
import logging
import zipfile
from datetime import date, datetime
from pathlib import Path
from xml.etree import ElementTree

from qracer.data.providers import (
    AlternativeRecord,
    FundamentalData,
    NewsArticle,
)

try:
    import httpx

    _HAS_HTTPX = True
except ImportError:
    _HAS_HTTPX = False

logger = logging.getLogger(__name__)

_BASE_URL = "https://opendart.fss.or.kr/api/"
_DISCLOSURE_VIEWER = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo="
_CORPCODE_CACHE_MAX_AGE_DAYS = 7
_NEWS_LOOKBACK_DAYS = 90
_ANNUAL_REPORT_CODE = "11011"  # 사업보고서 (annual)

# Account names (account_nm) that carry revenue / net income across filing formats.
_REVENUE_NAMES = frozenset({"매출액", "수익(매출액)", "영업수익", "매출"})
_NET_INCOME_NAMES = frozenset({"당기순이익", "당기순이익(손실)", "연결당기순이익"})

# DART status codes: "000" ok, "013" no data for the query (not an error).
_STATUS_OK = "000"
_STATUS_NO_DATA = "013"


# ---------------------------------------------------------------------------
# Pure helpers (no network) — unit-tested directly
# ---------------------------------------------------------------------------


def _validate_ticker(ticker: str) -> str:
    """Return the ticker if it is a 6-digit KRX stock code, else raise ValueError.

    Failing fast on non-Korean tickers lets the DataRegistry fall through to the
    next adapter (e.g. finnhub) instead of issuing a doomed DART request.
    """
    code = ticker.strip()
    if len(code) != 6 or not code.isdigit():
        raise ValueError(f"DART requires a 6-digit KRX stock code (e.g. '005930'), got {ticker!r}")
    return code


def _parse_corpcode_zip(content: bytes) -> dict[str, str]:
    """Parse OpenDART's corpCode.xml ZIP into a ``{stock_code: corp_code}`` map.

    Only listed companies (non-empty ``stock_code``) are included.
    """
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        name = next((n for n in zf.namelist() if n.lower().endswith(".xml")), None)
        if name is None:
            raise ValueError("corpCode ZIP contained no XML file")
        xml_bytes = zf.read(name)

    root = ElementTree.fromstring(xml_bytes)
    mapping: dict[str, str] = {}
    for item in root.iter("list"):
        stock_code = (item.findtext("stock_code") or "").strip()
        corp_code = (item.findtext("corp_code") or "").strip()
        if stock_code and corp_code:
            mapping[stock_code] = corp_code
    return mapping


def _amount_to_float(raw: object) -> float | None:
    """Convert a DART amount string (may contain commas / be '-') to float."""
    if raw is None:
        return None
    text = str(raw).strip().replace(",", "")
    if not text or text == "-":
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _parse_dart_date(raw: str | None) -> date:
    """Parse a DART ``YYYYMMDD`` (or ``YYYY.MM.DD``) date; fall back to today."""
    if raw:
        cleaned = raw.strip().replace(".", "").replace("-", "")
        if len(cleaned) >= 8 and cleaned[:8].isdigit():
            try:
                return datetime.strptime(cleaned[:8], "%Y%m%d").date()
            except ValueError:
                pass
    return date.today()


def _check_status(payload: dict) -> bool:
    """Return True if the payload has usable data.

    - ``"000"`` → has data (True)
    - ``"013"`` → valid query, no data (False)
    - anything else → raise RuntimeError so the registry falls back.
    """
    status = str(payload.get("status", ""))
    if status == _STATUS_OK:
        return True
    if status == _STATUS_NO_DATA:
        return False
    raise RuntimeError(f"DART API error {status}: {payload.get('message', 'unknown')}")


def _parse_fundamentals(payload: dict, ticker: str) -> FundamentalData:
    """Extract revenue + net income from a fnlttSinglAcntAll.json payload."""
    revenue: float | None = None
    earnings: float | None = None
    for row in payload.get("list", []):
        name = (row.get("account_nm") or "").strip()
        if revenue is None and name in _REVENUE_NAMES:
            revenue = _amount_to_float(row.get("thstrm_amount"))
        elif earnings is None and name in _NET_INCOME_NAMES:
            earnings = _amount_to_float(row.get("thstrm_amount"))
    return FundamentalData(
        ticker=ticker,
        revenue=revenue,
        earnings=earnings,
        fetched_at=datetime.now(),
    )


def _parse_disclosures(payload: dict, limit: int) -> list[NewsArticle]:
    """Map a list.json disclosure payload to NewsArticle rows."""
    articles: list[NewsArticle] = []
    for row in payload.get("list", [])[:limit]:
        rcept_no = (row.get("rcept_no") or "").strip()
        published = _parse_dart_date(row.get("rcept_dt"))
        articles.append(
            NewsArticle(
                title=(row.get("report_nm") or "").strip(),
                source="DART",
                published_at=datetime(published.year, published.month, published.day),
                url=f"{_DISCLOSURE_VIEWER}{rcept_no}" if rcept_no else "",
                summary=(row.get("flr_nm") or "").strip(),
                sentiment=None,
            )
        )
    return articles


def _parse_major_holders(payload: dict, ticker: str) -> list[AlternativeRecord]:
    """Map a majorstock.json payload (5% bulk-holding reports) to AlternativeRecords."""
    records: list[AlternativeRecord] = []
    for row in payload.get("list", []):
        records.append(
            AlternativeRecord(
                record_type="major_holders",
                ticker=ticker,
                data={
                    "reporter": (row.get("repror") or "").strip(),
                    "report_type": (row.get("report_tp") or "").strip(),
                    "shares_held": (row.get("stkqy") or "").strip(),
                    "holding_pct": (row.get("stkrt") or "").strip(),
                },
                source="dart",
                date=_parse_dart_date(row.get("rcept_dt")),
            )
        )
    return records


def _parse_insider(payload: dict, ticker: str) -> list[AlternativeRecord]:
    """Map an elestock.json payload (exec/major-shareholder ownership) to records."""
    records: list[AlternativeRecord] = []
    for row in payload.get("list", []):
        records.append(
            AlternativeRecord(
                record_type="insider_trades",
                ticker=ticker,
                data={
                    "reporter": (row.get("repror") or "").strip(),
                    "is_registered_exec": (row.get("isu_exctv_rgist_at") or "").strip(),
                    "position": (row.get("isu_exctv_ofcps") or "").strip(),
                    "shares_owned": (row.get("sp_stock_lmp_cnt") or "").strip(),
                },
                source="dart",
                date=_parse_dart_date(row.get("rcept_dt")),
            )
        )
    return records


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class DartAdapter:
    """Data adapter for OpenDART (Fundamentals, disclosures, insider/major holdings)."""

    def __init__(self, api_key: str | None = None, cache_dir: Path | None = None) -> None:
        if not _HAS_HTTPX:
            raise ImportError("httpx is not installed. Install it with: uv add 'qracer[dart]'")
        if not api_key:
            raise ValueError("DART_API_KEY is required. Get one at https://opendart.fss.or.kr")
        self._api_key = api_key
        self._client = httpx.AsyncClient(base_url=_BASE_URL, timeout=20.0)
        if cache_dir is None:
            from qracer.config.loader import _user_dir

            cache_dir = _user_dir()
        self._cache_path = Path(cache_dir) / "dart_corpcodes.json"
        self._corp_map: dict[str, str] | None = None

    # -- corp_code resolution ------------------------------------------------

    async def _corp_code(self, ticker: str) -> str:
        """Resolve a 6-digit stock code to its 8-digit DART corp_code."""
        code = _validate_ticker(ticker)
        mapping = await self._load_corp_map()
        corp_code = mapping.get(code)
        if not corp_code:
            raise ValueError(f"No DART corp_code found for stock code {code!r}")
        return corp_code

    async def _load_corp_map(self) -> dict[str, str]:
        if self._corp_map is not None:
            return self._corp_map

        cached = self._read_cache()
        if cached is not None:
            self._corp_map = cached
            return cached

        mapping = await self._fetch_corp_map()
        self._corp_map = mapping
        self._write_cache(mapping)
        return mapping

    def _read_cache(self) -> dict[str, str] | None:
        if not self._cache_path.exists():
            return None
        try:
            payload = json.loads(self._cache_path.read_text(encoding="utf-8"))
            fetched = datetime.strptime(payload["fetched"], "%Y-%m-%d").date()
            if (date.today() - fetched).days > _CORPCODE_CACHE_MAX_AGE_DAYS:
                return None
            return dict(payload["map"])
        except Exception:  # noqa: BLE001 - a corrupt/stale cache just triggers a refetch
            logger.debug("DART corp_code cache unreadable; refetching", exc_info=True)
            return None

    def _write_cache(self, mapping: dict[str, str]) -> None:
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            self._cache_path.write_text(
                json.dumps({"fetched": date.today().isoformat(), "map": mapping}),
                encoding="utf-8",
            )
        except Exception:  # noqa: BLE001 - caching is best-effort
            logger.debug("Could not write DART corp_code cache", exc_info=True)

    async def _fetch_corp_map(self) -> dict[str, str]:
        resp = await self._client.get("corpCode.xml", params={"crtfc_key": self._api_key})
        resp.raise_for_status()
        return _parse_corpcode_zip(resp.content)

    # -- capabilities --------------------------------------------------------

    async def _get_json(self, endpoint: str, corp_code: str, **params: str) -> dict:
        resp = await self._client.get(
            endpoint, params={"crtfc_key": self._api_key, "corp_code": corp_code, **params}
        )
        resp.raise_for_status()
        return resp.json()

    async def get_fundamentals(self, ticker: str) -> FundamentalData:
        """Revenue + net income from the most recent available annual report."""
        corp_code = await self._corp_code(ticker)
        for year in (date.today().year - 1, date.today().year - 2):
            payload = await self._get_json(
                "fnlttSinglAcntAll.json",
                corp_code,
                bsns_year=str(year),
                reprt_code=_ANNUAL_REPORT_CODE,
                fs_div="CFS",
            )
            if _check_status(payload):
                return _parse_fundamentals(payload, ticker)
        # No annual report data found in the recent window.
        return FundamentalData(ticker=ticker, fetched_at=datetime.now())

    async def get_news(self, ticker: str, limit: int = 10) -> list[NewsArticle]:
        """Recent disclosure filings for the company, newest first."""
        corp_code = await self._corp_code(ticker)
        from datetime import timedelta

        end = date.today()
        start = end - timedelta(days=_NEWS_LOOKBACK_DAYS)
        payload = await self._get_json(
            "list.json",
            corp_code,
            bgn_de=start.strftime("%Y%m%d"),
            end_de=end.strftime("%Y%m%d"),
            page_count="100",
        )
        if not _check_status(payload):
            return []
        return _parse_disclosures(payload, limit)

    async def get_alternative(self, ticker: str, record_type: str) -> list[AlternativeRecord]:
        """Major-holder (5%) or executive/insider shareholding reports."""
        corp_code = await self._corp_code(ticker)
        if record_type == "major_holders":
            payload = await self._get_json("majorstock.json", corp_code)
            return _parse_major_holders(payload, ticker) if _check_status(payload) else []
        if record_type == "insider_trades":
            payload = await self._get_json("elestock.json", corp_code)
            return _parse_insider(payload, ticker) if _check_status(payload) else []
        return []
