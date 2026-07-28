"""
Treaty Pricing Submission Analyst Agent — orchestrator.

Runs the governed tool pipeline end to end on a submission and drafts a
pricing review note. Guardrails are enforced both inside individual tools
(unapproved assumptions) and at the memo-drafting stage (no commercial
recommendation, technical/commercial separation, full reproducibility).
"""
from __future__ import annotations
import copy
import re

import tools
import guardrails
import rag_store


class TreatyPricingAgent:
    def __init__(self, submission: dict):
        self.submission = submission
        self.results = {}

    # ------------------------------------------------------------------
    def run_pipeline(self, overrides: dict | None = None) -> dict:
        """
        Runs steps 1-7 of the methodology. `overrides` lets the sensitivity
        tool re-run the same governed pipeline with one perturbed input,
        without duplicating logic outside the tool layer.
        """
        overrides = overrides or {}
        sub = copy.deepcopy(self.submission)
        treaty_terms = dict(sub["treaty_terms"])
        current_year = sub["current_year"]
        peril = sub.get("peril", "all_peril_blend")

        if "attachment_mult" in overrides:
            treaty_terms["attachment"] = treaty_terms["attachment"] * overrides["attachment_mult"]
        if "limit_mult" in overrides:
            treaty_terms["limit"] = treaty_terms["limit"] * overrides["limit_mult"]

        # Step 1: validation (only recorded once, for the base case, to avoid
        # cluttering the trace with repeat validations during sensitivities)
        if not overrides:
            validation = tools.validate_pricing_submission(sub)
            self.results["validation"] = validation
            if validation["status"] == "FAIL":
                raise RuntimeError(f"Submission failed validation: {validation['issues']}")

        # Step 2: trend and on-level
        trended = tools.trend_and_onlevel_claims(
            losses_by_year=sub["losses_by_year"],
            premium_by_year=sub["premium_by_year"],
            tiv_by_year=sub["tiv_by_year"],
            current_year=current_year,
            peril=peril,
        )

        if "trend_delta" in overrides:
            # Informational trend sensitivity: re-apply an incremental bump on
            # top of the approved trend, clearly separate from the base-case
            # tool call above (which always uses the approved trend only).
            extra_years_factor = 1 + overrides["trend_delta"]
            for y, events in trended["trended_events_by_year"].items():
                for ev in events:
                    ev["trended_amount"] = round(ev["trended_amount"] * extra_years_factor, 2)

        losses_by_year_for_structure = trended["trended_events_by_year"]
        if overrides.get("remove_largest_loss"):
            losses_by_year_for_structure = copy.deepcopy(losses_by_year_for_structure)
            flat = [(y, i, ev["trended_amount"])
                    for y, evs in losses_by_year_for_structure.items()
                    for i, ev in enumerate(evs)]
            if flat:
                y_max, i_max, _ = max(flat, key=lambda t: t[2])
                del losses_by_year_for_structure[y_max][i_max]

        # Step 3: apply treaty structure
        structured = tools.apply_treaty_structure_to_losses(losses_by_year_for_structure, treaty_terms)

        # Step 4: burning cost
        burning = tools.calculate_burning_cost(
            structured["losses_to_layer_by_year"], trended["onlevel_premium_by_year"]
        )

        # Step 5: frequency/severity model
        current_onlevel_premium = trended["onlevel_premium_by_year"][max(trended["onlevel_premium_by_year"])]
        freqsev = tools.fit_frequency_severity_models(
            structured["event_detail_by_year"], current_onlevel_premium
        )

        # Step 6: credibility
        credibility = tools.calculate_credibility_weight(freqsev["n_claims_to_layer"])

        # Step 7: blend + technical price
        technical = tools.calculate_technical_price(
            experience_indication_loss_cost=burning["premium_weighted_average_burning_cost"] * current_onlevel_premium,
            exposure_indication_loss_cost=freqsev["modeled_expected_annual_loss_to_layer"],
            Z=credibility["Z"],
            onlevel_premium=current_onlevel_premium,
        )

        pipeline_result = {
            "trended": trended,
            "structured": structured,
            "burning": burning,
            "freqsev": freqsev,
            "credibility": credibility,
            "technical": technical,
            "technical_premium": technical["technical_premium"],
            "current_onlevel_premium": current_onlevel_premium,
        }

        if not overrides:
            self.results.update(pipeline_result)

        return pipeline_result

    # ------------------------------------------------------------------
    def run_sensitivities(self) -> dict:
        sens = tools.run_pricing_sensitivities(
            base_case=self.results["technical"],
            submission=self.submission,
            pipeline_fn=self.run_pipeline,
        )
        self.results["sensitivities"] = sens
        return sens

    # ------------------------------------------------------------------
    def draft_pricing_memo(self) -> str:
        r = self.results
        sub = self.submission
        val = r["validation"]
        trended = r["trended"]
        structured = r["structured"]
        burning = r["burning"]
        freqsev = r["freqsev"]
        cred = r["credibility"]
        tech = r["technical"]
        sens = r["sensitivities"]

        expiring_price = sub.get("expiring_technical_premium")
        rate_change = None
        if expiring_price:
            rate_change = round((tech["technical_premium"] - expiring_price) / expiring_price, 4)
            # Recorded as its own trace step (not one of the 8 governed tools,
            # but still logged) so the figure is reproducible from the trace,
            # per Guardrail 4.
            tools.TRACE.append({
                "step": len(tools.TRACE) + 1,
                "tool": "compare_to_expiring (internal, non-governed calculation)",
                "inputs": {"technical_premium": tech["technical_premium"], "expiring_technical_premium": expiring_price},
                "outputs": {"indicated_rate_change_vs_expiring": rate_change},
            })

        flags_block = "\n".join(f"  - {i}" for i in val["issues"]) or "  - None"

        memo = f"""
# PRICING REVIEW NOTE
**Cedant / Program:** {sub.get('cedant_name', 'N/A')}
**Layer:** {structured['limit']:,.0f} xs {structured['attachment']:,.0f} ({structured['reinstatements']} reinstatement(s))
**Renewal period incepting:** {sub.get('current_year')}
**Prepared by:** Treaty Pricing Submission Analyst Agent (draft — pending peer review)

---

## SECTION A: TECHNICAL PRICE INDICATION (Actuarial)

### 1. Submission Validation
Status: **{val['status']}**
Years of history supplied: {val['n_years']}
Data limitation flag: {val['data_limitation_flag']}
Issues identified:
{flags_block}

### 2. Trend & On-Level
- Peril basis: {trended['peril']}
- Approved loss trend applied: {trended['loss_trend_used']:.2%} per annum (per Approved Trend Assumptions doc)
- TIV/on-level trend applied: {trended['tiv_trend_used']:.2%} per annum

### 3. Treaty Structure Applied to Losses
- Attachment: {structured['attachment']:,.0f}
- Limit: {structured['limit']:,.0f}
- Reinstatements: {structured['reinstatements']}
- Annual aggregate limit to layer: {structured['annual_aggregate_limit']:,.0f}

### 4. Burning Cost
- Years used: {burning['years_used']}
- Premium-weighted average burning cost ratio: {burning['premium_weighted_average_burning_cost']:.2%}
- Simple average burning cost ratio: {burning['simple_average_burning_cost']:.2%}
- Year-to-year volatility (std dev of annual ratio): {burning['burning_cost_std_dev']:.2%}
- Experience indication (current premium basis): {burning['experience_indication_premium_basis']:,.0f}

### 5. Frequency / Severity (Exposure) Model
- Claims to layer in experience period: {freqsev['n_claims_to_layer']} over {freqsev['n_years']} years
- Fitted annual frequency (Poisson λ): {freqsev['frequency_lambda']}
- Fitted severity to layer (lognormal μ, σ): {freqsev['severity_lognormal_mu']}, {freqsev['severity_lognormal_sigma']}
- Modeled expected annual loss to layer: {freqsev['modeled_expected_annual_loss_to_layer']:,.0f}
- Exposure indication loss cost ratio: {freqsev['exposure_indication_loss_cost_ratio']:.2%}

### 6. Credibility
- Claims to layer: {cred['n_claims_to_layer']}
- Full credibility standard: {cred['full_credibility_standard']} claims
- Credibility weight Z applied to experience indication: {cred['Z']}
- Basis: {cred['note']}

### 7. Technical Price Build-Up
- Blended expected annual loss cost: {tech['blended_expected_loss_cost']:,.0f}
- Expense load: {tech['expense_pct']:.2%} | Internal admin: {tech['admin_pct']:.2%}
- Capital load (proxy, % of loss cost): {tech['capital_load_pct_of_loss_cost']:.2%} = {tech['capital_load_amount']:,.0f}
- Target technical margin: {tech['target_margin_pct']:.2%}
- **Technical Price Indication: {tech['technical_premium']:,.0f}**
  ({tech['technical_price_as_pct_of_onlevel_premium']:.2%} of current on-level subject premium)

### 8. Sensitivities
| Scenario | Technical Premium | % vs Base |
|---|---:|---:|
""" + "\n".join(
            f"| {k} | {v:,.0f} | {sens['pct_change_vs_base'].get(k, 0):+.2%} |"
            for k, v in sens["scenario_technical_premiums"].items()
        ) + f"""

### 9. Comparison to Expiring
- Expiring technical premium: {f'{expiring_price:,.0f}' if expiring_price else 'Not supplied'}
- Indicated technical rate change vs expiring: {f'{rate_change:+.2%}' if rate_change is not None else 'N/A'}

*This technical price indication is an actuarial reference point derived strictly
from the governed methodology, approved assumptions, and the tool trace below.
It is not a quote, bind instruction, or commercial recommendation.*

---

## SECTION B: COMMERCIAL DECISION (Underwriting — Not Completed by Agent)

- Final commercial price, terms, and any negotiation positioning: **[to be
  determined by underwriting management]**
- Ceding commission / brokerage terms: **[to be determined by underwriting management]**
- Competitive and portfolio considerations: **[to be determined by underwriting management]**
- Decision to quote/bind/decline: **[to be determined by underwriting management]**

---

## Peer-Review Checklist Status
Per the Peer-Review Checklist, this draft must be confirmed by a human peer
reviewer before release. See governed document `peer_review_checklist` for
the full sign-off list (assumption traceability, section separation,
sensitivity coverage, reproducibility).

## Appendix: Full Tool Call Trace (for reproducibility)
"""
        for entry in tools.TRACE:
            memo += f"\n**Step {entry['step']} — `{entry['tool']}`**\n- Inputs: `{entry['inputs']}`\n- Outputs: `{entry['outputs']}`\n"

        return memo.strip() + "\n"

    # ------------------------------------------------------------------
    def run_full_workflow(self) -> str:
        self.run_pipeline()
        self.run_sensitivities()
        memo = self.draft_pricing_memo()

        # Guardrail enforcement before releasing the memo
        guardrails.assert_no_commercial_recommendation(memo)
        guardrails.assert_sections_separated(memo)

        # Extract numbers from the memo, distinguishing percentage-formatted
        # values (e.g. "157.65%") from plain amounts, since the trace stores
        # percentages as fractions (0.1 not 10).
        memo_pct_scale, memo_dollar_scale = set(), set()
        for match in re.finditer(r"-?\d[\d,]*\.\d+(%)?", memo.replace(",", "")):
            raw = match.group(0)
            is_pct = raw.endswith("%")
            val = float(raw.rstrip("%"))
            if is_pct:
                memo_pct_scale.add(round(val / 100.0, 4))
            elif abs(val) <= 5:
                memo_pct_scale.add(round(val, 4))
            else:
                memo_dollar_scale.add(round(val, 0))

        trace_numbers_raw = tools.all_numeric_outputs_in_trace()
        # Compare percentage/ratio-scale numbers at fraction precision, and
        # large dollar amounts at whole-dollar precision.
        trace_pct_scale = {round(n, 4) for n in trace_numbers_raw if abs(n) <= 5}
        trace_dollar_scale = {round(n, 0) for n in trace_numbers_raw if abs(n) > 5}

        guardrails.assert_trace_covers_numbers(memo_pct_scale, trace_pct_scale, tolerance=0.0006)
        guardrails.assert_trace_covers_numbers(memo_dollar_scale, trace_dollar_scale, tolerance=1.0)

        return memo
