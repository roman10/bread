# bread + Claude AI Integration Plan (Claude Max Plan)

## Context

bread is a fully automated Python swing trading bot (ETF momentum strategies, Alpaca broker, 15-min tick cycle, pure rule-based technical analysis, no LLM integration). The goal is to add a Claude AI layer for research, signal validation, and event monitoring.

**Key constraint: No Anthropic API SDK.** The user has a Claude Max Plan and all integration must go through Claude Code CLI or mcode MCP — zero API billing.

---

## Integration Backends Available (all use Claude Max Plan)

| Backend | How | Latency | Structured Output | Web Search | Independence |
|---------|-----|---------|-------------------|------------|-------------|
| **Claude Code CLI** | `claude -p "prompt" --output-format json` | 5-30s | Yes (`--json-schema`) | Yes | Standalone |
| **mcode MCP** | HTTP to `localhost:7532/mcp` → session/task | 10-60s | No (terminal buffer) | Yes | Requires mcode |
| **Claude Agent SDK** | `claude-agent-sdk` Python pkg | 3-15s | Yes (native) | Yes | Needs API key (**NOT Max Plan**) |

**Claude Agent SDK is ruled out** — it requires `ANTHROPIC_API_KEY` (pay-per-token), not Max Plan.

---

## Recommended Architecture: CLI-First, mcode-Optional

### Why CLI is the primary backend:
1. **Structured output** — `--output-format json` + `--json-schema` returns validated JSON matching a Pydantic-compatible schema. This is critical for getting typed `approve/reject` responses.
2. **No runtime dependency** — works standalone, no Electron app needed.
3. **Web search built-in** — `WebSearch` tool available in CLI.
4. **Model selection** — `--model sonnet` or `--model opus` or `--model haiku`.
5. **System prompt control** — `--append-system-prompt` for role injection.
6. **Tool control** — `--allowedTools "WebSearch,Read"` to restrict capabilities.
7. **5-30s latency** is fine in a 15-min tick cycle.

### When mcode MCP adds value (optional, Phase 2+):
- Visual monitoring of Claude's research in the mcode UI
- Session persistence across multiple prompts (conversational context)
- Task queue with retry and scheduling
- But: output is terminal buffer text (no structured JSON), requires mcode running

### Architecture Diagram

```
bread tick loop (every 15 min)
    │
    ├─ [Strategy.evaluate()] ──────── ClaudeStrategy calls CLI (Phase 4)
    │                                   claude -p "analyze {data}" --json-schema {...}
    │
    ├─ [ExecutionEngine.process_signals()]
    │   │
    │   ├── risk_manager.evaluate()        (existing, deterministic)
    │   │
    │   ├── get_active_alerts()            query event_alerts table for context
    │   │
    │   └── claude_client.review_signal()  approve/reject with event context
    │         │
    │         └── CLI: claude -p "review this signal" --json-schema {approve/reject}
    │
    └─ [broker.submit_bracket_order()]


APScheduler IntervalTrigger (every 4 hours, market hours only):
    run_research_scan()
        └── claude_client.research_events()
              └── CLI: claude -p "search for events" --allowedTools "WebSearch,WebFetch"
              └── store results → event_alerts table
              └── notify high-severity events via Discord
```

---

## Feasibility Assessment

### Use Case 1: Claude as strategy / part of strategy
**Feasible: YES** — CLI with `--json-schema` can return `Signal`-compatible structured output. bread's `Strategy` ABC is a clean integration point. A `ClaudeStrategy` registered via `@register("claude_analyst")` fits the existing pattern. The CLI call takes 5-15s, well within 15-min tick budget.

### Use Case 2: Claude as order confirmation (replace human)
**Feasible: YES, highest value** — Clean insertion at `engine.py:214-228` (between risk approval and order submission). Pass signal + portfolio context via CLI prompt, get structured `{approved: bool, reasoning: str}` back via `--json-schema`. Advisory mode by default.

### Use Case 3: Event monitoring / online search
**Feasible: YES** — CLI with `--allowedTools "WebSearch,WebFetch"` does web research. Run on APScheduler interval, store results in SQLite, surface via alerts and signal review enrichment. This is where Claude shines — qualitative analysis that rule-based systems can't do.

---

## Stack-Ranked Use Cases

