import unittest

import pandas as pd

from jira_dashboard.effort import build_effort_summary, weekly_effort_history


class EffortMetricsTests(unittest.TestCase):
    def setUp(self):
        self.now = pd.Timestamp("2026-10-12T02:00:00Z")

    def item(self, **overrides):
        return dict(
            issue_type="Story", status_category="To do", story_points=3,
            team="Credit", resolved=None,
        ) | overrides

    def test_summary_reports_recorded_estimates_and_coverage_in_story_bug_scope(self):
        issues = pd.DataFrame([
            self.item(story_points=3), self.item(issue_type="bUg", story_points=5),
            self.item(story_points=None), self.item(story_points=0),
            self.item(status_category="Done", story_points=8, resolved=self.now),
            self.item(status_category="done", story_points=None, resolved=self.now),
            self.item(issue_type="Epic", story_points=100),
            self.item(issue_type="Task", status_category="Done", story_points=100),
        ])
        summary = build_effort_summary(issues, self.now)
        self.assertEqual(summary["open_items"], 4)
        self.assertEqual(summary["estimated_open_items"], 3)
        self.assertEqual(summary["unestimated_open_items"], 1)
        self.assertEqual(summary["remaining_points"], 8)
        self.assertEqual(summary["estimated_done_items"], 1)
        self.assertEqual(summary["unestimated_done_items"], 1)
        self.assertEqual(summary["missing_completion_dates"], 0)
        self.assertFalse(any("projection" in key or "closure" in key for key in summary))

    def test_invalid_estimates_and_booleans_are_unestimated_while_zero_is_valid(self):
        issues = pd.DataFrame([
            self.item(story_points=value)
            for value in [True, False, None, "bad", -1, float("inf"), -float("inf"), float("nan"), 0, "2.5"]
        ])
        summary = build_effort_summary(issues, self.now)
        self.assertEqual(summary["estimated_open_items"], 2)
        self.assertEqual(summary["unestimated_open_items"], 8)
        self.assertEqual(summary["remaining_points"], 2.5)

    def test_missing_estimates_are_not_zero_and_no_open_work_is_zero(self):
        unestimated = build_effort_summary(pd.DataFrame([self.item(story_points=None)]), self.now)
        self.assertIsNone(unestimated["remaining_points"])
        done = build_effort_summary(pd.DataFrame([self.item(status_category="Done")]), self.now)
        self.assertEqual(done["open_items"], 0)
        self.assertEqual(done["remaining_points"], 0)
        self.assertTrue(done["per_team"].empty)
        empty = build_effort_summary(pd.DataFrame(), self.now)
        self.assertEqual(empty["open_items"], 0)
        self.assertEqual(empty["remaining_points"], 0)
        self.assertTrue(empty["per_team"].empty)

    def test_per_team_breakdown_preserves_coverage_and_unspecified_team(self):
        issues = pd.DataFrame([
            self.item(team=" Credit ", story_points=3),
            self.item(team="Credit", story_points=None),
            self.item(team="Collections", story_points=8),
            self.item(team="Unestimated team", story_points=None),
            self.item(team=None, story_points=2),
            self.item(team=" ", story_points=None),
            self.item(team="Finished team", status_category="Done"),
        ])
        summary = build_effort_summary(issues, self.now)
        teams = summary["per_team"].set_index("team")
        self.assertNotIn("Finished team", teams.index)
        self.assertEqual(teams.loc["Credit", "open_items"], 2)
        self.assertEqual(teams.loc["Credit", "estimated_open_items"], 1)
        self.assertEqual(teams.loc["Credit", "remaining_points"], 3)
        self.assertEqual(teams.loc["Collections", "remaining_points"], 8)
        self.assertEqual(teams.loc["Unspecified", "unestimated_open_items"], 1)
        self.assertTrue(pd.isna(teams.loc["Unestimated team", "remaining_points"]))
        self.assertEqual(teams["open_items"].sum(), summary["open_items"])

    def test_history_windows_have_no_overlap_and_include_current_done_scope_only(self):
        issues = pd.DataFrame([
            self.item(status_category="Done", resolved=self.now - pd.Timedelta(days=day), story_points=day + 1)
            for day in (0, 7, 14, 21, 28)
        ] + [
            self.item(status_category="Done", resolved=self.now - pd.Timedelta(days=28, seconds=1), story_points=100),
            self.item(status_category="Done", resolved=self.now + pd.Timedelta(seconds=1), story_points=100),
            self.item(status_category="Done", issue_type="Epic", resolved=self.now, story_points=100),
            self.item(status_category="In progress", resolved=self.now, story_points=100),
        ])
        history = weekly_effort_history(issues, self.now)
        self.assertEqual(history["completed_items"].tolist(), [1, 1, 1, 2])
        self.assertEqual(history["completed_points"].tolist(), [29, 22, 15, 9])
        self.assertEqual(history["estimated_completed_items"].tolist(), [1, 1, 1, 2])
        self.assertEqual(history["unestimated_completed_items"].tolist(), [0, 0, 0, 0])
        self.assertEqual(history.iloc[0]["period_start"], self.now - pd.Timedelta(days=28))
        self.assertEqual(history.iloc[-1]["period_end"], self.now)
        self.assertEqual(history["period_start"].tolist()[1:], history["period_end"].tolist()[:-1])

    def test_unestimated_completions_have_unknown_points_while_empty_week_is_zero(self):
        issues = pd.DataFrame([
            self.item(status_category="Done", resolved=self.now, story_points=None),
            self.item(status_category="Done", resolved=self.now, story_points=True),
        ])
        history = weekly_effort_history(issues, self.now)
        self.assertEqual(history.iloc[0]["completed_points"], 0)
        self.assertTrue(pd.isna(history.iloc[-1]["completed_points"]))
        self.assertEqual(history.iloc[-1]["completed_items"], 2)
        self.assertEqual(history.iloc[-1]["estimated_completed_items"], 0)
        self.assertEqual(history.iloc[-1]["unestimated_completed_items"], 2)

    def test_partial_estimates_keep_known_points_and_explicit_unestimated_count(self):
        issues = pd.DataFrame([
            self.item(status_category="Done", resolved=self.now, story_points=0),
            self.item(status_category="Done", resolved=self.now, story_points=None),
        ])
        recent = weekly_effort_history(issues, self.now).iloc[-1]
        self.assertEqual(recent["completed_points"], 0)
        self.assertEqual(recent["estimated_completed_items"], 1)
        self.assertEqual(recent["unestimated_completed_items"], 1)

    def test_unavailable_or_wholly_missing_completion_dates_give_empty_history(self):
        issues = pd.DataFrame([
            self.item(status_category="Done", resolved=None),
            self.item(status_category="Done", resolved="invalid"),
        ])
        self.assertTrue(weekly_effort_history(issues, self.now).empty)
        self.assertEqual(build_effort_summary(issues, self.now)["missing_completion_dates"], 2)
        for data, available in ((issues.drop(columns="resolved"), True), (issues, False)):
            self.assertTrue(weekly_effort_history(data, self.now, available).empty)
            self.assertIsNone(build_effort_summary(data, self.now, available)["missing_completion_dates"])

    def test_partial_completion_dates_retain_known_history_and_expose_missing_count(self):
        issues = pd.DataFrame([
            self.item(status_category="Done", resolved=self.now, story_points=5),
            self.item(status_category="Done", resolved=None, story_points=8),
            self.item(status_category="Done", resolved="invalid", story_points=None),
        ])
        summary = build_effort_summary(issues, self.now)
        self.assertEqual(summary["missing_completion_dates"], 2)
        self.assertEqual(summary["estimated_done_items"], 2)
        self.assertEqual(summary["unestimated_done_items"], 1)
        recent = weekly_effort_history(issues, self.now).iloc[-1]
        self.assertEqual(recent["completed_items"], 1)
        self.assertEqual(recent["completed_points"], 5)

    def test_no_done_with_available_resolution_field_produces_zero_history(self):
        issues = pd.DataFrame([self.item()])
        history = weekly_effort_history(issues, self.now, weeks=2)
        self.assertEqual(history["completed_items"].tolist(), [0, 0])
        self.assertEqual(history["completed_points"].tolist(), [0, 0])
        self.assertEqual(build_effort_summary(issues, self.now)["missing_completion_dates"], 0)
        self.assertTrue(weekly_effort_history(issues, self.now, weeks=0).empty)

    def test_utc_offsets_and_naive_now_align_with_delivery_windows(self):
        issues = pd.DataFrame([
            self.item(status_category="Done", resolved="2026-10-11T19:00:00-07:00", story_points=2),
            self.item(status_category="Done", resolved="2026-10-04T19:00:00-07:00", story_points=3),
        ])
        history = weekly_effort_history(issues, self.now.tz_localize(None))
        self.assertEqual(history.iloc[-1]["completed_items"], 2)
        self.assertEqual(history.iloc[-1]["completed_points"], 5)
        self.assertEqual(history.iloc[-2]["completed_items"], 0)

    def test_missing_columns_are_safe_and_source_data_is_unchanged(self):
        issues = pd.DataFrame([{"issue_type": "Story"}])
        original = issues.copy(deep=True)
        summary = build_effort_summary(issues, self.now)
        self.assertEqual(summary["open_items"], 1)
        self.assertEqual(summary["unestimated_open_items"], 1)
        self.assertIsNone(summary["remaining_points"])
        self.assertEqual(summary["per_team"].iloc[0]["team"], "Unspecified")
        self.assertTrue(weekly_effort_history(issues, self.now).empty)
        pd.testing.assert_frame_equal(issues, original)


if __name__ == "__main__":
    unittest.main()
