"""
Governed tools for the Treaty Pricing Submission Analyst Agent.

Every tool call is appended to a shared TRACE log (step #, tool name, inputs,
outputs). This is what lets guardrails.assert_trace_covers_numbers prove that
every number quoted in the final memo is reproducible from the trace, and
lets a peer reviewer replay the calculation end to end.
"""
from __future__ import annotations
import math
import functools
from dataclasses import dataclass, field
from typing import Any

import guardrails
from rag_store import (
    APPROVED_TREND_ASSUMPTIONS,
    APPROVED_TIV_TREND_DEFAULT,
    CREDIBILITY_STANDARD,
    EXPENSE_CAPITAL_PARAMETERS,
    MIN_YEARS_HISTORY_FULL,
    MIN_YEARS_HISTORY_FLAGGED,
)

TRACE: list[dict] = []


def traced(tool_name: str):
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            result = fn(*args, **kwargs)
            TRACE.append({
                "step": len(TRACE) + 1,
                "tool": tool_name,
                "inputs": _safe_repr(kwargs if kwargs else args),
                "outputs": _safe_repr(result),
            })
            return result
        return wrapper
    return decorator


def _safe_repr(obj: Any) -> Any:
    # Keep the trace JSON-ish / reproducible; leave numbers/dicts/lists as-is.
    return obj


def all_numeric_outputs_in_trace() -> set:
    """Flatten every numeric value ever produced, for guardrail reproducibility checks."""
    nums = set()

    def _walk(o):
        if isinstance(o, bool):
            return
        if isinstance(o, (int, float)):
            nums.add(round(float(o), 4))
        elif isinstance(o, dict):
            for v in o.values():
                _walk(v)
        elif isinstance(o, (list, tuple, set)):
            for v in o:
                _walk(v)

    for entry in TRACE:
        _walk(entry["inputs"])
        _walk(entry["outputs"])
    return nums


# ===========================================================================
# TOOL 1: validate_pricing_submission
# ===========================================================================
@traced("validate_pricing_submission")
def validate_pricing_submission(submission: dict) -> dict:
    issues = []
    losses_by_year = submission.get("losses_by_year", {})
    premium_by_year = submission.get("premium_by_year", {})
    tiv_by_year = submission.get("tiv_by_year", {})
    treaty_terms = submission.get("treaty_terms", {})

    years = sorted(set(losses_by_year) | set(premium_by_year) | set(tiv_by_year))
    n_years = len(years)

    if n_years < MIN_YEARS_HISTORY_FLAGGED:
        issues.append(
            f"Only {n_years} years of history supplied; below the {MIN_YEARS_HISTORY_FLAGGED}"
            f"-year absolute minimum. Indication cannot be produced."
        )
    elif n_years < MIN_YEARS_HISTORY_FULL:
        issues.append(
            f"Only {n_years} years of history supplied; below the "
            f"{MIN_YEARS_HISTORY_FULL}-year standard. Indication must be flagged "
            f"as subject to data limitation with reduced credibility."
        )

    for y in years:
        if y not in premium_by_year:
            issues.append(f"Missing premium for year {y}.")
        if y not in tiv_by_year:
            issues.append(f"Missing TIV for year {y} (exposure on-level fallback will be used).")
        if y in losses_by_year:
            for ev in losses_by_year[y]:
                if ev["amount"] < 0:
                    issues.append(f"Negative loss amount recorded in {y}: {ev}.")

    required_terms = ["attachment", "limit", "reinstatements", "reinstatement_cost_pct"]
    for t in required_terms:
        if t not in treaty_terms:
            issues.append(f"Missing treaty term '{t}'.")

    status = "FAIL" if any("cannot be produced" in i for i in issues) else (
        "PASS_WITH_FLAGS" if issues else "PASS"
    )

    return {
        "status": status,
        "years_supplied": years,
        "n_years": n_years,
        "issues": issues,
        "data_limitation_flag": n_years < MIN_YEARS_HISTORY_FULL,
    }


# ===========================================================================
# TOOL 2: trend_and_onlevel_claims
# ===========================================================================
@traced("trend_and_onlevel_claims")
def trend_and_onlevel_claims(
    losses_by_year: dict,
    premium_by_year: dict,
    tiv_by_year: dict,
    current_year: int,
    peril: str = "all_peril_blend",
) -> dict:
    trend = APPROVED_TREND_ASSUMPTIONS.get(peril.lower().replace(" ", "_"))
    guardrails.assert_approved_trend(peril, trend)
    tiv_trend = APPROVED_TIV_TREND_DEFAULT
    guardrails.assert_approved_tiv_trend(tiv_trend)

    trended_events_by_year = {}
    onlevel_premium_by_year = {}

    for y, events in losses_by_year.items():
        years_of_trend = current_year - y
        loss_trend_factor = (1 + trend) ** years_of_trend
        trended_events_by_year[y] = [
            {**ev, "trended_amount": round(ev["amount"] * loss_trend_factor, 2)}
            for ev in events
        ]

    for y in premium_by_year:
        base_tiv = tiv_by_year.get(y)
        current_tiv = tiv_by_year.get(current_year, base_tiv)
        onlevel_factor = (current_tiv / base_tiv) if base_tiv else (1 + tiv_trend) ** (current_year - y)
        onlevel_premium_by_year[y] = round(premium_by_year[y] * onlevel_factor, 2)

    return {
        "peril": peril,
        "loss_trend_used": trend,
        "tiv_trend_used": tiv_trend,
        "trended_events_by_year": trended_events_by_year,
        "onlevel_premium_by_year": onlevel_premium_by_year,
    }


