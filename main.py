from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph

TICKER = "TTD"
TRADE_DATE = "2026-09-03"

# DEFAULT_CONFIG already applies TRADINGAGENTS_* env-var overrides
# (llm_provider, deep_think_llm, quick_think_llm, backend_url, etc.),
# so users can switch models or endpoints purely via .env without
# editing this script. Override individual keys here only when you
# want a hard-coded value that should ignore the environment.
config = DEFAULT_CONFIG.copy()
config["llm_provider"] = "google"
config["deep_think_llm"] = "gemini-3.1-flash-lite"
config["quick_think_llm"] = "gemini-3.1-flash-lite"

# Initialize with custom config
ta = TradingAgentsGraph(debug=True, config=config)

# forward propagate
final_state, decision = ta.propagate(TICKER, TRADE_DATE)
print(decision)

# Save the full report (per-section markdown + complete_report.md) to disk
report_path = ta.save_reports(final_state, TICKER)
print(f"Report saved to: {report_path}")

# Memorize mistakes and reflect
# ta.reflect_and_remember(1000) # parameter is the position returns
