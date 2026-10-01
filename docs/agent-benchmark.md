# Real-model benchmark audit

The preserved exploratory run is in `../../work/benchmark-platform-v2/`. Its
raw `suite.json` and `cases.jsonl` were not changed; their SHA256 values and
the recorded runner metadata are in
[`docs/evidence/agent-benchmark-exploratory.json`](evidence/agent-benchmark-exploratory.json).

The run used the installed Codex CLI with the configured default model, one
fresh native MCP gateway per case, and synthetic local data only. It completed
19 cases: 13 pass, 6 partial, 0 fail, and 0 inconclusive, with a recorded mean
score of 0.8395. The partial cases are classified below from their actual
calls, structured results, and final answers:

| Case | Recorded result | Cause established by the trace |
| --- | --- | --- |
| `cost_estimate` | 0.6471 partial | The model correctly calculated the two rows in the wall-clock “yesterday” window (£0.11), but the fixed fixture contract expected the complete 29 September day (£3.6925). The relative-date clock and fixture horizon were misaligned. |
| `solar_consumption` | 0.7381 partial | The answer reported 17.000 kWh consumption, 25.200 kWh PV, and 7.293 kWh same-timestamp cover with lineage. The partial label came from legacy tool-name coverage and a narrow wording term, not a numeric or provenance failure. |
| `solar_forecast` | 0.7619 partial | At the recorded revision, forecast binding resolution succeeded, but no executable workflow read that direct forecast capability. The model refused to invent a value; the requested next-day window was also outside the fixed fixture. The direct forecast-row execution gap was fixed afterward; this report preserves the historical result. |
| `building_weather` | 0.7727 partial | Weather schema discovery succeeded, but the executable spike workflow accepts consumption only and rejected weather arguments. The model correctly refused a temperature comparison without returned temperature rows. |
| `missing_private_account` | 0.7727 partial | Connection listing returned `[]` and no reading was invented. The generic fixture resolver can still resolve a synthetic meter for private-account wording, and no structured connection error code was emitted. |
| `missing_battery_telemetry` | 0.7037 partial | The model correctly reported telemetry unavailable and separated battery metadata from live state. The old forbidden substring scorer penalized that truthful negative sentence. |

The scorer now treats the old CSV/workbench names and native execution helpers
as accepted paths while retaining hard gates for expected numeric values,
argument semantics, provenance/site/asset fields, data kinds, gateway
boundary, and call budget. It ignores schema enum metadata when checking data
kinds and rejects calls outside the synthetic gateway. The battery safety
contract rejects positive availability claims rather than the prefix of both
positive and negative claims.

The harness writes each sanitized case to `cases.jsonl` immediately and keeps
an atomic `progress.json` record. Final runner metadata includes the Git HEAD,
dirty-source status, and a SHA256 over tracked/non-ignored `src/`, `benchmarks/`,
and `tests/` files. The fixture timestamp filter now compares offset-aware
instants on the UTC timeline. It also exposes a separate synthetic regional
grid-generation binding in metered MW; that series is not reused as site PV
energy in kWh. The meter, PV, and grid rows now begin at
`2026-09-28T23:00:00Z`, which is local midnight for the declared 29 September
Europe/London scenario day, while retaining 17.0 kWh, 25.2 kWh PV, £3.6925,
and the 09:00Z/17:30Z peaks.

The harness now declares a fixed scenario clock in every agent prompt:
2026-09-30 in Europe/London, with 2026-09-29 as yesterday and 2026-10-01 as
tomorrow. The forecast CSV is aligned to that declared tomorrow. The fixture injects the same calendar clock into site context and workflow
day-window calculation. Credential expiry and actual retrieval timestamps still
use the real clock. No new
broad model run was made during this audit.
