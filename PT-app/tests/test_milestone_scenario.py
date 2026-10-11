from datetime import date
import unittest

import pandas as pd

from jira_dashboard.milestone_scenario import build_milestone_scenario


class MilestoneScenarioTests(unittest.TestCase):
    def setUp(self):
        self.now = pd.Timestamp("2026-10-11T19:00:00Z")
        self.scenario = {"target_completion_pct": 91, "scope_growth_until": "2026-10-30"}

    def scope(self, total, done):
        return pd.DataFrame([
            {"issue_type": "Story", "status_category": "Done" if index < done else "To do"}
            for index in range(total)
        ])

    def test_target_rounds_up_to_a_whole_item(self):
        result = build_milestone_scenario(self.scope(11, 5), self.now, scenario=self.scenario)
        self.assertEqual(result["target_completion_pct"], 91)
        self.assertEqual(result["residual_pct"], 9)
        self.assertEqual(result["total_items"], 11)
        self.assertEqual(result["done_items"], 5)
        self.assertAlmostEqual(result["current_done_pct"], 500 / 11)
        self.assertEqual(result["target_done_items"], 11)
        self.assertEqual(result["additional_done_needed"], 6)
        self.assertEqual(result["max_remaining_items"], 0)

    def test_ninety_one_percent_meets_target_without_full_completion(self):
        result = build_milestone_scenario(self.scope(100, 91), self.now, scenario=self.scenario)
        self.assertEqual(result["current_done_pct"], 91)
        self.assertEqual(result["target_done_items"], 91)
        self.assertEqual(result["additional_done_needed"], 0)
        self.assertEqual(result["max_remaining_items"], 9)

    def test_above_target_never_produces_a_negative_remaining_gap(self):
        for done in (92, 100):
            with self.subTest(done=done):
                result = build_milestone_scenario(self.scope(100, done), self.now, scenario=self.scenario)
                self.assertEqual(result["additional_done_needed"], 0)
                self.assertEqual(result["max_remaining_items"], 9)

    def test_acceptance_counts_all_stories_bugs_and_debt_without_point_weighting(self):
        issues = pd.DataFrame([
            {"issue_type": " Story ", "status_category": "done", "story_points": 1},
            {"issue_type": "BUG", "status_category": "Done", "story_points": 2},
            {"issue_type": "Story", "status_category": "In progress", "story_points": 100, "team": "Technical Debt"},
            {"issue_type": "Epic", "status_category": "Done", "story_points": 500},
            {"issue_type": "Task", "status_category": "Done", "story_points": 500},
        ])
        original = issues.copy(deep=True)
        result = build_milestone_scenario(issues, self.now, scenario=self.scenario)
        self.assertEqual(result["total_items"], 3)
        self.assertEqual(result["done_items"], 2)
        self.assertAlmostEqual(result["current_done_pct"], 200 / 3)
        self.assertEqual(result["target_done_items"], 3)
        self.assertEqual(result["additional_done_needed"], 1)
        pd.testing.assert_frame_equal(issues, original)
        self.assertFalse(any("forecast" in key or "projected" in key for key in result))

    def test_empty_or_non_issue_scope_has_unknown_counts(self):
        for issues in (pd.DataFrame(), pd.DataFrame([{"issue_type": "Epic", "status_category": "Done"}])):
            result = build_milestone_scenario(issues, self.now, scenario=self.scenario)
            for key in (
                "total_items", "done_items", "current_done_pct", "target_done_items",
                "additional_done_needed", "max_remaining_items",
            ):
                self.assertIsNone(result[key])
            self.assertEqual(result["target_completion_pct"], 91)
            self.assertTrue(result["scope_growth_open"])

    def test_scope_growth_cutoff_is_inclusive_in_pacific_calendar_time(self):
        for timestamp, expected in (
            ("2026-10-30T00:00:00-07:00", True),
            ("2026-10-31T06:59:59Z", True),
            ("2026-10-31T07:00:00Z", False),
            ("2026-10-31T06:59:59", True),
        ):
            with self.subTest(timestamp=timestamp):
                result = build_milestone_scenario(self.scope(100, 90), pd.Timestamp(timestamp), scenario=self.scenario)
                self.assertEqual(result["scope_growth_until"], date(2026, 10, 30))
                self.assertEqual(result["scope_growth_open"], expected)

    def test_decimal_threshold_avoids_float_rounding_up_an_exact_integer_target(self):
        scenario = self.scenario | {"target_completion_pct": 56.1}
        result = build_milestone_scenario(self.scope(1000, 0), self.now, scenario=scenario)
        self.assertEqual(result["target_done_items"], 561)
        self.assertEqual(result["residual_pct"], 43.9)
        result = build_milestone_scenario(self.scope(100, 0), self.now, scenario=self.scenario | {"target_completion_pct": 100})
        self.assertEqual(result["target_done_items"], 100)
        self.assertEqual(result["max_remaining_items"], 0)

    def test_missing_and_invalid_scenarios_return_none(self):
        invalid_values = [None, {}, [], True, self.scenario | {"extra": "unexpected"}]
        for key in self.scenario:
            missing = self.scenario.copy()
            del missing[key]
            invalid_values.append(missing)
        invalid_values.extend(self.scenario | {"target_completion_pct": threshold} for threshold in (
            0, -1, 100.1, 10 ** 1000, True, False, "91", None, [], {},
            float("nan"), float("inf"), float("-inf"),
        ))
        invalid_values.extend(self.scenario | {"scope_growth_until": cutoff} for cutoff in (
            "2026-1-30", "2026-10-3", "20261030", "2026-10-30T00:00:00", "2026-10-30 ",
            "2026-02-29", "2026-13-01", "0000-01-01", None, True, 20261030,
        ))
        for scenario in invalid_values:
            with self.subTest(scenario=scenario):
                self.assertIsNone(build_milestone_scenario(self.scope(1, 0), self.now, scenario=scenario))
        self.assertIsNone(build_milestone_scenario(self.scope(1, 0), self.now))

    def test_invalid_observation_time_is_unavailable(self):
        self.assertIsNone(build_milestone_scenario(self.scope(1, 0), pd.NaT, scenario=self.scenario))


if __name__ == "__main__":
    unittest.main()
