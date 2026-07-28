"""
Lightweight RAG document store for the Treaty Pricing Submission Analyst Agent.

In production this would be a vector index over the governed document set.
Here it is a simple named-document loader so the agent always retrieves
assumptions from the approved source of truth rather than inventing them.
"""
from pathlib import Path

DOCS_DIR = Path(__file__).parent / "rag_documents"

_DOC_FILES = {
    "methodology": "01_pricing_methodology.md",
    "trend_assumptions": "02_approved_trend_assumptions.md",
    "expense_capital": "03_expense_and_capital_parameters.md",
    "underwriting_guidelines": "04_underwriting_guidelines.md",
    "peer_review_checklist": "05_peer_review_checklist.md",
}


def get_doc(name: str) -> str:
    if name not in _DOC_FILES:
        raise KeyError(f"Unknown RAG document '{name}'. Known: {list(_DOC_FILES)}")
    return (DOCS_DIR / _DOC_FILES[name]).read_text()


def list_docs():
    return list(_DOC_FILES.keys())


# ---------------------------------------------------------------------------
# Structured, machine-readable extracts of the governed documents.
# These mirror what a real RAG pipeline would parse out of the markdown docs
# above. Keeping them structured lets the tools/guardrails enforce them
# programmatically instead of re-parsing prose at run time.
# ---------------------------------------------------------------------------

APPROVED_TREND_ASSUMPTIONS = {
    "wind": 0.060,
    "earthquake": 0.035,
    "severe_convective_storm": 0.080,
    "wildfire": 0.075,
    "all_peril_blend": 0.065,   # fallback/default
}

APPROVED_TIV_TREND_DEFAULT = 0.040

CREDIBILITY_STANDARD = {
    "full_credibility_claims": 15,
    "min_claims_floor": 3,
    "min_claims_floor_cap_z": 0.25,
}

EXPENSE_CAPITAL_PARAMETERS = {
    "default_cedant_expense_pct": 0.10,
    "internal_admin_pct": 0.015,
    "capital_load_proxy_pct": 0.12,   # applied to blended expected loss cost
    "target_margin_pct": 0.06,
}

MIN_YEARS_HISTORY_FULL = 10
MIN_YEARS_HISTORY_FLAGGED = 5