| Rank | Use Case | Value | Effort | Status |
|------|----------|-------|--------|--------|
| 1 | **Signal review before execution** | HIGH | LOW | ✅ Phase 2 |
| 2 | **Event monitoring + web search** | HIGH | MEDIUM | ✅ Phase 3 |
| 3 | **Claude-powered strategy** | MEDIUM | MEDIUM | ✅ Phase 4 |
| 4 | **Trade narrative / journaling** | LOW | LOW | ⬜ Deferred |
| 5 | **Market regime detection** | MEDIUM | HIGH | ⬜ Deferred |

---

## Foundation Module Design

### Module: `src/bread/ai/`

```
src/bread/ai/
    __init__.py      # Re-exports: ClaudeClient, CliResponse, EventAlert, MarketResearch, SignalReview, TradeContext
    cli_backend.py   # CliBackend — subprocess wrapper for `claude -p`
    client.py        # ClaudeClient — orchestrator (circuit breaker, batching, usage tracking)
    models.py        # Response dataclasses + JSON schemas: CliResponse, TradeContext, SignalReview, EventAlert, MarketResearch
    prompts.py       # System prompts + prompt builders for review and research
    research.py      # Event monitoring — run_research_scan(), get_active_alerts(), symbol collection
```

`ClaudeSettings` lives in `core/config.py` alongside `AlpacaSettings` (follows existing convention).

### `cli_backend.py` — Core Integration

```python
class CliBackend:
    """Calls Claude Code CLI via subprocess, uses Max Plan."""

    def query(
        self,
        prompt: str,
        *,
        json_schema: dict | None = None,      # For structured output
        system_prompt: str | None = None,       # --append-system-prompt
        model: str = "sonnet",                  # --model
        allowed_tools: list[str] | None = None, # --allowedTools
        max_turns: int = 3,                     # --max-turns (limit agent loops)
        timeout: int = 60,                      # subprocess timeout
    ) -> CliResponse:
        """Run `claude -p` and return parsed response."""
        args = ["claude", "-p", prompt, "--output-format", "json"]
        if json_schema:
            args += ["--json-schema", json.dumps(json_schema)]
        if system_prompt:
            args += ["--append-system-prompt", system_prompt]
        if model:
            args += ["--model", model]
        if allowed_tools:
            args += ["--allowedTools", ",".join(allowed_tools)]
        args += ["--max-turns", str(max_turns)]

        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return self._parse_response(result)
```

Key behaviors:
- **Structured output**: `--json-schema` validates Claude's response against a schema. bread defines schemas matching its response dataclasses (SignalReview, MarketResearch, etc.)
- **Timeout handling**: `subprocess.run(timeout=60)` kills the process on timeout
- **Error handling**: Parse stderr, detect rate limits, handle non-zero exit codes
- **Model selection**: Default to Sonnet for quality/speed balance; Haiku for simple reviews; Opus for deep research

### `client.py` — Orchestrator

```python
class ClaudeClient:
    def __init__(self, config: ClaudeSettings, session_factory):
        self._backend = CliBackend(config)
        self._session_factory = session_factory
        self._circuit_breaker = CircuitBreaker(max_failures=3, cooldown_seconds=300)

    def review_signal(self, signal, context, event_alerts=None) -> SignalReview:
        """Ask Claude to review a trading signal. Returns approve/reject."""

    def review_signals_batch(self, signals, context, event_alerts=None) -> list[SignalReview]:
        """Batch-review multiple signals in one CLI call. Fail-open."""

    def research_events(self, symbols, held_symbols) -> MarketResearch:
        """Web search for market-moving events. Uses WebSearch + WebFetch."""

    def _call(self, prompt, *, json_schema, system_prompt, model, ...) -> CliResponse:
        """Execute CLI call with circuit-breaker + usage logging."""
```

### `models.py` — Response Types + JSON Schemas

```python
@dataclass(frozen=True)
class SignalReview:          # Phase 1 — approve/reject with confidence
    approved: bool
    confidence: float        # 0.0-1.0
    reasoning: str
    risk_flags: list[str]

@dataclass(frozen=True)
class EventAlert:            # Phase 3 — single market-moving event
    symbol: str
    severity: str            # "high" | "medium" | "low" | "none"
    headline: str
    details: str
    event_type: str          # "earnings" | "fda" | "analyst" | "macro" | "sector" | "other"
    source: str

@dataclass(frozen=True)
class MarketResearch:        # Phase 3 — research scan response
    events: list[EventAlert]
    scan_summary: str

# All models have: json_schema() classmethod, from_dict() with defensive type coercion
```

