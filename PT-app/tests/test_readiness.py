import unittest

import pandas as pd

from jira_dashboard.readiness import assess_estimation_readiness


class EstimationReadinessTests(unittest.TestCase):
    def setUp(self):
        self.now = pd.Timestamp("2026-10-11T19:00:00Z")
        self.milestones = pd.DataFrame([
            {"milestone": "Unit testing validation", "due_date": "2027-04-30"},
        ])
        self.issues = pd.DataFrame([
            dict(issue_type="Story", status_category="To do", story_points=3, team="Credit", resolved=None),
            dict(issue_type="Bug", status_category="In progress", story_points=5, team="Credit", resolved=None),
            dict(issue_type="Story", status_category="Done", story_points=3, team="Credit", resolved=self.now - pd.Timedelta(days=29)),
            dict(issue_type="Bug", status_category="Done", story_points=5, team="Credit", resolved=self.now - pd.Timedelta(days=1)),
        ])
        self.confirmations = {
            key: {"confirmed": True, "owner": "Example owner", "evidence": f"Reviewed evidence for {key}."}
            for key in (
                "milestone_scope", "dependencies", "capacity", "remaining_work", "testing_rework",
                "history_representative", "model_validation",
            )
        }

    def assess(self, issues=None, milestones=None, **kwargs):
        return assess_estimation_readiness(
            self.issues if issues is None else issues,
            self.milestones if milestones is None else milestones,
            self.now,
            **kwargs,
        )

    def row(self, result, label):
        return result["rows"].set_index("input").loc[label]

    def test_empty_data_never_implies_available_zero_scope_or_a_forecast(self):
        result = self.assess(issues=pd.DataFrame(), milestones=pd.DataFrame())
        self.assertEqual(result["status"], "Not ready")
        self.assertFalse(result["forecast_available"])
        self.assertEqual(result["counts"], {"available": 0, "total": 10, "blocking": 10})
        self.assertEqual(result["rows"].columns.tolist(), ["input", "status", "evidence", "owner", "next_step"])
        self.assertEqual(self.row(result, "Story-point estimate coverage")["status"], "Missing")
        self.assertEqual(self.row(result, "Data provenance")["status"], "Blocked")

    def test_complete_real_inputs_allow_model_setup_but_never_a_forecast(self):
        result = self.assess(data_mode="real", confirmations=self.confirmations)
        self.assertEqual(result["status"], "Ready for model setup")
        self.assertFalse(result["forecast_available"])
        self.assertEqual(result["blockers"], [])
        self.assertEqual(result["counts"], {"available": 10, "total": 10, "blocking": 0})
        scope = self.row(result, "Milestone scope and acceptance")
        self.assertEqual(scope["status"], "Owner-confirmed")
        self.assertIn("not machine-validated", scope["evidence"])
        history = self.row(result, "Completion timing and representative history")
        self.assertIn("actual observed date span 28 days", history["evidence"])
        self.assertIn("screening rule, not statistical adequacy", history["evidence"])
        validation = self.row(result, "Forecast validation and calibration")
        self.assertIn("validation/calibration plan", validation["evidence"])
        self.assertIn("not an implemented or validated forecast model", validation["evidence"])

    def test_owner_statements_cannot_override_simulated_or_unknown_provenance(self):
        for mode in ("simulation", "unknown", "typo", None):
            result = self.assess(data_mode=mode, confirmations=self.confirmations)
            self.assertEqual(result["status"], "Not ready")
            self.assertEqual(self.row(result, "Data provenance")["status"], "Blocked")
        marked = self.issues.assign(is_simulated=[False, False, True, False])
        result = self.assess(issues=marked, data_mode="real", confirmations=self.confirmations)
        self.assertEqual(self.row(result, "Data provenance")["status"], "Blocked")
        self.assertIn("is_simulated=True", self.row(result, "Data provenance")["evidence"])

    def test_simulation_marker_anywhere_in_dataset_prevents_a_real_readiness_claim(self):
        extra = pd.DataFrame([dict(issue_type="Epic", status_category="Done", is_simulated=True)])
        issues = pd.concat([self.issues.assign(is_simulated=False), extra], ignore_index=True)
        result = self.assess(issues=issues, data_mode="real", confirmations=self.confirmations)
        self.assertEqual(self.row(result, "Data provenance")["status"], "Blocked")

    def test_real_mode_removes_only_provenance_gate(self):
        result = self.assess(data_mode="real")
        self.assertEqual(result["status"], "Not ready")
        self.assertEqual(self.row(result, "Data provenance")["status"], "Available")
        self.assertEqual(self.row(result, "Capacity and leave")["status"], "Needs confirmation")
        self.assertEqual(result["counts"], {"available": 3, "total": 10, "blocking": 7})

    def test_truthy_flags_missing_owner_and_missing_evidence_do_not_confirm(self):
        invalid_records = [
            {"confirmed": "true", "owner": "Owner", "evidence": "Evidence"},
            {"confirmed": 1, "owner": "Owner", "evidence": "Evidence"},
            {"confirmed": False, "owner": "Owner", "evidence": "Evidence"},
            {"confirmed": True, "owner": " ", "evidence": "Evidence"},
            {"confirmed": True, "owner": "Owner", "evidence": " "},
            {"confirmed": True, "owner": None, "evidence": "Evidence"},
            {"confirmed": True, "owner": "Owner", "evidence": 123},
            True,
        ]
        for invalid in invalid_records:
            with self.subTest(record=invalid):
                confirmations = dict(self.confirmations, dependencies=invalid)
                result = self.assess(data_mode="real", confirmations=confirmations)
                self.assertEqual(result["status"], "Not ready")
                self.assertEqual(self.row(result, "Dependencies")["status"], "Needs confirmation")

    def test_explicit_owner_review_of_no_dependencies_is_accepted_as_statement(self):
        confirmations = dict(self.confirmations, dependencies={
            "confirmed": True, "owner": "Workstream lead", "evidence": "Reviewed scope; no external or predecessor dependencies.",
        })
        result = self.assess(data_mode="real", confirmations=confirmations)
        self.assertEqual(self.row(result, "Dependencies")["status"], "Owner-confirmed")
        self.assertIn("no external", self.row(result, "Dependencies")["evidence"])

    def test_all_confirmations_do_not_override_missing_estimates(self):
        for estimate in (None, True, -1, float("inf")):
            with self.subTest(estimate=estimate):
                issues = self.issues.astype({"story_points": object})
                issues.loc[0, "story_points"] = estimate
                result = self.assess(issues=issues, data_mode="real", confirmations=self.confirmations)
                self.assertEqual(result["status"], "Not ready")
                self.assertEqual(self.row(result, "Story-point estimate coverage")["status"], "Partial")

    def test_four_generated_windows_are_not_four_weeks_of_actual_history(self):
        issues = self.issues.copy()
        issues.loc[2, "resolved"] = self.now - pd.Timedelta(days=2)
        result = self.assess(issues=issues, data_mode="real", confirmations=self.confirmations)
        history = self.row(result, "Completion timing and representative history")
        self.assertEqual(history["status"], "Partial")
        self.assertIn("actual observed date span 1 days", history["evidence"])
        self.assertEqual(result["status"], "Not ready")

    def test_timing_gaps_and_future_dates_prevent_ready_status(self):
        for timestamp in (None, "invalid", self.now + pd.Timedelta(days=1)):
            issues = self.issues.astype({"resolved": object})
            issues.loc[3, "resolved"] = timestamp
            result = self.assess(issues=issues, data_mode="real", confirmations=self.confirmations)
            self.assertEqual(result["status"], "Not ready")
            self.assertEqual(self.row(result, "Completion timing and representative history")["status"], "Partial")
        for issues in (self.issues.drop(columns="resolved"), self.issues.assign(resolved=None)):
            result = self.assess(issues=issues, data_mode="real", confirmations=self.confirmations)
            self.assertEqual(self.row(result, "Completion timing and representative history")["status"], "Missing")

    def test_old_history_without_recent_completions_does_not_pass(self):
        issues = self.issues.copy()
        issues.loc[2, "resolved"] = self.now - pd.Timedelta(days=100)
        issues.loc[3, "resolved"] = self.now - pd.Timedelta(days=40)
        result = self.assess(issues=issues, data_mode="real", confirmations=self.confirmations)
        self.assertEqual(self.row(result, "Completion timing and representative history")["status"], "Partial")
        self.assertIn("Last 28 days: 0 known completions", self.row(result, "Completion timing and representative history")["evidence"])

    def test_history_needs_representative_owner_confirmation_even_with_dates(self):
        confirmations = dict(self.confirmations)
        confirmations.pop("history_representative")
        result = self.assess(data_mode="real", confirmations=confirmations)
        self.assertEqual(self.row(result, "Completion timing and representative history")["status"], "Needs confirmation")
        self.assertEqual(result["status"], "Not ready")

    def test_completed_effort_history_needs_recent_estimated_completions(self):
        issues = self.issues.copy()
        issues.loc[issues["status_category"].eq("Done"), "story_points"] = None
        result = self.assess(issues=issues, data_mode="real", confirmations=self.confirmations)
        self.assertEqual(self.row(result, "Story-point estimate coverage")["status"], "Available")
        history = self.row(result, "Completion timing and representative history")
        self.assertEqual(history["status"], "Partial")
        self.assertIn("Last 28 days: 1 known completions, 0 estimated", history["evidence"])
        self.assertIn("recent estimated completions", history["next_step"])
        self.assertEqual(result["status"], "Not ready")
        self.assertFalse(result["forecast_available"])

    def test_missing_invalid_conflicting_and_overdue_targets_need_review(self):
        candidates = [
            pd.DataFrame(), pd.DataFrame([{"milestone": "Unit testing validation"}]),
            pd.DataFrame([{"milestone": "Unit testing validation", "due_date": pd.NaT}]),
            pd.DataFrame([{"milestone": "Unit testing validation", "due_date": "invalid"}]),
            pd.DataFrame([{"milestone": "Unit testing validation", "due_date": "2026-10-10"}]),
            pd.DataFrame([
                {"milestone": "Unit testing validation", "due_date": "2027-04-30"},
                {"milestone": "Unit testing validation", "due_date": "2027-05-31"},
            ]),
        ]
        for milestones in candidates:
            result = self.assess(milestones=milestones, data_mode="real", confirmations=self.confirmations)
            self.assertEqual(result["status"], "Not ready")
            self.assertEqual(self.row(result, "Scope and milestone target")["status"], "Partial")

    def test_no_open_work_never_bypasses_acceptance_or_provides_a_finish_date(self):
        closed = self.issues.loc[self.issues["status_category"].eq("Done")]
        result = self.assess(issues=closed, data_mode="real")
        self.assertEqual(self.row(result, "Story-point estimate coverage")["status"], "Available")
        self.assertEqual(self.row(result, "Milestone scope and acceptance")["status"], "Needs confirmation")
        self.assertFalse(result["forecast_available"])
        ready = self.assess(issues=closed, data_mode="real", confirmations=self.confirmations)
        self.assertEqual(ready["status"], "Ready for model setup")
        self.assertFalse(ready["forecast_available"])

    def test_non_story_bug_scope_missing_columns_and_bad_confirmation_container_are_safe(self):
        for issues in (pd.DataFrame([{"issue_type": "Epic"}]), pd.DataFrame([{"priority": "High"}])):
            result = self.assess(issues=issues, data_mode="real", confirmations=[True])
            self.assertEqual(self.row(result, "Scope and milestone target")["status"], "Missing")
            self.assertEqual(self.row(result, "Story-point estimate coverage")["status"], "Missing")
        original = self.issues.copy(deep=True)
        self.assess(data_mode="real", confirmations=self.confirmations)
        pd.testing.assert_frame_equal(self.issues, original)

    def test_target_uses_pacific_calendar_date_and_zero_estimates_remain_valid(self):
        self.now = pd.Timestamp("2026-10-12T02:00:00Z")
        self.milestones.loc[0, "due_date"] = "2026-10-11"
        issues = self.issues.copy()
        issues["story_points"] = 0
        result = self.assess(issues=issues, data_mode="real", confirmations=self.confirmations)
        self.assertEqual(self.row(result, "Scope and milestone target")["status"], "Available")
        self.assertEqual(self.row(result, "Story-point estimate coverage")["status"], "Available")


if __name__ == "__main__":
    unittest.main()
