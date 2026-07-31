"""Tests for KisAdapter — validation, token caching, and response parsing.

All HTTP is served by an ``httpx.MockTransport``; no live KIS credentials or
network are used.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest

httpx = pytest.importorskip("httpx")

from qracer.data.kis_adapter import (  # noqa: E402
    _REAL_URL,
    KisAdapter,
    _check_rt,
    _normalize_stock_code,
    _parse_futures,
    _parse_ohlcv,
    _parse_option_chain,
    _to_float,
    _to_int,
)
from qracer.data.providers import (  # noqa: E402
    DerivativesProvider,
    PriceProvider,
)

# ---------------------------------------------------------------------------
# Mock transport helpers
# ---------------------------------------------------------------------------

_TOKEN_BODY = {"access_token": "tok-abc", "token_type": "Bearer", "expires_in": 86400}


def _router(routes: dict[str, dict], counter: dict[str, int] | None = None):
    """Build a MockTransport handler dispatching by URL path.

    ``routes`` maps a path substring to the JSON body to return. A ``/oauth2``
    token route is always provided. ``counter`` (if given) tallies path hits.
    """

    def handler(request):  # noqa: ANN001, ANN202
        path = request.url.path
        if counter is not None:
            counter[path] = counter.get(path, 0) + 1
        if "/oauth2/tokenP" in path:
            return httpx.Response(200, json=_TOKEN_BODY)
        for frag, body in routes.items():
            if frag in path:
                return httpx.Response(200, json=body)
        return httpx.Response(404, json={"rt_cd": "1", "msg1": f"no route for {path}"})

    return handler


def _adapter(handler, tmp_path: Path) -> KisAdapter:
    """A KisAdapter whose HTTP client is a mock transport, token cache under tmp_path."""
    adapter = KisAdapter(api_key="appkey123", api_secret="secret456", cache_dir=tmp_path)
    adapter._client = httpx.AsyncClient(base_url=_REAL_URL, transport=httpx.MockTransport(handler))
    return adapter


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestNormalizeStockCode:
    def test_plain_six_digit(self) -> None:
        assert _normalize_stock_code("005930") == "005930"

    def test_strips_ks_kq_suffix(self) -> None:
        assert _normalize_stock_code("005930.KS") == "005930"
        assert _normalize_stock_code("035720.kq") == "035720"

    def test_rejects_non_korean(self) -> None:
        with pytest.raises(ValueError, match="6-digit KRX stock code"):
            _normalize_stock_code("AAPL")


class TestNumericParse:
    def test_to_float(self) -> None:
        assert _to_float("1,234.5") == 1234.5
        assert _to_float("") is None
        assert _to_float("-") is None
        assert _to_float(None) is None

    def test_to_int(self) -> None:
        assert _to_int("58,478,873") == 58478873
        assert _to_int("") is None


class TestCheckRt:
    def test_success_passes(self) -> None:
        _check_rt({"rt_cd": "0", "msg1": "정상"})  # no raise

    def test_failure_raises_with_message(self) -> None:
        with pytest.raises(RuntimeError, match="EGW00201|rate"):
            _check_rt({"rt_cd": "1", "msg1": "rate limit EGW00201"})


class TestParseOhlcv:
    def test_sorts_ascending_and_maps_fields(self) -> None:
        rows = [
            {
                "stck_bsop_date": "20260731",
                "stck_oprc": "257000",
                "stck_hgpr": "267000",
                "stck_lwpr": "243000",
                "stck_clpr": "262500",
                "acml_vol": "58478873",
            },
            {
                "stck_bsop_date": "20260730",
                "stck_oprc": "200000",
                "stck_hgpr": "210000",
                "stck_lwpr": "199000",
                "stck_clpr": "207000",
                "acml_vol": "1000",
            },
        ]
        bars = _parse_ohlcv(rows)
        assert [b.date for b in bars] == [date(2026, 7, 30), date(2026, 7, 31)]
        latest = bars[-1]
        assert latest.open == 257000 and latest.high == 267000
        assert latest.low == 243000 and latest.close == 262500
        assert latest.volume == 58478873

    def test_skips_unparseable_rows(self) -> None:
        rows = [
            {"stck_bsop_date": "bad", "stck_clpr": "1"},
            {"stck_bsop_date": "20260731", "stck_clpr": ""},  # no close
            {"stck_bsop_date": "20260731", "stck_clpr": "100"},
        ]
        bars = _parse_ohlcv(rows)
        assert len(bars) == 1 and bars[0].close == 100


class TestParseDerivatives:
    def test_futures(self) -> None:
        out = {
            "futs_prpr": "412.50",
            "hts_kor_isnm": "KOSPI200 F 202609",
            "futs_prdy_vrss": "-3.20",
            "prdy_ctrt": "-0.77",
            "acml_vol": "120000",
            "hts_otst_stpl_qty": "300000",
        }
        q = _parse_futures(out, "101W09")
        assert q.code == "101W09" and q.price == 412.5
        assert q.open_interest == 300000 and q.volume == 120000
        assert q.change == -3.2

    def test_option_chain_splits_calls_and_puts(self) -> None:
        payload = {
            "output1": [
                {
                    "optn_shrn_iscd": "201AB320",
                    "optn_prpr": "5.10",
                    "acpr": "320.0",
                    "acml_vol": "500",
                    "hts_otst_stpl_qty": "1200",
                    "delta": "0.55",
                },
            ],
            "output2": [
                {
                    "optn_shrn_iscd": "301AB320",
                    "optn_prpr": "4.20",
                    "acpr": "320.0",
                    "acml_vol": "400",
                },
                {"optn_shrn_iscd": "", "optn_prpr": "1", "acpr": "1"},  # dropped: no code
            ],
        }
        chain = _parse_option_chain(payload, "KOSPI200", "202609")
        assert chain.underlying == "KOSPI200" and chain.expiry == "202609"
        assert len(chain.calls) == 1 and chain.calls[0].right == "C"
        assert chain.calls[0].strike == 320.0 and chain.calls[0].delta == 0.55
        assert len(chain.puts) == 1 and chain.puts[0].right == "P"


# ---------------------------------------------------------------------------
# Constructor validation
# ---------------------------------------------------------------------------


class TestInit:
    @patch("qracer.data.kis_adapter._HAS_HTTPX", False)
    def test_missing_httpx_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ImportError, match="httpx is not installed"):
            KisAdapter(api_key="k", api_secret="s", cache_dir=tmp_path)

    def test_missing_secret_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="app key and app secret"):
            KisAdapter(api_key="k", api_secret=None, cache_dir=tmp_path)

    def test_missing_key_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="app key and app secret"):
            KisAdapter(api_key=None, api_secret="s", cache_dir=tmp_path)


# ---------------------------------------------------------------------------
# Capabilities (runtime protocol satisfaction)
# ---------------------------------------------------------------------------


class TestCapabilities:
    def test_satisfies_price_and_derivatives(self, tmp_path: Path) -> None:
        adapter = KisAdapter(api_key="k", api_secret="s", cache_dir=tmp_path)
        assert isinstance(adapter, PriceProvider)
        assert isinstance(adapter, DerivativesProvider)


# ---------------------------------------------------------------------------
# Token caching
# ---------------------------------------------------------------------------


class TestToken:
    async def test_token_issued_once_and_reused_in_memory(self, tmp_path: Path) -> None:
        counter: dict[str, int] = {}
        handler = _router(
            {"inquire-price": {"rt_cd": "0", "output": {"stck_prpr": "262500"}}}, counter
        )
        adapter = _adapter(handler, tmp_path)
        await adapter.get_price("005930")
        await adapter.get_price("005930")
        token_hits = sum(v for k, v in counter.items() if "/oauth2/tokenP" in k)
        assert token_hits == 1  # second call reused the in-memory token

    async def test_token_persisted_to_disk_and_reused_by_new_instance(self, tmp_path: Path) -> None:
        counter: dict[str, int] = {}
        handler = _router({"inquire-price": {"rt_cd": "0", "output": {"stck_prpr": "1"}}}, counter)
        a1 = _adapter(handler, tmp_path)
        await a1.get_price("005930")
        assert (tmp_path / "kis_token.json").exists()

        a2 = _adapter(handler, tmp_path)  # fresh instance, same cache dir
        await a2.get_price("005930")
        token_hits = sum(v for k, v in counter.items() if "/oauth2/tokenP" in k)
        assert token_hits == 1  # a2 read the disk cache instead of re-issuing

    def test_stale_disk_cache_ignored(self, tmp_path: Path) -> None:
        (tmp_path / "kis_token.json").write_text(
            json.dumps(
                {"base": _REAL_URL, "key": "appkey"[:6], "access_token": "old", "expires_at": 0}
            ),
            encoding="utf-8",
        )
        adapter = KisAdapter(api_key="appkey123", api_secret="s", cache_dir=tmp_path)
        assert adapter._read_token_cache() is None  # expired -> ignored


# ---------------------------------------------------------------------------
# Capability calls end-to-end (mocked HTTP)
# ---------------------------------------------------------------------------


class TestPriceProvider:
    async def test_get_price(self, tmp_path: Path) -> None:
        handler = _router({"inquire-price": {"rt_cd": "0", "output": {"stck_prpr": "262500"}}})
        adapter = _adapter(handler, tmp_path)
        assert await adapter.get_price("005930") == 262500.0

    async def test_get_price_rejects_non_korean_before_network(self, tmp_path: Path) -> None:
        handler = _router({})
        adapter = _adapter(handler, tmp_path)
        with pytest.raises(ValueError, match="6-digit KRX"):
            await adapter.get_price("AAPL")

    async def test_get_ohlcv(self, tmp_path: Path) -> None:
        body = {
            "rt_cd": "0",
            "output2": [
                {
                    "stck_bsop_date": "20260731",
                    "stck_oprc": "257000",
                    "stck_hgpr": "267000",
                    "stck_lwpr": "243000",
                    "stck_clpr": "262500",
                    "acml_vol": "58478873",
                },
            ],
        }
        handler = _router({"inquire-daily-itemchartprice": body})
        adapter = _adapter(handler, tmp_path)
        bars = await adapter.get_ohlcv("005930", date(2026, 7, 20), date(2026, 7, 31))
        assert len(bars) == 1 and bars[0].close == 262500

    async def test_api_error_raises(self, tmp_path: Path) -> None:
        handler = _router({"inquire-price": {"rt_cd": "1", "msg1": "권한 없음"}})
        adapter = _adapter(handler, tmp_path)
        with pytest.raises(RuntimeError, match="KIS API error"):
            await adapter.get_price("005930")


class TestDerivativesProvider:
    async def test_get_futures_quote(self, tmp_path: Path) -> None:
        body = {
            "rt_cd": "0",
            "output": {"futs_prpr": "412.50", "acml_vol": "120000", "hts_otst_stpl_qty": "300000"},
        }
        handler = _router({"domestic-futureoption/v1/quotations/inquire-price": body})
        adapter = _adapter(handler, tmp_path)
        q = await adapter.get_futures_quote("101W09")
        assert q.price == 412.5 and q.open_interest == 300000

    async def test_get_option_chain(self, tmp_path: Path) -> None:
        body = {
            "rt_cd": "0",
            "output1": [
                {"optn_shrn_iscd": "201AB320", "optn_prpr": "5.1", "acpr": "320", "acml_vol": "5"},
            ],
            "output2": [
                {"optn_shrn_iscd": "301AB320", "optn_prpr": "4.2", "acpr": "320", "acml_vol": "4"},
            ],
        }
        handler = _router({"display-board-option-list": body})
        adapter = _adapter(handler, tmp_path)
        chain = await adapter.get_option_chain("KOSPI200", "202609")
        assert len(chain.calls) == 1 and len(chain.puts) == 1
        assert chain.calls[0].strike == 320.0
