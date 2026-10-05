"""The statistics the phase gates are read through.

A wrong simulator returns a worse number; a wrong STATISTIC returns a confident one.
The acceptance sweep already demonstrated the cost: it judged its own discriminator
with a difference of means, on a quantity whose mean was 64.2 with sd 4212 against a
p95 of 3.61, and printed "NO separation; the discriminator does not work on real
data" over a population whose continuation rate in fact rose monotonically across
every band of that same quantity. Nothing crashed and no test failed, because there
was no test.

So the instruments are pinned here by their INVARIANTS - the things that must stay
true no matter what data arrives - rather than by recomputing their formulas:

  * a quantity that carries no information must read 0.5, including the degenerate
    case of a constant, which is exactly what the bug that has recurred four times in
    this project produces
  * a rank statistic must be unchanged by any monotone rescaling of its input, because
    a threshold that moves when the units change was never measuring the market
  * a proxy must collapse inside the strata of what it proxies for, and the thing it
    proxies for must survive inside the proxy's strata - the asymmetry IS the test
"""
import math
import unittest

from research import metrics


def _linear(count, start=0.0, step=1.0):
    return [start + step * index for index in range(count)]


class AucBasicsTests(unittest.TestCase):
    """The three anchors: perfect, none, inverted."""

    def test_perfect_separation_is_one(self):
        result = metrics.auc(_linear(40, 100.0), _linear(40, 0.0))
        self.assertAlmostEqual(result["auc"], 1.0, places=9)

    def test_perfect_inversion_is_zero(self):
        result = metrics.auc(_linear(40, 0.0), _linear(40, 100.0))
        self.assertAlmostEqual(result["auc"], 0.0, places=9)

    def test_identical_distributions_read_one_half(self):
        values = _linear(60)
        result = metrics.auc(list(values), list(values))
        self.assertAlmostEqual(result["auc"], 0.5, places=9)

    def test_interval_brackets_the_point(self):
        result = metrics.auc(_linear(80, 5.0), _linear(80, 0.0))
        self.assertLessEqual(result["low"], result["auc"])
        self.assertGreaterEqual(result["high"], result["auc"])

    def test_thin_samples_return_none_rather_than_a_wide_interval(self):
        # 19 per side. A rank statistic on a handful of points invites reading
        # structure into noise, so it refuses rather than reporting.
        self.assertIsNone(metrics.auc(_linear(19, 10.0), _linear(19, 0.0)))
        self.assertIsNotNone(metrics.auc(_linear(20, 10.0), _linear(20, 0.0)))

    def test_none_values_are_dropped_not_counted(self):
        result = metrics.auc([None] * 5 + _linear(25, 10.0), _linear(25, 0.0))
        self.assertEqual(result["n_pos"], 25)


class AucTieCorrectionTests(unittest.TestCase):
    """THE BUG-FAMILY GUARD.

    Four separate times in this project a measure has been compared against a bound
    derived from the measure itself, and the signature is always the same: the quantity
    collapses onto a single constant value. The acceptance ratio pinned at exactly
    1.000 for 22% of excursions was the clearest case.

    Without midrank tie handling, a constant scores 1.0 - a perfectly broken
    measurement reads as a perfect discriminator. That failure must be impossible.
    """

    def test_a_constant_carries_no_information(self):
        result = metrics.auc([1.0] * 40, [1.0] * 40)
        self.assertAlmostEqual(result["auc"], 0.5, places=9)

    def test_a_constant_stays_one_half_at_any_group_imbalance(self):
        # The imbalance matters: CONTINUED is ~2.5x rarer than REVERTED in the real
        # corpus, and a rank statistic that mishandled ties would skew with the ratio
        # of the two sample sizes rather than staying pinned.
        for positive_count, negative_count in ((25, 200), (200, 25), (60, 60)):
            result = metrics.auc([7.5] * positive_count, [7.5] * negative_count)
            self.assertAlmostEqual(result["auc"], 0.5, places=9)

    def test_mostly_tied_with_a_real_edge_is_between(self):
        positive = [1.0] * 30 + [2.0] * 10
        negative = [1.0] * 40
        result = metrics.auc(positive, negative)
        self.assertGreater(result["auc"], 0.5)
        self.assertLess(result["auc"], 0.75)


