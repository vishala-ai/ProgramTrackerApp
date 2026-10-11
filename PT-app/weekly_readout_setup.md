# Weekly program readout setup

The Friday job runs at 4:00 PM Pacific and saves a Markdown draft plus a matching `.forecast.json` file in `data/weekly_readouts/`. It reads Jira through the dashboard's local OAuth token and loads milestones, budget, actuals, and optional estimation inputs locally. It does not update Jira or publish outside this folder. The daily Jira simulator runs independently.

## What runs when a draft is generated

1. Fetch and normalize current Jira issues and load local program inputs.
2. Run the enabled simulated Unit testing model with its configured seed, trials, scope-growth cases, and workstream effort assumptions.
3. Check model scope arithmetic, dates, schema, and other deterministic invariants.
4. Ask the configured AI forecast critic to approve, reject, or request one trial-count increase. A permitted rerun doubles trials up to 2,000, preserves inputs/seed, and receives deterministic checks plus one further critic review.
5. Build the fact-based report with core program status, scope, budget, milestone watchlist, highlights/lowlights, and actions. Include only a compact forecast summary with earliest/latest modeled completion dates, brief assumptions, and a note directing readers to **Schedule Deep Dive** for forecast details. Keep the allowed unfinished-work and scope-growth risks brief. A primary simulated median beyond the planned deadline adds a timing lowlight and action; calendar-derived milestone status stays separate.
6. Generate executive commentary as a status line, short summary, and one to five bullets. Its separate evidence/risk reviewer can request one commentary revision. Python rejects extra section headings or Markdown tables to keep commentary from duplicating report sections.
7. Save the Markdown draft and forecast replay/diagnostics JSON, then make both available in **Other Reports → Weekly Readout**.

Ordinary Schedule Deep Dive and Weekly Readout context previews run cached Python plus deterministic checks without AI. They use the last successful Jira refresh time and are explicitly unreviewed. Draft generation and the Friday job invoke the forecast critic before commentary review. AI never calculates forecast dates or changes business assumptions.

Schedule Deep Dive retains the full cases, effort and completion-history tables, capacity evidence, readiness gates, and review diagnostics. Weekly Markdown and the Weekly Readout context preview omit those details. The summary's earliest date is the earliest 10% model date across configured cases, and its latest date is the latest 90% model date. This is an early-to-conservative conditional range, not a guaranteed first or last completion date. If any case cannot produce a 90% date within the model horizon, the latest date remains unavailable. Brief assumptions cover the 91% item-count target, full open story-point effort, fixed independent teams at 100% allocation, configured growth through October 30, model-only assumptions for missing estimates, and Done as final with downstream UAT/business closure excluded. The complete forecast JSON remains available for download and replay.

| Review outcome | Saved behavior |
| --- | --- |
| Deterministic model checks fail | Model dates and trial fractions are withheld; the report retains facts and a brief unavailable status. Full diagnostics remain in the forecast JSON. |
| Forecast critic rejects, including after its one permitted rerun | Model dates are withheld; the report retains facts and a brief unavailable status. Review findings remain in the forecast JSON. |
| Forecast critic unavailable or returns an invalid response | Valid Python outputs remain labeled unreviewed; no AI approval is claimed. |
| Forecast critic passes | The conditional earliest/latest date summary can appear; the full review record remains in the forecast JSON. Passing checks consistency, not real-world accuracy. |
| Commentary unavailable or fails after its one revision | Save the report without generated commentary; independently valid model results retain their own review status. |

## Scenario and local configuration

The approved KAN runtime scenario includes all Stories/Bugs and technical debt, targets at least 91% Done by item count by April 30, 2027, and names the TPM as owner. Done is final for this stage; UAT, business acceptance, and overall closure are separate. The up-to-9% unfinished allowance is an explicit weekly schedule risk. The 100 Stories KAN-244 through KAN-343 are already in current scope. Further arrivals through the October 30, 2026 growth cutoff remain unquantified; model growth fractions are sensitivities, not predicted demand.

The CSV Inventory completion due date is October 31, 2026, distinct from the October 30 scenario cutoff. A visible alignment note preserves the difference without blocking simulation. After the cutoff, modeled additions become zero but the report still asks the TPM to confirm the inventory baseline.

`data/estimation_inputs.json` is optional and ignored by Git. The existing local KAN configuration enables the simulated engine and records five owner declarations: scope, dependencies, capacity, remaining work, and testing/rework. The shipped [`estimation_inputs.example.json`](estimation_inputs.example.json) is disabled with all confirmations false. Do not interpret those demonstration declarations as independently verified real evidence. Invalid or project-mismatched configuration fails closed.

