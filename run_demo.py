"""
Demo: run the Treaty Pricing Submission Analyst Agent on a synthetic
property-catastrophe XL renewal submission.
"""
from agent import TreatyPricingAgent

SUBMISSION = {
    "cedant_name": "Example Mutual Insurance Co.",
    "peril": "wind",
    "current_year": 2027,
    "treaty_terms": {
        "attachment": 50_000_000,
        "limit": 50_000_000,
        "reinstatements": 1,
        "reinstatement_cost_pct": 1.0,
    },
    "expiring_technical_premium": 7_800_000,
    "tiv_by_year": {
        2017: 8_200_000_000, 2018: 8_500_000_000, 2019: 8_900_000_000,
        2020: 9_300_000_000, 2021: 9_800_000_000, 2022: 10_400_000_000,
        2023: 11_000_000_000, 2024: 11_700_000_000, 2025: 12_300_000_000,
        2026: 12_900_000_000, 2027: 13_500_000_000,
    },
    "premium_by_year": {
        2017: 5_400_000, 2018: 5_600_000, 2019: 5_900_000,
        2020: 6_100_000, 2021: 6_400_000, 2022: 6_800_000,
        2023: 7_100_000, 2024: 7_400_000, 2025: 7_700_000,
        2026: 8_000_000,
    },
    "losses_by_year": {
        2017: [{"event": "Hurricane A", "amount": 20_000_000}],
        2018: [],
        2019: [{"event": "Windstorm B", "amount": 65_000_000}],
        2020: [],
        2021: [{"event": "Hurricane C", "amount": 110_000_000}],
        2022: [],
        2023: [{"event": "Windstorm D", "amount": 15_000_000}],
        2024: [],
        2025: [{"event": "Hurricane E", "amount": 78_000_000}],
        2026: [{"event": "Windstorm F", "amount": 5_000_000}],
    },
}

if __name__ == "__main__":
    agent = TreatyPricingAgent(SUBMISSION)
    memo = agent.run_full_workflow()
    with open("pricing_review_note.md", "w") as f:
        f.write(memo)
    print(memo)
    print("\n\n=== Guardrail checks passed. Memo written to pricing_review_note.md ===")
