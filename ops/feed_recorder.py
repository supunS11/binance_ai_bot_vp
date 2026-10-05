"""Read-only order-flow recorder: trades by price and resting depth, for STACKED and RESTING.

Subscribes to PUBLIC Binance USDS-M streams only (aggregated trades and partial depth), on
the same endpoints bot_ds uses: trades on /market/stream, depth on /public/stream. It never
authenticates and never calls an order endpoint, so it cannot place, change or cancel
anything. It writes, per symbol and per UTC day, gzip JSON-lines:

  <out>/trades/<SYMBOL>/<YYYY-MM-DD>.jsonl.gz
      one line per minute: {"m": minute_ms, "lv": {price: [buy_qty, sell_qty]}}
      - price strings exactly as the exchange sends them, so tick levels never round
      - buy_qty is taker-buy volume (the trade hit the ask), sell_qty taker-sell

  <out>/depth/<SYMBOL>/<YYYY-MM-DD>.jsonl.gz
      one line per DEPTH_SAMPLE_SECONDS: {"t": ms, "b": [[price, qty], ...], "a": [[price, qty], ...]}
      - top 20 levels each side, sampled from the 500ms partial-depth stream

  Gap markers {"gap_from": ms, "gap_to": ms} are written when a socket was down. A reader
  must treat a gap as missing data, never as a quiet market.

Usage: python -m ops.feed_recorder --symbols BTCUSDT,ETHUSDT --out feed_data
"""
import argparse
import asyncio
import datetime as dt
import gzip
import json
import logging
import os
import sys
import time

import websockets

import config
from exchange.rest import RestClient

log = logging.getLogger(__name__)

MARKET_STREAM_BASE = "wss://fstream.binance.com/market/stream?streams="
PUBLIC_STREAM_BASE = "wss://fstream.binance.com/public/stream?streams="
MINUTE_MS = 60_000
DEPTH_LEVELS = 20
RECONNECT_MAX_SECONDS = 60


def utc_day(ms):
    return dt.datetime.fromtimestamp(ms / 1000.0, tz=dt.timezone.utc).strftime("%Y-%m-%d")


def parse_message(raw):
    """(stream_name, data) from a combined-stream frame, or (None, None) when unparseable."""
    try:
        frame = json.loads(raw)
    except (TypeError, ValueError):
        return None, None
    if not isinstance(frame, dict) or "stream" not in frame or "data" not in frame:
        return None, None
    return frame["stream"], frame["data"]


class MinuteFootprint:
    """Accumulates taker volume by price level for each UTC minute of one symbol."""

    def __init__(self):
        self._minute = None
        self._levels = {}

    def add_trade(self, time_ms, price, qty, buyer_is_maker):
        minute = time_ms - time_ms % MINUTE_MS
        completed = []
        if self._minute is not None and minute != self._minute:
            completed.append(self._close())
        self._minute = minute
        cell = self._levels.setdefault(price, [0.0, 0.0])
        cell[1 if buyer_is_maker else 0] += qty
        return completed

    def flush_before(self, now_ms):
        """Close the open minute once wall-clock has moved past it, even with no trades."""
        if self._minute is not None and now_ms >= self._minute + MINUTE_MS:
            return [self._close()]
        return []

    def _close(self):
        record = {"m": self._minute,
                  "lv": {price: [round(buy, 8), round(sell, 8)]
                         for price, (buy, sell) in self._levels.items()}}
        self._minute = None
        self._levels = {}
        return record


class DepthSampler:
    def __init__(self, every_ms):
        self._every_ms = every_ms
        self._last = None

    def offer(self, time_ms, bids, asks):
        if self._last is not None and time_ms - self._last < self._every_ms:
            return None
        self._last = time_ms
        return {"t": time_ms,
                "b": [[p, q] for p, q in bids[:DEPTH_LEVELS]],
                "a": [[p, q] for p, q in asks[:DEPTH_LEVELS]]}


class DailyWriter:
    """Appends gzip JSON-lines, one file per symbol per UTC day. Each append is a complete
    gzip member, so a crash loses at most the line being written and every reader handles
    the concatenated members."""

    def __init__(self, root):
        self._root = root

    def write(self, kind, symbol, time_ms, record):
        folder = os.path.join(self._root, kind, symbol)
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, f"{utc_day(time_ms)}.jsonl.gz")
        line = json.dumps(record, separators=(",", ":")) + "\n"
        with gzip.open(path, "at", encoding="utf-8") as handle:
            handle.write(line)