The optional `forecast` object defaults to 400 trials per case, seed `20261011`, 156 weeks, scope growth `[0, 0.10, 0.25]`, weekly capacity variation `0.35`, and fallback `8` relative points per assigned contributor per week. The horizon must extend through the milestone deadline; otherwise the run is unavailable and asks for a longer horizon. All workstreams retain their own point scales. Priority then larger items first is primary; priority then smaller items first is a sensitivity. Unknown open estimates receive model-only workstream upper-quartile imputation. Full rules and strict parameter bounds are in [`README.md`](README.md).

Model dates are conditional outcomes under these assumptions. Deadline percentages use all trials, including those unfinished within the horizon; unavailable percentiles remain unavailable. They are not calibrated real delivery probabilities. KAN's real-delivery readiness stays blocked by simulated provenance even when the demonstration model and critic pass.

## Configure the AI reviewer and commentary

Local Ollama is the default for both the forecast critic and commentary loop. Install and open Ollama on this Mac, then run `ollama run llama3.2` once to download the model. Keep Ollama running when generating a report; the Friday job uses the same local service.

```toml
[ai]
provider = "ollama"
model = "llama3.2"
base_url = "http://localhost:11434"
```

To use the configured OpenAI provider, set these local sections in `.streamlit/secrets.toml`. Keep the key in that ignored file.

```toml
[ai]
provider = "openai"

[openai]
api_key = "YOUR_OPENAI_API_KEY"
model = "gpt-6-luna"
```

If the provider is unavailable, the job records the review status, retains valid Python simulation outputs as unreviewed, and saves the report without AI commentary. Forecast and commentary are distinct reviews with separate findings and revision limits.

## Install the Friday job on this Mac

The LaunchAgent definition is `launchd/com.teamvishai.weekly-readout.plist`. It uses this workspace's current absolute path, so update paths if the project moves. After the dashboard has connected to Jira at least once, install and start it from `PT-app`:

```sh
mkdir -p "$HOME/Library/LaunchAgents"
cp launchd/com.teamvishai.weekly-readout.plist "$HOME/Library/LaunchAgents/"
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.teamvishai.weekly-readout.plist"
```

The job runs while this macOS user is logged in. Logs are written under `data/`; saved drafts appear in the dashboard after refresh. If the Mac is asleep at the scheduled time, launchd handles the missed calendar run after wake.

To stop the job:

```sh
launchctl bootout "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.teamvishai.weekly-readout.plist"
```

## Run once manually

From `PT-app`, run:

```sh
./.venv/bin/python weekly_readout_job.py
```

The job refreshes the saved Jira token if needed and writes `weekly_readout_YYYYMMDD_HHMMSS.md` plus `weekly_readout_YYYYMMDD_HHMMSS.forecast.json`. **Generate weekly readout draft** performs the same reporting/review stages using the currently loaded dashboard Jira data.

## Inspect and replay a saved model run

The JSON companion records model version, seed, final trial count, input fingerprint, run ID, normalized issue/milestone inputs, local scenario declarations/settings, assumptions, warnings, and deterministic/AI diagnostics. OAuth/model credential objects and raw Jira descriptions are excluded. Normalized contributor identifiers are retained because the fallback capacity calculation depends on assigned contributor counts. The dashboard offers **Download forecast inputs and diagnostics** beside saved drafts.

For a published run, replay the final Python simulation without contacting Jira or AI. Use the recorded model version and compatible runtime; this repeats numerical calculation, not the historical AI review. From `PT-app`, choose a saved companion file:

```python
import json
from pathlib import Path

import pandas as pd

from jira_dashboard.forecast import MODEL_VERSION, build_simulated_forecast

path = Path("data/weekly_readouts/weekly_readout_YYYYMMDD_HHMMSS.forecast.json")
saved = json.loads(path.read_text(encoding="utf-8"))
if not saved.get("published"):
    raise SystemExit("This run withheld model results; inspect its review diagnostics.")
if saved.get("model_version") != MODEL_VERSION:
    raise SystemExit("Replay requires the recorded model implementation/version.")
source = saved["reproducibility_inputs"]
result = build_simulated_forecast(
    pd.DataFrame(source["issues"]),
    pd.DataFrame(source["milestones"]),
    pd.Timestamp(source["as_of"]),
    estimation_inputs=source["estimation_inputs"],
    overrides={"trials": saved["trials"]},
)
assert result["scenarios"] == saved["scenarios"]
print("Saved scenario results reproduced.")
```

The final trial override preserves a critic-requested increase while retaining the original saved settings. Review the Markdown and JSON together before sharing. Empirical backtesting, capacity calibration, quantified future scope, and real acceptance evidence remain required for real delivery use.