### Key Design Decisions

1. **CLI subprocess, not SDK** — Uses Claude Max Plan, zero API cost.

2. **`--json-schema` for structured output** — Claude validates its response against the schema before returning. This gives typed, parseable responses without fragile text parsing.

3. **Circuit breaker** — After 3 consecutive CLI failures (timeout, crash, rate limit), disable Claude calls for 5 minutes. Fall back to pure rule-based execution. Never let Claude instability prevent trading.

4. **Advisory mode by default** — `review_mode: "advisory" | "gating"` config. Advisory logs Claude's opinion but does NOT block order submission. Gating requires Claude approval. Default: advisory (risk manager is deterministic and battle-tested).

5. **Batch multiple signals per CLI call** — If 3 BUY signals pass risk validation, don't spawn 3 separate CLI processes (30s). Instead, batch them into one prompt ("Review these 3 signals") and get a list of reviews back in one 10-15s call. Reduces latency proportionally with signal count.

6. **Synchronous in tick loop** — A single batched CLI call (5-15s) is negligible in a 15-min cycle. No async needed for Use Cases 1 & 2. Use Case 3 (event monitoring) runs on an APScheduler `IntervalTrigger`.

7. **Usage tracking in SQLite** — New `claude_usage_log` table records every call (model, prompt length, duration, result). For monitoring usage patterns even though Max Plan is "unlimited."

8. **Text-parse fallback** — If `--json-schema` fails or produces unexpected output, fall back to parsing Claude's text response with a regex/heuristic. Belt-and-suspenders for the most fragile part of the integration.

### Config Additions

In `config/default.yaml`:
```yaml
claude:
  enabled: false
  cli_path: "claude"                 # path to claude binary
  default_model: "sonnet"            # haiku | sonnet | opus
  review_model: "sonnet"             # model for signal review
  research_model: "sonnet"           # model for event research
  timeout_seconds: 60                # CLI subprocess timeout
  max_turns: 3                       # limit agent loops per call
  review_mode: "advisory"            # advisory | gating
  research_enabled: false            # enable background event monitoring
  research_interval_hours: 4         # how often to scan for events
  circuit_breaker_max_failures: 3
  circuit_breaker_cooldown_seconds: 300
```

In `core/config.py` — `ClaudeSettings` Pydantic model on `AppConfig`, `on_research: bool` on `AlertSettings`.

### Integration Points in Existing Code

1. **`execution/engine.py`** — Three-phase BUY loop: risk approval → `get_active_alerts()` for event context → `review_signals_batch()` with event enrichment → order submission. ✅ Done (Phase 2+3)

2. **`app.py::run()`** — Initializes `ClaudeClient` when `claude.enabled`, passes to `ExecutionEngine`. Adds `event_research` scheduler job when `research_enabled`. Enriches BUY trade alerts with Claude reasoning. ✅ Done (Phase 2+3)

3. **`db/models.py`** — `ClaudeUsageLog` (all calls) + `EventAlertLog` (research events). ✅ Done (Phase 1+3)

4. **`monitoring/alerts.py`** — `notify_event_alert()` for high-severity events, `notify_trade()` enriched with AI reasoning. ✅ Done (Phase 2+3)

5. **`strategy/claude_analyst.py`** — New `@register("claude_analyst")` strategy. `app.py` uses `inspect.signature` to auto-detect and pass `claude_client`. ✅ Done (Phase 4)

---

## Implementation Phases

### Phase 1: Foundation — COMPLETE (ec2a98a)
**New files (4):**
- `src/bread/ai/__init__.py` — package with re-exports
- `src/bread/ai/cli_backend.py` — `CliBackend` subprocess wrapper for `claude -p --output-format json`, parses JSON envelope (`result`/`structured_output`), text-parse fallback, maps exceptions (`FileNotFoundError` → `ClaudeCliNotFoundError`, `TimeoutExpired` → `ClaudeTimeoutError`)
- `src/bread/ai/client.py` — `ClaudeClient` with `CircuitBreaker` (3-state: CLOSED→OPEN→HALF_OPEN→CLOSED), usage logging to SQLite, `review_signal()` method. `_call()` wraps every CLI invocation with circuit-breaker + logging.
- `src/bread/ai/models.py` — `CliResponse` (parsed envelope), `TradeContext` (portfolio snapshot), `SignalReview` (approve/reject with `json_schema()` and `from_dict()` classmethods)

