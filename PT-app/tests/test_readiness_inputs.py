import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from jira_dashboard.readiness_inputs import CONFIRMATION_KEYS, load_estimation_inputs


class ReadinessInputsTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "estimation_inputs.json"

    def write(self, value):
        self.path.write_text(json.dumps(value), encoding="utf-8")

    def valid(self, **overrides):
        return {
            "project_key": "KAN", "data_mode": "simulation",
            "confirmations": {
                "dependencies": {
                    "confirmed": False, "evidence": "", "owner": "Workstream lead",
                },
            },
        } | overrides

    def assert_invalid(self, value, *, project_key="KAN"):
        self.write(value)
        result = load_estimation_inputs(self.path, project_key=project_key)
        self.assertEqual(result["data_mode"], "simulation" if project_key == "KAN" else "unknown")
        self.assertEqual(result["confirmations"], {})
        self.assertTrue(result["error"])
        self.assertNotIn("milestone_scenario", result)

    def test_missing_file_uses_project_default_without_error(self):
        self.assertEqual(load_estimation_inputs(self.path), {
            "data_mode": "simulation", "confirmations": {}, "error": None,
        })
        self.assertEqual(load_estimation_inputs(self.path, project_key="REAL"), {
            "data_mode": "unknown", "confirmations": {}, "error": None,
        })

    def test_valid_modes_and_all_confirmations_preserve_declarations(self):
        confirmations = {
            key: {"confirmed": True, "evidence": "Reviewed plan §2", "owner": "TPM"}
            for key in CONFIRMATION_KEYS
        }
        for mode in ("simulation", "real", "unknown"):
            with self.subTest(mode=mode):
                self.write(self.valid(data_mode=mode, confirmations=confirmations))
                self.assertEqual(load_estimation_inputs(self.path), {
                    "data_mode": mode, "confirmations": confirmations, "error": None,
                })

    def test_confirmation_subset_or_empty_is_valid(self):
        for confirmations in ({}, self.valid()["confirmations"]):
            self.write(self.valid(confirmations=confirmations))
            result = load_estimation_inputs(self.path)
            self.assertIsNone(result["error"])
            self.assertEqual(result["confirmations"], confirmations)

    def test_legacy_input_preserves_original_result_shape(self):
        self.write(self.valid())
        self.assertEqual(load_estimation_inputs(self.path), {
            "data_mode": "simulation",
            "confirmations": self.valid()["confirmations"],
            "error": None,
        })

    def test_valid_scenario_preserves_threshold_and_inventory_closure_date(self):
        scenario = {"target_completion_pct": 91, "scope_growth_until": "2026-10-30"}
        self.write(self.valid(milestone_scenario=scenario))
        self.assertEqual(load_estimation_inputs(self.path), {
            "data_mode": "simulation", "confirmations": self.valid()["confirmations"],
            "error": None, "milestone_scenario": scenario,
        })

    def test_valid_scenario_accepts_fractional_threshold_and_upper_boundary(self):
        for threshold in (0.01, 91.5, 100):
            with self.subTest(threshold=threshold):
                scenario = {"target_completion_pct": threshold, "scope_growth_until": "2028-02-29"}
                self.write(self.valid(milestone_scenario=scenario))
                result = load_estimation_inputs(self.path)
                self.assertIsNone(result["error"])
                self.assertEqual(result["milestone_scenario"], scenario)

    def test_forecast_settings_are_normalized_and_optional(self):
        self.write(self.valid(forecast={"enabled": True, "trials": 100}))
        result = load_estimation_inputs(self.path)
        self.assertIsNone(result["error"])
        self.assertTrue(result["forecast"]["enabled"])
        self.assertEqual(result["forecast"]["trials"], 100)
        self.assertEqual(result["forecast"]["growth_fractions"], [0, .10, .25])

    def test_invalid_forecast_discards_confirmations_and_scenario(self):
        for value in (None, [], {"enabled": "yes"}, {"trials": True},
                      {"trials": 99}, {"trials": 2001}, {"seed": -1},
                      {"growth_fractions": [.1]}, {"weekly_capacity_cv": float("nan")},
                      {"unknown": 1}):
            with self.subTest(value=value):
                self.assert_invalid(self.valid(
                    milestone_scenario={"target_completion_pct": 91, "scope_growth_until": "2026-10-30"},
                    forecast=value,
                ))
                self.assertNotIn("forecast", load_estimation_inputs(self.path))

    def test_scenario_requires_exact_keys_and_an_object(self):
        scenario = {"target_completion_pct": 91, "scope_growth_until": "2026-10-30"}
        invalid_values = [None, [], True, 91, {}, scenario | {"extra": "unexpected"}]
        for key in scenario:
            missing = scenario.copy()
            del missing[key]
            invalid_values.append(missing)
        for value in invalid_values:
            with self.subTest(value=value):
                self.assert_invalid(self.valid(milestone_scenario=value))

    def test_invalid_scenario_threshold_discards_all_declarations(self):
        for threshold in (
            0, -1, 100.1, 10 ** 1000, True, False, "91", None, [], {},
            float("nan"), float("inf"), float("-inf"),
        ):
            with self.subTest(threshold=threshold):
                scenario = {"target_completion_pct": threshold, "scope_growth_until": "2026-10-30"}
                self.assert_invalid(self.valid(data_mode="real", milestone_scenario=scenario))

    def test_scenario_closure_date_requires_strict_valid_calendar_date(self):
        for closure_date in (
            "2026-2-03", "2026-10-3", "26-10-30", "20261030", "2026-10-30T00:00:00",
            "2026-10-30 ", " 2026-10-30", "2026-02-29", "2026-13-01", "2026-10-32",
            "0000-10-30", "2026-00-10", "2026-10-00", "٢٠٢٦-١٠-٣٠", None, 20261030, True,
        ):
            with self.subTest(closure_date=closure_date):
                scenario = {"target_completion_pct": 91, "scope_growth_until": closure_date}
                self.assert_invalid(self.valid(milestone_scenario=scenario))

    def test_project_mismatch_and_non_string_keys_fail_closed(self):
        for project in ("OTHER", "kan", "KAN ", None, 123):
            self.assert_invalid(self.valid(project_key=project))
        self.assert_invalid(self.valid(data_mode="real"), project_key="OTHER")

    def test_top_level_and_required_keys_are_strict(self):
        for value in (None, [], "text", 1, {}, {"data_mode": "real"}, self.valid(extra=True)):
            self.assert_invalid(value)
        for key in ("project_key", "data_mode", "confirmations"):
            value = self.valid()
            del value[key]
            self.assert_invalid(value)

    def test_invalid_modes_and_confirmation_maps_fail_closed(self):
        for mode in ("REAL", "production", None, True, [], {}):
            self.assert_invalid(self.valid(data_mode=mode))
        for confirmations in (None, [], "yes", True, {"unknown": {}}):
            self.assert_invalid(self.valid(confirmations=confirmations))

    def test_confirmation_values_require_exact_schema_and_types(self):
        confirmation = self.valid()["confirmations"]["dependencies"]
        invalid_values = [None, [], True, {}, confirmation | {"extra": "unexpected"}]
        for key in ("confirmed", "evidence", "owner"):
            missing = confirmation.copy()
            del missing[key]
            invalid_values.append(missing)
        invalid_values.extend(confirmation | {"confirmed": value} for value in (0, 1, "true", None))
        for key in ("evidence", "owner"):
            invalid_values.extend(confirmation | {key: value} for value in (None, 1, [], True))
        for invalid_value in invalid_values:
            self.assert_invalid(self.valid(confirmations={"dependencies": invalid_value}))

    def test_one_invalid_confirmation_discards_other_valid_evidence(self):
        confirmations = self.valid()["confirmations"] | {
            "capacity": {"confirmed": True, "evidence": "Roster approved", "owner": "Lead"},
            "model_validation": {"confirmed": "yes", "evidence": "", "owner": "TPM"},
        }
        self.assert_invalid(self.valid(data_mode="real", confirmations=confirmations))

    def test_invalid_json_duplicate_keys_and_encoding_fail_closed(self):
        for content in (
            "{", "", '{"project_key":"KAN","project_key":"OTHER","data_mode":"real","confirmations":{}}',
            '{"project_key":"KAN","data_mode":"real","confirmations":{"capacity":{"confirmed":false,"confirmed":true,"evidence":"Roster","owner":"Lead"}}}',
        ):
            self.path.write_text(content, encoding="utf-8")
            result = load_estimation_inputs(self.path)
            self.assertEqual(result["data_mode"], "simulation")
            self.assertEqual(result["confirmations"], {})
            self.assertTrue(result["error"])
        self.path.write_bytes(b"\xff")
        self.assertTrue(load_estimation_inputs(self.path)["error"])

    def test_unreadable_file_reports_error_instead_of_missing_file_defaults(self):
        self.path.mkdir()
        result = load_estimation_inputs(self.path)
        self.assertEqual(result["data_mode"], "simulation")
        self.assertEqual(result["confirmations"], {})
        self.assertTrue(result["error"])

    def test_shipped_example_is_safe_and_matches_allowed_confirmation_keys(self):
        example = Path(__file__).resolve().parents[1] / "estimation_inputs.example.json"
        result = load_estimation_inputs(example)
        self.assertIsNone(result["error"])
        self.assertEqual(result["data_mode"], "simulation")
        self.assertEqual(set(result["confirmations"]), CONFIRMATION_KEYS)
        self.assertTrue(all(value["confirmed"] is False for value in result["confirmations"].values()))
        self.assertTrue(all(value["evidence"] == "" for value in result["confirmations"].values()))
        self.assertEqual(result["milestone_scenario"], {
            "target_completion_pct": 91, "scope_growth_until": "2026-10-30",
        })


if __name__ == "__main__":
    unittest.main()
