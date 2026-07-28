"""Tests for DartAdapter — validation, corpCode mapping, and response parsing."""

from __future__ import annotations

import io
import json
import zipfile
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest

httpx = pytest.importorskip("httpx")

from qracer.data.dart_adapter import (  # noqa: E402
    _BASE_URL,
    DartAdapter,
    _amount_to_float,
    _check_status,
    _parse_corpcode_zip,
    _parse_dart_date,
    _parse_disclosures,
    _parse_fundamentals,
    _parse_insider,
    _parse_major_holders,
    _validate_ticker,
)
from qracer.data.providers import (  # noqa: E402
    AlternativeProvider,
    FundamentalProvider,
    NewsProvider,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _corpcode_zip(*entries: tuple[str, str]) -> bytes:
    """Build a corpCode.xml ZIP from (corp_code, stock_code) pairs."""
    rows = "".join(
        f"<list><corp_code>{cc}</corp_code><corp_name>n</corp_name>"
        f"<stock_code>{sc}</stock_code><modify_date>20230101</modify_date></list>"
        for cc, sc in entries
    )
    xml = f'<?xml version="1.0" encoding="utf-8"?><result>{rows}</result>'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("CORPCODE.xml", xml.encode("utf-8"))
    return buf.getvalue()


def _adapter(handler, tmp_path: Path) -> DartAdapter:
    """A DartAdapter whose HTTP client is a mock transport, corp map preloaded."""
    adapter = DartAdapter(api_key="key", cache_dir=tmp_path)
    adapter._client = httpx.AsyncClient(base_url=_BASE_URL, transport=httpx.MockTransport(handler))
    adapter._corp_map = {"005930": "00126380"}
    return adapter


# ---------------------------------------------------------------------------
# Constructor validation
# ---------------------------------------------------------------------------


class TestInit:
    @patch("qracer.data.dart_adapter._HAS_HTTPX", False)
    def test_missing_httpx_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ImportError, match="httpx is not installed"):
            DartAdapter(api_key="key", cache_dir=tmp_path)

    def test_missing_api_key_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="DART_API_KEY is required"):
            DartAdapter(api_key=None, cache_dir=tmp_path)

    def test_empty_api_key_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="DART_API_KEY is required"):
            DartAdapter(api_key="", cache_dir=tmp_path)

    def test_satisfies_capability_protocols(self, tmp_path: Path) -> None:
        adapter = DartAdapter(api_key="key", cache_dir=tmp_path)
        assert isinstance(adapter, FundamentalProvider)
        assert isinstance(adapter, NewsProvider)
        assert isinstance(adapter, AlternativeProvider)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestValidateTicker:
    def test_accepts_6_digit_code(self) -> None:
        assert _validate_ticker(" 005930 ") == "005930"

    @pytest.mark.parametrize("bad", ["AAPL", "05930", "0059300", "00593A"])
    def test_rejects_non_korean_codes(self, bad: str) -> None:
        with pytest.raises(ValueError, match="6-digit KRX stock code"):
            _validate_ticker(bad)


