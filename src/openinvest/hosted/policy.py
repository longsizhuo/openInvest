"""Hosted-only capability boundary; local/private MCP registration stays unchanged."""
ALLOWED = frozenset({
    "doctor", "status", "strategy", "history", "live_prices", "what_if", "rebalance_plan",
    "discipline", "decisions", "explain_decision", "record_execution", "buy", "sell", "deposit", "withdraw",
    "record_existing_position", "record_dividend", "record_fee", "record_split", "record_return_of_capital",
    "place_fund_order", "confirm_fund_order", "cancel_fund_order", "set_allocations", "track_asset",
    "untrack_asset", "prepare_committee", "submit_committee",
})
MONEY = frozenset({"buy", "sell", "deposit", "withdraw", "record_existing_position", "record_dividend",
                   "record_fee", "record_split", "record_return_of_capital", "place_fund_order",
                   "confirm_fund_order", "cancel_fund_order"})
READ = frozenset({"doctor", "status", "strategy", "history", "live_prices", "what_if", "rebalance_plan",
                  "discipline", "decisions", "explain_decision"})


def scope_for(name):
    return "invest:read" if name in READ else "invest:analyze" if name in {"prepare_committee", "submit_committee"} else "invest:write"
