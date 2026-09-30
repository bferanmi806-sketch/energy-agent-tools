# Connector catalogue

Statuses describe the initial implementation, not a promise of an external
service's availability. `stable` means the bounded adapter contract is tested;
private credentials and site data are separately qualified below. Engineering
models remain experimental even when their numerical tests pass.

| Connector/toolkit | Runtime | Status | Working actions | Evidence |
|---|---|---|---|---|
| Carbon Intensity GB / NESO | HTTP | stable | National and regional actual/forecast intensity | Contract tests and live national/regional queries |
| Open-Meteo | HTTP | stable | Hourly weather, solar radiation and wind resource forecasts | Contract tests and live hourly forecast |
| Octopus public tariffs | HTTP | stable | Published electricity unit rates | Pagination/error tests and live Agile rates |
| Octopus account consumption | HTTP | requires credentials | Meter interval consumption | Basic-auth, pagination and semantics fixtures; no live private account |
| Home Assistant | HTTP | requires credentials | Entity states and bounded history | Bearer-auth/unit/state-class/history fixtures; no live installation |
| OpenEnergyMonitor / Emoncms | HTTP | requires credentials | Current feed and bounded feed history | Read-key/unit/null/error fixtures; no live installation |
| Elexon Insights | HTTP | stable | Reviewed generation, demand and forecast datasets | Contract tests and live FUELHH generation |
| pvlib | Python | experimental | Fixed-tilt PVWatts AC generation estimate | Real library, daylight/night tests and workflow integration |
| pandapower | Python | experimental | Caller-supplied balanced AC power flow | Real solver, voltage response and power conservation |
| Battery optimizer | Python / SciPy | experimental | Cost/carbon charging schedule with efficiency/SOC/power constraints | Real MILP, feasibility, no simultaneous charge/discharge |
| Thermal calculation | Native Python | experimental | Envelope/ventilation/thermal-bridge heat loss | Numerical engineering assertions |
| Local energy CSV | Native Python | stable | Scoped time-series imports | Path-boundary tests and real stdio MCP workflow |
| Workbench | Native Python / pandas | stable | Summaries, resample, joins, pivots and anomaly screening | DST, gaps, ownership, units and provenance tests |
| Local MCP import | MCP stdio | experimental | Reviewed upstream tools | Real initialization/discovery/call/error/auth fixtures |
| Remote MCP import | MCP streamable HTTP | experimental | Reviewed upstream tools | Live local HTTP test server through the gateway |
| Fixed executable adapter | Executable | experimental | Operator-owned JSON stdin/stdout command | Real subprocess protocol, timeout, output bounds and environment isolation |

The engineering actions share one toolkit. Octopus account actions use
`octopus-energy-account`, while public rates use `octopus-energy`. Configure
private account references with the matching toolkit ID. See generated
[manifests](../manifests/) for exact schemas and action policies.

## Universal capabilities

Implemented capability IDs include `get_energy_consumption`, `get_current_power`,
`get_generation`, `get_storage_state`, `get_tariff`, `get_carbon_intensity`,
`get_weather`, `estimate_solar_generation`, `run_power_flow`, `run_simulation`,
`perform_engineering_calculation`, `plan_battery_charging`, `analyse_timeseries`,
`detect_anomaly`, `compare_energy_data` and `import_timeseries`.

Generic telemetry capabilities depend on the configured entity/feed. The runtime
preserves its physical meaning and units. There is no universal automatic routing
or automatic conversion of cumulative counters into interval energy.

## Independently investigated candidates

| Candidate | Status in this project | Reason and next requirement |
|---|---|---|
| [NESO general data portal](https://www.neso.energy/data-portal/api-guidance) | unavailable as a general connector | Carbon intensity is supported. General CKAN resources need curated resource IDs, field units and publication/revision semantics. |
| [ENTSO-E](https://transparencyplatform.zendesk.com/hc/en-us/articles/12845911031188-How-to-get-security-token) | unavailable | Requires approved token and reviewed XML/time-zone/document-type normalization. No token was available. |
| [Electricity Maps](https://app.electricitymaps.com/developer-hub/api/reference) | unavailable | Current API/service entitlement and attribution vary by plan; no credentials were available. Avoid a stale v3 wrapper. |
| [PyPSA](https://docs.pypsa.org/latest/) | unavailable | Network optimization is a different model contract from balanced power flow; needs solver and reference-model tests. |
| [EnergyPlus](https://energyplus.readthedocs.io/en/stable/quick_start/quick_start.html) / OpenStudio | unavailable | Requires validated IDF/EPW models, installed engine and bounded artifact execution. Fixed-executable runtime is available; an EnergyPlus integration is not claimed. |
| [OpenDSS / DSS-Extensions](https://dss-extensions.org/OpenDSSDirect.py/notebooks/GettingStarted.html) | unavailable | Requires separate unbalanced model semantics and numerical reference tests. |
| [pandapipes](https://pandapipes.readthedocs.io/en/0.13.0/pipeflow/pipeflow_procedure.html) | unavailable | Gas/thermal hydraulic models need reviewed fluid assumptions and reference networks. |
| [PowerMCP](https://github.com/Power-Agent/PowerMCP) | unavailable as a validated integration | Generic MCP ingestion is implemented, but vendor software prerequisites and action metadata need individual validation. |

No placeholder actions for these candidates are in the executable registry.
