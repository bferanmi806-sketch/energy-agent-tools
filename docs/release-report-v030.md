# Energy Agent Tools v0.3.0 core report

Release status: [published v0.3.0](https://github.com/bferanmi806-sketch/energy-agent-tools/releases/tag/v0.3.0). This release extends
the open-source, self-hostable Python gateway for energy data and engineering
tools. It adds a composed consumption forecast and bill workflow, automatic
weather context, observed high-resolution meter aggregation, and persistent
large-dataset tools. The project remains on its path toward a Composio-style
gateway for energy; v0.3.0 does not complete the developer product or hosted
control plane.

Exact source, build hashes and verification are recorded in the
[v0.3.0 release evidence](evidence/release-v030.json). See the
[v0.2.0 report](release-report-v020.md) for the previous milestone.

## Forecast consumption and estimate a bill

The `consumption-forecast` and `forecast-bill` workflows use the scoped gateway
to resolve meter history and tariff data. Defaults use three calendar months
of complete metered interval energy and forecast eight days. Historical meter
data stays `metered`, predicted energy is `forecast`, and forecast cost is
`calculated` with `forecast_consumption` as its calculation basis.

The default history cutoff is midnight at the start of today in the site's
timezone. The forecast starts at tomorrow's local midnight, so the current day
does not enter model fitting. The workflow retrieves history in bounded calls
and aggregates complete observed intervals to 15, 30 or 60-minute model input.
It refuses missing history, gaps, overlaps, and intervals that cross an
aggregation boundary. It does not fill or prorate meter readings.

Automatic temperature context is optional. When enabled, the workflow retrieves
historical weather in bounded chunks and future weather for the forecast
window. It aligns both sources with a bounded hold and carries the assumptions
into the forecast result. Open-Meteo historical temperatures are hourly
`estimated` gridded analysis or reanalysis, while future weather remains
`forecast`. If automatic context is unavailable, the default mode records the
reason and uses the weekly calendar model. `required` mode returns an error
without complete context. The model compares the context-conditioned candidate
with the calendar model using chronological holdout weeks; residual bands are
diagnostics, not guaranteed future coverage.

Tariffs must cover every predicted interval. The workflow reports an energy
cost estimate when standing charges or tax treatment are missing. It refuses
unknown future tariff periods and reports assumptions when rates change during
an interval.

Synthetic acceptance covers two forecast paths. Three months of interval data
produce an eight-day forecast of 192 kWh and an estimated bill of GBP 42.72. A
one-minute, 132,480-row fixture is aggregated to 30-minute intervals and
produces 576 kWh and GBP 123.36. The high-resolution fixture uses SDK and MCP
paths with both preloaded datasets and a resolved dataset provider. These
values verify workflow behavior with generated data; they do not show accuracy
for a physical site. See the [forecast evidence](evidence/forecast-weather-oct02.json)
the [chunked weather evidence](evidence/chunked-weather-oct02.json),
and [high-resolution evidence](evidence/high-resolution-forecast-oct02.json).

## Import and analyze large meter datasets

Operators can stream approved CSV data into private, partitioned SQLite
datasets. Imports require declared data kind, unit, timezone and quantity shape.
The gateway provides bounded paging, streamed summaries and time-window reads.
Pages stay within row and response-byte limits, and summaries only sum data
declared as interval energy. User and session scope apply to dataset reads.
Dataset chunks share artifact quotas, retention and the SQLite backup and
restore boundary.

`WORKBENCH_AGGREGATE_ENERGY` streams complete observed interval energy into
15, 30 or 60-minute bins. It preserves metered lineage and refuses gaps,
overlaps, and intervals that need splitting at a bin boundary. The output is
an aggregation of observed values, not gap filling or an upgrade to meter
quality.

The million-row qualification uses a synthetic one-minute fixture. It checks an
independent total, a one-day window, bounded inline paging, rejection of
foreign-scope access and backup/restore. Compatible day windows decode two
chunks containing 2,000 candidate rows; exact selection returns 1,440 rows.
Old or uncertain chunk bounds retain a scan fallback. The indexed run peaks at
2,340,701 traced Python bytes. Its allocation measurement covers
traced Python allocations, not process RSS or native SQLite memory. The
qualification does not establish physical meter access, gateway load capacity,
or sustained operation. See the [indexed time-series evidence](evidence/indexed-timeseries-oct02.json)
and [large time-series guide](large-timeseries.md).

## Scope that remains open

The TypeScript SDK, Connect Apps web application, and full persistent connection
control plane remain unfinished. The control plane still needs workspaces,
user and key management, shared-connection ACLs, persistent tenant state,
dynamic MCP sessions, and managed OAuth configuration.

Private-provider fixtures and a public weather probe do not qualify a physical
site. The live 24-hour Berlin weather response is gridded reference data, not a
physical thermometer or meter, and it does not measure forecast accuracy.
Forecast accuracy for a real installation remains open.

The broader completion gates also remain open: 100 or more actual agent tasks
with held-out evaluation across two model families, independent review of
discovery results, an outside-authored connector, and a 30-day deployment soak.
The [product execution audit](product-execution.md) tracks these requirements
and the remaining qualification work.
