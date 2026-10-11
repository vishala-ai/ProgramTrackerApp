"""Load optional owner-supplied evidence for estimation-readiness checks.

These declarations supplement Jira facts. They do not independently verify the
evidence or override detected simulation activity. Optional forecast settings
enable only the explicitly labelled simulated scenario engine.
"""

from __future__ import annotations

import json
import math
import re
from datetime import date
from pathlib import Path

from .forecast import validate_forecast_options


CONFIRMATION_KEYS = frozenset({
    "milestone_scope", "dependencies", "capacity", "remaining_work",
    "testing_rework", "history_representative", "model_validation",
})
DATA_MODES = frozenset({"simulation", "real", "unknown"})
_FILE_KEYS = frozenset({"project_key", "data_mode", "confirmations"})
_OPTIONAL_FILE_KEYS = frozenset({"milestone_scenario", "forecast"})
_CONFIRMATION_FIELDS = frozenset({"confirmed", "evidence", "owner"})
_SCENARIO_FIELDS = frozenset({"target_completion_pct", "scope_growth_until"})


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key: " + key)
        result[key] = value
    return result


def load_estimation_inputs(path: Path, *, project_key: str = "KAN") -> dict:
    """Return validated declarations, or safe defaults for missing/invalid input.

    KAN defaults to simulation because this app schedules simulated Jira work;
    other projects default to unknown. A missing file is expected and has no
    error. Any invalid file discards every declaration and returns an error.
    Project keys must match exactly to prevent evidence leaking across projects.
    Confirmations remain declarations even when their schema is valid.
    An optional milestone_scenario records a completion threshold and inventory
    closure date; it does not establish a forecast or independently verify scope.
    """
    default = {
        "data_mode": "simulation" if project_key == "KAN" else "unknown",
        "confirmations": {},
        "error": None,
    }

    def invalid(message: str) -> dict:
        return dict(default, error=message)

    try:
        content = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return default
    except UnicodeError:
        return invalid("Estimation inputs must use UTF-8 text.")
    except OSError as exc:
        return invalid("Could not read estimation inputs: " + (exc.strerror or type(exc).__name__))

    try:
        supplied = json.loads(content, object_pairs_hook=_unique_json_object)
    except (ValueError, RecursionError):
        return invalid("Estimation inputs are not valid JSON or contain duplicate keys.")

    if not isinstance(supplied, dict):
        return invalid("Estimation inputs must be a JSON object.")
    if not _FILE_KEYS.issubset(supplied) or not set(supplied).issubset(_FILE_KEYS | _OPTIONAL_FILE_KEYS):
        return invalid("Estimation inputs require project_key, data_mode, and confirmations; milestone_scenario and forecast are optional.")
    if not isinstance(supplied["project_key"], str) or supplied["project_key"] != project_key:
        return invalid("Estimation inputs project_key does not match the requested project.")
    mode = supplied["data_mode"]
    if not isinstance(mode, str) or mode not in DATA_MODES:
        return invalid("Estimation inputs data_mode must be simulation, real, or unknown.")
    confirmations = supplied["confirmations"]
    if not isinstance(confirmations, dict):
        return invalid("Estimation inputs confirmations must be an object.")
    if not set(confirmations).issubset(CONFIRMATION_KEYS):
        return invalid("Estimation inputs contain an unknown confirmation key.")
    for key, confirmation in confirmations.items():
        if not isinstance(confirmation, dict) or set(confirmation) != _CONFIRMATION_FIELDS:
            return invalid(key + " requires exactly confirmed, evidence, and owner.")
        if type(confirmation["confirmed"]) is not bool:
            return invalid(key + ".confirmed must be a JSON boolean.")
        if not isinstance(confirmation["evidence"], str) or not isinstance(confirmation["owner"], str):
            return invalid(key + ".evidence and .owner must be strings.")
    result = {"data_mode": mode, "confirmations": confirmations, "error": None}
    if "milestone_scenario" in supplied:
        scenario = supplied["milestone_scenario"]
        if not isinstance(scenario, dict) or set(scenario) != _SCENARIO_FIELDS:
            return invalid("milestone_scenario requires exactly target_completion_pct and scope_growth_until.")
        threshold = scenario["target_completion_pct"]
        if (isinstance(threshold, bool) or not isinstance(threshold, (int, float))
                or not 0 < threshold <= 100 or not math.isfinite(threshold)):
            return invalid("milestone_scenario.target_completion_pct must be a finite number greater than 0 and at most 100.")
        closure_date = scenario["scope_growth_until"]
        if not isinstance(closure_date, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", closure_date):
            return invalid("milestone_scenario.scope_growth_until must be a valid date in YYYY-MM-DD format.")
        try:
            date.fromisoformat(closure_date)
        except ValueError:
            return invalid("milestone_scenario.scope_growth_until must be a valid date in YYYY-MM-DD format.")
        result["milestone_scenario"] = scenario
    if "forecast" in supplied:
        try:
            result["forecast"] = validate_forecast_options(supplied["forecast"])
        except ValueError as exc:
            return invalid("Invalid forecast settings: " + str(exc))
    return result