class AucScaleInvarianceTests(unittest.TestCase):
    """A rank statistic must not care what unit its input arrived in.

    This is the mechanical form of the lesson the acceptance analysis produced the hard
    way: the SAME excursions read as a reversion edge in value widths and a
    continuation edge in ATR, because the LABEL's threshold carried the unit. A
    statistic whose own value moves under rescaling would hide that kind of problem
    instead of exposing it.
    """

    def test_multiplying_both_sides_changes_nothing(self):
        positive, negative = _linear(50, 3.0), _linear(50, 0.0)
        base = metrics.auc(positive, negative)["auc"]
        for factor in (0.001, 2.0, 1000.0):
            scaled = metrics.auc([value * factor for value in positive],
                                 [value * factor for value in negative])
            self.assertAlmostEqual(scaled["auc"], base, places=9)

    def test_any_monotone_transform_changes_nothing(self):
        positive, negative = _linear(50, 3.0, 0.5), _linear(50, 0.1, 0.5)
        base = metrics.auc(positive, negative)["auc"]
        for transform in (math.log, lambda value: value ** 3,
                          lambda value: math.atan(value)):
            moved = metrics.auc([transform(value) for value in positive],
                                [transform(value) for value in negative])
            self.assertAlmostEqual(moved["auc"], base, places=9)

    def test_an_extreme_outlier_cannot_move_it_far(self):
        """The specific failure that produced the wrong verdict.

        A mean-difference test on this data reports a large gap; the rank statistic
        moves by at most the one observation's worth of rank. The assertion is on the
        DIFFERENCE between the two, which is what makes it a regression test for the
        misreading rather than a restatement of the formula.
        """
        positive, negative = _linear(60), _linear(60)
        clean = metrics.auc(positive, negative)["auc"]
        polluted = metrics.auc(positive + [10.0 ** 9], negative)["auc"]
        self.assertLess(abs(polluted - clean), 0.02)

        mean_clean = metrics.mean_interval(positive)["mean"]
        mean_polluted = metrics.mean_interval(positive + [10.0 ** 9])["mean"]
        self.assertGreater(mean_polluted / max(mean_clean, 1e-9), 1000.0)


class AucVerdictTests(unittest.TestCase):

    def test_three_verdicts_and_the_thin_case(self):
        self.assertEqual(metrics.auc_verdict(None), "too thin")
        self.assertEqual(
            metrics.auc_verdict({"auc": 0.60, "low": 0.55, "high": 0.65}),
            "informative")
        self.assertEqual(
            metrics.auc_verdict({"auc": 0.40, "low": 0.35, "high": 0.45}),
            "INVERTED")
        self.assertEqual(
            metrics.auc_verdict({"auc": 0.51, "low": 0.48, "high": 0.54}),
            "null")

    def test_an_interval_touching_one_half_is_null_not_informative(self):
        # The boundary matters: a gate is not allowed to be enabled on an interval that
        # includes no information.
        self.assertEqual(
            metrics.auc_verdict({"auc": 0.52, "low": 0.50, "high": 0.54}), "null")


