# TeamVishAI Jira Program Dashboard

A local, read-only Streamlit dashboard for Jira project `KAN`. It includes the executive summary, milestone timeline, Jira portfolio charts, and Finance & Accounting, Credit, and Collections views.

The program summary includes a weekly trend chart for open and completed Jira stories and bugs. Each **Refresh from Jira** saves a local snapshot in `data/weekly_work_snapshots.csv`, replacing that week's point with the latest counts. The trend fills in as you refresh in subsequent weeks; generated snapshots are excluded from source control.

The executive page also shows a proposed $10 million USD Phase 2 migration budget: $4 million for the implementation partner SOW through closure and hypercare, $4 million for 13 in-house resources, $1 million for compute/platform/support, and $1 million in contingency. Edit `program_budget.csv` to revise allocations or coverage.

The milestone chart is a phase timeline spanning September 20, 2026 through October 31, 2027. Phase start and end dates are maintained in `milestones.csv`.

## Run locally

Use Python 3.10 or later from this folder:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
streamlit run app.py
```

The executive summary and milestone timeline work without Jira credentials. Jira cards and charts remain empty until the OAuth connection is configured and refreshed.

## Jira OAuth setup

The Jira connection used in ChatGPT is separate from this standalone local app. For live project data, an Atlassian administrator must provide a registered OAuth 2.0 (3LO) integration for the organization. The app requests the `read:jira-work` and `offline_access` scopes only.

1. Copy `.streamlit/secrets.toml.example` to `.streamlit/secrets.toml`.
2. Add the OAuth client ID and secret supplied by the Atlassian administrator.
3. Configure the same callback URL in the registered integration as `redirect_uri` (default: `http://localhost:8501/`).
4. Restart the app and choose **Connect to Jira** in the sidebar.
5. Authorize the TeamVishAI Jira site and use **Refresh from Jira**.

Never commit `.streamlit/secrets.toml` or paste the client secret into chat. The app saves OAuth tokens in `data/jira_oauth_token.json` so the connection can be restored after a Streamlit rerun or restart. The `data/` directory is excluded from source control, and the token file is restricted to the current user on macOS/Linux. Use **Disconnect** in the sidebar to remove the saved token.

## Dashboard data rules

- Project: TeamVishAI, key `KAN`.
- Epic pages: Finance & Accounting `KAN-6`, Collection `KAN-7`, Credit `KAN-8`.
- Team custom field: `customfield_10001`.
- Stories assigned to `finance_accounting`, `credit`, or `collection` count as backlog stories.
- Stories assigned to `platform readiness` count as technical-debt stories; they are grouped under their parent team epic in team pages.
- Bugs are counted by Jira issue type `Bug` and grouped under their parent team epic.
- Milestones and executive summary are read from `milestones.csv` and `executive_summary.md`.

The dashboard reads issues visible to the connected Jira account. Refresh the data in the sidebar to get the latest Jira statuses and counts.
