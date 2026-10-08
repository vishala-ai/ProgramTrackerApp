# Weekly program readout setup

The weekly readout is designed to run every Friday at 4:00 PM Pacific and save a Markdown draft in `data/weekly_readouts/`. It reads Jira through the dashboard's local OAuth token and reads milestones, budget, and actuals from the local CSV files. It does not update Jira or publish outside this folder.

## Enable OpenAI commentary

Add this section to `.streamlit/secrets.toml` using your own API key locally. Do not commit that file or paste the key into chat.

```toml
[openai]
api_key = "YOUR_OPENAI_API_KEY"
model = "gpt-6-luna"
```

If no key is present, the job still saves the deterministic fact-based report without AI commentary.

## Install the Friday job on this Mac

The LaunchAgent definition is `launchd/com.teamvishai.weekly-readout.plist`. It uses this workspace's current absolute path, so update paths if the project moves. After the dashboard has connected to Jira at least once, install and start it with:

```sh
mkdir -p "$HOME/Library/LaunchAgents"
cp launchd/com.teamvishai.weekly-readout.plist "$HOME/Library/LaunchAgents/"
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.teamvishai.weekly-readout.plist"
```

The job runs while this macOS user is logged in. If the Mac is asleep at the scheduled time, launchd runs a missed calendar job after wake. Logs are written under `data/`; generated drafts appear in the dashboard's **Weekly Readout** tab after refresh.

To stop it:

```sh
launchctl bootout "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.teamvishai.weekly-readout.plist"
```

## Run once manually

From `PT-app`, run `./.venv/bin/python weekly_readout_job.py`. It refreshes the saved Jira token if needed and writes one readout draft.
