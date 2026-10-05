import unittest
from datetime import datetime

import sessions


class WeeklyBoundaryTests(unittest.TestCase):
    """`week_start_ms`/`current_week`/`previous_week`: pre-existing helpers that sat
    unused until PLAN item 21 (scanner.frozen_weekly_bundle) became their first real
    caller. Never unit-tested before that, so verified directly here rather than
    trusted on the strength of their docstrings alone."""

    def test_week_start_lands_on_monday_00_00_utc(self):
        # 2026-09-30 is a Wednesday.
        ts = sessions.to_ms(datetime(2026, 9, 30, 14, 30))
        start = sessions.week_start_ms(ts)
        self.assertEqual(sessions.to_utc(start), sessions.to_utc(start).replace(
            hour=0, minute=0, second=0, microsecond=0))
        self.assertEqual(sessions.to_utc(start).weekday(), 0)
        self.assertEqual(sessions.to_utc(start).date().isoformat(), "2026-09-28")

    def test_week_start_is_a_no_op_when_already_on_monday(self):
        monday = sessions.to_ms(datetime(2026, 9, 28))
        self.assertEqual(sessions.week_start_ms(monday), monday)

    def test_current_week_spans_exactly_seven_days(self):
        ts = sessions.to_ms(datetime(2026, 9, 30))
        start, end = sessions.current_week(ts)
        self.assertEqual(end - start, 7 * sessions.MS_DAY)

    def test_previous_week_is_the_seven_days_immediately_before_current(self):
        ts = sessions.to_ms(datetime(2026, 9, 30))
        cur_start, _ = sessions.current_week(ts)
        prev_start, prev_end = sessions.previous_week(ts)
        self.assertEqual(prev_end, cur_start,
                         "previous week must end exactly where the current one "
                         "starts - no gap, no overlap")
        self.assertEqual(cur_start - prev_start, 7 * sessions.MS_DAY)

    def test_previous_week_never_touches_the_current_week(self):
        ts = sessions.to_ms(datetime(2026, 9, 30))
        _, prev_end = sessions.previous_week(ts)
        cur_start, _ = sessions.current_week(ts)
        self.assertLessEqual(prev_end, cur_start)

    def test_a_monday_itself_belongs_to_the_week_it_starts(self):
        monday = sessions.to_ms(datetime(2026, 9, 28))
        start, end = sessions.current_week(monday)
        self.assertEqual(start, monday)
        self.assertLess(monday, end)


if __name__ == "__main__":
    unittest.main()
