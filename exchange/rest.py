"""Binance USD-M futures REST client.

Written directly against the HTTP API rather than through a wrapper library,
because the parts that matter here are exactly the parts wrappers hide: request
weight accounting, the difference between a retryable and a terminal error code,
and idempotent order submission. Each of those is a correctness requirement, not
a convenience.

RATE LIMITS, as the venue actually enforces them. Two independent budgets:

  REQUEST_WEIGHT   2400 per minute, per IP. Every endpoint costs a documented
                   weight; klines cost 1-10 depending on `limit`, exchangeInfo
                   costs 1 but returns megabytes, 24h tickers cost 40 for the
                   full list.
  ORDERS           300 per 10 seconds AND 1200 per minute, per account. Separate
                   from weight - an order costs both.

The venue reports live usage in response headers (X-MBX-USED-WEIGHT-1M,
X-MBX-ORDER-COUNT-1M, X-MBX-ORDER-COUNT-10S). Those are authoritative and this
client trusts them over its own count, because its own count cannot see requests
made by anything else sharing the IP or key. Exceeding a limit earns HTTP 429;
ignoring a 429 earns HTTP 418, which is a timed IP ban.

CLOCK SKEW. Signed requests carry a millisecond timestamp and are refused with
-1021 if it falls outside recvWindow of the server's clock. A Windows host
drifts enough to hit this within hours, so the offset is measured once at
startup and re-measured whenever -1021 appears, rather than assuming the local
clock is right.
"""
import hashlib
import hmac
import json
import logging
import threading
import time
import urllib.parse

import requests

import config

log = logging.getLogger(__name__)

MAINNET = "https://fapi.binance.com"
TESTNET = "https://testnet.binancefuture.com"

# Terminal: the request is wrong and will be wrong again. Retrying wastes budget
# and, for order placement, risks a duplicate if the first attempt actually
# landed. Each of these is a caller bug or a genuine market-state refusal.
TERMINAL_CODES = {
    -1013,  # filter failure (generic)
    -1102,  # mandatory parameter missing
    -1111,  # precision over maximum -> a rounding bug upstream
    -1116,  # invalid order type
    -1117,  # invalid side
    -1121,  # invalid symbol
    -2010,  # new order rejected
    -2011,  # cancel rejected (already gone)
    -2013,  # order does not exist
    -2019,  # margin is insufficient
    -2021,  # order would immediately trigger
    -2022,  # reduceOnly rejected
    -4003,  # quantity less than zero
    -4028,  # invalid leverage
    -4046,  # no need to change margin type
    -4131,  # counterparty best price does not meet PERCENT_PRICE
    -4164,  # order notional below minimum
    -5022,  # post-only (GTX) order would immediately match
}

# Transient: the same request may succeed shortly. Backed off and retried.
RETRYABLE_CODES = {
    -1000,  # unknown error
    -1001,  # internal error / disconnect
    -1003,  # too many requests
    -1007,  # timeout waiting for backend
    -1021,  # timestamp outside recvWindow (offset is re-synced first)
}


class ApiError(Exception):
    """A structured error response from the venue."""

    def __init__(self, code, message, status=None, endpoint=None):
        super().__init__(f"[{code}] {message} ({endpoint})")
        self.code = int(code) if code is not None else None
        self.message = message
        self.status = status
        self.endpoint = endpoint

    @property
    def terminal(self):
        return self.code in TERMINAL_CODES

    @property
    def retryable(self):
        return self.code in RETRYABLE_CODES


