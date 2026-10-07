import datetime as dt
import gzip
import json
import os
import shutil
import tempfile
import unittest

from research import heatmap_export as hx

MIN = 60_000
T0 = 1_700_000_000_000 - (1_700_000_000_000 % (24 * 60 * MIN))


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _write(self, kind, records):
        folder = os.path.join(self.root, kind, "BTCUSDT")
        os.makedirs(folder, exist_ok=True)
        day = dt.datetime.fromtimestamp(T0 / 1000.0, tz=dt.timezone.utc).strftime("%Y-%m-%d")
        path = os.path.join(folder, f"{day}.jsonl.gz")
        with gzip.open(path, "wt", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record) + "\n")

    def test_a_covered_window_builds_depth_and_trades(self):
        self._write("depth", [{"t": T0, "b": [["100.0", "1.5"]], "a": [["100.1", "2.0"]]}])
        self._write("trades", [{"m": T0, "lv": {"100.0": [1.0, 2.0]}}])

        payload = hx.build(self.root, "BTCUSDT", T0, T0 + MIN,
                           zone={"low": 99.5, "high": 100.5})

        self.assertFalse(payload["sample"])
        self.assertEqual(payload["zone"], {"low": 99.5, "high": 100.5})
        self.assertEqual(payload["depth"], [{"t": T0, "b": [[100.0, 1.5]], "a": [[100.1, 2.0]]}])
        self.assertEqual(payload["trades"], [{"m": T0, "lv": {"100.0": [1.0, 2.0]}}])

    def test_an_uncovered_depth_window_refuses_to_build(self):
        self._write("depth", [{"t": T0 + 10 * MIN, "b": [], "a": []}])
        self._write("trades", [{"m": T0, "lv": {}}])
        with self.assertRaises(SystemExit):
            hx.build(self.root, "BTCUSDT", T0, T0 + MIN)

    def test_an_uncovered_trades_window_refuses_to_build(self):
        self._write("depth", [{"t": T0, "b": [], "a": []}])
        self._write("trades", [{"m": T0 + 10 * MIN, "lv": {}}])
        with self.assertRaises(SystemExit):
            hx.build(self.root, "BTCUSDT", T0, T0 + MIN)


class ParseMsTests(unittest.TestCase):
    def test_a_digit_string_is_read_as_epoch_ms(self):
        self.assertEqual(hx._parse_ms("1700000000000"), 1700000000000)

    def test_an_iso_timestamp_is_parsed_as_utc(self):
        self.assertEqual(hx._parse_ms("2023-11-14T22:13:20"), 1700000000000)


if __name__ == "__main__":
    unittest.main()
