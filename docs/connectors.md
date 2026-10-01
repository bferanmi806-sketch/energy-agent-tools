# Connector catalogue

`stable` describes a tested bounded adapter contract. It does not establish
private-provider access. Numerical models remain experimental despite passing
real solver tests. Missing optional dependencies produce explicit unavailability.

| Toolkit | Working actions | Qualification |
|---|---|---|
| Carbon Intensity GB | National/regional actual and forecast intensity | Contract tests and live public queries |
| Open-Meteo | Weather, radiation and wind forecasts | Contract tests and live public query |
| Octopus public tariffs | Published unit rates | Pagination fixtures and live Agile query |
| Octopus account | Meter consumption | Auth/pagination/semantics fixtures; no live private meter |
| Home Assistant | States and bounded history | Bearer, unit and state-class fixtures; no live installation |
| OpenEnergyMonitor / Emoncms | Current/history feed | Read-key, unit, null and error fixtures; no live installation |
| Elexon Insights | Generation, demand and forecast datasets | Contract tests and live FUELHH query |
| NESO data portal | CKAN search, dataset metadata and bounded datastore rows | Rate-limit and response fixtures; live catalogue/metadata probe |
| Electricity Maps v4 | Carbon, mix, load and forecasts by entitled mode | Auth/flow-traced/estimated/forecast fixtures; no paid-service access |
| ENTSO-E | Bounded load, generation and wind/solar series | Token/XML/document/position/gap fixtures; no live token |
| pvlib | Fixed-tilt PVWatts estimate | Real library and weather workflow tests |
| windpowerlib | Offline power-curve ModelChain | Real library tests; explicit inputs |
| pandapower | Bounded balanced AC flow | Real solver, voltage response and conservation |
| PyPSA | Bounded AC power flow | Real solver and matching pandapower cross-validation |
| pandapipes | Bounded water/gas hydraulics | Real solver and mass-balance tests |
| SciPy battery optimizer | Cost/carbon charging with SOC/efficiency limits | Real MILP feasibility tests |
| Thermal calculation | Envelope/ventilation/thermal-bridge heat loss | Numerical assertions |
| Local CSV | Scoped imports | Path, bounds, time-filter and real MCP tests |
| Read-only SQLite | Generated scoped SELECT | Identifier/path/authorizer/time-range fixtures |
| Workbench | Energy operations and bounded summaries | DST, missing/counter/power/unit/lineage/ownership tests |
| Reviewed MCP | Local/remote import, schemas and calls | Real stdio/HTTP, auth, review and drift tests |
| Fixed executable | Operator-owned JSON command | Real subprocess timeout/output/environment tests |
| OpenDSS / DSS-Extensions | Bounded balanced and unbalanced snapshot power flow | Real balanced/unbalanced references and power balance |
| Optional EnergyPlus | Trusted IDF/EPW execution | Official 26.2.0 binary and example/weather run; bounded adapter |

The numerical adapters require optional extras. EnergyPlus is registered through
`register_energyplus(registry, executable, model_root)` by a trusted operator.
No arbitrary solver file, Python or SQL is accepted through agent tools.
See [upstream research](upstream-research.md), [network models](network-solvers.md)
and [verification](verification.md) for contracts and evidence.

## Capability contracts

Reviewed builtins cover consumption, weather, tariffs, carbon, solar estimation,
wind estimation, power flow, pipe flow, heat loss and battery planning. A builtin
preserves the source's exact schema. Generic telemetry and local data need
operator bindings that identify the account, asset, kind, unit and argument map.
A capability label alone is a discovery hint. Unreviewed bindings cannot execute.

Site-generation workflows require site-generation data. Public grid series in MW
cannot substitute for a site's PV energy in kWh. Forecasts and calculated carbon
retain their own kinds. Counter data needs explicit differencing; power needs
explicit integration before energy analysis. See [workflows](workflows.md).

## Deferred engines

OpenDSS now has a bounded snapshot contract and two real reference checks.
Controls, faults, protection and dynamic simulation remain outside its contract.
OpenStudio needs installed software and trusted building models. Vendor PowerMCP
servers need individual prerequisite, schema, permission and model qualification.
Generic MCP ingestion does not establish any of these integrations as validated.

No action claims real private access when only fixture evidence exists. Provider
entitlements, attribution and limits remain operator responsibilities.

New engine evidence is recorded in [engine qualification](engine-qualification.md).
