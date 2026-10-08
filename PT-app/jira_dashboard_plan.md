# Jira Program Dashboard — Plan

## Purpose

Build a read-only dashboard for the transformation program. The dashboard will combine the program context and milestone plan with current work-item data from the Jira project. Jira remains the execution system; this application is a reporting view.

## Recommended implementation

- **Dashboard:** Streamlit, Python, and Plotly. This matches the existing Python dashboard approach in `Stock_app` and supports a quick local implementation.
- **Jira data:** Jira Cloud REST API, using JQL to retrieve the project issues and their relevant fields. Request only the fields needed by the dashboard and follow Jira's pagination model.
- **Authentication:** OAuth 2.0 authorization code flow (3LO) for a registered integration, with read-only Jira scopes and the signed-in user's Jira permissions. Do not ask the user to paste an API token into the app. Atlassian recommends OAuth 2.0 (3LO) for most direct Jira Cloud integrations and documents the registered-app flow in its [OAuth 2.0 guide](https://developer.atlassian.com/cloud/jira/service-desk/oauth-2-authorization-code-grants-3lo-for-apps/).
- **Refresh:** Provide a **Refresh from Jira** action and show the last successful sync time. Cache results briefly to reduce repeated API calls; show a clear message if Jira cannot be reached.
- **Access:** Keep the first version read-only. Jira's issue search requires the connected user to have Browse Projects access, and only issues visible to that user will be included. See Atlassian's [Jira issue search API](https://developer.atlassian.com/cloud/jira/platform/rest/v2/api-group-issue-search/).

## Dashboard layout

The app will have four pages: a program summary, followed by Finance & Accounting, Credit, and Collections.

### Page 1 — Program summary

1. **Executive summary:** Show the program purpose, Jira execution approach, reporting-app role, current highlights, and low-lights statement from `executive_summary.md`.
2. **Program timeline:** Show the milestone schedule from `milestones.csv` as a horizontal timeline/Gantt view, with milestone name, target date, and status. Initial dates:
   - Inventory completion — December 31, 2026
   - Conversion and overall analysis — March 31, 2027
   - Unit testing validation — April 30, 2027
   - Business UAT validation — June 30, 2027
   - Overall prod parallel — August 31, 2027
   - Overall closure and the cutover — October 31, 2027
3. **Program budget:** Show a proposed USD allocation by delivery component across the mid-September 2026–October 31, 2027 Phase 2 TD-to-GCP migration, with $4 million for implementation partner SOW, $4 million for 13 in-house resources, $1 million for compute/platform/support, and $1 million contingency. The proposed cap is $10 million. Show detailed components in a table and aggregate them into four broader buckets in a pie chart. Maintain allocations in `program_budget.csv`.
4. **Program KPIs:** Show total stories, stories by status, total bugs and bugs by status, and technical-debt stories by status.
5. **Overall backlog charts:** Use a stacked bar chart for issue type by status and a Team-field chart for distribution across Finance & Accounting, Credit, Collection, and platform readiness. A status chart can group the live Jira statuses into To do, In progress, and Done while retaining the original status in the issue table.
6. **Weekly work trend:** Show weekly snapshots of open and completed stories and bugs as two lines. Save snapshots locally on Jira refresh and replace the current week's point with the latest count.

### Page 2 — Finance & Accounting

Show Finance & Accounting story, bug, and technical-debt counts by status; charts by status and priority; the related epic(s); and a resource-to-story/bug mapping with a stacked workload chart by assignee. Include `platform readiness` work under this team when it belongs to the Finance & Accounting epic.

### Page 3 — Credit

Use the same layout for Credit, including its epic, status/priority charts, and resource-to-story/bug mapping with a stacked workload chart by assignee. Include `platform readiness` work under the Credit epic.

### Page 4 — Collections

Use the same layout for Collections, including its epic, status/priority charts, and resource-to-story/bug mapping with a stacked workload chart by assignee. Include `platform readiness` work under the Collections epic.

## Data definitions

- **Project scope:** TeamVishAI, Jira project key `KAN`, at `https://vishala-ai.atlassian.net` (Jira Cloud access verified).
- **Story count:** Jira issue type `Story`; epics and subtasks are not included in the story total.
- **Bug count:** Jira issue type `Bug`.
- **Team assignment:** Use Jira's **Team** custom field (`customfield_10001`) and the epic mapping below. Values seen on current work items include `finance_accounting`, `credit`, `collection`, and `platform readiness`.
- **Components:** KAN currently has no project components configured, and sampled issues have an empty Components field. The dashboard groups work by the Team field and does not depend on components.
- **Backlog vs. technical-debt classification:** Use the Team field to classify `platform readiness` as technical debt. Classify work assigned to `finance_accounting`, `credit`, or `collection` as team backlog. Report the two categories separately; platform-readiness work remains grouped under its parent team epic on the team detail pages.
- **Status:** Display the Jira status name in tables; map Jira status categories to To do, In progress, or Done in summary charts. The dashboard uses the status category returned by Jira.
- **Weekly work trend:** Save open and completed story/bug counts to `data/weekly_work_snapshots.csv` on refresh. The first point appears immediately; refresh in subsequent weeks to build the trend. The generated snapshot file is ignored by source control.
- **Milestone status:** Initially read from the local `milestones.csv`; later, optionally map milestones to Jira issues if the program wants Jira to be the sole source of milestone status.

## Jira project and epic mapping

| Team | Epic | Jira issue |
|---|---|---|
| Finance & Accounting | Finance Accounting | [KAN-6](https://vishala-ai.atlassian.net/browse/KAN-6) |
| Collection | Collection | [KAN-7](https://vishala-ai.atlassian.net/browse/KAN-7) |
| Credit | Credit | [KAN-8](https://vishala-ai.atlassian.net/browse/KAN-8) |

## Current Jira inventory check

Read access to the Atlassian site and project `KAN` was verified on October 4, 2026. The three mapped epics and the requested issue structure already exist: 10 functional stories, 10 platform-readiness stories, and 5 bugs under each team epic (75 child work items total). No duplicate issues were created during the check. Jira returned zero configured project components; current story examples use the Team custom field instead. The project also contains five unrelated Jira onboarding tasks (`KAN-1` through `KAN-5`).

## Implementation status

1. **Jira setup verified:** The Team field values, three epics, and project status categories were inspected. `platform readiness` is classified as technical debt; `finance_accounting`, `credit`, and `collection` are functional backlog teams.
2. **Dashboard implementation complete:** `app.py` provides the program summary and three team pages, with milestone timeline, Jira issue charts, and linked work-item tables.
3. **Read-only Jira adapter complete:** OAuth authorization, refresh-token handling, accessible-site selection, paginated issue search, and issue normalization are implemented.
4. **Connection setup pending:** A standalone local app needs the organization's registered OAuth client ID and secret in `.streamlit/secrets.toml`. The connected Jira session in ChatGPT does not provide those app credentials.
5. **Live validation pending connection:** After OAuth is configured, refresh the app and compare its counts against Jira for project `KAN`.

## Decisions recorded

- Use the Jira Team field as the source for team grouping and backlog-versus-technical-debt classification. No Jira components are needed for this dashboard.
- Keep milestone dates and status in `milestones.csv` for the first version.
- First version runs locally for one user. A hosted stakeholder version would need a deployment and OAuth callback configured for that host.