class AucWithinAttributionTests(unittest.TestCase):
    """The attribution test, on data whose answer is known by construction.

    `real` drives the label. `proxy` is a noisy copy of `real` and has NO independent
    relationship to it. A correct attribution test must therefore show:

        proxy within real's strata  ->  collapses to ~0.5   (it was borrowing)
        real  within proxy's strata ->  survives            (it owns the signal)

    which is the exact asymmetry that identified volume_rate_ratio as a proxy for
    excursion distance. If this test ever passes symmetrically, the instrument is
    broken and every "carries its own information" verdict it has produced is void.
    """

    def _corpus(self):
        import random
        generator = random.Random(20260927)
        rows = []
        for _ in range(4000):
            real = generator.gauss(0.0, 1.0)
            rows.append({
                "real": real,
                "proxy": real + generator.gauss(0.0, 0.35),
                # The label depends on `real` ALONE. `proxy` reaches it only through
                # `real`, which is what makes it a proxy rather than a second signal.
                "label": real + generator.gauss(0.0, 0.8) > 0.0,
            })
        return rows

    def test_both_separate_the_label_when_measured_alone(self):
        rows = self._corpus()
        for field in ("real", "proxy"):
            result = metrics.auc([row[field] for row in rows if row["label"]],
                                 [row[field] for row in rows if not row["label"]])
            self.assertGreater(result["low"], 0.5,
                               f"{field} should look informative on its own")

    def test_the_proxy_collapses_when_the_control_is_fine_enough(self):
        """A pure proxy must read no information once `real` is held tightly fixed.

        This is the instrument's core claim. The proxy is a strong standalone predictor
        and reaches the label ONLY through `real`, so with `real` pinned closely its
        remaining variation is its own noise and must carry nothing.

        Fine strata rather than "exact" ones, because exact strata are not achievable
        through quantile cuts: on a tied variable the cuts de-duplicate, and any stratum
        holding two levels lets `real` vary inside it again. Granularity is the knob
        that controls how exact the control is, which is what the next test measures.
        """
        import random
        generator = random.Random(20260927)
        rows = []
        for _ in range(20000):
            real = generator.gauss(0.0, 1.0)
            rows.append({"real": real,
                         "proxy": real + generator.gauss(0.0, 0.35),
                         "label": real + generator.gauss(0.0, 0.8) > 0.0})

        alone = metrics.auc([row["proxy"] for row in rows if row["label"]],
                            [row["proxy"] for row in rows if not row["label"]])
        self.assertGreater(alone["low"], 0.65)

        pooled, _ = metrics.auc_within(
            rows, lambda row: row["real"], lambda row: row["proxy"],
            lambda row: row["label"], bins=25)
        self.assertIsNotNone(pooled)
        self.assertFalse(pooled["vacuous"])
        self.assertGreater(pooled["strata"], 15)
        self.assertEqual(metrics.auc_verdict(pooled), "null")
        self.assertLess(abs(pooled["auc"] - 0.5), 0.02)

    def test_a_constant_stratifier_is_flagged_vacuous_not_reported_as_a_control(self):
        """THE BUG FAMILY, IN THE CONTROL ITSELF.

        A stratifier that does not vary applies no control: every row lands in one
        stratum and the pooled number is just the unstratified AUC. Reported without a
        flag, that reads as "the quantity survived the control" when no control ran.

        This is not hypothetical. `consecutive_closes` is pinned to exactly
        ACCEPT_MIN_CANDLES in every row of the default acceptance corpus, so
        stratifying on it is precisely this case.
        """
        rows = self._corpus()
        for row in rows:
            row["frozen"] = 3.0

        pooled, detail = metrics.auc_within(
            rows, lambda row: row["frozen"], lambda row: row["proxy"],
            lambda row: row["label"])
        self.assertIsNotNone(pooled)
        self.assertEqual(pooled["strata"], 1)
        self.assertTrue(pooled["vacuous"])

        unstratified = metrics.auc(
            [row["proxy"] for row in rows if row["label"]],
            [row["proxy"] for row in rows if not row["label"]])
        self.assertAlmostEqual(pooled["auc"], unstratified["auc"], places=6)

    def test_a_real_stratifier_is_not_flagged_vacuous(self):
        rows = self._corpus()
        pooled, _ = metrics.auc_within(
            rows, lambda row: row["real"], lambda row: row["proxy"],
            lambda row: row["label"])
        self.assertEqual(pooled["strata"], 5)
        self.assertFalse(pooled["vacuous"])

    def test_coarse_strata_leave_residual_confounding_and_finer_ones_remove_it(self):
        """HOW TO READ A REAL RESULT FROM THIS INSTRUMENT.

        When the stratifying variable is CONTINUOUS, a quintile still contains a wide
        range of it, so a close copy predicts position WITHIN the stratum and keeps
        some apparent power. That residual is a property of the binning, not of the
        data, and it falls monotonically as the strata get finer.

        The direction is what matters for interpretation: coarse stratification
        OVERSTATES a proxy. So a real measurement that already reads ~0.50 at five
        bins is a CONSERVATIVE null - the bias was working in favour of the quantity
        and it still showed nothing. That is exactly the situation
        volume_rate_ratio is in at 0.5035 within distance quintiles, and this test is
        what licenses reading it that way.
        """
        rows = self._corpus()
        results = []
        for bins in (2, 5, 25):
            pooled, _ = metrics.auc_within(
                rows, lambda row: row["real"], lambda row: row["proxy"],
                lambda row: row["label"], bins=bins)
            results.append(pooled["auc"])
        self.assertGreater(results[0], results[1])
        self.assertGreater(results[1], results[2])
        # Coarse binning overstates; the fine one is near null.
        self.assertGreater(results[0], 0.65)
        self.assertLess(results[2], 0.56)

    def test_the_real_signal_survives_inside_the_proxys_strata(self):
        rows = self._corpus()
        pooled, _ = metrics.auc_within(
            rows, lambda row: row["proxy"], lambda row: row["real"],
            lambda row: row["label"])
        self.assertIsNotNone(pooled)
        self.assertGreater(pooled["low"], 0.5)
        self.assertEqual(metrics.auc_verdict(pooled), "informative")

    def test_an_independent_second_signal_is_not_called_a_proxy(self):
        """The other half of the guard: a test that collapses EVERYTHING is useless.

        Here two independent quantities both contribute to the label, so neither is a
        proxy and both must survive the other's strata. A stratified test that reported
        "proxy" for this would condemn every real signal it ever saw.
        """
        import random
        generator = random.Random(4242)
        rows = []
        for _ in range(4000):
            first = generator.gauss(0.0, 1.0)
            second = generator.gauss(0.0, 1.0)
            rows.append({"first": first, "second": second,
                         "label": first + second + generator.gauss(0.0, 0.5) > 0.0})
        for stratify, measure in (("first", "second"), ("second", "first")):
            pooled, _ = metrics.auc_within(
                rows, lambda row, k=stratify: row[k],
                lambda row, k=measure: row[k], lambda row: row["label"])
            self.assertGreater(pooled["low"], 0.5,
                               f"{measure} within {stratify} should survive")

    def test_missing_values_are_excluded_from_both_roles(self):
        rows = self._corpus()
        for index in range(0, 400):
            rows[index]["proxy"] = None
        pooled, _ = metrics.auc_within(
            rows, lambda row: row["real"], lambda row: row["proxy"],
            lambda row: row["label"])
        self.assertIsNotNone(pooled)

    def test_too_few_rows_returns_none_rather_than_a_verdict(self):
        rows = self._corpus()[:100]
        pooled, _ = metrics.auc_within(
            rows, lambda row: row["real"], lambda row: row["proxy"],
            lambda row: row["label"])
        self.assertIsNone(pooled)


