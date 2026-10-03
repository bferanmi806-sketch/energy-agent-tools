# Real-model benchmark

This benchmark invokes the installed Codex CLI against a native Energy Agent
MCP server. It is the project’s real-model evaluation path; the unit tests only
exercise parsing, scoring, and the deterministic fixture and do not claim agent
quality.

Run one case while iterating:

```sh
uv run python -m benchmarks.harness \
  --repo "$PWD" \
  --output work/benchmark-one \
  --case consumption_total
```

Run the full natural-language suite:

```sh
uv run python -m benchmarks.harness \
  --repo "$PWD" \
  --output work/benchmark-$(date +%Y%m%d-%H%M%S)
```

The runner uses the existing Codex login from `CODEX_HOME`, leaves the model
unset so the configured default is used, passes `--ignore-user-config`, and
starts one fresh local MCP process per case. The child environment is reduced
to runtime basics and the Codex login location; API keys, tokens, passwords,
and other secret-looking variables are not copied into the model process.

The fixture server uses the project’s `EnergyAgent` and `create_server`, with
reviewed local bindings for synthetic meter, PV, tariff, carbon, weather, and
forecast rows. It registers no HTTP connectors and therefore cannot reach a
public provider or a private account. Every result is labelled synthetic and
retains site, asset, unit, data-kind, and file provenance. The fixture contains
17.0 kWh of meter energy, a 7.0 kW peak at `2026-09-29T09:00:00Z`, and a
`£3.6925` interval-price total; these values are used by the numeric scorer.
Its separate `grid.csv` binding reports synthetic regional grid generation in
metered MW, and is never used as site PV energy in kWh.

The default runner uses `--ask-for-approval never`, `-s read-only`, and
`features.shell_tool=false`. It also scopes the ephemeral MCP approval setting
to the local `energy` server with
`mcp_servers.energy.default_tools_approval_mode="approve"`; it does not enable
the global dangerous-bypass flag. The gateway itself exposes only read and
bounded calculation/simulation actions, and the prompt forbids shell commands.
Use `--no-approval-bypass` when checking the generated command without
executing model tool calls.

Results are written as `suite.json`, one sanitized `cases.jsonl` record per
case, and an atomic `progress.json` updated after every case. Records include
the actual JSONL MCP calls and arguments, final answer, usage, completion
status, timeout/exit status, per-dimension evidence, and explicit
partial/fail/inconclusive labels. Runner metadata records the source commit
and a SHA256 over the dirty benchmarked source paths. A passing score requires
numeric truth where a case specifies expected values, structured
provenance/context, correct units and data kinds, bounded calls, and safe
handling of missing or ambiguous connections. Keyword matches alone do not
pass.

The default suite contains the original 19 cases. Additional qualified
scenario environments are selected explicitly, for example:

```sh
uv run python -m benchmarks.harness --repo "$PWD" --output work/telemetry-engineering \
  --case dev_consumption_home_assistant \
  --case dev_units_kw_kwh \
  --case dev_heat_loss \
  --case dev_power_flow_two_bus
```

The shared environment dispatcher currently exposes 16 independently checked
development scenarios. Telemetry scenarios run the shipped HTTP connectors
against an isolated `httpx.MockTransport` with provider-shaped responses and
encrypted fixture credentials. Engineering scenarios execute the installed
production calculators and optional native engines with visible model inputs.
These are synthetic development environments. Their acceptance tests do not
count as actual model successes, physical-site qualification, or held-out
results. See `docs/telemetry-evaluation-environments.md` and
`docs/engineering-evaluation-environments.md` for cases and explicit exclusions.

A run records source identity again at completion so changes during execution
are visible in `source_unchanged`. Keep the benchmark checkout frozen for the
whole run. If the model CLI reports that its usage limit has been reached, the
runner preserves the inconclusive case, stops, and records
`interrupted_reason: model_usage_limit` and `unattempted_case_ids`. It does not
purchase credits, reset usage, or switch models.

The runner accepts explicit `--model` and `--reasoning-effort` options and
records both overrides. The 2 October `gpt-6-luna`/`max` CLI probe was
rejected for this account. Capacity returned on 3 October: a frozen run at
source `8917e79` attempted all 19 original cases and recorded six passes,
three partial results and ten timeout-inconclusive results. The mean heuristic
score was 0.8517. The requested alias and effort were recorded; the resolved
backend model family was not independently verified. These are known synthetic
cases, with no held-out results. The earlier rejection remains historical evidence.
Older runs with null model metadata retain that limitation. Model settings do
not change fixture truth, scoring or the release thresholds.

The default subprocess runner now records `codex-streaming-events-v1` timing
metadata. It records monotonic process and pipe-event receipt times, MCP starts
and completions when emitted, terminal events, timeout boundaries and partial
usage when emitted. Times measure when the local runner reads output; they do
not measure backend emission or provider execution latency. A duration requires
both MCP events. Missing usage or model identity is not inferred. Capture and
parse diagnostics are bounded, and POSIX timeouts terminate the child process
group. Existing benchmark results are not retroactively instrumented or rescored.
