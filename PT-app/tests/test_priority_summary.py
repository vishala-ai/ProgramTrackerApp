import unittest

import pandas as pd

from jira_dashboard.priority import priority_status_summary, story_point_distribution


class PrioritySummaryTests(unittest.TestCase):
    def test_counts_and_points_include_only_stories_and_bugs(self):
        issues = pd.DataFrame([
            dict(issue_type="Story", priority="High", status_category="To do", story_points=3),
            dict(issue_type="Bug", priority="High", status_category="To do", story_points=None),
            dict(issue_type="Story", priority="High", status_category="Done", story_points=5),
            dict(issue_type="Epic", priority="High", status_category="Done", story_points=100),
        ])
        summary = priority_status_summary(issues).set_index(["priority", "status_category"])
        self.assertEqual(summary.loc[("High", "To do"), "items"], 2)
        self.assertEqual(summary.loc[("High", "To do"), "estimated_items"], 1)
        self.assertEqual(summary["story_points"].sum(), 8)
        self.assertEqual(summary["items"].sum(), 3)

    def test_missing_estimates_are_not_zero(self):
        summary = priority_status_summary(pd.DataFrame([
            dict(issue_type="Story", priority=None, status_category="Done"),
        ]))
        self.assertEqual(summary.iloc[0]["priority"], "Unspecified")
        self.assertEqual(summary.iloc[0]["estimated_items"], 0)
        self.assertTrue(pd.isna(summary.iloc[0]["story_points"]))

    def test_zero_estimate_is_valid_but_invalid_values_are_excluded(self):
        summary = priority_status_summary(pd.DataFrame([
            dict(issue_type="Story", priority="Medium", status_category="In progress", story_points=value)
            for value in [0, "2.5", -3, "bad", float("inf")]
        ]))
        self.assertEqual(summary.iloc[0]["items"], 5)
        self.assertEqual(summary.iloc[0]["estimated_items"], 2)
        self.assertEqual(summary.iloc[0]["story_points"], 2.5)

    def test_empty_scope(self):
        self.assertTrue(priority_status_summary(pd.DataFrame()).empty)


class StoryPointDistributionTests(unittest.TestCase):
    def test_counts_stories_at_each_estimate_across_statuses(self):
        issues = pd.DataFrame([
            dict(issue_type="Story", status_category="To do", story_points=3),
            dict(issue_type="Story", status_category="Done", story_points=3),
            dict(issue_type="Story", status_category="In progress", story_points=8),
            dict(issue_type="Bug", story_points=3),
            dict(issue_type="Epic", story_points=8),
        ])
        distribution = story_point_distribution(issues)
        self.assertEqual(distribution.to_dict("records"), [
            {"story_point_estimate": "3 points", "stories": 2},
            {"story_point_estimate": "8 points", "stories": 1},
        ])

    def test_keeps_unknown_estimates_separate_from_zero_and_sorts_numerically(self):
        issues = pd.DataFrame([
            dict(issue_type="Story", story_points=value)
            for value in [10, "2.5", 1, 0, None, -3, "bad", float("inf"), True]
        ])
        distribution = story_point_distribution(issues)
        self.assertEqual(distribution["story_point_estimate"].tolist(),
                         ["0 points", "1 point", "2.5 points", "10 points", "Not estimated"])
        self.assertEqual(distribution["stories"].tolist(), [1, 1, 1, 1, 5])

    def test_empty_and_unestimated_story_scope(self):
        self.assertTrue(story_point_distribution(pd.DataFrame()).empty)
        self.assertTrue(story_point_distribution(pd.DataFrame([dict(issue_type="Bug", story_points=3)])).empty)
        self.assertEqual(story_point_distribution(pd.DataFrame([dict(issue_type="Story")])).to_dict("records"),
                         [{"story_point_estimate": "Not estimated", "stories": 1}])


if __name__ == "__main__":
    unittest.main()
