"""Bounded review of deterministic simulation results.

The critic can request more Monte Carlo trials once. It cannot edit a date,
change business assumptions, or turn simulated outcomes into delivery confidence.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_CEILING
from typing import Callable

import pandas as pd


_BUNDLE_FIELDS = {
    "status", "label", "milestone", "as_of", "target_date", "scenarios", "assumptions",
    "warnings", "evidence", "model_version", "seed", "trials", "reasons",
}
_SOURCE_FIELDS = {
    "key", "issue_type", "status_category", "story_points", "priority", "team", "team_key",
    "created", "resolved", "updated", "due_date", "is_simulated", "project_key", "assignee",
}
_SENSITIVE_KEYS = {
    "api_key", "apikey", "access_token", "refresh_token", "token", "password", "secret",
    "client_secret", "authorization", "credentials", "credential", "secrets",
}
_POLICIES = {"priority_larger_first", "priority_smaller_first"}


def _json_value(value: object) -> object:
    """Copy public model data into strict JSON values, omitting credential fields."""
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()
                if str(key).casefold() not in _SENSITIVE_KEYS
                and not any(part in str(key).casefold() for part in ("password", "secret", "credential", "access_token", "refresh_token"))}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, (pd.Timestamp, date, datetime)):
        return None if pd.isna(value) else value.isoformat()
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, pd.DataFrame):
        return _json_value(value.to_dict(orient="records"))
    if hasattr(value, "item"):
        return _json_value(value.item())
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return str(value)


def _digest(value: object) -> str:
    encoded = json.dumps(_json_value(value), sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _fingerprint_frame(frame: pd.DataFrame, fields: set[str], dates: set[str]) -> pd.DataFrame:
    result = frame[[column for column in frame.columns if column in fields]].copy()
    for column in dates & set(result.columns):
        parsed = pd.to_datetime(result[column], errors="coerce", utc=True, format="mixed")
        result[column] = parsed.map(lambda value: None if pd.isna(value) else value.isoformat())
    if "story_points" in result:
        points = pd.to_numeric(result["story_points"], errors="coerce")
        result["story_points"] = points.map(lambda value: float(value) if pd.notna(value) and math.isfinite(value) else None)
    return result


def _public_bundle(raw: object) -> dict:
    if not isinstance(raw, dict):
        return {"status": "unavailable", "scenarios": [], "reasons": ["The model did not return a result object."]}
    return _json_value({key: value for key, value in raw.items() if key in _BUNDLE_FIELDS})


def _date(value: object) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = date.fromisoformat(value)
        return parsed if parsed.isoformat() == value else None
    except ValueError:
        return None


def _integer(value: object) -> bool:
    return type(value) is int and value >= 0


def _finite_number(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _ceil_product(count: int, factor: object) -> int:
    return int((Decimal(count) * Decimal(str(factor))).to_integral_value(rounding=ROUND_CEILING))


def _source_counts(issues: pd.DataFrame) -> tuple[int, int]:
    if "issue_type" not in issues:
        return 0, 0
    work = issues.loc[issues["issue_type"].fillna("").astype(str).str.strip().str.casefold().isin(["story", "bug"])]
    status = work.get("status_category", pd.Series("", index=work.index))
    return len(work), int(status.fillna("").astype(str).str.strip().str.casefold().eq("done").sum())


def _validate_bundle(
    bundle: dict,
    issues: pd.DataFrame,
    milestones: pd.DataFrame,
    now: pd.Timestamp,
    inputs: dict,
    *,
    expected_seed: int | None = None,
    expected_trials: int | None = None,
) -> list[str]:
    """Independently validate scope arithmetic and the published result schema."""
    errors = []
    if bundle.get("label") != "Simulated scenario":
        errors.append("Model output must be labeled Simulated scenario.")
    if bundle.get("milestone") != "Unit testing validation":
        errors.append("The model output must refer to Unit testing validation.")
    target = _date(bundle.get("target_date"))
    milestone_dates = []
    start_dates = []
    if {"milestone", "due_date"}.issubset(milestones.columns):
        targets = milestones.loc[milestones["milestone"].fillna("").astype(str).str.strip().str.casefold().eq("unit testing validation"), "due_date"]
        milestone_dates = pd.to_datetime(targets, errors="coerce").dropna().map(lambda value: value.date()).tolist()
        if "start_date" in milestones:
            starts = milestones.loc[milestones["milestone"].fillna("").astype(str).str.strip().str.casefold().eq("unit testing validation"), "start_date"]
            start_dates = pd.to_datetime(starts, errors="coerce").dropna().map(lambda value: value.date()).tolist()
    if target is None or target not in milestone_dates:
        errors.append("The output target date does not match the Unit testing milestone.")
    if len(set(milestone_dates)) != 1 or len(set(start_dates)) != 1:
        errors.append("One unambiguous Unit testing planned start and target date are required.")
    observation = pd.Timestamp(now)
    if observation.tzinfo is None:
        observation = observation.tz_localize("UTC")
    observation = observation.tz_convert("UTC")
    try:
        as_of = pd.Timestamp(bundle.get("as_of"))
        if as_of.tzinfo is None or as_of.tz_convert("UTC") != observation:
            errors.append("The model observation time does not match the supplied source time.")
    except (TypeError, ValueError):
        errors.append("The model observation time is invalid.")

    trials = bundle.get("trials")
    seed = bundle.get("seed")
    options = inputs.get("forecast", {})
    expected_seed = options.get("seed", 20261011) if expected_seed is None else expected_seed
    expected_trials = options.get("trials", 400) if expected_trials is None else expected_trials
    if not _integer(trials) or not 100 <= trials <= 2000:
        errors.append("Trial count must be an integer between 100 and 2000.")
    if not _integer(seed) or seed > 2**32 - 1:
        errors.append("The random seed is invalid.")
    if expected_seed is not None and seed != expected_seed:
        errors.append("The permitted revision changed the random seed.")
    if expected_trials is not None and trials != expected_trials:
        errors.append("The model did not use the permitted trial count.")
    evidence = bundle.get("evidence")
    if not isinstance(evidence, dict):
        return errors + ["The model evidence is missing."]
    base, done = _source_counts(issues)
    if not _integer(evidence.get("current_total_items")) or not _integer(evidence.get("current_done_items")) or evidence.get("current_total_items") != base or evidence.get("current_done_items") != done:
        errors.append("The model scope counts do not match current Stories and Bugs.")
    scenario_inputs = inputs.get("milestone_scenario", {})
    threshold = scenario_inputs.get("target_completion_pct", 91)
    if not _finite_number(threshold) or not 0 < threshold <= 100:
        errors.append("The configured completion threshold is invalid.")
        threshold = 91
    if evidence.get("target_completion_pct") != threshold:
        errors.append("The model changed the completion threshold.")
    cutoff = _date(evidence.get("scope_growth_until"))
    configured_cutoff = scenario_inputs.get("scope_growth_until")
    if cutoff is None or configured_cutoff is not None and cutoff != _date(configured_cutoff):
        errors.append("The model scope-growth cutoff is missing or differs from the configured cutoff.")
    today = observation.tz_convert("America/Los_Angeles").date()
    earliest = _date(evidence.get("earliest_attainment_date"))
    start = start_dates[0] if len(set(start_dates)) == 1 else None
    if start is not None and target is not None and start > target:
        errors.append("The Unit testing planned start follows its target date.")
    expected_earliest = max(today, start or today, cutoff if cutoff is not None and today <= cutoff else today)
    if earliest != expected_earliest:
        errors.append("The earliest model attainment date violates the observation or scope-growth gate.")
    if "milestone_start_date" in evidence and _date(evidence["milestone_start_date"]) != start:
        errors.append("The model changed the milestone planned start.")
    configured_growth = options.get("growth_fractions", [0, .1, .25])
    if not isinstance(configured_growth, list) or not all(_finite_number(value) and 0 <= value <= 1 for value in configured_growth):
        errors.append("Configured growth sensitivities are invalid.")
        configured_growth = []
    horizon = options.get("horizon_weeks", 156)
    horizon_end = today + timedelta(weeks=horizon) if type(horizon) is int and 26 <= horizon <= 260 else None

    scenarios = bundle.get("scenarios")
    if not isinstance(scenarios, list) or not scenarios:
        return errors + ["No model scenarios were returned."]
    seen_ids = set()
    seen_cases = set()
    for index, scenario in enumerate(scenarios):
        prefix = f"Scenario {index + 1}: "
        if not isinstance(scenario, dict):
            errors.append(prefix + "output is not an object.")
            continue
        identifier = scenario.get("id")
        if not isinstance(identifier, str) or not identifier or identifier in seen_ids:
            errors.append(prefix + "scenario identifier is missing or duplicated.")
        else:
            seen_ids.add(identifier)
        if scenario.get("selection_policy") not in _POLICIES:
            errors.append(prefix + "selection policy is unsupported.")
        growth = scenario.get("growth_fraction")
        if not _finite_number(growth) or not 0 <= growth <= 1:
            errors.append(prefix + "growth fraction is invalid.")
            continue
        case = (growth, scenario.get("selection_policy"))
        if growth not in configured_growth or case in seen_cases:
            errors.append(prefix + "growth sensitivity is unconfigured or duplicates a policy case.")
        seen_cases.add(case)
        additional = _ceil_product(base, growth) if cutoff is not None and today <= cutoff else 0
        total = base + additional
        if scenario.get("additional_items") != additional or scenario.get("total_items") != total:
            errors.append(prefix + "scenario scope does not equal the source base plus configured arrivals.")
        target_count = _ceil_product(total, Decimal(str(threshold)) / Decimal(100))
        if scenario.get("target_done_items") != target_count:
            errors.append(prefix + "target item count does not use the exact ceiling of the completion threshold.")
        finished = scenario.get("finished_trials")
        censored = scenario.get("censored_trials")
        if not _integer(finished) or not _integer(censored) or finished + censored != trials or scenario.get("total_trials") != trials:
            errors.append(prefix + "finished plus censored trials must equal the configured trial count.")
        fraction = scenario.get("target_attainment_pct")
        if not _finite_number(fraction) or not 0 <= fraction <= 100:
            errors.append(prefix + "conditional target-attainment fraction is outside 0–100.")
        elif _integer(finished) and _integer(trials) and trials > 0 and fraction > finished / trials * 100 + 1e-8:
            errors.append(prefix + "target attainment exceeds all finished trials.")
        values = []
        missing_quantile = False
        for key, quantile in (("p10_date", .1), ("p50_date", .5), ("p90_date", .9)):
            raw = scenario.get(key)
            should_exist = _integer(finished) and _integer(trials) and finished >= math.ceil(trials * quantile)
            if raw is None:
                if should_exist:
                    errors.append(prefix + key + " is missing despite enough finished trials.")
                missing_quantile = True
                continue
            parsed = _date(raw)
            if not should_exist:
                errors.append(prefix + key + " exists despite too few finished trials.")
            if parsed is None or earliest is not None and parsed < earliest or horizon_end is not None and parsed > horizon_end:
                errors.append(prefix + key + " is invalid or earlier than the model gate.")
            elif missing_quantile:
                errors.append(prefix + "a later date quantile cannot exist after a censored earlier quantile.")
            else:
                values.append(parsed)
        if values != sorted(values):
            errors.append(prefix + "date quantiles are out of order.")
        for key in ("label", "selection_policy"):
            if "confidence" in str(scenario.get(key, "")).casefold():
                errors.append(prefix + "scenario metadata contains an unsupported confidence claim.")
        if any("confidence" in str(key).casefold() for key in scenario):
            errors.append(prefix + "scenario output includes an unsupported confidence field.")
    expected_cases = {(growth, policy) for growth in configured_growth for policy in _POLICIES}
    if seen_cases != expected_cases:
        errors.append("The output does not cover exactly the configured growth sensitivities and selection policies.")
    return list(dict.fromkeys(errors))


def _review_response(value: object) -> tuple[dict | None, str | None]:
    if not isinstance(value, dict) or set(value) != {"approved", "findings", "revision"}:
        return None, "AI review returned an invalid response schema."
    if type(value["approved"]) is not bool or not isinstance(value["findings"], list) or not all(isinstance(item, str) for item in value["findings"]):
        return None, "AI review returned invalid approval or findings values."
    if value["revision"] not in {"none", "increase_trials"}:
        return None, "AI review requested an unsupported revision."
    if value["approved"] and (value["revision"] != "none" or value["findings"]):
        return None, "AI review returned contradictory approval and findings."
    return copy.deepcopy(value), None


def run_estimation_loop(
    issues: pd.DataFrame,
    milestones: pd.DataFrame,
    now: pd.Timestamp,
    *,
    estimation_inputs: dict,
    reviewer: Callable[[dict], dict] | None = None,
    forecast_builder: Callable | None = None,
) -> dict:
    """Run the model, independent checks, and at most two critic calls."""
    if forecast_builder is None:
        from jira_dashboard.forecast import build_simulated_forecast

        forecast_builder = build_simulated_forecast
    source = issues.copy(deep=True)
    targets = milestones.copy(deep=True)
    inputs = copy.deepcopy(estimation_inputs)
    observation = pd.Timestamp(now)
    observation = observation.tz_localize("UTC") if observation.tzinfo is None else observation.tz_convert("UTC")
    fingerprint = _digest({
        "issues": _fingerprint_frame(source, _SOURCE_FIELDS, {"created", "resolved", "updated", "due_date"}),
        "milestones": _fingerprint_frame(targets, {"milestone", "start_date", "due_date", "status"}, {"start_date", "due_date"}),
        "now": observation, "estimation_inputs": inputs,
    })
    review = {"status": "not_requested", "passes": 0, "revisions": 0, "findings": []}

    def build(overrides: dict | None = None) -> dict:
        try:
            raw = forecast_builder(source.copy(deep=True), targets.copy(deep=True), pd.Timestamp(now),
                                   estimation_inputs=copy.deepcopy(inputs), overrides=copy.deepcopy(overrides))
            return _public_bundle(raw)
        except Exception as exc:
            return {"status": "unavailable", "scenarios": [], "reasons": [f"The simulation model could not run ({type(exc).__name__})."]}

    bundle = build()
    validation = {"status": "not_run", "findings": []}
    published = False

    def validate(candidate: dict, **expectations) -> list[str]:
        try:
            return _validate_bundle(candidate, source, targets, now, inputs, **expectations)
        except Exception as exc:
            return [f"Model output could not be deterministically validated ({type(exc).__name__})."]

    if bundle.get("status") == "available":
        failures = validate(bundle)
        validation = {"status": "rejected" if failures else "passed", "findings": failures}
        if failures:
            review.update(status="rejected", findings=["Deterministic validation: " + item for item in failures])
        else:
            published = True
            if reviewer is not None:
                original_seed = bundle.get("seed")
                for attempt in range(2):
                    review["passes"] += 1
                    # The critic receives a detached JSON-only copy, never a live engine object.
                    review_input = json.loads(json.dumps({**bundle, "deterministic_validation": validation}, allow_nan=False))
                    try:
                        response, error = _review_response(reviewer(review_input))
                    except Exception as exc:
                        response, error = None, f"AI review was unavailable ({type(exc).__name__})."
                    if error:
                        review.update(status="unavailable", findings=[error])
                        break
                    if response["approved"]:
                        review.update(status="passed", findings=[])
                        break
                    review.update(status="rejected", findings=response["findings"] or ["The AI reviewer did not approve these scenario outputs."])
                    if attempt == 0 and response["revision"] == "increase_trials" and bundle["trials"] < 2000:
                        revised_trials = min(2000, bundle["trials"] * 2)
                        review["revisions"] = 1
                        previous_bundle = bundle
                        bundle = build({"trials": revised_trials})
                        failures = (validate(bundle, expected_seed=original_seed, expected_trials=revised_trials)
                                    if bundle.get("status") == "available" else ["The permitted model revision did not produce available results."])
                        for key in ("label", "milestone", "as_of", "target_date", "model_version", "assumptions"):
                            if bundle.get(key) != previous_bundle.get(key):
                                failures.append("The permitted revision changed model identity or business assumptions.")
                        prior_evidence = copy.deepcopy(previous_bundle.get("evidence", {}))
                        revised_evidence = copy.deepcopy(bundle.get("evidence", {}))
                        for evidence in (prior_evidence, revised_evidence):
                            if isinstance(evidence.get("forecast_options"), dict):
                                evidence["forecast_options"].pop("trials", None)
                        if prior_evidence != revised_evidence:
                            failures.append("The permitted revision changed evidence or business parameters.")
                        validation = {"status": "rejected" if failures else "passed", "findings": failures}
                        if failures:
                            review.update(status="rejected", findings=["Deterministic validation: " + item for item in failures])
                            published = False
                            break
                        continue
                    published = False
                    break
    if not published:
        bundle["scenarios"] = []
        if bundle.get("status") == "available":
            bundle["status"] = "unavailable"
        if isinstance(bundle.get("evidence"), dict):
            bundle["evidence"].pop("earliest_attainment_date", None)
    bundle.update(
        review=review, published=published, deterministic_validation=validation,
        input_fingerprint=fingerprint,
        run_id=_digest({"input_fingerprint": fingerprint, "model_version": bundle.get("model_version"),
                        "seed": bundle.get("seed"), "trials": bundle.get("trials")})[:20],
    )
    return bundle