class SymbolRecorder:
    def __init__(self, symbol, writer, depth_every_ms):
        self.symbol = symbol
        self._writer = writer
        self._footprint = MinuteFootprint()
        self._depth = DepthSampler(depth_every_ms)

    def on_trade(self, data):
        time_ms = int(data["T"])
        for record in self._footprint.add_trade(time_ms, data["p"], float(data["q"]),
                                                bool(data["m"])):
            self._writer.write("trades", self.symbol, record["m"], record)

    def on_depth(self, data):
        time_ms = int(data.get("T") or data.get("E") or time.time() * 1000)
        sample = self._depth.offer(time_ms, data.get("b", []), data.get("a", []))
        if sample is not None:
            self._writer.write("depth", self.symbol, time_ms, sample)

    def flush(self, now_ms):
        for record in self._footprint.flush_before(now_ms):
            self._writer.write("trades", self.symbol, record["m"], record)

    def gap(self, kind, start_ms, end_ms):
        marker = {"gap_from": start_ms, "gap_to": end_ms}
        if kind == "trades":
            self._footprint = MinuteFootprint()
        self._writer.write(kind, self.symbol, start_ms, marker)


def trade_stream_names(symbols):
    return [f"{symbol.lower()}@aggTrade" for symbol in symbols]


def depth_stream_names(symbols):
    return [f"{symbol.lower()}@depth{DEPTH_LEVELS}@500ms" for symbol in symbols]


def chunk_symbols(symbols, max_streams):
    return [symbols[i:i + max_streams] for i in range(0, len(symbols), max_streams)]


async def run_socket(kind, url, recorders, flush_every=False):
    """One socket, reconnecting with backoff; a gap marker covers every outage."""
    down_since = None
    backoff = 1
    while True:
        try:
            async with websockets.connect(url, ping_interval=20, max_size=2 ** 22) as ws:
                if down_since is not None:
                    for recorder in recorders.values():
                        recorder.gap(kind, down_since, int(time.time() * 1000))
                    down_since = None
                backoff = 1
                log.info("%s socket connected: %d symbols", kind, len(recorders))
                while True:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=5)
                    except asyncio.TimeoutError:
                        if flush_every:
                            now = int(time.time() * 1000)
                            for recorder in recorders.values():
                                recorder.flush(now)
                        continue
                    stream, data = parse_message(raw)
                    if stream is None:
                        continue
                    recorder = recorders.get(stream.split("@", 1)[0].upper())
                    if recorder is None:
                        continue
                    if kind == "trades":
                        recorder.on_trade(data)
                    else:
                        recorder.on_depth(data)
        except (OSError, websockets.WebSocketException) as exc:
            if down_since is None:
                down_since = int(time.time() * 1000)
            log.warning("%s socket down (%s); retrying in %ss", kind, exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, RECONNECT_MAX_SECONDS)


def select_symbols(rest, quote_asset, count):
    """Top `count` TRADING USDT-style perpetuals by 24h quote volume. Public endpoints only."""
    listed = {item["symbol"] for item in rest.exchange_info().get("symbols", [])
              if item.get("status") == "TRADING"
              and item.get("contractType") == "PERPETUAL"
              and item.get("quoteAsset") == quote_asset}
    volumes = {row["symbol"]: float(row.get("quoteVolume") or 0.0)
               for row in rest.ticker_24hr() if row.get("symbol") in listed}
    ranked = sorted(volumes, key=lambda symbol: volumes[symbol], reverse=True)
    return ranked[:count]


async def main_async(symbols, out_root):
    writer = DailyWriter(out_root)
    depth_every_ms = int(config.FEED_DEPTH_SAMPLE_SECONDS * 1000)
    tasks = []
    for chunk in chunk_symbols(symbols, config.FEED_SYMBOLS_PER_SOCKET):
        recorders = {symbol: SymbolRecorder(symbol, writer, depth_every_ms) for symbol in chunk}
        tasks.append(run_socket(
            "trades", MARKET_STREAM_BASE + "/".join(trade_stream_names(chunk)),
            recorders, flush_every=True))
        tasks.append(run_socket(
            "depth", PUBLIC_STREAM_BASE + "/".join(depth_stream_names(chunk)), recorders))
    await asyncio.gather(*tasks)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", default=",".join(config.FEED_SYMBOLS))
    parser.add_argument("--out", default=config.FEED_OUT_DIR)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    if not symbols:
        rest = RestClient(api_key="", api_secret="", testnet=False)
        symbols = select_symbols(rest, config.FEED_QUOTE_ASSET, config.FEED_SYMBOL_COUNT)
        if not symbols:
            raise SystemExit("no symbols selected: exchangeInfo or ticker/24hr returned none")
        log.info("selected %d symbols by 24h quote volume", len(symbols))
    asyncio.run(main_async(symbols, args.out))


if __name__ == "__main__":
    main(sys.argv[1:])
