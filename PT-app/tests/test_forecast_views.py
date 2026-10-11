import copy
import unittest

from streamlit.testing.v1 import AppTest


BUNDLE = {
    "status": "available",
    "published": True,
    "label": "Simulated scenario",
    "milestone": "Unit testing validation",
    "as_of": "2026-10-11",
    "target_date": "2027-04-30",
    "scenarios": [{
        "id": "baseline", "label": "Baseline", "growth_fraction": 0,
        "additional_items": 0, "total_items": 220, "target_done_items": 201,
        "selection_policy": "priority_first", "p10_date": "2027-01-10",
        "p50_date": "2027-02-01", "p90_date": None,
        "target_attainment_pct": 88.2, "finished_trials": 94,
        "total_trials": 100, "censored_trials": 6,
    }],
    "assumptions": ["Team capacity remains at the recorded scenario level."],
    "warnings": ["This demonstration is not calibrated against real delivery outcomes."],
    "evidence": {
        "current_total_items": 220, "current_done_items": 15,
        "target_completion_pct": 91, "scope_growth_until": "2026-10-30",
        "earliest_attainment_date": "2027-01-10",
        "per_team": [{"team": "Credit", "capacity_basis": "assumed_capacity", "weekly_points": 12}],
    },
    "review": {"status": "passed", "passes": 1, "revisions": 0, "findings": []},
    "deterministic_validation": {"status": "passed", "findings": []},
    "model_version": "demo-v1", "seed": 42, "trials": 100,
}


class ForecastViewTests(unittest.TestCase):
    def run_app(self, bundle, *, repeat=False):
        script = (
            "from jira_dashboard.forecast_views import render_estimation_forecast\n"
            f"bundle = {bundle!r}\n"
            "render_estimation_forecast(bundle, key_prefix='unit-testing')\n"
        )
        if repeat:
            script += "render_estimation_forecast(bundle, key_prefix='second-view')\n"
        app = AppTest.from_string(script).run(timeout=15)
        self.assertEqual(len(app.exception), 0, list(app.exception))
        return app

    def scenario_tables(self, app):
        return [item.value for item in app.dataframe if "50% model date" in item.value.columns]

    def rendered_text(self, app):
        values = [item.value for kind in (app.markdown, app.caption, app.info, app.warning) for item in kind]
        values += [frame.value.to_string() for frame in app.dataframe]
        return "\n".join(values)

    def test_passed_available_output_has_conditional_table_and_missing_percentile(self):
        app = self.run_app(copy.deepcopy(BUNDLE))
        self.assertEqual(app.subheader[0].value, "Unit testing forecast")
        table = self.scenario_tables(app)[0]
        self.assertEqual(table.iloc[0]["50% model date"], "Feb 01, 2027")
        self.assertEqual(table.iloc[0]["90% model date"], "Beyond horizon / unavailable")
        self.assertEqual(table.iloc[0]["Trials reaching target by milestone"], "88.2%")
        self.assertEqual(table.iloc[0]["Unfinished trials"], "6 of 100")
        text = self.rendered_text(app)
        self.assertIn("not real delivery confidence", text)
        self.assertIn("UAT is a separate milestone", text)
        self.assertIn("not full backlog closure or acceptance sign-off", text)
        self.assertIn("Assumed capacity", text)
        self.assertEqual(len(app.success), 0)

    def test_incomplete_input_withholds_scenarios_and_model_dates(self):
        bundle = copy.deepcopy(BUNDLE)
        bundle.update(status="unavailable", published=False, reasons=["Capacity input is missing."])
        bundle["review"] = {"status": "not_requested", "passes": 0, "revisions": 0, "findings": []}
        app = self.run_app(bundle)
        self.assertEqual(self.scenario_tables(app), [])
        text = self.rendered_text(app)
        self.assertIn("Capacity input is missing", text)
        self.assertNotIn("2027-01-10", text)
        self.assertNotIn("Jan 10, 2027", text)
        self.assertNotIn("2027-02-01", text)

    def test_rejected_ai_review_withholds_dates_even_in_findings(self):
        bundle = copy.deepcopy(BUNDLE)
        bundle["published"] = False
        bundle["review"] = {"status": "rejected", "passes": 2, "revisions": 1,
                            "findings": ["Date 2027-02-01 is unsupported; Jan 10, 2027 is unsupported."]}
        app = self.run_app(bundle)
        self.assertEqual(self.scenario_tables(app), [])
        text = self.rendered_text(app)
        self.assertIn("AI evidence review rejected", text)
        self.assertIn("[withheld date]", text)
        self.assertNotIn("2027-02-01", text)
        self.assertNotIn("Jan 10, 2027", text)

    def test_automatic_run_without_ai_review_is_explicitly_unreviewed(self):
        bundle = copy.deepcopy(BUNDLE)
        bundle["review"] = {"status": "not_requested", "passes": 0, "revisions": 0, "findings": []}
        app = self.run_app(bundle)
        self.assertEqual(len(self.scenario_tables(app)), 1)
        self.assertTrue(any("Unreviewed simulation" in item.value for item in app.warning))
        self.assertIn("Not requested — unreviewed", self.rendered_text(app))
        self.assertFalse(any("review passed" in item.value for item in app.info))

    def test_ai_unavailable_can_show_published_output_with_unreviewed_warning(self):
        bundle = copy.deepcopy(BUNDLE)
        bundle["review"] = {"status": "unavailable", "passes": 1, "revisions": 0, "findings": ["Reviewer connection failed."]}
        app = self.run_app(bundle)
        self.assertEqual(len(self.scenario_tables(app)), 1)
        self.assertTrue(any("reviewer was unavailable" in item.value for item in app.warning))
        self.assertIn("Unavailable — unreviewed", self.rendered_text(app))

    def test_deterministic_rejection_is_distinct_from_ai_rejection(self):
        bundle = copy.deepcopy(BUNDLE)
        bundle["published"] = False
        bundle["deterministic_validation"] = {"status": "rejected", "findings": ["Model percentile ordering is invalid."]}
        bundle["review"] = {"status": "rejected", "passes": 0, "revisions": 0, "findings": []}
        app = self.run_app(bundle)
        self.assertEqual(self.scenario_tables(app), [])
        self.assertTrue(any("deterministic model checks rejected" in item.value for item in app.warning))
        self.assertFalse(any("AI evidence review rejected" in item.value for item in app.warning))
        self.assertIn("Model percentile ordering is invalid", self.rendered_text(app))
        self.assertIn("AI evidence review:** Not run", self.rendered_text(app))

    def test_explicit_rejection_suppresses_inconsistently_published_bundle(self):
        bundle = copy.deepcopy(BUNDLE)
        bundle["review"]["status"] = "rejected"
        app = self.run_app(bundle)
        self.assertEqual(self.scenario_tables(app), [])

    def test_multiple_panels_use_distinct_dataframe_keys(self):
        app = self.run_app(copy.deepcopy(BUNDLE), repeat=True)
        self.assertEqual(len(self.scenario_tables(app)), 2)


if __name__ == "__main__":
    unittest.main()
