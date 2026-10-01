# Extended connector research

This document records the upstream contracts used by `connectors/extended.py`.
Provider behavior and availability can change, so the links below are the source
of truth for credentials, limits, field meanings, and current response details.

## NESO CKAN

Sources:

- [NESO Data Portal API guidance](https://www.neso.energy/data-portal/api-guidance)
- [NESO Data Portal](https://www.neso.energy/data-portal)

The public API is CKAN's `/api/3/action` surface. The adapter uses only the
documented `package_search`, `package_show`, and `datastore_search` actions. It
does not expose `datastore_search_sql` or accept a caller-supplied SQL string.
`package_show` selects an active datastore resource; `datastore_search` receives
an explicit resource ID, row limit, offset, and at most eight scalar filters.
Optional time filtering is applied after the bounded response is parsed.

NESO's guidance recommends no more than one CKAN request per second and no more
than two datastore requests per minute. The connector serializes requests made
through one `httpx.AsyncClient` with a monotonic one-second minimum interval for
catalogue actions and a 30-second interval after a datastore request. It does
not automatically bulk-page a dataset. Results are capped at 10,000 rows
and the response body is capped at 2 MiB. Dataset semantics remain
provider-defined; names containing `forecast` are reported as `forecast`, and
other rows are reported as `calculated` with a warning rather than being called
metered.

The fixture tests cover package/resource discovery, scalar filters, no SQL
parameter, forecast semantics, and bounded registration. A live qualification
probe used the public `package_search` and `package_show` actions and observed
the current `embedded-wind-and-solar-forecasts` package and its active
datastore resource. Live access is intentionally not part of the test suite.

## Electricity Maps v4

Sources:

- [Electricity Maps API documentation](https://app.electricitymaps.com/docs)
- [Authorization](https://app.electricitymaps.com/docs/quickstart/authorization)
- [Carbon intensity](https://app.electricitymaps.com/docs/reference/carbon-intensity)
- [Electricity mix](https://app.electricitymaps.com/docs/reference/electricity-mix)
- [Total load](https://app.electricitymaps.com/docs/reference/total-load)
- [Total reported load](https://app.electricitymaps.com/docs/reference/total-reported-load)
- [Net load](https://app.electricitymaps.com/docs/reference/net-load)

The adapter uses the fixed HTTPS origin `https://api.electricitymaps.com/v4`,
the documented `auth-token` header, and the documented signal/mode paths. The
credential is resolved from the execution context and never appears in tool
schemas, errors, or provenance. Carbon values are normalized to
`gCO2eq/kWh`; mix, total load, reported load, and net load preserve the
provider's MW unit. Mix storage objects are flattened into variables such as
`battery storage.charge`, while `isEstimated` and `flowTraced` remain explicit
in each row. Past-range requests use the documented `start` and `end` query
parameters; the optional `flow_traced` input maps to the documented
`flowTraced` parameter.

The connector enforces conservative range limits (three days at five-minute
granularity, four days at fifteen-minute, ten days hourly, and 365 days daily)
before making a request. It never follows redirects or accepts an account
configured origin. The tests cover the v4 path, auth header, nested mix
flattening, and secret-free 401 handling. A live probe confirmed that the public
`/v4/zones` endpoint is reachable and that a carbon request without an
`auth-token` receives the documented 401 response; no credential was stored.

## ENTSO-E Transparency Platform

Sources:

- [Security token registration](https://transparencyplatform.zendesk.com/hc/en-us/articles/12845911031188-How-to-get-security-token)
- [Request endpoint](https://transparencyplatform.zendesk.com/hc/en-us/articles/15696677194644-Request-Endpoint)
- [Request parameters](https://transparencyplatform.zendesk.com/hc/en-us/articles/15696716612372-Request-Parameters)
- [Response time zone](https://transparencyplatform.zendesk.com/hc/en-us/articles/12786431986964-Response-Time-Zone)
- [Document types](https://transparencyplatform.zendesk.com/hc/en-us/articles/15857043092756-DocumentType)
- [Process types](https://transparencyplatform.zendesk.com/hc/en-us/articles/15857039772052-ProcessType)
- [Query response](https://transparencyplatform.zendesk.com/hc/en-us/articles/15727773247124-Query-Response)
- [Query size limits](https://transparencyplatform.zendesk.com/hc/en-us/articles/15854536354964-API-Query-Size-Limit)
- [API rate limits](https://transparencyplatform.zendesk.com/hc/en-us/articles/12783148966036-API-Rate-Limit-Part-1)

The adapter targets only the production HTTPS endpoint
`https://web-api.tp.entsoe.eu/api`. It accepts a bounded allow-list of document
types (`A65`, `A69`, `A73`, `A75`), process types, area IDs, and domain
parameters. It formats the mandatory UTC interval as `periodStart` and
`periodEnd`; the security token is a context credential and is never copied to
the result.

XML is rejected before parsing if it contains a DTD, entity declaration, or
CDATA declaration. Responses are capped at 4 MiB and parsed with the standard
library tree parser. Period resolutions are allow-listed, positions are
validated against the period interval, duplicates fail, and missing positions
are emitted as rows with `value: null` at the correct timestamp. A 200 response
with reason code 999 is represented as an empty no-data result, matching the
provider's documented response behavior. A69 is reported as `forecast`; the
actual load and generation document types are reported as `metered`.

The tests cover UTC request parameters, accurate position expansion with a
missing position, DTD/entity rejection, and secret-free structured errors.
Credentialed live qualification requires an ENTSO-E account and is deliberately
not run in CI.

## windpowerlib

Sources:

- [windpowerlib documentation](https://windpowerlib.readthedocs.io/en/stable/)
- [Model description](https://windpowerlib.readthedocs.io/en/stable/model_description.html)
- [windpowerlib on PyPI](https://pypi.org/project/windpowerlib/)

The supported release line is `0.2.2` (`windpowerlib>=0.2.2,<0.3`). The adapter
builds the documented pandas MultiIndex weather frame and calls
`WindTurbine` plus `ModelChain.run_model`. It accepts either a named built-in
turbine loaded from windpowerlib's packaged `oedb` data or an explicit power
curve. It does not accept a path or fetch turbine/weather data. Weather
timestamps, measurement height, roughness, interval ordering, curve monotonicity,
and row count are validated at the boundary. Output contains modelled power and
interval energy in kW/kWh and is labelled `estimated`.

The test suite runs a real ModelChain smoke test when the optional dependency is
installed; environments without the extra report a skip. The checked
qualification command is:

```text
uv run --with 'windpowerlib>=0.2.2,<0.3' pytest -q tests/test_extended.py -k windpowerlib
```

## Read-only SQLite

Source: [Python `sqlite3` documentation](https://docs.python.org/3/library/sqlite3.html).

SQLite access is enabled only when the operator passes `data_root` to
`extended.register`. A requested database path is resolved below that root and
must have a SQLite extension. The tool accepts table and column identifiers only
after a strict identifier check; it generates one parameterized `SELECT` with an
explicit `LIMIT`, uses URI `mode=ro`, enables `PRAGMA query_only`, and installs
an authorizer that denies writes, schema changes, transactions, attachment,
extension loading, and arbitrary pragma/function operations. No raw SQL is in
the public input schema. Timestamp values must be offset-aware ISO timestamps;
kind and unit are explicit operator inputs and are preserved in the result.

Tests cover a real temporary database, path escape rejection, bounded row
reading, exact UTC filtering across `Z` and non-UTC offsets, and the read-only
connection path. The SQLite tool intentionally leaves `result_kind` and
`result_unit` unset in registry metadata because both are caller-declared.

## Capability labels and dynamic semantics

The registry uses canonical labels only where the default operation supports the
claim: `get_carbon_intensity` for the Electricity Maps default signal,
`get_grid_load` for the ENTSO-E default A65 load document, and
`estimate_wind_generation` for the windpowerlib model. The generic NESO dataset,
Electricity Maps signal, ENTSO-E document, and SQLite tools leave static result
kind metadata unset when a caller can select a different semantic. A capability
binding that selects one of those dynamic modes must either keep `kind` and
`unit` unset or constrain the input defaults and review the resulting contract.

## EnergyPlus

Sources:

- [EnergyPlus quick start](https://energyplus.readthedocs.io/en/stable/quick_start/quick_start.html)
- [EnergyPlus API and command-line options](https://energyplus.readthedocs.io/en/latest/api.html)

EnergyPlus is optional because it is a locally installed, long-running simulator.
`register_energyplus` requires an existing executable and operator-owned model
root. The tool accepts only relative IDF/epJSON and EPW paths under that root,
constructs a fixed argument vector, invokes without a shell, writes to a
temporary output directory, discards unbounded process streams, and enforces a
120-second timeout. Output is capped at 4 MiB and 10,000 CSV rows. No arbitrary
command, working directory, output path, or environment is accepted from a
tool call. Install and model qualification remain an operator responsibility;
the base registry does not claim EnergyPlus availability.
