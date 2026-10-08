"""Minimal read-only Jira Cloud OAuth and issue search client."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

import requests

AUTH_BASE = "https://auth.atlassian.com"
API_BASE = "https://api.atlassian.com"
TIMEOUT_SECONDS = 30
READ_SCOPES = "read:jira-work offline_access"


class JiraAPIError(RuntimeError):
    """An expected Jira HTTP or response error, safe to show in the UI."""


def authorization_url(client_id: str, redirect_uri: str, state: str) -> str:
    params = {
        "audience": "api.atlassian.com",
        "client_id": client_id,
        "scope": READ_SCOPES,
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


def fetch_project_issues(
    access_token: str,
    cloud_id: str,
    project_key: str,
    team_field: str = "customfield_10001",
) -> list[dict[str, Any]]:
    """Fetch every visible Jira issue in a project, following nextPageToken."""
    endpoint = f"{API_BASE}/ex/jira/{cloud_id}/rest/api/3/search/jql"
    fields = [
        "summary", "status", "issuetype", "parent", "components", "labels",
        "assignee", "priority", "created", "updated", "resolutiondate", "duedate", team_field,
    ]
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
        issues.extend(page.get("issues", []))
        next_token = page.get("nextPageToken")
        if page.get("isLast", not bool(next_token)) or not next_token:
            break
        payload["nextPageToken"] = next_token
    return issues
