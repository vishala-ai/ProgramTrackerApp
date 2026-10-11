# Jira simulation job setup

The job simulates KAN **Story** and **Bug** issues. It uses a separate OAuth token so the Streamlit dashboard remains read-only. The schedule targets at least 91% of all Stories and Bugs in Done by April 30, 2027; already-Done issues count toward that percentage.

## Independent daily schedule

The simulator is independent of the weekly readout. It has its own `com.teamvishai.jira-simulation` LaunchAgent, runs every day at 4:00 PM Pacific, and reads its own saved plan and audit log under `data/jira_simulation/`. It does not require the weekly job, its Friday timer, or a weekly report snapshot. The weekly readout has a different LaunchAgent and only saves a report draft; it makes no Jira changes.

## 1. Create a dedicated simulation OAuth app

Keep the dashboard OAuth app read-only. In the [Atlassian Developer Console](https://developer.atlassian.com/), create a separate **OAuth 2.0 integration** for the simulator. In the new app:

1. Under **Authorization → OAuth 2.0 (3LO) → Configure**, set and save this callback URL:

```text
http://localhost:8765/callback
```

2. Under **Permissions**, add the **Jira API** if needed, open its configuration, and add these **classic scopes**:
   - `read:jira-work`
   - `write:jira-work`
3. Save the permission changes and copy this new app's client ID and secret into the local secrets file as shown below.

The simulator requests `offline_access` in its OAuth authorization URL to receive a refresh token; you do not need to add it under the Jira API scopes. Ignore the Jira Service Management API notice because this job does not call Jira Service Management. Atlassian recommends classic scopes where available; this job requests `read:jira-work` and `write:jira-work`. The callback URL sent by the app must match the one configured for that OAuth app. The Jira account that authorizes it also needs permission to edit and transition KAN issues. After changing scopes, authorize the simulator again to grant consent. See Atlassian's [OAuth 2.0 (3LO) setup](https://developer.atlassian.com/cloud/jira/platform/oauth-2-3lo-apps/) and [Jira scope guidance](https://developer.atlassian.com/cloud/jira/platform/scopes-for-oauth-2-3LO-and-forge-apps/).

Add the non-secret simulation settings to `.streamlit/secrets.toml`:

```toml
[jira_simulation]
client_id = "YOUR_DEDICATED_SIMULATION_OAUTH_CLIENT_ID"
client_secret = "YOUR_DEDICATED_SIMULATION_OAUTH_CLIENT_SECRET"
project_key = "KAN"
redirect_uri = "http://localhost:8765/callback"
random_seed = 20261009
```

The simulator uses this separate OAuth app and token for Jira writes. It reuses `[jira].site_url` to select the Jira site and the dashboard's read-only token to build the plan. Never commit `.streamlit/secrets.toml` or paste client secrets into chat.

## 2. Generate or convert the local schedule

From the `PT-app` folder, run:

```sh
./.venv/bin/python jira_simulation_job.py --plan-only --replan-to-goal
```

This refreshes Story and Bug statuses through the read-only Jira token, calculates the 91% completion threshold, and creates `data/jira_simulation/schedule.json` with gradual daily transitions to meet the April 30, 2027 goal. Existing Done items count toward the threshold. The first transition is scheduled for the plan date; the active milestone remains **At Risk** because the target depends on consistent daily delivery. It also saves today's baseline program and team chart snapshots. The command backs up an existing unapplied plan and does not write to Jira.

If you only want to convert a saved schedule's cadence while preserving its existing assignments, use this command instead:

```sh
./.venv/bin/python jira_simulation_job.py --plan-only --replan-daily
```

This retimes the saved events across daily dates while preserving the existing story and milestone assignments, removes the startup delay, and keeps a local backup of the old schedule. It does not call Jira or transition issues. It is allowed only while all scheduled transitions remain pending. Review the printed count of days with planned movement and peak movements per day.

## 3. Authorize the writer

Run:

```sh
./.venv/bin/python jira_simulation_job.py --authorize
```

Approve the Jira permissions in the browser. If you previously authorized this app before changing its scopes, approve the updated consent prompt. The separate simulation refresh token is stored at `data/jira_simulation/oauth_token.json` with owner-only file permissions. It is excluded from Git.

## 4. Install the daily simulator LaunchAgent

Install this agent separately; installing or running the weekly readout is not required. This activates the simulator schedule independently.

```sh
mkdir -p "$HOME/Library/LaunchAgents" data/jira_simulation
cp launchd/com.teamvishai.jira-simulation.plist "$HOME/Library/LaunchAgents/"
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.teamvishai.jira-simulation.plist"
```

The job runs every day at 4:00 PM Pacific and applies at most five due transitions per run, with no more than one transition per issue in a run. It also saves a daily program snapshot and daily per-team status snapshots after each run, even on days without a transition. If the Mac is asleep, `launchd` runs the missed calendar job after wake while the user is logged in.

Review `data/jira_simulation/schedule.json`, `audit.jsonl`, and `jira_simulation_job.stdout.log` after each run.

The dashboard's **Daily Velocity** tab shows current Story/Bug counts by Jira status and daily status history. It is actor-neutral and does not depend on the simulator audit to count backlog status. The simulator saves a daily status snapshot automatically. While the dashboard is open, PT-app checks for a newer snapshot every minute and fetches the latest Jira state across the pages. It also detects changes to `milestones.csv`, `program_budget.csv`, `program_actuals.csv`, and `executive_summary.md` and rerenders the dashboard. Direct Jira edits that do not produce a simulator snapshot require **Refresh from Jira**.

To stop future runs:

```sh
launchctl bootout "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.teamvishai.jira-simulation.plist"
```

Stopping the job does not undo issue transitions that have already happened. Those KAN updates are marked `SIMULATION` and remain in Jira.