class RateLimiter:
    """Weight and order-count budget, driven by the venue's own headers.

    Deliberately conservative: it pauses at a fraction of each published limit
    rather than at the limit, because the headers report usage as of the last
    response and a burst issued in between is invisible until it is too late.
    """

    WEIGHT_PER_MINUTE = 2400
    ORDERS_PER_MINUTE = 1200
    ORDERS_PER_10S = 300
    SAFETY = 0.80

    def __init__(self):
        self._lock = threading.RLock()
        self._weight_1m = 0
        self._orders_1m = 0
        self._orders_10s = 0
        self._last_request_at = 0.0

    def observe(self, headers):
        """Adopt the venue's live counters. These override any local estimate."""
        with self._lock:
            for key, attr in (
                ("x-mbx-used-weight-1m", "_weight_1m"),
                ("x-mbx-order-count-1m", "_orders_1m"),
                ("x-mbx-order-count-10s", "_orders_10s"),
            ):
                raw = headers.get(key) or headers.get(key.upper())
                if raw is not None:
                    try:
                        setattr(self, attr, int(raw))
                    except (TypeError, ValueError):
                        pass

    def wait(self, is_order=False):
        """Throttle to a fixed minimum spacing, and stall near a budget ceiling."""
        with self._lock:
            gap = config.REST_THROTTLE_SECONDS
            elapsed = time.time() - self._last_request_at
            if elapsed < gap:
                time.sleep(gap - elapsed)

            if self._weight_1m > self.WEIGHT_PER_MINUTE * self.SAFETY:
                log.warning("weight budget %s/%s - pausing 5s",
                            self._weight_1m, self.WEIGHT_PER_MINUTE)
                time.sleep(5.0)
                self._weight_1m = 0
            if is_order and (
                self._orders_10s > self.ORDERS_PER_10S * self.SAFETY
                or self._orders_1m > self.ORDERS_PER_MINUTE * self.SAFETY
            ):
                log.warning("order budget 10s=%s 1m=%s - pausing 2s",
                            self._orders_10s, self._orders_1m)
                time.sleep(2.0)
                self._orders_10s = 0

            self._last_request_at = time.time()

    def snapshot(self):
        with self._lock:
            return {
                "weight_1m": self._weight_1m,
                "orders_1m": self._orders_1m,
                "orders_10s": self._orders_10s,
            }


