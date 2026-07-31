"""KisAdapter — Price/OHLCV and derivatives (futures & option chains) via KIS.

한국투자증권(Korea Investment & Securities) 오픈API (https://apiportal.koreainvestment.com)
is a REST gateway to KRX market data. Unlike a keyless feed it authenticates with an
``appkey`` + ``appsecret`` pair exchanged for a short-lived OAuth bearer token, so this
adapter needs *two* credentials (wired through ``ProviderConfig.api_key_env`` +
``secret_env``). It provides two capabilities, keyed by KRX contract code:

- PriceProvider       → current price + daily OHLCV for a 6-digit KRX stock code
- DerivativesProvider → futures quotes and option chains (국내선물옵션)

Tokens are issued at most once per minute per app, so the access token is cached in
memory and on disk (``~/.qracer/kis_token.json``) and refreshed only near expiry.

The stock endpoints (``inquire-price`` / ``inquire-daily-itemchartprice``) are verified
against the live API. The 국내선물옵션 endpoints' exact ``tr_id`` / response fields are
mapped from the KIS portal docs and should be smoke-tested against a live account
(they can vary by account type); see docs/architecture.md.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime
from pathlib import Path

from qracer.data.providers import (
    OHLCV,
    FuturesQuote,
    OptionChain,
    OptionQuote,
)

try:
    import httpx

    _HAS_HTTPX = True
except ImportError:
    _HAS_HTTPX = False

logger = logging.getLogger(__name__)

_REAL_URL = "https://openapi.koreainvestment.com:9443"
_MOCK_URL = "https://openapivts.koreainvestment.com:29443"

# tr_id codes (per KIS portal). Stock ones are live-verified; derivatives are from docs.
_TR_STOCK_PRICE = "FHKST01010100"  # 주식현재가 시세
_TR_STOCK_DAILY = "FHKST03010100"  # 주식일별 기간별시세
_TR_FUT_PRICE = "FHMIF10000000"  # 선물옵션 시세
_TR_OPT_BOARD = "FHPIF05030100"  # 국내옵션전광판 콜풋

# Refresh the token when fewer than this many seconds of its life remain.
_TOKEN_REFRESH_MARGIN = 300


# ---------------------------------------------------------------------------
# Pure helpers (no network) — unit-tested directly
# ---------------------------------------------------------------------------


def _normalize_stock_code(ticker: str) -> str:
    """Return a bare 6-digit KRX code, stripping an optional ``.KS``/``.KQ`` suffix.

    Non-Korean tickers fail fast with ``ValueError`` so the DataRegistry falls
    straight through to the next PriceProvider (e.g. yfinance).
    """
    code = ticker.strip().upper()
    for suffix in (".KS", ".KQ"):
        if code.endswith(suffix):
            code = code[: -len(suffix)]
            break
    if len(code) != 6 or not code.isdigit():
        raise ValueError(f"KIS requires a 6-digit KRX stock code (e.g. '005930'), got {ticker!r}")
    return code


def _to_float(raw: object) -> float | None:
    """Parse a KIS numeric string (may be blank / contain commas / sign) to float."""
    if raw is None:
        return None
    text = str(raw).strip().replace(",", "")
    if not text or text in {"-", "."}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _to_int(raw: object) -> int | None:
    value = _to_float(raw)
    return int(value) if value is not None else None


def _check_rt(payload: dict) -> None:
    """Raise if a KIS response is not a success (``rt_cd == '0'``)."""
    rt_cd = str(payload.get("rt_cd", ""))
    if rt_cd != "0":
        msg = payload.get("msg1") or payload.get("msg_cd") or "unknown error"
        raise RuntimeError(f"KIS API error (rt_cd={rt_cd!r}): {msg}")


def _parse_ohlcv(output2: list[dict]) -> list[OHLCV]:
    """Map an ``inquire-daily-itemchartprice`` ``output2`` array to OHLCV bars.

    KIS returns rows newest-first; the result is sorted oldest-first. Rows whose
    date or close cannot be parsed are skipped rather than raising.
    """
    bars: list[OHLCV] = []
    for row in output2:
        raw_date = (row.get("stck_bsop_date") or "").strip()
        close = _to_float(row.get("stck_clpr"))
        if len(raw_date) != 8 or not raw_date.isdigit() or close is None:
            continue
        bars.append(
            OHLCV(
                date=datetime.strptime(raw_date, "%Y%m%d").date(),
                open=_to_float(row.get("stck_oprc")) or close,
                high=_to_float(row.get("stck_hgpr")) or close,
                low=_to_float(row.get("stck_lwpr")) or close,
                close=close,
                volume=_to_int(row.get("acml_vol")) or 0,
            )
        )
    bars.sort(key=lambda b: b.date)
    return bars


def _parse_futures(output: dict, code: str) -> FuturesQuote:
    """Map a 선물옵션 ``inquire-price`` ``output`` object to a FuturesQuote."""
    price = _to_float(output.get("futs_prpr")) or _to_float(output.get("stck_prpr")) or 0.0
    return FuturesQuote(
        code=code,
        price=price,
        underlying=output.get("hts_kor_isnm") or None,
        change=_to_float(output.get("futs_prdy_vrss") or output.get("prdy_vrss")),
        change_pct=_to_float(output.get("prdy_ctrt")),
        volume=_to_int(output.get("acml_vol")),
        open_interest=_to_int(output.get("hts_otst_stpl_qty") or output.get("otst_stpl_qty")),
    )


def _parse_option_row(row: dict, right: str) -> OptionQuote | None:
    """Map one row of an 옵션전광판 payload to an OptionQuote (None if unparseable)."""
    code = (row.get("optn_shrn_iscd") or row.get("mksc_shrn_iscd") or "").strip()
    price = _to_float(row.get("optn_prpr") or row.get("stck_prpr"))
    strike = _to_float(row.get("acpr") or row.get("optn_strk_prc"))
    if not code or price is None or strike is None:
        return None
    return OptionQuote(
        code=code,
        right=right,
        strike=strike,
        price=price,
        change=_to_float(row.get("optn_prdy_vrss") or row.get("prdy_vrss")),
        volume=_to_int(row.get("acml_vol")),
        open_interest=_to_int(row.get("hts_otst_stpl_qty") or row.get("otst_stpl_qty")),
        implied_vol=_to_float(row.get("hts_ints_vltl") or row.get("optn_ints_vltl")),
        delta=_to_float(row.get("delta")),
        gamma=_to_float(row.get("gama") or row.get("gamma")),
        theta=_to_float(row.get("theta")),
        vega=_to_float(row.get("vega")),
    )


def _parse_option_chain(payload: dict, underlying: str, expiry: str | None) -> OptionChain:
    """Map an 옵션전광판 콜풋 payload (call rows + put rows) to an OptionChain.

    KIS returns calls and puts in two arrays; field names differ by account tier,
    so both common spellings are accepted and unparseable rows are dropped.
    """
    calls_raw = payload.get("output1") or payload.get("call") or []
    puts_raw = payload.get("output2") or payload.get("put") or []
    calls = [q for row in calls_raw if (q := _parse_option_row(row, "C"))]
    puts = [q for row in puts_raw if (q := _parse_option_row(row, "P"))]
    return OptionChain(underlying=underlying, expiry=expiry, calls=calls, puts=puts)


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class KisAdapter:
    """Data adapter for KIS (Price/OHLCV + futures/option derivatives)."""

    def __init__(
        self,
        api_key: str | None = None,
        api_secret: str | None = None,
        *,
        use_mock: bool = False,
        cache_dir: Path | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not _HAS_HTTPX:
            raise ImportError("httpx is not installed. Install it with: uv add 'qracer[kis]'")
        if not api_key or not api_secret:
            raise ValueError(
                "KIS requires both an app key and app secret. Get them at "
                "https://apiportal.koreainvestment.com"
            )
        self._app_key = api_key
        self._app_secret = api_secret
        self._base_url = _MOCK_URL if use_mock else _REAL_URL
        self._client = client or httpx.AsyncClient(base_url=self._base_url, timeout=20.0)
        if cache_dir is None:
            from qracer.config.loader import _user_dir

            cache_dir = _user_dir()
        self._token_cache_path = Path(cache_dir) / "kis_token.json"
        self._token: str | None = None
        self._token_expiry: float = 0.0

    # -- OAuth token ---------------------------------------------------------

    async def _access_token(self) -> str:
        """Return a valid bearer token, reusing the memory/disk cache when fresh."""
        now = time.time()
        if self._token and now < self._token_expiry - _TOKEN_REFRESH_MARGIN:
            return self._token

        cached = self._read_token_cache()
        if cached is not None:
            self._token, self._token_expiry = cached
            return self._token

        resp = await self._client.post(
            "/oauth2/tokenP",
            json={
                "grant_type": "client_credentials",
                "appkey": self._app_key,
                "appsecret": self._app_secret,
            },
        )
        resp.raise_for_status()
        payload = resp.json()
        token = payload.get("access_token")
        if not token:
            raise RuntimeError(f"KIS token request failed: {payload}")
        self._token = token
        self._token_expiry = now + float(payload.get("expires_in", 86400))
        self._write_token_cache(token, self._token_expiry)
        return token

    def _read_token_cache(self) -> tuple[str, float] | None:
        if not self._token_cache_path.exists():
            return None
        try:
            data = json.loads(self._token_cache_path.read_text(encoding="utf-8"))
            if data.get("base") != self._base_url or data.get("key") != self._app_key[:6]:
                return None
            expiry = float(data["expires_at"])
            if time.time() >= expiry - _TOKEN_REFRESH_MARGIN:
                return None
            return str(data["access_token"]), expiry
        except Exception:  # noqa: BLE001 - a corrupt/stale cache just triggers a refetch
            logger.debug("KIS token cache unreadable; refetching", exc_info=True)
            return None

    def _write_token_cache(self, token: str, expiry: float) -> None:
        try:
            self._token_cache_path.parent.mkdir(parents=True, exist_ok=True)
            self._token_cache_path.write_text(
                json.dumps(
                    {
                        "base": self._base_url,
                        "key": self._app_key[:6],
                        "access_token": token,
                        "expires_at": expiry,
                    }
                ),
                encoding="utf-8",
            )
        except Exception:  # noqa: BLE001 - caching is best-effort
            logger.debug("Could not write KIS token cache", exc_info=True)

    async def _headers(self, tr_id: str) -> dict[str, str]:
        token = await self._access_token()
        return {
            "content-type": "application/json",
            "authorization": f"Bearer {token}",
            "appkey": self._app_key,
            "appsecret": self._app_secret,
            "tr_id": tr_id,
            "custtype": "P",
        }

    async def _get(self, endpoint: str, tr_id: str, params: dict[str, str]) -> dict:
        resp = await self._client.get(endpoint, headers=await self._headers(tr_id), params=params)
        resp.raise_for_status()
        payload = resp.json()
        _check_rt(payload)
        return payload

    # -- PriceProvider -------------------------------------------------------

    async def get_price(self, ticker: str) -> float:
        """Current price for a 6-digit KRX stock code."""
        code = _normalize_stock_code(ticker)
        payload = await self._get(
            "/uapi/domestic-stock/v1/quotations/inquire-price",
            _TR_STOCK_PRICE,
            {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code},
        )
        price = _to_float((payload.get("output") or {}).get("stck_prpr"))
        if price is None:
            raise RuntimeError(f"KIS returned no price for {code!r}")
        return price

    async def get_ohlcv(self, ticker: str, start: date, end: date) -> list[OHLCV]:
        """Daily OHLCV bars for a KRX stock code over ``[start, end]`` (adjusted)."""
        code = _normalize_stock_code(ticker)
        payload = await self._get(
            "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
            _TR_STOCK_DAILY,
            {
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD": code,
                "FID_INPUT_DATE_1": start.strftime("%Y%m%d"),
                "FID_INPUT_DATE_2": end.strftime("%Y%m%d"),
                "FID_PERIOD_DIV_CODE": "D",
                "FID_ORG_ADJ_PRC": "0",
            },
        )
        return _parse_ohlcv(payload.get("output2") or [])

    # -- DerivativesProvider -------------------------------------------------

    async def get_futures_quote(self, code: str) -> FuturesQuote:
        """Quote for a single futures contract (e.g. a KOSPI 200 futures month)."""
        contract = code.strip().upper()
        payload = await self._get(
            "/uapi/domestic-futureoption/v1/quotations/inquire-price",
            _TR_FUT_PRICE,
            {"FID_COND_MRKT_DIV_CODE": "F", "FID_INPUT_ISCD": contract},
        )
        return _parse_futures(payload.get("output") or {}, contract)

    async def get_option_chain(self, underlying: str, expiry: str | None = None) -> OptionChain:
        """Call/put option chain for an underlying (optionally a specific expiry ``YYYYMM``)."""
        target = underlying.strip().upper()
        params = {
            "FID_COND_MRKT_DIV_CODE": "O",
            "FID_COND_SCR_DIV_CODE": "20503",
            "FID_MRKT_CLS_CODE": target,
        }
        if expiry:
            params["FID_INPUT_DATE_1"] = expiry
        payload = await self._get(
            "/uapi/domestic-futureoption/v1/quotations/display-board-option-list",
            _TR_OPT_BOARD,
            params,
        )
        return _parse_option_chain(payload, target, expiry)

    async def aclose(self) -> None:
        await self._client.aclose()