class TestParseHelpers:
    def test_parse_corpcode_zip_keeps_only_listed(self) -> None:
        content = _corpcode_zip(("00126380", "005930"), ("00999999", " "))
        assert _parse_corpcode_zip(content) == {"005930": "00126380"}

    def test_amount_to_float(self) -> None:
        assert _amount_to_float("1,234,567") == 1234567.0
        assert _amount_to_float("-") is None
        assert _amount_to_float("") is None
        assert _amount_to_float(None) is None
        assert _amount_to_float("abc") is None

    def test_parse_dart_date(self) -> None:
        assert _parse_dart_date("20260115") == date(2026, 1, 15)
        assert _parse_dart_date("2026.01.15") == date(2026, 1, 15)
        assert _parse_dart_date(None) == date.today()
        assert _parse_dart_date("garbage") == date.today()

    def test_check_status(self) -> None:
        assert _check_status({"status": "000"}) is True
        assert _check_status({"status": "013"}) is False
        with pytest.raises(RuntimeError, match="DART API error 020"):
            _check_status({"status": "020", "message": "rate limit"})

    def test_parse_fundamentals(self) -> None:
        payload = {
            "status": "000",
            "list": [
                {"account_nm": "매출액", "thstrm_amount": "1,000,000"},
                {"account_nm": "영업이익", "thstrm_amount": "300,000"},
                {"account_nm": "당기순이익", "thstrm_amount": "200,000"},
            ],
        }
        fd = _parse_fundamentals(payload, "005930")
        assert fd.ticker == "005930"
        assert fd.revenue == 1_000_000.0
        assert fd.earnings == 200_000.0
        assert fd.market_cap is None  # DART does not provide price-derived metrics

    def test_parse_disclosures(self) -> None:
        payload = {
            "status": "000",
            "list": [
                {
                    "report_nm": "분기보고서",
                    "rcept_no": "20260101000123",
                    "rcept_dt": "20260101",
                    "flr_nm": "삼성전자",
                },
                {"report_nm": "extra", "rcept_no": "x", "rcept_dt": "20260102"},
            ],
        }
        articles = _parse_disclosures(payload, limit=1)
        assert len(articles) == 1
        assert articles[0].title == "분기보고서"
        assert articles[0].source == "DART"
        assert "20260101000123" in articles[0].url

    def test_parse_major_holders(self) -> None:
        payload = {"list": [{"repror": "국민연금", "stkrt": "8.5", "rcept_dt": "20260103"}]}
        records = _parse_major_holders(payload, "005930")
        assert records[0].record_type == "major_holders"
        assert records[0].data["reporter"] == "국민연금"
        assert records[0].date == date(2026, 1, 3)

    def test_parse_insider(self) -> None:
        payload = {
            "list": [{"repror": "홍길동", "sp_stock_lmp_cnt": "1000", "rcept_dt": "20260104"}]
        }
        records = _parse_insider(payload, "005930")
        assert records[0].record_type == "insider_trades"
        assert records[0].data["shares_owned"] == "1000"


# ---------------------------------------------------------------------------
# corp_code resolution + caching
# ---------------------------------------------------------------------------


class TestCorpCodeMapping:
    async def test_fast_fail_on_non_korean_ticker(self, tmp_path: Path) -> None:
        adapter = DartAdapter(api_key="key", cache_dir=tmp_path)
        with pytest.raises(ValueError, match="6-digit KRX stock code"):
            await adapter._corp_code("AAPL")

    async def test_fetches_parses_and_caches(self, tmp_path: Path) -> None:
        content = _corpcode_zip(("00126380", "005930"))
        calls = {"n": 0}

        def handler(request):  # noqa: ANN001, ANN202
            calls["n"] += 1
            return httpx.Response(200, content=content)

        adapter = DartAdapter(api_key="key", cache_dir=tmp_path)
        adapter._client = httpx.AsyncClient(
            base_url=_BASE_URL, transport=httpx.MockTransport(handler)
        )
        assert await adapter._corp_code("005930") == "00126380"
        assert calls["n"] == 1

        # Cache file written; a fresh adapter reads it without re-fetching.
        cache = json.loads((tmp_path / "dart_corpcodes.json").read_text(encoding="utf-8"))
        assert cache["map"] == {"005930": "00126380"}

        adapter2 = DartAdapter(api_key="key", cache_dir=tmp_path)
        adapter2._client = httpx.AsyncClient(
            base_url=_BASE_URL, transport=httpx.MockTransport(handler)
        )
        assert await adapter2._corp_code("005930") == "00126380"
        assert calls["n"] == 1  # served from disk cache, no new fetch

    async def test_unknown_stock_code_raises(self, tmp_path: Path) -> None:
        adapter = _adapter(lambda r: httpx.Response(200), tmp_path)
        adapter._corp_map = {"005930": "00126380"}
        with pytest.raises(ValueError, match="No DART corp_code found"):
            await adapter._corp_code("000660")

    async def test_stale_cache_is_ignored(self, tmp_path: Path) -> None:
        (tmp_path / "dart_corpcodes.json").write_text(
            json.dumps({"fetched": "2000-01-01", "map": {"005930": "OLD"}}), encoding="utf-8"
        )
        content = _corpcode_zip(("00126380", "005930"))
        adapter = DartAdapter(api_key="key", cache_dir=tmp_path)
        adapter._client = httpx.AsyncClient(
            base_url=_BASE_URL,
            transport=httpx.MockTransport(lambda r: httpx.Response(200, content=content)),
        )
        assert await adapter._corp_code("005930") == "00126380"  # refetched, not "OLD"


# ---------------------------------------------------------------------------
# Capability methods over a mock transport
# ---------------------------------------------------------------------------