# ===========================================================================
# TOOL 3: apply_treaty_structure_to_losses
# ===========================================================================
@traced("apply_treaty_structure_to_losses")
def apply_treaty_structure_to_losses(trended_events_by_year: dict, treaty_terms: dict) -> dict:
    attachment = treaty_terms["attachment"]
    limit = treaty_terms["limit"]
    reinstatements = treaty_terms["reinstatements"]
    annual_aggregate_limit = limit * (1 + reinstatements)

    losses_to_layer_by_year = {}
    event_detail_by_year = {}

    for y, events in trended_events_by_year.items():
        year_total = 0.0
        detail = []
        for ev in events:
            loss_to_layer = min(max(ev["trended_amount"] - attachment, 0.0), limit)
            detail.append({**ev, "loss_to_layer": round(loss_to_layer, 2)})
            year_total += loss_to_layer
        year_total_capped = min(year_total, annual_aggregate_limit)
        losses_to_layer_by_year[y] = round(year_total_capped, 2)
        event_detail_by_year[y] = detail

    return {
        "attachment": attachment,
        "limit": limit,
        "reinstatements": reinstatements,
        "annual_aggregate_limit": round(annual_aggregate_limit, 2),
        "losses_to_layer_by_year": losses_to_layer_by_year,
        "event_detail_by_year": event_detail_by_year,
    }


# ===========================================================================
# TOOL 4: calculate_burning_cost
# ===========================================================================
@traced("calculate_burning_cost")
def calculate_burning_cost(losses_to_layer_by_year: dict, onlevel_premium_by_year: dict) -> dict:
    years = sorted(set(losses_to_layer_by_year) & set(onlevel_premium_by_year))
    annual_bc = {y: round(losses_to_layer_by_year[y] / onlevel_premium_by_year[y], 4) for y in years}

    simple_avg = round(sum(annual_bc.values()) / len(annual_bc), 4) if annual_bc else 0.0
    total_losses = sum(losses_to_layer_by_year[y] for y in years)
    total_premium = sum(onlevel_premium_by_year[y] for y in years)
    weighted_avg = round(total_losses / total_premium, 4) if total_premium else 0.0

    mean = simple_avg
    variance = (sum((v - mean) ** 2 for v in annual_bc.values()) / len(annual_bc)) if annual_bc else 0.0
    std_dev = round(math.sqrt(variance), 4)

    return {
        "years_used": years,
        "annual_burning_cost_ratio": annual_bc,
        "simple_average_burning_cost": simple_avg,
        "premium_weighted_average_burning_cost": weighted_avg,
        "burning_cost_std_dev": std_dev,
        "experience_indication_premium_basis": round(weighted_avg * onlevel_premium_by_year[max(years)], 2)
        if years else 0.0,
    }


# ===========================================================================
# TOOL 5: fit_frequency_severity_models
# ===========================================================================
@traced("fit_frequency_severity_models")
def fit_frequency_severity_models(event_detail_by_year: dict, current_onlevel_premium: float) -> dict:
    all_events_to_layer = [
        ev["loss_to_layer"]
        for events in event_detail_by_year.values()
        for ev in events
        if ev["loss_to_layer"] > 0
    ]
    n_years = len(event_detail_by_year)
    n_claims = len(all_events_to_layer)

    # Frequency: Poisson lambda (claims to layer per year)
    lam = round(n_claims / n_years, 4) if n_years else 0.0

    # Severity: method-of-moments lognormal fit on positive losses-to-layer
    if n_claims > 0:
        mean_sev = sum(all_events_to_layer) / n_claims
        if n_claims > 1:
            var_sev = sum((x - mean_sev) ** 2 for x in all_events_to_layer) / (n_claims - 1)
        else:
            var_sev = mean_sev ** 2 * 0.25  # thin-data fallback variance assumption
        cv2 = (var_sev / mean_sev ** 2) if mean_sev else 0.0
        sigma2 = math.log(1 + cv2) if cv2 > -1 else 0.0
        sigma = math.sqrt(max(sigma2, 0.0))
        mu = math.log(mean_sev) - sigma2 / 2 if mean_sev > 0 else 0.0
        expected_severity = mean_sev
    else:
        mu, sigma, expected_severity = 0.0, 0.0, 0.0

    modeled_expected_annual_loss_to_layer = round(lam * expected_severity, 2)

    return {
        "n_claims_to_layer": n_claims,
        "n_years": n_years,
        "frequency_lambda": lam,
        "severity_lognormal_mu": round(mu, 4),
        "severity_lognormal_sigma": round(sigma, 4),
        "expected_severity_to_layer": round(expected_severity, 2),
        "modeled_expected_annual_loss_to_layer": modeled_expected_annual_loss_to_layer,
        "exposure_indication_loss_cost_ratio": round(
            modeled_expected_annual_loss_to_layer / current_onlevel_premium, 4
        ) if current_onlevel_premium else 0.0,
    }


