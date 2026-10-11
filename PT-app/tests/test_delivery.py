import unittest

import pandas as pd

from jira_dashboard.delivery import (
    build_delivery_summary,
    open_priority_status,
    weekly_completion_history,
)


class DeliveryMetricsTests(unittest.TestCase):
    def setUp(self):
        # UTC date is Oct 12, while the Pacific calendar date is still Oct 11.
        self.now = pd.Timestamp("2026-10-12T02:00:00Z")

    def item(self, **overrides):
        return dict(
            issue_type="Story", status_category="Done", priority="Medium",
            resolved=self.now - pd.Timedelta(days=1), due_date=None,
        ) | overrides

    def test_completion_boundaries_and_current_scope(self):
        issues = pd.DataFrame([
            self.item(resolved=self.now),
            self.item(resolved=self.now - pd.Timedelta(days=7), issue_type="bUg"),
            self.item(resolved=self.now - pd.Timedelta(days=14)),
            self.item(resolved=self.now - pd.Timedelta(days=7, seconds=1)),
            self.item(resolved=self.now + pd.Timedelta(seconds=1)),
            self.item(resolved=self.now - pd.Timedelta(days=14, seconds=1)),
            self.item(issue_type="Epic"),
            self.item(status_category="In progress"),  # Reopened items are not completed work.
            self.item(resolved="invalid"),
        ])
        result = build_delivery_summary(issues, self.now)
        self.assertEqual(result["total_items"], 8)
        self.assertEqual(result["open_items"], 1)
        self.assertEqual(result["completed_last_7_days"], 2)
        self.assertEqual(result["completed_previous_7_days"], 2)

    def test_high_priority_overdue_uses_pacific_today_and_only_open_items(self):
        issues = pd.DataFrame([
            self.item(priority=" HIGH ", status_category="To do", due_date="2026-10-10"),
            self.item(priority="highest", status_category="In progress", due_date="2026-10-11"),
            self.item(priority="High", status_category="To do", due_date="invalid"),
            self.item(priority="High", due_date="2026-10-01"),
            self.item(priority="Low", status_category="To do", due_date="2026-10-01"),
            self.item(priority=None, status_category="To do"),
            self.item(priority="High", issue_type="Task", status_category="To do", due_date="2026-10-01"),
        ])
        result = build_delivery_summary(issues, self.now)
        self.assertEqual(result["high_priority_open"], 3)
        self.assertEqual(result["high_priority_overdue"], 1)
        self.assertEqual(result["open_items"], 5)

    def test_item_throughput_does_not_create_a_finish_date_or_mutate_input(self):
        issues = pd.DataFrame([
            self.item(), self.item(issue_type="Bug"),
            *[self.item(status_category="To do", resolved=None) for _ in range(3)],
        ])
        original = issues.copy(deep=True)
        result = build_delivery_summary(issues, self.now)
        self.assertEqual(result["completed_last_7_days"], 2)
        self.assertNotIn("projected_closure", result)
        pd.testing.assert_frame_equal(issues, original)

    def test_zero_completions_and_no_open_work_remain_descriptive(self):
        for issues in (
            pd.DataFrame([self.item(status_category="To do", resolved=None)]),
            pd.DataFrame([self.item()]),
        ):
            self.assertNotIn("projected_closure", build_delivery_summary(issues, self.now))
        zero = build_delivery_summary(
            pd.DataFrame([self.item(status_category="To do", resolved=None)]), self.now,
        )
        self.assertEqual(zero["completed_last_7_days"], 0)
        self.assertEqual(zero["completed_previous_7_days"], 0)

    def test_unavailable_resolution_data_is_distinct_from_zero(self):
        issues = pd.DataFrame([self.item()])
        for data, available in ((issues.drop(columns="resolved"), True), (issues, False)):
            result = build_delivery_summary(data, self.now, available)
            self.assertIsNone(result["completed_last_7_days"])
            self.assertIsNone(result["completed_previous_7_days"])
            self.assertIsNone(result["missing_completion_dates"])
            self.assertTrue(weekly_completion_history(data, self.now, available).empty)

    def test_empty_and_missing_scope_data(self):
        for issues in (pd.DataFrame(), pd.DataFrame([{"priority": "High"}])):
            result = build_delivery_summary(issues, self.now)
            self.assertEqual(result["total_items"], 0)
            self.assertEqual(result["open_items"], 0)
            self.assertEqual(result["high_priority_overdue"], 0)
            self.assertIsNone(result["completed_last_7_days"])
            self.assertTrue(open_priority_status(issues).empty)
        present_empty_scope = pd.DataFrame(columns=["issue_type", "resolved"])
        self.assertEqual(build_delivery_summary(present_empty_scope, self.now)["completed_last_7_days"], 0)

    def test_history_has_nonoverlapping_trailing_windows(self):
        issues = pd.DataFrame([
            *[self.item(resolved=self.now - pd.Timedelta(days=day)) for day in (0, 7, 14, 21, 28)],
            self.item(resolved=self.now - pd.Timedelta(days=28, seconds=1)),
            self.item(issue_type="Epic"),
            self.item(status_category="To do"),
        ])
        history = weekly_completion_history(issues, self.now)
        self.assertEqual(history["completed_items"].tolist(), [1, 1, 1, 2])
        self.assertEqual(history.iloc[0]["period_start"], self.now - pd.Timedelta(days=28))
        self.assertEqual(history.iloc[-1]["period_end"], self.now)
        self.assertEqual(history["period_start"].tolist()[1:], history["period_end"].tolist()[:-1])
        summary = build_delivery_summary(issues, self.now)
        self.assertEqual(history.iloc[-1]["completed_items"], summary["completed_last_7_days"])
        self.assertEqual(history.iloc[-2]["completed_items"], summary["completed_previous_7_days"])

    def test_history_distinguishes_available_zeroes_and_accepts_custom_window_count(self):
        issues = pd.DataFrame([self.item(status_category="To do", resolved=None)])
        history = weekly_completion_history(issues, self.now, weeks=2)
        self.assertEqual(history["completed_items"].tolist(), [0, 0])
        self.assertEqual(build_delivery_summary(issues, self.now)["missing_completion_dates"], 0)
        self.assertTrue(weekly_completion_history(issues, self.now, weeks=0).empty)

    def test_all_done_dates_missing_makes_completion_timing_unavailable(self):
        issues = pd.DataFrame([
            self.item(resolved=None),
            self.item(resolved="invalid", issue_type="Bug"),
            self.item(issue_type="Epic"),
            self.item(status_category="In progress"),
        ])
        summary = build_delivery_summary(issues, self.now)
        self.assertEqual(summary["missing_completion_dates"], 2)
        self.assertIsNone(summary["completed_last_7_days"])
        self.assertIsNone(summary["completed_previous_7_days"])
        self.assertTrue(weekly_completion_history(issues, self.now).empty)

    def test_partial_resolution_dates_retain_known_counts_and_expose_coverage(self):
        issues = pd.DataFrame([
            self.item(),
            self.item(resolved=self.now - pd.Timedelta(days=8)),
            self.item(resolved="invalid"),
            self.item(resolved=None),
            self.item(status_category="To do", resolved=None),
        ])
        summary = build_delivery_summary(issues, self.now)
        self.assertEqual(summary["missing_completion_dates"], 2)
        self.assertEqual(summary["completed_last_7_days"], 1)
        self.assertEqual(summary["completed_previous_7_days"], 1)
        self.assertEqual(weekly_completion_history(issues, self.now)["completed_items"].tolist(), [0, 0, 1, 1])

    def test_resolution_timestamps_with_offsets_are_compared_in_utc(self):
        issues = pd.DataFrame([
            self.item(resolved="2026-10-11T19:00:00-07:00"),
            self.item(resolved="2026-10-04T19:00:00-07:00"),
            self.item(resolved="2026-10-11T19:00:01-07:00"),
        ])
        result = build_delivery_summary(issues, self.now)
        self.assertEqual(result["completed_last_7_days"], 2)
        self.assertEqual(result["completed_previous_7_days"], 0)

    def test_open_priority_summary_reuses_estimate_coverage_and_excludes_done(self):
        issues = pd.DataFrame([
            self.item(status_category="To do", priority="High", story_points=3),
            self.item(status_category="To do", priority="High", story_points=None),
            self.item(status_category="done", priority="High", story_points=5),
            self.item(issue_type="Epic", status_category="To do", story_points=100),
        ])
        result = open_priority_status(issues)
        self.assertEqual(result["items"].sum(), 2)
        self.assertEqual(result["estimated_items"].sum(), 1)
        self.assertEqual(result["story_points"].sum(), 3)

    def test_missing_detail_columns_are_safe_for_priority_counts(self):
        result = build_delivery_summary(pd.DataFrame([{"issue_type": "Story"}]), self.now)
        self.assertEqual(result["open_items"], 1)
        self.assertEqual(result["high_priority_open"], 0)
        priority = open_priority_status(pd.DataFrame([{"issue_type": "Story"}]))
        self.assertEqual(priority.iloc[0]["priority"], "Unspecified")
        self.assertEqual(priority.iloc[0]["status_category"], "Other")


if __name__ == "__main__":
    unittest.main()
