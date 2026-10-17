"""Model-call protocol + JSON helpers.

jobbuddy does NOT call any external LLM API. The intelligence work (scoring, tailoring, cover letters,
answers) is done by the on-box AI agent via `agent-pull` / `agent-push-*` (see jobbuddy/prompts/).
`LLMClient` remains as a protocol so library functions can be exercised with a fake in tests; in CI and
the CLI it is always None and the deterministic paths run.
"""
from jobbuddy.llm.client import (  # noqa: F401
    BudgetExceeded,
    LLMClient,
    LLMResponse,
    TokenBudget,
    parse_json_response,
)