# ===========================================================================
# TOOL 6: calculate_credibility_weight
# ===========================================================================
@traced("calculate_credibility_weight")
def calculate_credibility_weight(n_claims_to_layer: int) -> dict:
    full = CREDIBILITY_STANDARD["full_credibility_claims"]
    guardrails.assert_credibility_standard(full)
    floor = CREDIBILITY_STANDARD["min_claims_floor"]
    floor_cap = CREDIBILITY_STANDARD["min_claims_floor_cap_z"]

    z_raw = math.sqrt(min(1.0, n_claims_to_layer / full))
    if n_claims_to_layer < floor:
        z = min(z_raw, floor_cap)
        note = f"Below minimum claim floor ({floor}); Z capped at {floor_cap}."
    else:
        z = z_raw
        note = "Standard partial credibility formula applied."

    return {
        "n_claims_to_layer": n_claims_to_layer,
        "full_credibility_standard": full,
        "Z": round(z, 4),
        "note": note,
    }


# ===========================================================================
# TOOL 7: calculate_technical_price
# ===========================================================================
@traced("calculate_technical_price")
def calculate_technical_price(
    experience_indication_loss_cost: float,
    exposure_indication_loss_cost: float,
    Z: float,
    onlevel_premium: float,
    expense_pct: float = None,
    admin_pct: float = None,
    capital_pct: float = None,
    margin_pct: float = None,
) -> dict:
    p = EXPENSE_CAPITAL_PARAMETERS
    expense_pct = p["default_cedant_expense_pct"] if expense_pct is None else expense_pct
    admin_pct = p["internal_admin_pct"] if admin_pct is None else admin_pct
    capital_pct = p["capital_load_proxy_pct"] if capital_pct is None else capital_pct
    margin_pct = p["target_margin_pct"] if margin_pct is None else margin_pct
    guardrails.assert_approved_expense_capital_margin(expense_pct, admin_pct, capital_pct, margin_pct)

    blended_expected_loss_cost = round(
        Z * experience_indication_loss_cost + (1 - Z) * exposure_indication_loss_cost, 2
    )
    capital_load = round(blended_expected_loss_cost * capital_pct, 2)
    total_load_pct = expense_pct + admin_pct + margin_pct
    # capital load expressed as % of blended loss cost is converted onto premium basis below
    denominator = 1 - total_load_pct
    technical_premium = round((blended_expected_loss_cost + capital_load) / denominator, 2)
    technical_price_ratio = round(technical_premium / onlevel_premium, 4) if onlevel_premium else 0.0

    return {
        "blended_expected_loss_cost": blended_expected_loss_cost,
        "expense_pct": expense_pct,
        "admin_pct": admin_pct,
        "capital_load_pct_of_loss_cost": capital_pct,
        "capital_load_amount": capital_load,
        "target_margin_pct": margin_pct,
        "technical_premium": technical_premium,
        "technical_price_as_pct_of_onlevel_premium": technical_price_ratio,
    }


# ===========================================================================
# TOOL 8: run_pricing_sensitivities
# ===========================================================================
@traced("run_pricing_sensitivities")
def run_pricing_sensitivities(base_case: dict, submission: dict, pipeline_fn) -> dict:
    """
    pipeline_fn(overrides: dict) -> technical_price dict
    Re-runs the full pipeline under bumped assumptions. Each scenario is
    computed via the same governed tools (so it is equally reproducible),
    just with one input perturbed at a time.
    """
    scenarios = {}

    scenarios["base_case"] = base_case["technical_premium"]

    # Attachment sensitivity: +/- 25%
    for label, mult in [("attachment_minus_25pct", 0.75), ("attachment_plus_25pct", 1.25)]:
        result = pipeline_fn({"attachment_mult": mult})
        scenarios[label] = result["technical_premium"]

    # Limit sensitivity: +/- 25%
    for label, mult in [("limit_minus_25pct", 0.75), ("limit_plus_25pct", 1.25)]:
        result = pipeline_fn({"limit_mult": mult})
        scenarios[label] = result["technical_premium"]

    # Trend sensitivity: +/- 200bps around approved trend (shown as informational
    # range only — does not replace the approved trend in the base case)
    for label, delta in [("trend_minus_200bps", -0.02), ("trend_plus_200bps", 0.02)]:
        result = pipeline_fn({"trend_delta": delta})
        scenarios[label] = result["technical_premium"]

    # Large-loss sensitivity: largest single event removed
    result = pipeline_fn({"remove_largest_loss": True})
    scenarios["largest_loss_removed"] = result["technical_premium"]

    base = base_case["technical_premium"]
    deltas_pct = {
        k: round((v - base) / base, 4) if base else 0.0
        for k, v in scenarios.items() if k != "base_case"
    }

    return {"scenario_technical_premiums": scenarios, "pct_change_vs_base": deltas_pct}
