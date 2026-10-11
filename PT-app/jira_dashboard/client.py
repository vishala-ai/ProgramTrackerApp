"""Jira Cloud OAuth and issue search helpers."""

from __future__ import annotations

from math import isfinite
from typing import Any
from urllib.parse import urlencode

import requests

AUTH_BASE = "https://auth.atlassian.com"
API_BASE = "https://api.atlassian.com"
TIMEOUT_SECONDS = 30
READ_SCOPES = "read:jira-work offline_access"
WRITE_SCOPES = "read:jira-work write:jira-work offline_access"


class JiraAPIError(RuntimeError):
    """An expected Jira HTTP or response error, safe to show in the UI."""


def authorization_url(
    client_id: str,
    redirect_uri: str,
    state: str,
    scopes: str = READ_SCOPES,
) -> str:
    params = {
        "audience": "api.atlassian.com",
        "client_id": client_id,
        "scope": scopes,
        "redirect_uri": redirect_uri,
        "state": state,
        "response_type": "code",
        "prompt": "consent",
    }
    return f"{AUTH_BASE}/authorize?{urlencode(params)}"


def _raise_http_error(response: requests.Response, action: str) -> None:
    if response.ok:
        return
    try:
        detail = response.json().get("error_description") or response.json().get("message")
    except (ValueError, AttributeError):
        detail = ""
    if not detail:
        detail = response.text[:400]
    raise JiraAPIError(f"{action} failed (HTTP {response.status_code}). {detail}")


def exchange_authorization_code(code: str, client_id: str, client_secret: str, redirect_uri: str) -> dict[str, Any]:
    response = requests.post(
        f"{AUTH_BASE}/oauth/token",
        json={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "redirect_uri": redirect_uri,
        },
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        timeout=TIMEOUT_SECONDS,
    )
    _raise_http_error(response, "Jira authorization")
    return response.json()


def refresh_access_token(refresh_token: str, client_id: str, client_secret: str) -> dict[str, Any]:
    response = requests.post(
        f"{AUTH_BASE}/oauth/token",
        json={
            "grant_type": "refresh_token",
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
        },
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        timeout=TIMEOUT_SECONDS,
    )
    _raise_http_error(response, "Jira token refresh")
    return response.json()


def fetch_accessible_sites(access_token: str) -> list[dict[str, Any]]:
    response = requests.get(
        f"{API_BASE}/oauth/token/accessible-resources",
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        timeout=TIMEOUT_SECONDS,
    )
    _raise_http_error(response, "Jira site lookup")
    resources = response.json()
    jira_resources = []
    for resource in resources:
        scopes = resource.get("scopes", [])
        products = resource.get("products", [])
        has_jira_scope = any("jira" in str(scope).lower() for scope in scopes)
        has_jira_product = any(
            isinstance(product, dict) and str(product.get("id", "")).lower() == "jira"
            for product in products
        )
        if has_jira_scope or has_jira_product:
            jira_resources.append({
                "id": resource["id"],
                "url": resource.get("url", ""),
                "name": resource.get("name", ""),
            })
    return jira_resources


def fetch_story_point_fields(access_token: str, cloud_id: str) -> list[str]:
    """Discover estimate fields by metadata; IDs differ between Jira sites.

    Story Points and Story point estimate can coexist for different project types.
    Request both, preferring Story Points when an issue has values in both fields.
    This optional lookup must not prevent loading the rest of the dashboard.
    """
    try:
        response = requests.get(
            f"{API_BASE}/ex/jira/{cloud_id}/rest/api/3/field",
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            timeout=TIMEOUT_SECONDS,
        )
        _raise_http_error(response, "Jira estimate-field lookup")
        metadata = response.json()
    except (JiraAPIError, requests.RequestException, ValueError):
        return []
    if not isinstance(metadata, list):
        return []
    matches: dict[str, int] = {}
    names = {"story points": 0, "story point estimate": 1}
    for field in metadata:
        if not isinstance(field, dict):
            continue
        name = " ".join(str(field.get("name", "")).casefold().split())
        field_id = field.get("id")
        schema = field.get("schema") or {}
        if (
            name in names
            and isinstance(field_id, str)
            and field_id
            and isinstance(schema, dict)
            and schema.get("type") in (None, "number")
        ):
            matches[field_id] = names[name]
    return sorted(matches, key=lambda field_id: (matches[field_id], field_id))


def _story_point_value(fields: dict[str, Any], estimate_fields: list[str]) -> float | None:
    """Keep unestimated work distinct from a genuine zero-point estimate."""
    for field_id in estimate_fields:
        value = fields.get(field_id)
        if value is None or isinstance(value, bool):
            continue
        try:
            numeric = float(value)
        except (TypeError, ValueError, OverflowError):
            continue
        if isfinite(numeric) and numeric >= 0:
            return numeric
    return None


def fetch_project_issues(
    access_token: str,
    cloud_id: str,
    project_key: str,
    team_field: str = "customfield_10001",
) -> list[dict[str, Any]]:
    """Fetch project issues with optional estimates, following nextPageToken."""
    endpoint = f"{API_BASE}/ex/jira/{cloud_id}/rest/api/3/search/jql"
    estimate_fields = fetch_story_point_fields(access_token, cloud_id)
    fields = [
        "summary", "status", "issuetype", "parent", "components", "labels",
        "assignee", "priority", "created", "updated", "resolutiondate", "duedate", team_field,
    ]
    fields = list(dict.fromkeys([*fields, *estimate_fields]))
    payload: dict[str, Any] = {
        "jql": f"project = {project_key} ORDER BY created ASC",
        "fields": fields,
        "maxResults": 100,
    }
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    issues: list[dict[str, Any]] = []
    while True:
        response = requests.post(endpoint, json=payload, headers=headers, timeout=TIMEOUT_SECONDS)
        _raise_http_error(response, "Jira issue search")
        page = response.json()
        for issue in page.get("issues", []):
            # Preserve the list-of-raw-issues API used by the app and scheduled jobs.
            # The private alias also survives JSON/session caches without metadata.
            issue["_story_points"] = _story_point_value(issue.get("fields") or {}, estimate_fields)
            issues.append(issue)
        next_token = page.get("nextPageToken")
        if page.get("isLast", not bool(next_token)) or not next_token:
            break
        payload["nextPageToken"] = next_token
    return issues
