# CSV quantity semantics

`CSV_READ_TIMESERIES` accepts optional `quantity_shape` and `resolution`
arguments. Supply them when the CSV source's measurement semantics are known:

```json
{
  "file": "meter.csv",
  "kind": "metered",
  "unit": "kWh",
  "timezone": "Europe/London",
  "quantity_shape": "interval",
  "resolution": "30min"
}
```

`quantity_shape` must be `interval`, `instantaneous` or `counter`. An interval
value is energy recorded for a period and can be summed. An instantaneous value
is a point observation, such as power in kW; integrate it over explicit
durations before calculating energy. A counter is a cumulative reading, such as
a meter's lifetime kWh total; calculate differences with the explicit counter
operation before summing energy.

`resolution` is the source's declared sampling or interval spacing, such as
`30min` or `PT30M`. It must contain a non-whitespace character and is retained
as supplied. The import records these declarations in the result and its source
provenance. It does not infer shape or resolution from column names, values,
units or timestamps, and it does not verify declarations against the hardware.

Both arguments are optional for compatibility with existing imports. When
omitted, the result retains an unknown quantity shape or resolution. Workbench
summary and resampling operations allow direct summation only for known interval
quantities. They suppress summary totals and reject resampling sums for declared
counters and instantaneous observations. Power units are also prevented from
being summed as energy.