**Modified files (4):**
- `src/bread/core/config.py` — `ClaudeSettings` Pydantic model added to `AppConfig` (follows `AlertSettings` pattern, `enabled: false` default)
- `src/bread/core/exceptions.py` — `ClaudeError` hierarchy: `ClaudeTimeoutError`, `ClaudeParseError`, `ClaudeUnavailableError`, `ClaudeCliNotFoundError`
- `src/bread/db/models.py` — `ClaudeUsageLog` table (model, use_case, prompt_length, duration_ms, success, error, cost_usd, tokens)
- `config/default.yaml` — `claude:` section (disabled by default)

**Tests (2) — 40 tests:**
- `tests/unit/test_cli_backend.py` — 23 tests: arg building, structured output, error handling, JSON fallback
- `tests/unit/test_claude_client.py` — 17 tests: circuit breaker states, signal review, usage logging, DB failure resilience

**Key implementation details:**
- CLI envelope: `--json-schema` puts structured data in `envelope["structured_output"]`, not `envelope["result"]`
- `MarketResearch` model deferred to Phase 3, `review_signals_batch()` to Phase 2
- `CircuitBreaker` uses `time.monotonic()` for clock-change-safe cooldown timing
- `_log_usage()` swallows all exceptions — DB failures never block trading

### Phase 2: Signal Review — COMPLETE (1311ebd)
**New files (2):**
- `src/bread/ai/prompts.py` — `REVIEW_SYSTEM_PROMPT`, `BATCH_REVIEW_SYSTEM_PROMPT`, `build_single_review_prompt()`, `build_batch_review_prompt()`. Prompt templates extracted from `client.py` and centralized.
- `tests/unit/test_prompts.py` — 7 tests: signal/context field presence, batch numbering, shared context, ordering instruction

**Modified files (5):**
- `src/bread/ai/models.py` — added `SignalReview.batch_json_schema()` classmethod (wraps array in `{"reviews": [...]}` for CLI compatibility with `CliResponse.result: dict | str`)
- `src/bread/ai/client.py` — refactored to use `prompts.py`; added `review_signals_batch()` (batches N signals into one CLI call), `_parse_batch_reviews()`, `_DEFAULT_REVIEW` fail-open sentinel
- `src/bread/execution/engine.py` — three-phase BUY loop (risk approval → Claude review → order submission); added `_claude_review_batch()`, `get_last_review()`, `_last_reviews` dict; constructor accepts optional `claude_client`
- `src/bread/app.py` — initializes `ClaudeClient` when `claude.enabled`, passes to `ExecutionEngine`; enriches BUY trade alerts with Claude reasoning via `get_last_review()`
- `tests/unit/test_claude_client.py` — 11 new batch tests: empty/single/multi signals, batch schema, fail-open on CLI failure/malformed/length mismatch, usage logging
- `tests/unit/test_execution_engine.py` — 10 new Claude integration tests: no-client/disabled/advisory/gating modes, fail-open on error, risk-only filtering, review storage/clearing, mixed approvals

**Key implementation details:**
- Batch schema wraps array in object (`{"reviews": [...]}`) to keep `CliResponse.result` typed as `dict | str`
- Three-phase BUY loop: Phase A collects risk-approved signals (decrements `buying_power` per approval), Phase B does one Claude batch review, Phase C submits orders
- Advisory mode (default): logs Claude opinion, submits all orders regardless. Gating mode: blocks Claude-rejected signals, logs `REJECTED` OrderLog with reasoning
- Fail-open everywhere: Claude errors → all signals proceed; circuit breaker open → no review; malformed response → default approved reviews
- Single signal optimizes to `review_signal()` (existing method); 2+ signals batch into one CLI call
- `_DEFAULT_REVIEW` is a frozen `SignalReview(approved=True, confidence=0.0)` — safe to share references
- Alert enrichment: `app.py` appends `| AI: {reasoning[:150]}` to BUY trade notifications via `engine.get_last_review()`

**Tests: 28 new (total 68 across AI module + engine integration)**