class RestClient:
    def __init__(self, api_key=None, api_secret=None, testnet=None):
        self._key = api_key if api_key is not None else config.BINANCE_API_KEY
        self._secret = (api_secret if api_secret is not None
                        else config.BINANCE_API_SECRET)
        use_testnet = config.USE_TESTNET if testnet is None else testnet
        self.base = TESTNET if use_testnet else MAINNET
        self.limiter = RateLimiter()
        self._time_offset_ms = 0
        self._session = requests.Session()
        if self._key:
            self._session.headers["X-MBX-APIKEY"] = self._key
        self._session.headers["Accept"] = "application/json"

    # ------------------------------------------------------------ plumbing

    def _timestamp(self):
        return int(time.time() * 1000) + self._time_offset_ms

    def sync_time(self):
        """Measure local-vs-server clock offset and keep it for signing.

        Signed requests are rejected with -1021 when the timestamp falls outside
        recvWindow. Rather than widening recvWindow - which weakens the replay
        protection the timestamp exists for - the offset is measured and applied.
        """
        payload = self._request("GET", "/fapi/v1/time", weight=1, signed=False)
        server_ms = int(payload.get("serverTime", 0))
        if server_ms:
            self._time_offset_ms = server_ms - int(time.time() * 1000)
            log.info("clock offset %+d ms", self._time_offset_ms)
        return self._time_offset_ms

    def _sign(self, params):
        query = urllib.parse.urlencode(params, doseq=True)
        signature = hmac.new(
            self._secret.encode("utf-8"),
            query.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return f"{query}&signature={signature}"

    def _request(self, method, path, params=None, weight=1, signed=False,
                 is_order=False):
        params = dict(params or {})
        attempt = 0

        while True:
            attempt += 1
            self.limiter.wait(is_order=is_order)

            if signed:
                if not self._key or not self._secret:
                    raise ApiError(None, "signed request without credentials",
                                   endpoint=path)
                params["timestamp"] = self._timestamp()
                params.setdefault("recvWindow", 5000)
                body = self._sign(params)
                url = f"{self.base}{path}?{body}"
                send_params = None
            else:
                url = f"{self.base}{path}"
                send_params = params

            try:
                response = self._session.request(
                    method, url, params=send_params,
                    timeout=config.REST_TIMEOUT_SECONDS,
                )
            except requests.RequestException as exc:
                if attempt > config.REST_MAX_RETRIES:
                    raise ApiError(None, f"transport: {exc}", endpoint=path)
                self._backoff(attempt, f"transport error: {exc}")
                continue

            self.limiter.observe(response.headers)

            # 429 asks us to slow down; 418 means we did not and are now banned
            # for a period the venue names in Retry-After.
            if response.status_code in (429, 418):
                retry_after = float(response.headers.get("Retry-After", 5))
                log.warning("HTTP %s from %s - sleeping %.0fs",
                            response.status_code, path, retry_after)
                time.sleep(retry_after + 1.0)
                if attempt > config.REST_MAX_RETRIES:
                    raise ApiError(-1003, "rate limited", response.status_code, path)
                continue

            try:
                payload = response.json()
            except (ValueError, json.JSONDecodeError):
                if attempt > config.REST_MAX_RETRIES:
                    raise ApiError(None, f"non-JSON body: {response.text[:200]}",
                                   response.status_code, path)
                self._backoff(attempt, "non-JSON body")
                continue

            if response.status_code >= 400 or (
                isinstance(payload, dict) and payload.get("code") is not None
                and int(payload.get("code", 0)) < 0
            ):
                error = ApiError(payload.get("code"), payload.get("msg", ""),
                                 response.status_code, path)
                if error.code == -1021:
                    # Re-sync and retry: the request was well-formed, the clock
                    # was not. Do not count this as a normal retry.
                    self.sync_time()
                    if attempt <= config.REST_MAX_RETRIES:
                        continue
                if error.terminal or attempt > config.REST_MAX_RETRIES:
                    raise error
                if not error.retryable:
                    raise error
                self._backoff(attempt, f"{error.code} {error.message}")
                continue

            return payload

    @staticmethod
    def _backoff(attempt, reason):
        delay = min(30.0, 0.5 * (2 ** (attempt - 1)))
        log.warning("retry %s in %.1fs: %s", attempt, delay, reason)
        time.sleep(delay)

    # -------------------------------------------------------------- public

    def exchange_info(self):
        return self._request("GET", "/fapi/v1/exchangeInfo", weight=1)

    def ticker_24hr(self, symbol=None):
        """Full-list weight is 40; a single symbol costs 1."""
        params = {"symbol": symbol.upper()} if symbol else {}
        return self._request("GET", "/fapi/v1/ticker/24hr", params,
                             weight=1 if symbol else 40)

    def klines(self, symbol, interval, start_ms=None, end_ms=None, limit=1500):
        """Raw klines. Weight scales with `limit`: 1/2/5/10 by bucket.

        1500 is the maximum per request, which is why a 1m profile of one UTC
        day (1440 candles) fits in exactly one call.
        """
        params = {"symbol": symbol.upper(), "interval": interval,
                  "limit": min(int(limit), 1500)}
        if start_ms is not None:
            params["startTime"] = int(start_ms)
        if end_ms is not None:
            params["endTime"] = int(end_ms)
        weight = 10 if limit > 1000 else 5 if limit > 500 else 2 if limit > 100 else 1
        return self._request("GET", "/fapi/v1/klines", params, weight=weight)

    def mark_price(self, symbol=None):
        params = {"symbol": symbol.upper()} if symbol else {}
        return self._request("GET", "/fapi/v1/premiumIndex", params,
                             weight=1 if symbol else 10)

    def book_ticker(self, symbol):
        """Best bid/ask - the spread gate and passive entry pricing need this."""
        return self._request("GET", "/fapi/v1/ticker/bookTicker",
                             {"symbol": symbol.upper()}, weight=2)

    def funding_rate_history(self, symbol, start_ms=None, end_ms=None, limit=1000):
        """Historical funding rate. Unlike open interest, NOT capped to ~30 days -
        Binance's own docs give no retention limit here, and it verifies empirically
        back to a symbol's perpetual listing date. `limit` maxes at 1000, so a full
        year (~1095 records at one per 8h) needs at most two calls per symbol.

        Rides its own rate-limit bucket (500 req/5min/IP, shared with
        GET /fapi/v1/fundingInfo) separate from the standard weight system this
        client otherwise throttles against - the declared weight below is a local
        estimate only, harmless given how rarely this is called.
        """
        params = {"symbol": symbol.upper(), "limit": min(int(limit), 1000)}
        if start_ms is not None:
            params["startTime"] = int(start_ms)
        if end_ms is not None:
            params["endTime"] = int(end_ms)
        return self._request("GET", "/fapi/v1/fundingRate", params, weight=1)

    # ------------------------------------------------------------- account

    def account(self):
        return self._request("GET", "/fapi/v2/account", weight=5, signed=True)

    def balances(self):
        return self._request("GET", "/fapi/v2/balance", weight=5, signed=True)

    def position_risk(self, symbol=None):
        params = {"symbol": symbol.upper()} if symbol else {}
        return self._request("GET", "/fapi/v2/positionRisk", params,
                             weight=5, signed=True)

    def set_leverage(self, symbol, leverage):
        return self._request("POST", "/fapi/v1/leverage",
                             {"symbol": symbol.upper(), "leverage": int(leverage)},
                             weight=1, signed=True)

    def set_margin_type(self, symbol, margin_type):
        """-4046 means it is already set, which is success for our purposes."""
        try:
            return self._request("POST", "/fapi/v1/marginType",
                                 {"symbol": symbol.upper(),
                                  "marginType": margin_type.upper()},
                                 weight=1, signed=True)
        except ApiError as exc:
            if exc.code == -4046:
                return {"code": 200, "msg": "already set"}
            raise

    def position_mode(self):
        return self._request("GET", "/fapi/v1/positionSide/dual",
                             weight=30, signed=True)

    # --------------------------------------------------------------- orders

    def new_order(self, **params):
        """Place one order. Every caller must pass newClientOrderId.

        Idempotency is the reason. A transport failure leaves the outcome
        genuinely unknown - the order may have been accepted - and the only safe
        recovery is to query by a client id we chose in advance. Placing a second
        order blind is how a position ends up doubled.
        """
        if "newClientOrderId" not in params:
            raise ValueError("newClientOrderId is required for idempotency")
        payload = {key: value for key, value in params.items() if value is not None}
        payload["symbol"] = payload["symbol"].upper()
        return self._request("POST", "/fapi/v1/order", payload,
                             weight=1, signed=True, is_order=True)

    def query_order(self, symbol, order_id=None, client_order_id=None):
        params = {"symbol": symbol.upper()}
        if order_id is not None:
            params["orderId"] = int(order_id)
        if client_order_id is not None:
            params["origClientOrderId"] = client_order_id
        return self._request("GET", "/fapi/v1/order", params, weight=1, signed=True)

    def cancel_order(self, symbol, order_id=None, client_order_id=None):
        params = {"symbol": symbol.upper()}
        if order_id is not None:
            params["orderId"] = int(order_id)
        if client_order_id is not None:
            params["origClientOrderId"] = client_order_id
        return self._request("DELETE", "/fapi/v1/order", params,
                             weight=1, signed=True, is_order=True)

    def cancel_all_orders(self, symbol):
        return self._request("DELETE", "/fapi/v1/allOpenOrders",
                             {"symbol": symbol.upper()},
                             weight=1, signed=True, is_order=True)

    def open_orders(self, symbol=None):
        params = {"symbol": symbol.upper()} if symbol else {}
        return self._request("GET", "/fapi/v1/openOrders", params,
                             weight=1 if symbol else 40, signed=True)

    def user_trades(self, symbol, start_ms=None, limit=500):
        """Realised fills, with commission and realised PnL per trade.

        Needed because an order's average price omits the fee, and R accounting
        that ignores fees overstates every result.
        """
        params = {"symbol": symbol.upper(), "limit": min(int(limit), 1000)}
        if start_ms is not None:
            params["startTime"] = int(start_ms)
        return self._request("GET", "/fapi/v1/userTrades", params,
                             weight=5, signed=True)

    def income(self, symbol=None, income_type=None, start_ms=None, limit=1000):
        """Funding payments, commissions, realised PnL - the ledger truth."""
        params = {"limit": min(int(limit), 1000)}
        if symbol:
            params["symbol"] = symbol.upper()
        if income_type:
            params["incomeType"] = income_type
        if start_ms is not None:
            params["startTime"] = int(start_ms)
        return self._request("GET", "/fapi/v1/income", params, weight=30, signed=True)

    # ---------------------------------------------------------- user stream

    def create_listen_key(self):
        return self._request("POST", "/fapi/v1/listenKey", weight=1, signed=True)

    def keepalive_listen_key(self):
        return self._request("PUT", "/fapi/v1/listenKey", weight=1, signed=True)
