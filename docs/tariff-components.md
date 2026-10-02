# Tariff components

`energy_agent_tools.billing.calculate_bill` adds caller-defined standing charges and tax to a complete interval-cost result from the time-series `cost` operation. It returns one calculated scalar result with the energy cost, standing charge, chargeable days, taxable subtotal, tax, total, currency and billing window.

The input must be a single `EnergyResult` with `kind="calculated"`, `quantity_shape="interval"`, a GBP, USD or EUR unit, cost-operation provenance, and rows containing a cost, an explicit start timestamp and an explicit interval end. The default columns are `cost`, `timestamp` and `end`; callers can select alternate names with the corresponding parameters. Every timestamp must carry a UTC offset.

The required parameters are `start`, `finish`, `timezone`, `standing_charge`, `tax` and `source`. The `start` and `finish` strings must be aware ISO-8601 values that land on local midnight in the selected IANA timezone. The billing window includes `start` and excludes `finish`. Intervals must cover the entire window exactly, with no gaps, overlaps, duplicate intervals, missing costs or rows outside the window. Whole local calendar days determine the standing charge, so a daylight-saving day counts once even when it has 23 or 25 hours. Partial-day windows are rejected.

`standing_charge` has exactly three fields: `amount_per_day` (a finite non-negative number), `currency` (`GBP`, `USD` or `EUR`) and `taxable` (a boolean). Its currency must match the interval-cost currency. `tax` has exactly two fields: `rate` (a finite number from 0 through 1) and `energy_taxable` (a boolean). `source` is a nonblank label for the tariff schedule supplied by the caller. Include an explicit zero amount or rate when a component does not apply; the operation does not select or infer statutory rates.

Billing uses decimal arithmetic from the supplied numeric values and preserves calculated amounts without currency rounding. The result stores amounts as `Decimal`; Pydantic JSON serialization represents those values as decimal strings so no digits are lost. It allows a negative energy-cost subtotal for a negative tariff. Booleans, strings, non-finite numbers, unknown parameter fields and additional fields inside the two component objects are rejected.

For example, 10 kWh priced at GBP 0.20/kWh produces GBP 2.00 of energy cost. Across two local calendar days with a GBP 0.50 daily standing charge, and a caller-supplied 5% rate applied to both components, the result is GBP 2.00 energy cost + GBP 1.00 standing charge = GBP 3.00 taxable subtotal, GBP 0.15 tax and GBP 3.15 total.

Amounts use Decimal arithmetic over the supplied cost values. JSON represents
them as decimal strings to preserve precision; upstream interval calculations
may already contain floating-point precision limits. No currency rounding is
applied. If a unit rate already includes tax, supply an explicit zero additional
tax rate unless the caller-defined schedule requires another charge.

The gateway exposes this calculation as `WORKBENCH_ENERGY_OPERATION` with
`operation="bill"`. `electricity-cost` also accepts a `billing` object containing
`standing_charge`, `tax` and `source`, alongside a complete local-day `start`/`end`
window and a selected site. The workflow uses the site timezone and preserves
the interval-cost artifact in its evidence. For `tariff-comparison`, supply both
`billing` and `alternative_billing` so each schedule remains explicit. Without
these objects, cost workflows continue to return energy charges only.