class TestCapabilities:
    async def test_get_fundamentals(self, tmp_path: Path) -> None:
        def handler(request):  # noqa: ANN001, ANN202
            assert request.url.path.endswith("fnlttSinglAcntAll.json")
            return httpx.Response(
                200,
                json={
                    "status": "000",
                    "list": [
                        {"account_nm": "매출액", "thstrm_amount": "1,000,000"},
                        {"account_nm": "당기순이익", "thstrm_amount": "200,000"},
                    ],
                },
            )

        adapter = _adapter(handler, tmp_path)
        fd = await adapter.get_fundamentals("005930")
        assert fd.revenue == 1_000_000.0
        assert fd.earnings == 200_000.0

    async def test_get_fundamentals_no_data_returns_empty(self, tmp_path: Path) -> None:
        adapter = _adapter(lambda r: httpx.Response(200, json={"status": "013"}), tmp_path)
        fd = await adapter.get_fundamentals("005930")
        assert fd.ticker == "005930"
        assert fd.revenue is None and fd.earnings is None

    async def test_get_news(self, tmp_path: Path) -> None:
        def handler(request):  # noqa: ANN001, ANN202
            assert request.url.path.endswith("list.json")
            return httpx.Response(
                200,
                json={
                    "status": "000",
                    "list": [
                        {
                            "report_nm": "주요사항보고서",
                            "rcept_no": "20260105000001",
                            "rcept_dt": "20260105",
                            "flr_nm": "삼성전자",
                        }
                    ],
                },
            )

        adapter = _adapter(handler, tmp_path)
        news = await adapter.get_news("005930", limit=5)
        assert len(news) == 1
        assert news[0].title == "주요사항보고서"
        assert news[0].source == "DART"

    async def test_get_alternative_major_holders(self, tmp_path: Path) -> None:
        def handler(request):  # noqa: ANN001, ANN202
            assert request.url.path.endswith("majorstock.json")
            return httpx.Response(
                200, json={"status": "000", "list": [{"repror": "국민연금", "stkrt": "8.5"}]}
            )

        adapter = _adapter(handler, tmp_path)
        records = await adapter.get_alternative("005930", "major_holders")
        assert records[0].record_type == "major_holders"

    async def test_get_alternative_insider(self, tmp_path: Path) -> None:
        def handler(request):  # noqa: ANN001, ANN202
            assert request.url.path.endswith("elestock.json")
            return httpx.Response(
                200,
                json={"status": "000", "list": [{"repror": "홍길동", "sp_stock_lmp_cnt": "1000"}]},
            )

        adapter = _adapter(handler, tmp_path)
        records = await adapter.get_alternative("005930", "insider_trades")
        assert records[0].record_type == "insider_trades"

    async def test_get_alternative_unknown_type_returns_empty(self, tmp_path: Path) -> None:
        adapter = _adapter(lambda r: httpx.Response(200, json={"status": "000"}), tmp_path)
        assert await adapter.get_alternative("005930", "congress_trades") == []


# ---------------------------------------------------------------------------
# DataRegistry integration — DART primary, fallback for non-Korean tickers
# ---------------------------------------------------------------------------


class TestRegistryFallback:
    async def test_korean_code_hits_dart_us_ticker_falls_through(self, tmp_path: Path) -> None:
        from qracer.data.providers import FundamentalData, FundamentalProvider
        from qracer.data.registry import DataRegistry

        class StubFundamentals:
            async def get_fundamentals(self, ticker: str) -> FundamentalData:
                return FundamentalData(ticker=ticker, revenue=42.0)

        def handler(request):  # noqa: ANN001, ANN202
            return httpx.Response(
                200,
                json={"status": "000", "list": [{"account_nm": "매출액", "thstrm_amount": "999"}]},
            )

        dart = _adapter(handler, tmp_path)
        registry = DataRegistry()
        registry.register("dart", dart, [FundamentalProvider])  # higher priority (first)
        registry.register("finnhub", StubFundamentals(), [FundamentalProvider])

        # Korean 6-digit code → DART answers.
        kr = await registry.async_get_with_fallback(
            FundamentalProvider, "get_fundamentals", "005930"
        )
        assert kr.revenue == 999.0

        # US ticker → DART fast-fails (ValueError), registry falls through to the stub.
        us = await registry.async_get_with_fallback(FundamentalProvider, "get_fundamentals", "AAPL")
        assert us.revenue == 42.0
