"""The shard merge: the one step between three hours of replay and the answer.

It was entirely untested. That is the worst place in the project for a gap, because
every failure here is quiet and arrives only after the expensive part is done:

  * a missing shard yields a result measured on two thirds of the universe, and looks
    exactly like a correct result
  * a blank cell read as 0.0 scores an unfilled entry as break-even, which flatters a
    setup in proportion to how often its entries go unfilled - and unfilled candidates
    are disproportionately the winners
  * a numeric column left as a string either raises much later, in an analysis, or
    sorts and compares as text

The type coercion is asserted against TRADE_FIELDS itself rather than against a copy,
so a field added to the replay output without a declared type fails here instead of
arriving as text in a Phase 2 comparison months later.
"""
import csv
import os
import shutil
import tempfile
import unittest

from research import merge, replay


def _write_shard(directory, index, rows, fields=None):
    path = os.path.join(directory, f"trades_shard{index}.csv")
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields or replay.TRADE_FIELDS,
                                extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return path


def _row(**overrides):
    row = {key: "" for key in replay.TRADE_FIELDS}
    row.update({
        "symbol": "TESTUSDT", "qv_rank": 3, "session_id": "2026-09-01",
        "setup": "S1-POC", "direction": "BUY", "decision_ts": 1788220800000,
        "decision_index": 12, "entry_price": 100.0, "stop_price": 99.0,
        "target_price": 102.0, "quantity": 2.5, "risk_distance": 1.0,
        "r_multiple": 2.0, "cost_r": 0.02, "outcome": "TARGET",
        "exit_price": 102.0, "exit_ts": 1788224400000, "bars_to_exit": 7,
        "bars_to_fill": 1, "gross_r": 2.0, "net_r": 1.98, "mfe_r": 2.1,
        "mae_r": -0.4, "ambiguous_bars": 0, "atr": 3.0, "value_width": 1.2,
        "prior_shape": "D", "auction_state": "balance",
        "open_relationship": "INSIDE_VALUE", "target_kind": "fixed_r",
        "confirmations": "poc_touch", "confirmation_count": 1,
        "poc_prominence": 2.4, "va_range_ratio": 0.5, "acceptance_ratio": 0.9,
        "is_control": 0, "control_of": "",
    })
    row.update(overrides)
    return row


class FieldTypeCoverageTests(unittest.TestCase):
    """The declaration must cover the schema, or coercion silently skips a column."""

    def test_every_declared_type_is_a_real_field(self):
        unknown = set(replay.TRADE_FIELD_TYPES) - set(replay.TRADE_FIELDS)
        self.assertEqual(unknown, set(),
                         f"TRADE_FIELD_TYPES names fields that do not exist: {unknown}")

    def test_every_field_is_either_typed_or_deliberately_a_string(self):
        """Adding a numeric field without declaring it must fail HERE.

        The allowlist below is the set of columns that are genuinely text. It is spelled
        out so that a new field is forced into one list or the other by a failing test,
        rather than defaulting to string and arriving as text in an analysis.
        """
        strings = {
            "symbol", "session_id", "setup", "direction", "outcome",
            "prior_shape", "auction_state", "open_relationship", "target_kind",
            "target_candidates", "ofr_tier", "ofr_signals", "ofr_zone_kind",
            "ofr_signal_states", "entry_mode", "confirmations", "control_of", "value_migration",
            "stop_mode", "stop_reference", "tp2_kind", "day_bias", "dev_shape_label",
        }
        unclassified = set(replay.TRADE_FIELDS) - set(replay.TRADE_FIELD_TYPES) - strings
        self.assertEqual(
            unclassified, set(),
            f"these replay fields have no declared type and are not known strings: "
            f"{sorted(unclassified)} - add them to TRADE_FIELD_TYPES or to this test's "
            f"string allowlist")

    def test_the_fields_that_had_drifted_are_now_typed(self):
        # The specific regression: the hand-written lists in merge.py omitted these
        # three, and nothing noticed because no code did arithmetic on them.
        for field in ("exit_price", "exit_ts", "decision_index"):
            self.assertIn(field, replay.TRADE_FIELD_TYPES)


