"""
Guardrails for the Treaty Pricing Submission Analyst Agent.

These are enforced in code (not just prompted), so a violation raises an
exception rather than silently producing a bad memo.

Guardrails implemented:
1. The agent cannot select an unapproved assumption (trend, credibility
   standard, expense/capital/margin loads).
2. The agent cannot quote, bind, or recommend a final commercial price.
3. The technical calculation and commercial decision must be clearly
   separated in any output document.
4. Every numeric result must be reproducible from the tool trace.
"""
from __future__ import annotations
import re
from rag_store import (
    APPROVED_TREND_ASSUMPTIONS,
    APPROVED_TIV_TREND_DEFAULT,
    CREDIBILITY_STANDARD,
    EXPENSE_CAPITAL_PARAMETERS,
)


class GuardrailViolation(Exception):
    """Raised when the agent attempts an action outside its governed bounds."""


# --------------------------------------------------------------------------
# Guardrail 1: no unapproved assumptions
# --------------------------------------------------------------------------
def assert_approved_trend(peril: str, trend: float) -> None:
    peril_key = peril.lower().replace(" ", "_")
    approved = APPROVED_TREND_ASSUMPTIONS.get(peril_key)
    if approved is None:
        raise GuardrailViolation(
            f"Peril '{peril}' has no approved trend on file. Escalate to Chief "
            f"Actuary; the agent cannot invent a trend assumption. Approved "
            f"perils: {list(APPROVED_TREND_ASSUMPTIONS)}"
        )
    if abs(trend - approved) > 1e-9:
        raise GuardrailViolation(
            f"Trend {trend:.3%} for peril '{peril}' does not match the approved "
            f"assumption {approved:.3%}. The agent cannot select an unapproved "
            f"assumption without Chief Actuary sign-off."
        )


def assert_approved_tiv_trend(tiv_trend: float) -> None:
    if abs(tiv_trend - APPROVED_TIV_TREND_DEFAULT) > 1e-9:
        raise GuardrailViolation(
            f"TIV trend {tiv_trend:.3%} does not match the approved default "
            f"{APPROVED_TIV_TREND_DEFAULT:.3%} and no submission-evidenced "
            f"override was supplied with justification."
        )


def assert_approved_expense_capital_margin(expense_pct, admin_pct, capital_pct, margin_pct) -> None:
    p = EXPENSE_CAPITAL_PARAMETERS
    checks = {
        "expense_pct": (expense_pct, p["default_cedant_expense_pct"]),
        "admin_pct": (admin_pct, p["internal_admin_pct"]),
        "capital_pct": (capital_pct, p["capital_load_proxy_pct"]),
        "margin_pct": (margin_pct, p["target_margin_pct"]),
    }
    for name, (used, approved) in checks.items():
        if abs(used - approved) > 1e-9:
            raise GuardrailViolation(
                f"{name} value {used:.3%} does not match approved parameter "
                f"{approved:.3%} in the Expense and Capital Parameters document."
            )


def assert_credibility_standard(full_credibility_claims: int) -> None:
    if full_credibility_claims != CREDIBILITY_STANDARD["full_credibility_claims"]:
        raise GuardrailViolation(
            "Full credibility claim count does not match the approved "
            "credibility standard on file."
        )


# --------------------------------------------------------------------------
# Guardrail 2 & 3: no commercial recommendation; technical/commercial separation
# --------------------------------------------------------------------------
_FORBIDDEN_COMMERCIAL_PHRASES = [
    r"\bwe recommend (a )?(final )?price\b",
    r"\bwe recommend (quoting|binding)\b",
    r"\bquote (at|of)\b",
    r"\bbind (at|the treaty)\b",
    r"\brecommended (renewal|commercial) price\b",
    r"\bfinal price should be\b",
    r"\bagent recommends binding\b",
]


def assert_no_commercial_recommendation(memo_text: str) -> None:
    lowered = memo_text.lower()
    for pattern in _FORBIDDEN_COMMERCIAL_PHRASES:
        if re.search(pattern, lowered):
            raise GuardrailViolation(
                f"Draft memo contains commercial-recommendation language "
                f"matching pattern '{pattern}'. The agent may only produce a "
                f"technical price indication, never a quote/bind/commercial "
                f"recommendation."
            )


def assert_sections_separated(memo_text: str) -> None:
    required_markers = [
        "SECTION A: TECHNICAL PRICE INDICATION",
        "SECTION B: COMMERCIAL DECISION",
    ]
    for marker in required_markers:
        if marker not in memo_text:
            raise GuardrailViolation(
                f"Memo is missing required section marker '{marker}'. Technical "
                f"and commercial content must be clearly separated."
            )
    idx_a = memo_text.index("SECTION A: TECHNICAL PRICE INDICATION")
    idx_b = memo_text.index("SECTION B: COMMERCIAL DECISION")
    if idx_b < idx_a:
        raise GuardrailViolation("Commercial section must not precede the technical section.")


# --------------------------------------------------------------------------
# Guardrail 4: reproducibility from tool trace
# --------------------------------------------------------------------------
def assert_trace_covers_numbers(memo_numbers: set, trace_numbers: set, tolerance=1e-6) -> None:
    """
    Every rounded numeric value quoted in the memo must be traceable to a
    value produced somewhere in the tool call trace (within tolerance).
    """
    unexplained = []
    for n in memo_numbers:
        if not any(abs(n - t) <= max(tolerance, abs(t) * 1e-6) for t in trace_numbers):
            unexplained.append(n)
    if unexplained:
        raise GuardrailViolation(
            f"The following numeric values in the memo could not be matched to "
            f"any tool trace output and are not reproducible: {unexplained}"
        )