class NormalQuantileTests(unittest.TestCase):
    """Pinned against published values, since a wrong bar silently changes every verdict."""

    def test_matches_known_quantiles(self):
        for probability, expected in ((0.975, 1.959964), (0.995, 2.575829),
                                      (0.95, 1.644854), (0.9995, 3.290527),
                                      (0.5, 0.0)):
            self.assertAlmostEqual(metrics._normal_quantile(probability), expected,
                                   places=5)

    def test_is_symmetric(self):
        for probability in (0.6, 0.8, 0.95, 0.999):
            self.assertAlmostEqual(metrics._normal_quantile(probability),
                                   -metrics._normal_quantile(1.0 - probability),
                                   places=6)

    def test_rejects_probabilities_outside_the_open_interval(self):
        for bad in (0.0, 1.0, -0.1, 1.5):
            with self.assertRaises(ValueError):
                metrics._normal_quantile(bad)

    def test_bonferroni_z_rises_with_the_number_of_tests(self):
        singles = metrics.bonferroni_z(1)
        self.assertAlmostEqual(singles, 1.959964, places=5)
        previous = singles
        for tests in (2, 4, 8, 16):
            current = metrics.bonferroni_z(tests)
            self.assertGreater(current, previous)
            previous = current

    def test_four_tests_gives_the_expected_bar(self):
        self.assertAlmostEqual(metrics.bonferroni_z(4), 2.4977, places=3)


