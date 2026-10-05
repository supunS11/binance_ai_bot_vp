import gzip
import json
import os
import shutil
import tempfile
import unittest

from ops import feed_recorder as fr

MIN = 60_000
T0 = 1_700_000_000_000 - (1_700_000_000_000 % (24 * 60 * MIN))


class _Sink:
    def __init__(self):
        self.records = []

    def write(self, kind, symbol, time_ms, record):
        self.records.append((kind, symbol, record))


def _read(path):
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


class ParseTests(unittest.TestCase):
    def test_combined_frame_is_split_into_stream_and_data(self):
        stream, data = fr.parse_message('{"stream":"btcusdt@aggTrade","data":{"p":"1"}}')
        self.assertEqual(stream, "btcusdt@aggTrade")
        self.assertEqual(data, {"p": "1"})

    def test_depth_book_sides_use_the_futures_keys(self):
        recorder = fr.SymbolRecorder("BTCUSDT", _Sink(), depth_every_ms=5000)
        recorder.on_depth({"E": T0, "b": [["100", "1"]], "a": [["101", "2"]]})
        self.assertEqual(recorder._writer.records[0][2]["b"], [["100", "1"]])
        self.assertEqual(recorder._writer.records[0][2]["a"], [["101", "2"]])

    def test_garbage_and_bare_frames_are_ignored(self):
        self.assertEqual(fr.parse_message("not json"), (None, None))
        self.assertEqual(fr.parse_message('{"result":null,"id":1}'), (None, None))


class FootprintTests(unittest.TestCase):
    def test_volume_is_split_by_aggressor_side_per_price_level(self):
        footprint = fr.MinuteFootprint()
        footprint.add_trade(T0 + 1000, "100.10", 2.0, buyer_is_maker=False)
        footprint.add_trade(T0 + 2000, "100.10", 3.0, buyer_is_maker=True)
        records = footprint.flush_before(T0 + MIN)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["m"], T0)
        self.assertEqual(records[0]["lv"], {"100.10": [2.0, 3.0]})

    def test_a_new_minute_closes_the_previous_one(self):
        footprint = fr.MinuteFootprint()
        footprint.add_trade(T0 + 1000, "100", 1.0, buyer_is_maker=False)
        closed = footprint.add_trade(T0 + MIN + 5, "101", 1.0, buyer_is_maker=True)
        self.assertEqual([r["m"] for r in closed], [T0])

    def test_quiet_minutes_are_closed_by_wall_clock_only_once_complete(self):
        footprint = fr.MinuteFootprint()
        footprint.add_trade(T0 + 1000, "100", 1.0, buyer_is_maker=False)
        self.assertEqual(footprint.flush_before(T0 + MIN - 1), [])
        self.assertEqual(len(footprint.flush_before(T0 + MIN)), 1)


class DepthSamplerTests(unittest.TestCase):
    def test_samples_are_taken_at_the_configured_cadence(self):
        sampler = fr.DepthSampler(every_ms=5000)
        bids = [["100", "1"]] * 25
        asks = [["101", "2"]]
        self.assertIsNotNone(sampler.offer(T0, bids, asks))
        self.assertIsNone(sampler.offer(T0 + 4000, bids, asks))
        sample = sampler.offer(T0 + 5000, bids, asks)
        self.assertEqual(len(sample["b"]), fr.DEPTH_LEVELS)
        self.assertEqual(sample["a"], [["101", "2"]])


class WriterTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_records_are_appended_per_symbol_and_utc_day(self):
        writer = fr.DailyWriter(self.root)
        writer.write("trades", "BTCUSDT", T0, {"m": T0})
        writer.write("trades", "BTCUSDT", T0 + MIN, {"m": T0 + MIN})
        path = os.path.join(self.root, "trades", "BTCUSDT", fr.utc_day(T0) + ".jsonl.gz")
        self.assertEqual([r["m"] for r in _read(path)], [T0, T0 + MIN])

    def test_a_gap_discards_the_open_minute_and_writes_a_marker(self):
        writer = fr.DailyWriter(self.root)
        recorder = fr.SymbolRecorder("BTCUSDT", writer, depth_every_ms=5000)
        recorder.on_trade({"T": T0 + 1000, "p": "100", "q": "1", "m": False})
        recorder.gap(T0 + 2000, T0 + 9000)
        recorder.on_trade({"T": T0 + 10_000, "p": "100", "q": "1", "m": False})
        recorder.flush(T0 + MIN)
        path = os.path.join(self.root, "trades", "BTCUSDT", fr.utc_day(T0) + ".jsonl.gz")
        lines = _read(path)
        self.assertEqual(lines[0], {"gap_from": T0 + 2000, "gap_to": T0 + 9000})
        self.assertEqual(lines[1]["lv"], {"100": [1.0, 0.0]})


class _FakeRest:
    def exchange_info(self):
        return {"symbols": [
            {"symbol": "AAAUSDT", "status": "TRADING", "contractType": "PERPETUAL", "quoteAsset": "USDT"},
            {"symbol": "BBBUSDT", "status": "TRADING", "contractType": "PERPETUAL", "quoteAsset": "USDT"},
            {"symbol": "CCCUSDT", "status": "BREAK", "contractType": "PERPETUAL", "quoteAsset": "USDT"},
            {"symbol": "DDDBUSD", "status": "TRADING", "contractType": "PERPETUAL", "quoteAsset": "BUSD"},
        ]}

    def ticker_24hr(self):
        return [{"symbol": "AAAUSDT", "quoteVolume": "10"},
                {"symbol": "BBBUSDT", "quoteVolume": "50"},
                {"symbol": "CCCUSDT", "quoteVolume": "999"},
                {"symbol": "DDDBUSD", "quoteVolume": "999"}]


class SymbolSelectionTests(unittest.TestCase):
    def test_top_tradeable_quote_asset_perpetuals_by_volume(self):
        self.assertEqual(fr.select_symbols(_FakeRest(), "USDT", 10), ["BBBUSDT", "AAAUSDT"])

    def test_count_limits_the_list(self):
        self.assertEqual(fr.select_symbols(_FakeRest(), "USDT", 1), ["BBBUSDT"])


class ChunkingTests(unittest.TestCase):
    def test_two_streams_per_symbol(self):
        names = fr.stream_names(["btcusdt"])
        self.assertEqual(names, ["btcusdt@trade", "btcusdt@depth20@500ms"])

    def test_symbols_are_split_so_no_connection_exceeds_the_stream_limit(self):
        symbols = [f"S{i}USDT" for i in range(250)]
        chunks = fr.chunk_symbols(symbols, max_streams=200)
        self.assertTrue(all(len(c) * 2 <= 200 for c in chunks))
        self.assertEqual(sum(len(c) for c in chunks), 250)


if __name__ == "__main__":
    unittest.main()
