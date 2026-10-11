from copy import deepcopy
from datetime import date
import json
import unittest

import pandas as pd

from jira_dashboard.forecast import DEFAULT_FORECAST_OPTIONS, build_simulated_forecast, validate_forecast_options


class SimulatedForecastTests(unittest.TestCase):
    def setUp(self):
        self.now = pd.Timestamp("2026-10-11T19:00:00Z")
        self.milestones = pd.DataFrame([{
            "milestone": "Unit testing validation", "start_date": "2026-10-01", "due_date": "2027-04-30",
        }])
        self.inputs = {
            "data_mode": "simulation",
            "milestone_scenario": {"target_completion_pct": 91, "scope_growth_until": "2026-10-01"},
            "confirmations": {
                key: {"confirmed": True, "owner": "TPM", "evidence": "User-confirmed simulation assumption"}
                for key in ("milestone_scope", "dependencies", "capacity", "remaining_work", "testing_rework")
            },
            "forecast": {"enabled": True, "trials": 100, "growth_fractions": [0], "weekly_capacity_cv": 0},
        }

    def item(self, **overrides):
        return {
            "issue_type": "Story", "team": "Credit", "status_category": "To do",
            "story_points": 4, "priority": "Medium", "assignee": "Engineer", "resolved": None,
        } | overrides

    def scope(self, total=10, done=1, open_points=4):
        return pd.DataFrame([
            self.item(status_category="Done", story_points=4, resolved=self.now - pd.Timedelta(days=1))
            if index < done else self.item(story_points=open_points)
            for index in range(total)
        ])

    def build(self, issues=None, *, inputs=None, milestones=None, overrides=None):
        return build_simulated_forecast(
            self.scope() if issues is None else issues,
            self.milestones if milestones is None else milestones, self.now,
            estimation_inputs=self.inputs if inputs is None else inputs, overrides=overrides,
        )

    def test_default_disabled_and_option_validation_is_strict(self):
        self.assertEqual(validate_forecast_options({}), DEFAULT_FORECAST_OPTIONS)
        copied = validate_forecast_options({})
        copied["growth_fractions"].append(0.5)
        self.assertEqual(DEFAULT_FORECAST_OPTIONS["growth_fractions"], [0, 0.1, 0.25])
        self.assertEqual(self.build(inputs={})["status"], "disabled")
        invalid = [None, [], True, {"extra": True}, {"enabled": "yes"}]
        for key in ("seed", "trials", "horizon_weeks"):
            invalid.extend({key: value} for value in (True, 1.0, -1, 10 ** 1000))
        invalid.extend({"growth_fractions": value} for value in (
            [], [0.1], [0, 0], [0, True], [0, -0.1], [0, 1.1], [0, float("nan")],
            [0, 0.1, 0.2, 0.3, 0.4, 0.5], "0,0.1",
        ))
        for key in ("weekly_capacity_cv", "fallback_points_per_person_week"):
            invalid.extend({key: value} for value in (True, None, -1, float("inf"), float("nan")))
        invalid.extend([{"weekly_capacity_cv": 1.1}, {"fallback_points_per_person_week": 0}, {"fallback_points_per_person_week": 101}])
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    validate_forecast_options(value)

    def test_seed_reproduces_json_safe_results_without_mutating_inputs(self):
        inputs = deepcopy(self.inputs)
        inputs["forecast"]["weekly_capacity_cv"] = 0.35
        original = deepcopy(inputs)
        first, second = self.build(inputs=inputs), self.build(inputs=inputs)
        self.assertEqual(first, second)
        self.assertEqual(inputs, original)
        self.assertEqual(first["status"], "available")
        self.assertEqual(first["label"], "Simulated scenario")
        self.assertEqual(first["milestone"], "Unit testing validation")
        json.dumps(first, allow_nan=False)

    def test_larger_remaining_estimates_delay_matching_item_counts(self):
        small = self.build(self.scope(open_points=4))
        large = self.build(self.scope(open_points=12))
        self.assertEqual(small["evidence"]["current_total_items"], large["evidence"]["current_total_items"])
        self.assertLess(small["scenarios"][0]["p50_date"], large["scenarios"][0]["p50_date"])
        self.assertEqual(small["evidence"]["per_team"][0]["weekly_points"], 4)
        self.assertEqual(large["evidence"]["per_team"][0]["weekly_points"], 4)

    def test_growth_recomputes_total_and_target_without_adding_existing_hundred_again(self):
        inputs = deepcopy(self.inputs)
        inputs["milestone_scenario"]["scope_growth_until"] = "2026-10-30"
        inputs["forecast"]["growth_fractions"] = [0, 0.1, 0.25]
        result = self.build(self.scope(total=100, done=20), inputs=inputs)
        scenarios = result["scenarios"]
        self.assertEqual(len(scenarios), 6)
        self.assertEqual([row["additional_items"] for row in scenarios[::2]], [0, 10, 25])
        self.assertEqual([row["total_items"] for row in scenarios[::2]], [100, 110, 125])
        self.assertEqual([row["target_done_items"] for row in scenarios[::2]], [91, 101, 114])
        self.assertTrue(all(row["p10_date"] >= "2026-10-30" for row in scenarios))

    def test_target_is_ninety_one_not_full_completion_and_done_needs_no_effort(self):
        issues = self.scope(total=100, done=91, open_points=10 ** 6)
        issues.loc[:89, "story_points"] = None
        result = self.build(issues)
        self.assertEqual(result["status"], "available")
        self.assertEqual(result["evidence"]["current_done_items"], 91)
        self.assertEqual(result["scenarios"][0]["target_done_items"], 91)
        self.assertEqual(result["scenarios"][0]["p50_date"], "2026-10-11")
        self.assertEqual(result["scenarios"][0]["target_attainment_pct"], 100)
        self.assertTrue(any("Done is final" in line for line in result["assumptions"]))

    def test_residual_selection_policy_changes_finish_when_points_differ(self):
        issues = pd.DataFrame([
            self.item(status_category="Done", story_points=4, resolved=self.now - pd.Timedelta(days=1))
            for _ in range(90)
        ] + [self.item(story_points=1)] * 9 + [self.item(story_points=1000)])
        result = self.build(issues)
        larger, smaller = result["scenarios"]
        self.assertEqual(larger["selection_policy"], "priority_larger_first")
        self.assertEqual(smaller["selection_policy"], "priority_smaller_first")
        self.assertGreater(larger["p50_date"], smaller["p50_date"])
        self.assertEqual(larger["target_done_items"], 91)

    def test_missing_open_points_are_imputed_only_in_model_and_disclosed(self):
        issues = pd.DataFrame([
            self.item(story_points=1), self.item(story_points=3), self.item(story_points=8),
            self.item(story_points=None), self.item(story_points=True),
        ])
        original = issues.copy(deep=True)
        result = self.build(issues)
        self.assertEqual(result["status"], "available")
        self.assertEqual(result["evidence"]["unestimated_open_items"], 2)
        self.assertEqual(result["evidence"]["imputed_open_items"], 2)
        self.assertEqual(result["evidence"]["per_team"][0]["imputation_points"], 8)
        self.assertTrue(any("model-only imputation" in line for line in result["warnings"]))
        pd.testing.assert_frame_equal(issues, original)
        issues["story_points"] = None
        unavailable = self.build(issues)
        self.assertEqual(unavailable["status"], "unavailable")
        self.assertTrue(any("point scale cannot be invented" in line for line in unavailable["reasons"]))

    def test_capacity_fallback_is_explicit_and_uses_current_assigned_contributors(self):
        issues = pd.DataFrame([
            self.item(assignee="One"), self.item(assignee="Two"), self.item(assignee="Unassigned"),
        ])
        result = self.build(issues)
        team = result["evidence"]["per_team"][0]
        self.assertEqual(team["capacity_basis"], "assumed_capacity")
        self.assertEqual(team["current_assigned_contributors"], 2)
        self.assertEqual(team["weekly_points"], 16)
        self.assertTrue(any("not observed capacity" in line for line in result["warnings"]))
        issues["assignee"] = "Unassigned"
        self.assertEqual(self.build(issues)["evidence"]["per_team"][0]["modeled_contributors"], 1)

    def test_future_or_missing_resolution_dates_do_not_calibrate_capacity(self):
        issues = pd.DataFrame([
            self.item(status_category="Done", story_points=100, resolved=self.now + pd.Timedelta(days=1)),
            self.item(status_category="Done", story_points=100, resolved=None), self.item(story_points=4),
        ])
        result = self.build(issues)
        self.assertEqual(result["evidence"]["current_done_items"], 2)
        self.assertEqual(result["evidence"]["future_done_resolution_dates"], 1)
        self.assertEqual(result["evidence"]["missing_done_resolution_dates"], 1)
        self.assertEqual(result["evidence"]["per_team"][0]["weekly_points"], 8)

    def test_workstream_point_scales_stay_separate_and_capacity_is_not_pooled(self):
        issues = pd.DataFrame([
            self.item(team="A", status_category="Done", story_points=1, resolved=self.now),
            self.item(team="B", status_category="Done", story_points=100, resolved=self.now),
            self.item(team="A", story_points=100), self.item(team="B", story_points=100),
        ])
        result = self.build(issues)
        self.assertEqual([row["weekly_points"] for row in result["evidence"]["per_team"]], [1, 100])
        self.assertEqual(result["scenarios"][0]["p50_date"], (date(2026, 10, 11) + pd.Timedelta(weeks=100)).isoformat())
        scaled = issues.copy()
        scaled.loc[scaled["team"].eq("B"), "story_points"] *= 10
        self.assertEqual(result["scenarios"], self.build(scaled)["scenarios"])

    def test_horizon_censoring_keeps_all_trials_in_quantiles_and_attainment(self):
        issues = self.scope(open_points=1000)
        inputs = deepcopy(self.inputs)
        inputs["forecast"].update(horizon_weeks=30, weekly_capacity_cv=0.8)
        result = self.build(issues, inputs=inputs)
        scenario = result["scenarios"][0]
        self.assertEqual(scenario["finished_trials"], 0)
        self.assertEqual(scenario["censored_trials"], 100)
        self.assertIsNone(scenario["p10_date"])
        self.assertIsNone(scenario["p50_date"])
        self.assertIsNone(scenario["p90_date"])
        self.assertEqual(scenario["target_attainment_pct"], 0)
        inputs["forecast"].update(horizon_weeks=30, weekly_capacity_cv=1)
        partial = self.build(self.scope(open_points=12), inputs=inputs)["scenarios"][0]
        self.assertGreater(partial["finished_trials"], 0)
        self.assertLess(partial["finished_trials"], 100)
        self.assertEqual(partial["finished_trials"] + partial["censored_trials"], partial["total_trials"])
        if partial["finished_trials"] < 90:
            self.assertIsNone(partial["p90_date"])
        self.assertLessEqual(partial["target_attainment_pct"], partial["finished_trials"])

    def test_deadline_beyond_horizon_is_unavailable_instead_of_false_failure_rate(self):
        inputs = deepcopy(self.inputs)
        inputs["forecast"]["horizon_weeks"] = 26
        result = self.build(inputs=inputs)
        self.assertEqual(result["status"], "unavailable")
        self.assertTrue(any("Extend the simulation horizon" in line for line in result["reasons"]))

    def test_planned_start_gates_acceptance_and_passed_cutoff_adds_no_future_items(self):
        inputs = deepcopy(self.inputs)
        inputs["forecast"]["growth_fractions"] = [0, 0.25]
        milestones = self.milestones.copy()
        milestones["start_date"] = "2027-02-01"
        result = self.build(self.scope(total=100, done=91), inputs=inputs, milestones=milestones)
        self.assertTrue(all(row["additional_items"] == 0 for row in result["scenarios"]))
        self.assertTrue(all(row["p50_date"] == "2027-02-01" for row in result["scenarios"]))
        self.assertTrue(any("closure is unverified" in line for line in result["warnings"]))

    def test_inventory_plan_date_difference_is_explicit_and_does_not_change_growth_cutoff(self):
        inputs = deepcopy(self.inputs)
        inputs["milestone_scenario"]["scope_growth_until"] = "2026-10-30"
        milestones = pd.concat([self.milestones, pd.DataFrame([{
            "milestone": "Inventory completion", "start_date": "2026-09-20", "due_date": "2026-10-31",
        }])], ignore_index=True)
        result = self.build(inputs=inputs, milestones=milestones)
        self.assertEqual(result["evidence"]["scope_growth_until"], "2026-10-30")
        self.assertTrue(any("2026-10-30" in text and "2026-10-31" in text and "distinct dates" in text for text in result["warnings"]))

    def test_real_unknown_empty_unconfirmed_or_invalid_inputs_do_not_forecast(self):
        for mode in ("real", "unknown", None):
            inputs = deepcopy(self.inputs)
            inputs["data_mode"] = mode
            self.assertEqual(self.build(inputs=inputs)["status"], "unavailable")
        self.assertEqual(self.build(pd.DataFrame())["status"], "unavailable")
        self.assertEqual(self.build(pd.DataFrame([self.item(issue_type="Epic")]))["status"], "unavailable")
        for key in self.inputs["confirmations"]:
            inputs = deepcopy(self.inputs)
            inputs["confirmations"][key]["confirmed"] = False
            self.assertEqual(self.build(inputs=inputs)["status"], "unavailable")
        inputs = deepcopy(self.inputs)
        inputs["milestone_scenario"]["target_completion_pct"] = True
        self.assertEqual(self.build(inputs=inputs)["status"], "unavailable")
        self.assertEqual(self.build(milestones=pd.DataFrame())["status"], "unavailable")
        self.assertEqual(self.build(overrides={"seed": 42})["status"], "unavailable")
        self.assertEqual(self.build(overrides={"trials": 200})["trials"], 200)


if __name__ == "__main__":
    unittest.main()