### Phase 3: Event Monitoring — COMPLETE (1dfa954)
**New files (3):**
- `src/bread/ai/research.py` — `run_research_scan()` orchestrator (APScheduler job, not background thread), `collect_research_symbols()` (30-symbol cap, held first), `get_active_alerts()` (DB query for active high/medium events, fail-open). Private helpers: `_store_results()`, `_deactivate_stale_alerts()` (48h), `_send_high_severity_alerts()`
- `tests/unit/test_research.py` — 14 tests: symbol collection, DB storage, staleness, alerts, fail-open
- `tests/unit/test_market_research_models.py` — 13 tests: EventAlert/MarketResearch from_dict, schema, validation

**Modified files (9):**
- `src/bread/ai/models.py` — Added `EventAlert` (frozen, severity validation, `from_dict()`) and `MarketResearch` (frozen, `json_schema()`, `from_dict()` with defensive parsing)
- `src/bread/ai/client.py` — Added `research_events()` method using `WebSearch`/`WebFetch` tools, research_model, max_turns=8, timeout=120. Added `event_alerts` parameter to `review_signal()` and `review_signals_batch()` for enrichment
- `src/bread/ai/prompts.py` — Added `RESEARCH_SYSTEM_PROMPT`, `build_research_prompt()`, `format_event_context()`. Modified `build_single_review_prompt()` and `build_batch_review_prompt()` to accept optional `event_alerts` parameter
- `src/bread/ai/__init__.py` — Exports `EventAlert`, `MarketResearch`
- `src/bread/db/models.py` — Added `EventAlertLog` table (symbol, severity, headline, details, event_type, source, scan_summary, is_active, scanned_at_utc) with indexes
- `src/bread/core/config.py` — Added `on_research: bool = True` to `AlertSettings`
- `config/default.yaml` — Added `on_research: true` to alerts section
- `src/bread/monitoring/alerts.py` — Added `notify_event_alert()` method (WARNING-level, gated by `on_research`)
- `src/bread/execution/engine.py` — `_claude_review_batch()` now queries `get_active_alerts()` and passes event context to review prompts
- `src/bread/app.py` — Added `event_research` scheduler job (`IntervalTrigger`, market-hours guard, collects symbols from engine + strategies)

**Key implementation details:**
- Scheduled function via APScheduler (not background thread) — matches existing `tick()` pattern
- DB as communication channel between research and tick loop — no shared mutable state
- Fail-open everywhere: `run_research_scan()` swallows all exceptions, `get_active_alerts()` returns `[]` on error
- Shared circuit breaker for research + review calls
- Signal review enrichment closes the value loop: research → store → enrich reviews → better decisions
- Dashboard event display deferred to Phase 3b

**Tests: 31 new (total ~100 across AI module + engine integration)**

### Phase 3b: Dashboard Event Display — COMPLETE (5aa86f4)
**Modified files (2):**
- `src/bread/dashboard/data.py` — Added `get_recent_events()` method querying `EventAlertLog` with 48h window, formatted for AG Grid display
- `src/bread/dashboard/pages/portfolio.py` — Added event alerts AG Grid table with severity color-coding (HIGH=red, MEDIUM=amber, LOW=gray), headline tooltips showing details on hover, auto-refresh callback

**Key implementation details:**
- No `is_active` filter — shows all recent events for visibility (active/inactive is a backend concern for signal review enrichment)
- Pagination at 10 rows per page (events are less frequent than signals)
- Placed at bottom of portfolio page — dashboard flows from actionable (KPIs, positions) to contextual (signals, events)

### Phase 4: Claude Strategy — COMPLETE (cd37cd0)
**New files (3):**
- `src/bread/strategy/claude_analyst.py` — `@register("claude_analyst")` strategy: compresses enriched DataFrames into compact text summaries (price returns, RSI, SMAs, EMAs, MACD, Bollinger Bands, ATR, volume), sends one batched Claude CLI call per tick, converts structured BUY/SELL/HOLD recommendations into Signal objects. ATR-based stop loss (deterministic). Fail-safe: returns no signals on any Claude error.
- `config/strategies/claude_analyst.yaml` — Universe (SPY, QQQ, IWM, DIA, XLF, XLK), ATR stop mult 1.5, time stop 15 days
- `tests/unit/test_claude_analyst.py` — 15 tests: construction, summary generation, evaluate happy path/errors/edge cases