class CoerceRowTests(unittest.TestCase):

    def test_numbers_are_restored_from_strings(self):
        row = replay.coerce_row({"net_r": "1.98", "bars_to_exit": "7",
                                 "entry_price": "100.5"})
        self.assertEqual(row["net_r"], 1.98)
        self.assertEqual(row["bars_to_exit"], 7)
        self.assertIsInstance(row["bars_to_exit"], int)
        self.assertEqual(row["entry_price"], 100.5)

    def test_blank_stays_none_and_never_becomes_zero(self):
        """THE ASSERTION THAT MATTERS MOST.

        0.0 is a real R value meaning break-even. None means the trade never resolved.
        Conflating them biases every setup mean toward zero by exactly the rate at which
        its entries go unfilled.
        """
        row = replay.coerce_row({"net_r": "", "gross_r": None, "mfe_r": "None",
                                 "bars_to_exit": ""})
        self.assertIsNone(row["net_r"])
        self.assertIsNone(row["gross_r"])
        self.assertIsNone(row["mfe_r"])
        self.assertIsNone(row["bars_to_exit"])
        for key in ("net_r", "gross_r", "mfe_r", "bars_to_exit"):
            self.assertNotEqual(row[key], 0.0)

    def test_an_integer_column_written_as_a_float_string_survives(self):
        # int("3.0") raises; the coercion goes through float() first.
        row = replay.coerce_row({"bars_to_exit": "3.0", "qv_rank": "12.0"})
        self.assertEqual(row["bars_to_exit"], 3)
        self.assertEqual(row["qv_rank"], 12)

    def test_negative_and_exponential_values_survive(self):
        row = replay.coerce_row({"mae_r": "-0.4", "atr": "1.5e-05"})
        self.assertAlmostEqual(row["mae_r"], -0.4)
        self.assertAlmostEqual(row["atr"], 1.5e-05)

    def test_coercion_is_idempotent(self):
        row = replay.coerce_row({"net_r": "1.5", "bars_to_exit": "4"})
        again = replay.coerce_row(dict(row))
        self.assertEqual(again["net_r"], 1.5)
        self.assertEqual(again["bars_to_exit"], 4)

    def test_string_columns_are_left_alone(self):
        row = replay.coerce_row({"setup": "S1-POC", "outcome": "TARGET",
                                 "confirmations": "poc_touch,delta"})
        self.assertEqual(row["setup"], "S1-POC")
        self.assertEqual(row["confirmations"], "poc_touch,delta")