class ClusteredIntervalTests(unittest.TestCase):
    """The correction that stops correlated trades from buying unearned confidence.

    Two analytically known cases bracket the behaviour:

      * trades uncorrelated within clusters -> the clustered interval should land close
        to the naive one, so the correction never penalises genuinely independent data
      * trades IDENTICAL within clusters -> each cluster is really one observation, so the
        standard error must grow by about sqrt(cluster size)

    If the first case failed, every result would be needlessly discarded. If the second
    failed, the function would not be doing anything and the gate would keep its false
    confidence.
    """

    @staticmethod
    def _rows(values, cluster_size):
        return [{"net_r": value, "session_id": f"day{index // cluster_size}"}
                for index, value in enumerate(values)]

    def test_uncorrelated_within_clusters_is_close_to_the_naive_interval(self):
        import random
        generator = random.Random(11)
        values = [generator.gauss(0.1, 1.0) for _ in range(1200)]
        rows = self._rows(values, cluster_size=20)
        naive = metrics.mean_interval(values)
        clustered = metrics.clustered_mean_interval(
            rows, lambda row: row["net_r"], lambda row: row["session_id"])
        self.assertEqual(clustered["clusters"], 60)
        self.assertAlmostEqual(clustered["mean"], naive["mean"], places=9)
        ratio = clustered["se"] / naive["se"]
        self.assertGreater(ratio, 0.75)
        self.assertLess(ratio, 1.35)

    def test_identical_within_clusters_inflates_the_error_by_root_cluster_size(self):
        import random
        generator = random.Random(12)
        cluster_size = 25
        values = []
        for _ in range(48):
            # One market day: every trade returns the same thing.
            day_value = generator.gauss(0.1, 1.0)
            values.extend([day_value] * cluster_size)
        rows = self._rows(values, cluster_size=cluster_size)
        naive = metrics.mean_interval(values)
        clustered = metrics.clustered_mean_interval(
            rows, lambda row: row["net_r"], lambda row: row["session_id"])
        self.assertEqual(clustered["clusters"], 48)
        ratio = clustered["se"] / naive["se"]
        self.assertGreater(ratio, 0.8 * math.sqrt(cluster_size))
        self.assertLess(ratio, 1.2 * math.sqrt(cluster_size))

    def test_a_single_cluster_is_flagged_degenerate_rather_than_silently_naive(self):
        """One cluster cannot estimate between-cluster variance.

        Returning the naive interval unflagged would hand back exactly the number this
        function exists to replace, which is the worst possible failure for it.
        """
        rows = [{"net_r": value, "session_id": "day0"} for value in (0.5, -1.0, 2.0, 0.1)]
        result = metrics.clustered_mean_interval(
            rows, lambda row: row["net_r"], lambda row: row["session_id"])
        self.assertEqual(result["clusters"], 1)
        self.assertTrue(result["degenerate"])

    def test_none_values_are_excluded(self):
        rows = [{"net_r": None, "session_id": "day0"},
                {"net_r": 1.0, "session_id": "day0"},
                {"net_r": -1.0, "session_id": "day1"},
                {"net_r": 2.0, "session_id": "day2"}]
        result = metrics.clustered_mean_interval(
            rows, lambda row: row["net_r"], lambda row: row["session_id"])
        self.assertEqual(result["n"], 3)
        self.assertEqual(result["clusters"], 3)

    def test_empty_input_does_not_raise(self):
        result = metrics.clustered_mean_interval([], lambda row: row["net_r"],
                                                 lambda row: row["session_id"])
        self.assertEqual(result["n"], 0)


