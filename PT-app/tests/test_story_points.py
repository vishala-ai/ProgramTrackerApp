import unittest
from unittest.mock import Mock, patch

import pandas as pd
import requests

from jira_dashboard.client import fetch_project_issues, fetch_story_point_fields
from jira_dashboard.data import normalize_issues


def response(payload, ok=True, status_code=200):
    result = Mock(ok=ok, status_code=status_code, text="Unavailable")
    result.json.return_value = payload
    return result


class StoryPointFetchTests(unittest.TestCase):
    def test_discovers_numeric_estimate_fields_by_name_without_assuming_ids(self):
        metadata = [
            {"id": "customfield_725", "name": "Story point estimate", "schema": {"type": "number"}},
            {"id": "customfield_310", "name": " Story  Points ", "schema": {"type": "number"}},
            {"id": "customfield_450", "name": "Story Points notes", "schema": {"type": "string"}},
            {"id": "customfield_550", "name": "Story Points", "schema": {"type": "string"}},
        ]
        with patch("jira_dashboard.client.requests.get", return_value=response(metadata)) as get:
            self.assertEqual(fetch_story_point_fields("test-token", "test-site"), ["customfield_310", "customfield_725"])
        self.assertTrue(get.call_args.args[0].endswith("/test-site/rest/api/3/field"))

    def test_fetch_requests_discovered_fields_on_every_page_and_keeps_zero(self):
        metadata = [
            {"id": "customfield_725", "name": "Story point estimate"},
            {"id": "customfield_310", "name": "Story Points"},
        ]
        pages = [
            response({"issues": [
                {"key": "TEST-1", "fields": {"customfield_310": 0, "customfield_725": 8}},
                {"key": "TEST-2", "fields": {"customfield_310": None, "customfield_725": 2.5}},
            ], "nextPageToken": "second-page", "isLast": False}),
            response({"issues": [{"key": "TEST-3", "fields": {}}], "isLast": True}),
        ]
        search_payloads = []

        def search(*args, **kwargs):
            search_payloads.append(dict(kwargs["json"]))
            return pages.pop(0)

        with patch("jira_dashboard.client.requests.get", return_value=response(metadata)), patch(
            "jira_dashboard.client.requests.post", side_effect=search
        ):
            issues = fetch_project_issues("test-token", "test-site", "TEST")
        self.assertEqual([issue["_story_points"] for issue in issues], [0.0, 2.5, None])
        self.assertNotIn("nextPageToken", search_payloads[0])
        self.assertEqual(search_payloads[1]["nextPageToken"], "second-page")
        for payload in search_payloads:
            self.assertIn("customfield_310", payload["fields"])
            self.assertIn("customfield_725", payload["fields"])
            self.assertIn("labels", payload["fields"])

    def test_optional_metadata_failure_does_not_prevent_issue_search(self):
        failures = [
            response({"message": "Not permitted"}, ok=False, status_code=403),
            response({"unexpected": "shape"}),
            requests.Timeout("timeout"),
        ]
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                get_options = {"side_effect": failure} if isinstance(failure, Exception) else {"return_value": failure}
                with patch("jira_dashboard.client.requests.get", **get_options), patch(
                    "jira_dashboard.client.requests.post",
                    return_value=response({"issues": [{"key": "TEST-1", "fields": {}}], "isLast": True}),
                ):
                    issues = fetch_project_issues("test-token", "test-site", "TEST")
                self.assertEqual(len(issues), 1)
                self.assertIsNone(issues[0]["_story_points"])


class StoryPointNormalizationTests(unittest.TestCase):
    def test_numeric_estimates_are_nullable_and_legacy_cached_issues_still_work(self):
        issues = [
            {"key": "TEST-1", "fields": {}, "_story_points": 0},
            {"key": "TEST-2", "fields": {}, "_story_points": 2.5},
            {"key": "TEST-3", "fields": {"story_points": "8"}},
            {"key": "TEST-4", "fields": {"customfield_999": 21}},
        ]
        result = normalize_issues(issues, project_key="TEST")
        self.assertEqual(str(result["story_points"].dtype), "Float64")
        self.assertEqual(result["story_points"].iloc[:3].tolist(), [0, 2.5, 8])
        self.assertTrue(pd.isna(result["story_points"].iloc[3]))
        self.assertEqual(result["project_key"].unique().tolist(), ["TEST"])

    def test_invalid_values_are_not_counted_as_estimates(self):
        issues = [
            {"key": f"TEST-{index}", "fields": {}, "_story_points": value}
            for index, value in enumerate([None, "invalid", -3, float("inf"), float("nan"), True])
        ]
        self.assertTrue(normalize_issues(issues)["story_points"].isna().all())

    def test_empty_issue_list_has_nullable_estimate_column(self):
        result = normalize_issues([])
        self.assertTrue(result.empty)
        self.assertEqual(str(result["story_points"].dtype), "Float64")

    def test_simulation_labels_are_retained_for_estimation_provenance(self):
        result = normalize_issues([
            {"key": "TEST-1", "fields": {"labels": ["SIMULATION", "demo"]}},
            {"key": "TEST-2", "fields": {"labels": ["real-work"]}},
            {"key": "TEST-3", "fields": {}},
        ])
        self.assertEqual(result["is_simulated"].tolist(), [True, False, False])


if __name__ == "__main__":
    unittest.main()