class MergeTradesTests(unittest.TestCase):

    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="vp_merge_")

    def tearDown(self):
        shutil.rmtree(self.directory, ignore_errors=True)

    def test_merges_all_shards(self):
        _write_shard(self.directory, 0, [_row(symbol="AAAUSDT")])
        _write_shard(self.directory, 1, [_row(symbol="BBBUSDT"), _row(symbol="CCCUSDT")])
        _write_shard(self.directory, 2, [_row(symbol="DDDUSDT")])
        rows, paths = merge.merge_trades(self.directory, expected_shards=3)
        self.assertEqual(len(paths), 3)
        self.assertEqual(len(rows), 4)
        self.assertEqual({row["symbol"] for row in rows},
                         {"AAAUSDT", "BBBUSDT", "CCCUSDT", "DDDUSDT"})

    def test_a_missing_shard_is_refused_not_quietly_merged(self):
        """The assert that protects the sample size.

        Two of three shards is a result measured on two thirds of the universe, and it
        reports as cleanly as a complete one.
        """
        _write_shard(self.directory, 0, [_row()])
        _write_shard(self.directory, 2, [_row()])
        with self.assertRaises(SystemExit) as caught:
            merge.merge_trades(self.directory, expected_shards=3)
        self.assertIn("expected 3 shard files", str(caught.exception))

    def test_no_assertion_when_expected_shards_is_omitted(self):
        _write_shard(self.directory, 0, [_row()])
        rows, paths = merge.merge_trades(self.directory)
        self.assertEqual(len(paths), 1)
        self.assertEqual(len(rows), 1)

    def test_merged_rows_come_back_typed(self):
        _write_shard(self.directory, 0, [_row(net_r=1.98, bars_to_exit=7)])
        rows, _ = merge.merge_trades(self.directory, expected_shards=1)
        self.assertIsInstance(rows[0]["net_r"], float)
        self.assertIsInstance(rows[0]["bars_to_exit"], int)
        self.assertIsInstance(rows[0]["exit_price"], float)
        self.assertIsInstance(rows[0]["exit_ts"], int)
        self.assertIsInstance(rows[0]["decision_index"], int)

    def test_an_unfilled_trade_round_trips_as_none(self):
        _write_shard(self.directory, 0, [
            _row(outcome="EXPIRED", exit_price=None, exit_ts=None,
                 net_r=None, gross_r=None, bars_to_exit=None),
        ])
        rows, _ = merge.merge_trades(self.directory, expected_shards=1)
        self.assertEqual(rows[0]["outcome"], "EXPIRED")
        for key in ("exit_price", "exit_ts", "net_r", "gross_r", "bars_to_exit"):
            self.assertIsNone(rows[0][key], f"{key} should stay None")

    def test_an_empty_shard_file_contributes_nothing_and_does_not_raise(self):
        _write_shard(self.directory, 0, [])
        _write_shard(self.directory, 1, [_row()])
        rows, paths = merge.merge_trades(self.directory, expected_shards=2)
        self.assertEqual(len(paths), 2)
        self.assertEqual(len(rows), 1)

    def test_the_merged_output_is_not_itself_picked_up_on_a_rerun(self):
        """trades.csv must not match trades_shard*.csv, or a second merge doubles rows."""
        _write_shard(self.directory, 0, [_row()])
        rows, _ = merge.merge_trades(self.directory, expected_shards=1)
        replay.write_csv(os.path.join(self.directory, "trades.csv"), rows)
        again, paths = merge.merge_trades(self.directory, expected_shards=1)
        self.assertEqual(len(paths), 1)
        self.assertEqual(len(again), 1)

    def test_round_trip_through_write_csv_preserves_values(self):
        original = _row(net_r=-1.02, mae_r=-1.0, quantity=0.001)
        _write_shard(self.directory, 0, [original])
        rows, _ = merge.merge_trades(self.directory, expected_shards=1)
        path = replay.write_csv(os.path.join(self.directory, "trades.csv"), rows)
        with open(path, "r", encoding="utf-8", newline="") as handle:
            reread = [replay.coerce_row(row) for row in csv.DictReader(handle)]
        self.assertAlmostEqual(reread[0]["net_r"], -1.02)
        self.assertAlmostEqual(reread[0]["quantity"], 0.001)
        self.assertEqual(reread[0]["symbol"], "TESTUSDT")


class MergeRejectsTests(unittest.TestCase):

    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="vp_rejects_")

    def tearDown(self):
        shutil.rmtree(self.directory, ignore_errors=True)

    def _write_rejects(self, index, tally):
        path = os.path.join(self.directory, f"rejects_shard{index}.csv")
        with open(path, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=["reason", "count"])
            writer.writeheader()
            for reason, count in tally.items():
                writer.writerow({"reason": reason, "count": count})

    def test_counts_are_summed_across_shards(self):
        self._write_rejects(0, {"NO_CONTINUATION": 10, "PROFILE_THIN": 3})
        self._write_rejects(1, {"NO_CONTINUATION": 5, "STOP_TOO_WIDE": 2})
        tally = merge.merge_rejects(self.directory)
        self.assertEqual(tally["NO_CONTINUATION"], 15)
        self.assertEqual(tally["PROFILE_THIN"], 3)
        self.assertEqual(tally["STOP_TOO_WIDE"], 2)

    def test_no_reject_files_gives_an_empty_tally(self):
        self.assertEqual(merge.merge_rejects(self.directory), {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
