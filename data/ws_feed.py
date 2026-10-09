"""Live 1m closed-candle feed over websockets, with a watchdog.

The REST path in KlineCache stays the source of truth. This feed only ACCELERATES it: a
closed 1m candle arriving by websocket is appended to the cache when it extends the
contiguous series, so the next scan's REST request covers only what the socket missed.
Anything the socket drops - a gap, a restart, a stale connection - is left for REST to
fill. The feed can therefore be wrong about timing without ever being wrong about data.

Endpoints follow bot_ds: /market/stream on mainnet, and the testnet market base on testnet.
Sockets are chunked by symbol count and each runs its own reconnect loop with backoff. A
watchdog closes any socket that has gone quiet, so the reconnect loop replaces it.
"""
import asyncio
import json
import logging
import threading
import time

import websockets

import config
from data.klines import Candle

log = logging.getLogger(__name__)

MAINNET_MARKET_BASE = "wss://fstream.binance.com/market/stream?streams="
TESTNET_MARKET_BASE = "wss://stream.binancefuture.com/market/stream?streams="
RECONNECT_MAX_SECONDS = 60
STREAM_SUFFIX = "@kline_1m"


def market_base():
    return TESTNET_MARKET_BASE if config.USE_TESTNET else MAINNET_MARKET_BASE


def candle_from_payload(kline):
    """A closed candle from a kline payload, or None while the candle is still forming.

    Field mapping matches the REST kline row KlineCache already consumes, so a candle is
    the same object whichever path delivered it.
    """
    if not kline.get("x"):
        return None
    return Candle(
        open_time=int(kline["t"]), open=float(kline["o"]), high=float(kline["h"]),
        low=float(kline["l"]), close=float(kline["c"]), volume=float(kline["v"]),
        close_time=int(kline["T"]), quote_volume=float(kline["q"]), trades=int(kline["n"]),
        taker_buy_base=float(kline["V"]), taker_buy_quote=float(kline["Q"]),
    )


def stale_sockets(last_seen, now, stale_after):
    """Indexes of sockets with no message for longer than stale_after seconds."""
    return [index for index, seen in last_seen.items() if now - seen > stale_after]


def chunk(symbols, size):
    return [symbols[i:i + size] for i in range(0, len(symbols), size)]