class SummariseRTests(unittest.TestCase):

    @staticmethod
    def _correlated_rows(days=40, per_day=25, mean=0.15):
        """1000 trades that are really `days` observations, with an EXACT mean.

        Deterministic rather than seeded, because the interesting window is narrow and a
        random draw kept missing it. With every trade in a day identical, the pooled sd
        equals the day-to-day sd, so:

            naive SE     ~ sd / sqrt(1000) = 0.032   -> significant above ~0.062
            clustered SE ~ sd / sqrt(40)   = 0.158   -> significant above ~0.310

        A mean of 0.15 sits between those bars, which is exactly the region where the
        independence assumption invents a finding. Day values are a symmetric ramp scaled
        to unit sd, so the realised mean is the requested one to floating-point accuracy
        and the test cannot drift with a seed.
        """
        offsets = [index - (days - 1) / 2.0 for index in range(days)]
        scale = math.sqrt(sum(value * value for value in offsets) / days)
        rows = []
        for day, offset in enumerate(offsets):
            day_value = mean + offset / scale
            for _ in range(per_day):
                rows.append({"net_r": day_value, "session_id": f"2026-01-{day:02d}",
                             "direction": "BUY"})
        return rows

    def test_the_fixture_sits_between_the_two_bars(self):
        """Guards the fixture itself: if it drifts, the demotion tests prove nothing."""
        rows = self._correlated_rows()
        values = [row["net_r"] for row in rows]
        self.assertAlmostEqual(sum(values) / len(values), 0.15, places=9)
        naive = metrics.mean_interval(values)
        self.assertGreater(0.15, 1.96 * naive["se"])
        clustered = metrics.clustered_mean_interval(
            rows, lambda row: row["net_r"], lambda row: row["session_id"])
        self.assertLess(0.15, 1.96 * clustered["se"])

    def test_clustering_can_demote_a_naively_significant_result(self):
        """The case this whole change exists for.

        One thousand trades that are really forty observations. The naive interval calls
        it significant; the clustered one must not, and `significant_naive` has to record
        that so the demotion is visible rather than silent.
        """
        rows = self._correlated_rows()
        summary = metrics.summarise_r(rows)
        self.assertEqual(summary["n"], 1000)
        self.assertEqual(summary["clusters"], 40)
        self.assertTrue(summary["significant_naive"])
        self.assertFalse(summary["significant"])
        self.assertGreater(summary["se_inflation"], 3.0)

    def test_naive_and_clustered_bounds_are_both_reported(self):
        rows = self._correlated_rows()
        summary = metrics.summarise_r(rows)
        self.assertLess(summary["r_low"], summary["naive_low"])
        self.assertGreater(summary["r_high"], summary["naive_high"])

    def test_opting_out_of_clustering_reproduces_the_naive_interval(self):
        rows = self._correlated_rows()
        summary = metrics.summarise_r(rows, cluster_of=None)
        self.assertAlmostEqual(summary["r_low"], summary["naive_low"], places=9)
        self.assertIsNone(summary["clusters"])

    def test_a_stricter_z_widens_the_interval_and_can_withdraw_a_star(self):
        import random
        generator = random.Random(3)
        rows = [{"net_r": generator.gauss(0.05, 0.5),
                 "session_id": f"2026-02-{index % 90:02d}"} for index in range(3000)]
        loose = metrics.summarise_r(rows, z=1.96)
        strict = metrics.summarise_r(rows, z=metrics.bonferroni_z(4))
        self.assertLess(strict["r_low"], loose["r_low"])
        self.assertGreater(strict["r_high"], loose["r_high"])

    def test_empty_rows_report_n_zero(self):
        self.assertEqual(metrics.summarise_r([])["n"], 0)
        self.assertEqual(metrics.summarise_r([{"net_r": None}])["n"], 0)

    def test_format_marks_a_demoted_result_with_a_tilde(self):
        rows = self._correlated_rows()
        line = metrics.format_r_summary("S3-BRK", metrics.summarise_r(rows))
        self.assertIn("~", line)
        self.assertNotIn("*", line)

    def test_format_marks_a_surviving_result_with_a_star(self):
        # A genuine effect: large per-day means relative to their spread, many clusters.
        import random
        generator = random.Random(5)
        rows = []
        for day in range(120):
            day_value = generator.gauss(0.8, 0.2)
            for _ in range(5):
                rows.append({"net_r": day_value, "session_id": f"2026-03-{day:03d}"})
        line = metrics.format_r_summary("S1-POC", metrics.summarise_r(rows))
        self.assertIn("*", line)
        self.assertNotIn("~", line)

    def test_format_handles_an_empty_summary(self):
        self.assertIn("n=0", metrics.format_r_summary("S2-VAR", {"n": 0}))


class WilsonIntervalTests(unittest.TestCase):
    """Pinned because every band rate in the acceptance report is read through it."""

    def test_zero_total_does_not_divide_by_zero(self):
        self.assertEqual(metrics.wilson_interval(0, 0), (0.0, 0.0, 0.0))

    def test_bounds_stay_inside_zero_and_one_at_the_extremes(self):
        for successes, total in ((0, 30), (30, 30), (1, 500), (499, 500)):
            point, low, high = metrics.wilson_interval(successes, total)
            self.assertGreaterEqual(low, 0.0)
            self.assertLessEqual(high, 1.0)
            self.assertLessEqual(low, point)
            self.assertGreaterEqual(high, point)

    def test_interval_narrows_as_n_grows(self):
        _, low_small, high_small = metrics.wilson_interval(50, 100)
        _, low_big, high_big = metrics.wilson_interval(5000, 10000)
        self.assertGreater(high_small - low_small, high_big - low_big)


if __name__ == "__main__":
    unittest.main(verbosity=2)