**Modified files (8):**
- `src/bread/ai/models.py` — Added `StrategyRecommendation` (frozen, action validation, `from_dict()` with clamping) and `StrategyAnalysis` (frozen, `json_schema()`, `from_dict()` skips malformed recs)
- `src/bread/ai/client.py` — Added `analyze_technicals()` method using `STRATEGY_SYSTEM_PROMPT`, `strategy_model`, and `StrategyAnalysis` schema
- `src/bread/ai/prompts.py` — Added `STRATEGY_SYSTEM_PROMPT` constant for technical analysis role
- `src/bread/ai/__init__.py` — Exports `StrategyRecommendation`, `StrategyAnalysis`
- `src/bread/core/config.py` — Added `strategy_model: str = "sonnet"` to `ClaudeSettings`
- `config/default.yaml` — Added `strategy_model: "sonnet"` to claude section; added `claude_analyst` strategy entry (disabled by default, paper-only)
- `src/bread/app.py` — Uses `inspect.signature` to auto-detect strategies that accept `claude_client` kwarg and passes it. Skips with warning if Claude is disabled.
- `tests/unit/test_claude_client.py` — 5 new tests: `analyze_technicals()` success, model selection, schema, parse error, usage logging

**Key implementation details:**
- One CLI call per tick, all symbols batched (~3500 input tokens for 10 symbols)
- Column names derived from `indicator_settings` (resilient to config changes)
- SELL signals always get `strength=1.0`; BUY uses Claude's recommendation
- `inspect.signature`-based dependency injection — no magic class attributes, constructor IS the spec
- Pure technical analysis — no event enrichment (events already injected during signal review in Phase 2+3)
- Shared circuit breaker across strategy analysis, signal review, and research

**Tests: 20 new (total 596 across full unit suite)**

### Phase 5 (optional): mcode MCP Integration
- Add `src/bread/ai/mcode_backend.py` — HTTP client for mcode MCP
- Session persistence for multi-turn research conversations
- Visual monitoring of Claude's analysis in mcode UI

---

## Verification Plan

1. **Unit tests** (596 passing): `pytest tests/unit/`
   - `test_cli_backend.py` — 23 tests: arg building, structured output, error handling, JSON fallback
   - `test_claude_client.py` — 37 tests: circuit breaker, signal review, batch review, research events, strategy analysis, usage logging
   - `test_prompts.py` — 16 tests: review prompts, batch prompts, research prompts, event context formatting
   - `test_research.py` — 15 tests: symbol collection, DB storage, staleness, alerts, fail-open
   - `test_market_research_models.py` — 13 tests: EventAlert/MarketResearch dataclasses, schemas, validation
   - `test_execution_engine.py` — 10 tests: advisory/gating modes, fail-open, review storage, mixed approvals
   - `test_claude_analyst.py` — 15 tests: construction, summary generation, evaluate happy path/errors/edge cases

2. **Type check**: `mypy src/` — strict mode, no errors

3. **Integration test**: With Claude Code CLI installed, run a real `claude -p "What is 2+2?" --output-format json` and verify parsing

4. **Manual test**: Run `bread run --mode paper` with `claude.enabled: true, claude.research_enabled: true`:
   - Verify Claude review logs appear for BUY signals
   - Verify event context appears in review prompts when events exist
   - Verify research scan logs appear on schedule (every N hours, market hours only)
   - Verify `event_alerts` table populated with high/medium/low events
   - Verify high-severity events trigger Discord alerts
   - Verify circuit breaker activates when CLI is unavailable
   - Verify trading continues normally when Claude is disabled or errors
   - Verify `claude_usage_log` table records all calls (review + research)
   - Check that total tick time stays under 15 minutes
   - Verify `claude_analyst` strategy emits signals during tick cycle when enabled
   - Verify `claude_analyst` is skipped with warning when `claude.enabled: false`
   - Verify `strategy_analysis` entries appear in `claude_usage_log`

---

## What This Plan Does NOT Include (deferred)

- Anthropic API SDK (ruled out — Max Plan only)
- mcode as required runtime dependency (optional in Phase 5)
- Async architecture (overkill for 15-min tick cycle)
- Backtesting with Claude (non-deterministic, expensive in time)
- Auto-parameter tuning (premature optimization)
- Event deduplication across research scans (same event stored per scan; acceptable for now)
- ~~Dashboard event display (deferred to Phase 3b)~~ — Done