class KlineFeed:
    """Runs the sockets on a private event loop in a background thread.

    on_candle(symbol, candle) is called from that thread for every closed 1m candle, so it
    must be thread-safe. KlineCache.ingest is.
    """

    def __init__(self, on_candle):
        self._on_candle = on_candle
        self._symbols = ()
        self._thread = None
        self._loop = None
        self._stop_event = None
        self._last_seen = {}
        self._sockets = {}
        self._restarts = 0

    @property
    def symbols(self):
        return self._symbols

    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def set_symbols(self, symbols):
        """Start, or restart, the feed for this set. A no-op when the set is unchanged."""
        symbols = tuple(sorted({s.upper() for s in symbols}))
        if symbols == self._symbols and self.running():
            return
        self.stop()
        self._symbols = symbols
        if not symbols:
            return
        self._thread = threading.Thread(target=self._run, name="kline-feed", daemon=True)
        self._thread.start()
        log.info("kline feed started for %d symbols", len(symbols))

    def stop(self):
        if self._loop is not None and self._stop_event is not None:
            try:
                self._loop.call_soon_threadsafe(self._stop_event.set)
            except RuntimeError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=config.WS_SHUTDOWN_TIMEOUT_SECONDS + 5)
            if self._thread.is_alive():
                # _main()'s own shutdown is bounded by WS_SHUTDOWN_TIMEOUT_SECONDS,
                # so reaching this means the thread is stuck somewhere OUTSIDE
                # that bound (or stop() was called before the thread ever got
                # that far) - proceeding anyway (set_symbols always has) but
                # LOUDLY, because silently losing track of a thread holding a
                # live socket is exactly what let this go unnoticed before.
                log.warning("kline feed thread did not stop in time - abandoning "
                           "it; it will keep running until its own shutdown "
                           "eventually completes")
        self._thread = None
        self._loop = None
        self._stop_event = None
        self._last_seen = {}
        self._sockets = {}

    def _run(self):
        # LOCAL variable, deliberately not self._loop, in both the creation and
        # the close below. set_symbols() can start a NEW thread (and overwrite
        # self._loop with ITS loop) while this thread is still finishing up, if
        # the previous stop() didn't actually confirm this thread had exited -
        # reading self._loop in the finally block would then close whichever
        # loop happens to be newest, which is a RUNNING loop in another thread.
        # Each thread must only ever close the loop it itself created.
        loop = asyncio.new_event_loop()
        self._loop = loop
        try:
            loop.run_until_complete(self._main(self._symbols))
        finally:
            loop.close()

    async def _main(self, symbols):
        self._stop_event = asyncio.Event()
        tasks = [asyncio.create_task(self._socket(index, part))
                 for index, part in enumerate(chunk(list(symbols), config.WS_SYMBOLS_PER_SOCKET))]
        tasks.append(asyncio.create_task(self._watchdog()))
        await self._stop_event.wait()
        for task in tasks:
            task.cancel()
        # BOUNDED, so a task that does not actually respond to cancellation -
        # a hung websocket close, a swallowed CancelledError - cannot keep this
        # coroutine (and so run_until_complete, and so this whole thread) alive
        # forever. An unbounded wait here is what turns one stuck socket into a
        # thread leak that quietly holds a real connection open indefinitely.
        try:
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=config.WS_SHUTDOWN_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            still_running = sum(1 for task in tasks if not task.done())
            log.warning("kline feed: %d task(s) still running %ss after cancel - "
                       "giving up on them so the loop can still close",
                       still_running, config.WS_SHUTDOWN_TIMEOUT_SECONDS)

    async def _socket(self, index, symbols):
        url = market_base() + "/".join(s.lower() + STREAM_SUFFIX for s in symbols)
        backoff = 1
        while not self._stop_event.is_set():
            try:
                async with websockets.connect(url, ping_interval=20, max_size=2 ** 22) as ws:
                    self._sockets[index] = ws
                    self._last_seen[index] = time.monotonic()
                    backoff = 1
                    async for raw in ws:
                        self._last_seen[index] = time.monotonic()
                        self._handle(raw)
            except asyncio.CancelledError:
                raise
            except (OSError, websockets.WebSocketException) as exc:
                log.warning("kline socket %d down (%s); retrying in %ss", index, exc, backoff)
            self._sockets.pop(index, None)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, RECONNECT_MAX_SECONDS)

    async def _watchdog(self):
        cooldown_until = 0.0
        while True:
            await asyncio.sleep(config.WS_WATCHDOG_INTERVAL_SECONDS)
            now = time.monotonic()
            if now < cooldown_until:
                continue
            for index in stale_sockets(self._last_seen, now, config.WS_STALE_SECONDS):
                ws = self._sockets.get(index)
                if ws is None:
                    continue
                log.warning("kline socket %d silent for over %ss; closing to reconnect",
                            index, config.WS_STALE_SECONDS)
                self._restarts += 1
                self._last_seen[index] = now
                await ws.close()
                cooldown_until = now + config.WS_RESTART_COOLDOWN_SECONDS

    def _handle(self, raw):
        try:
            frame = json.loads(raw)
        except (TypeError, ValueError):
            return
        data = frame.get("data") if isinstance(frame, dict) else None
        if not data or data.get("e") != "kline":
            return
        candle = candle_from_payload(data["k"])
        if candle is not None:
            self._on_candle(data["s"].upper(), candle)

    def stats(self):
        return {"running": self.running(), "symbols": len(self._symbols),
                "sockets_up": len(self._sockets), "restarts": self._restarts}

