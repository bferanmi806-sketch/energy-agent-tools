# Offline workflow reference

After installing the project's base dependencies, run this from the repository root:

```sh
PYTHONPATH=src:. uv run --no-sync python examples/reference_projects/workflows.py
```

The example creates its CSVs, state database, and artifacts in a temporary
directory, then uses `EnergyAgentTools` and `session.skill` to run six production
recipes through the reviewed capability resolver and workbench. It prints one JSON
report and makes no network or model calls. The test runs the same script as a
subprocess and checks its numbers, source lineage, and failure cases.
The project runtime dependencies are sufficient; no engineering solver extra is
needed for these six time-series recipes.

The scenario clock is fixed at `2026-09-30T12:00:00Z`, the site timezone is UTC,
and the local day under analysis is September 29, 2026. All interval timestamps
include UTC offsets. The meter fixture has 24 hourly interval-energy rows for
September 28 at 0.5 kWh each and 24 rows for September 29 at 1 kWh, except for a
5 kWh value at 09:00. The tariff fixture charges GBP 0.10/kWh for the first twelve
hours and GBP 0.30/kWh for the next twelve. The alternative fixture charges GBP
0.20/kWh throughout the day.

Every synthetic CSV row carries `physical_meter=false`. The energy artifact is
caller-declared `metered` interval energy because that is the input contract for
these consumption recipes; it is synthetic data and makes no claim about a real
meter. The CSV connector retains the declared kind, unit, quantity shape,
resolution, source file, site, and owner asset in the evidence.
The fixture's explicit CSV bindings exercise the local provider path; they do not
qualify live provider substitution, accounts, or authentication gates.

| Recipe | Inputs and contract | Independent check |
| --- | --- | --- |
| `yesterday-consumption` | 24 hourly rows of synthetic metered interval energy in kWh | The sum is 28 kWh. |
| `building-spike` | The same day of interval energy | One anomaly is reported; the flagged value is 5 kWh. This is statistical screening, not a cause diagnosis. |
| `electricity-cost` | Metered interval kWh, a forecast tariff in GBP/kWh, a full UTC day, and an explicit billing schedule | Energy cost is GBP 5.20; GBP 0.30 standing charge and 5% energy tax give GBP 5.76 total. |
| `energy-baseline` | Metered interval kWh and a four-observation rolling window | The baseline at hour 4 is 1 kWh; the spike residual is 4 kWh. |
| `building-comparison` | Two full days of metered interval kWh, grouped by UTC calendar day | September 28 totals 12 kWh; September 29 totals 28 kWh, a 16 kWh increase. |
| `tariff-comparison` | The same consumption, primary and alternative forecast tariffs, a full UTC day, and two explicit billing schedules | The primary bill is GBP 5.76. The alternative energy cost is GBP 5.60; its separate GBP 0.45 standing charge and 10% energy tax produce GBP 6.61 total. |

Each tariff schedule supplies its source label, standing charge amount and
taxability, and tax rate and energy taxability. The example does not infer
statutory charges. Tariff comparison is arithmetic over the supplied schedules;
it does not determine which tariff a customer is eligible to use.

The reference runs a meaningful failure alongside each success: a site-less
yesterday request, instantaneous power passed as consumption, a bill without a
complete day window, a zero-length baseline, a forecast artifact supplied where
metered comparison data is required, and tariff comparison without its alternative
tariff. The report includes each returned gateway error code.

The baseline uses the previous observations and does not adjust for occupancy or
seasonality. Building comparison groups the selected observations into calendar
days and does not fill missing data. The statistics and bills describe only these
small synthetic fixtures; they do not qualify a provider, physical meter, or live
tariff.
