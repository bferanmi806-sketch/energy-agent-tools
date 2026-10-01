"""Workflow guidance is data, independent of connector implementations."""

from .models import Json
from .workflows import RECIPES

SKILLS: list[Json] = [
    {
        "id": "yesterday-consumption",
        "intent": "How much electricity did I use yesterday?",
        "capabilities": ["get_energy_consumption", "analyse_timeseries"],
        "sequence": [
            "Resolve site, timezone and account.",
            "Convert yesterday's local midnight boundaries to explicit UTC offsets.",
            "Fetch interval energy, persist it, sum kWh and check gaps.",
        ],
        "supporting_tools": ["WORKBENCH_SUMMARIZE", "WORKBENCH_RESAMPLE"],
        "pitfalls": [
            "DST days have 23 or 25 hours.",
            "Do not sum cumulative meter counters or power samples as kWh.",
            "Recent smart-meter intervals may arrive late.",
        ],
    },
    {
        "id": "building-spike",
        "intent": "Why did my building's consumption spike?",
        "capabilities": ["get_energy_consumption", "get_weather", "detect_anomaly"],
        "sequence": [
            "Retrieve baseline and suspect consumption periods.",
            "Persist and resample to matching bins.",
            "Screen anomalies, join weather and inspect occupancy/equipment evidence.",
        ],
        "supporting_tools": ["WORKBENCH_ANOMALY", "WORKBENCH_JOIN"],
        "pitfalls": [
            "Correlation is not cause.",
            "Counter resets, missing intervals and timezone errors mimic spikes.",
        ],
    },
    {
        "id": "battery-economics",
        "intent": "When is the cheapest/cleanest time to charge my battery?",
        "capabilities": ["get_tariff", "get_carbon_intensity", "plan_battery_charging"],
        "sequence": [
            "Retrieve forward tariff and carbon intervals.",
            "Align intervals and distinguish forecasts.",
            "Plan against energy requirement, power limit, efficiency and deadline.",
        ],
        "supporting_tools": ["WORKBENCH_JOIN"],
        "pitfalls": [
            "This is an advisory calculation, never a dispatch command.",
            "Cheapest and cleanest are distinct objectives; state which is optimized.",
            "Include efficiency, degradation and standing charges for a complete economic analysis.",
        ],
    },
    {
        "id": "solar-consumption",
        "intent": "Estimate tomorrow's solar generation and compare it with consumption",
        "capabilities": ["get_weather", "estimate_solar_generation", "get_energy_consumption"],
        "sequence": [
            "Fetch irradiance/weather forecast for site.",
            "Apply PV geometry and model assumptions.",
            "Compare forecast generation with historical metered consumption in aligned intervals.",
        ],
        "supporting_tools": ["WORKBENCH_RESAMPLE", "WORKBENCH_JOIN"],
        "pitfalls": [
            "Forecast irradiance is not measured generation.",
            "Shading, clipping and snow affect output.",
        ],
    },
    {
        "id": "grid-conditions",
        "intent": "Analyse grid conditions",
        "capabilities": ["get_generation", "get_carbon_intensity"],
        "sequence": [
            "Fetch grid generation/demand and carbon series.",
            "Match location and time horizon.",
            "Report observed/calculated values separately from forecasts.",
        ],
        "supporting_tools": ["WORKBENCH_JOIN"],
        "pitfalls": [
            "Grid averages do not represent marginal emissions or individual site conditions."
        ],
    },
    {
        "id": "power-flow",
        "intent": "Run a power flow on this network",
        "capabilities": ["run_power_flow"],
        "sequence": [
            "Validate buses, lines, loads, units and slack reference.",
            "Run local AC simulation.",
            "Inspect convergence, voltages, loading and power balance.",
        ],
        "supporting_tools": [],
        "pitfalls": [
            "Balanced steady-state power flow is not protection or transient analysis.",
            "Model results are simulated; a study cannot authorize real switching.",
        ],
    },
]


def search_skills(query: str) -> list[Json]:
    words = set(query.lower().replace("_", " ").split())
    return [
        s
        for s in SKILLS
        if words
        & set(
            (s["intent"] + " " + s["id"] + " " + " ".join(s["capabilities"]))
            .lower()
            .replace("-", " ")
            .replace("_", " ")
            .split()
        )
    ][:3]


for skill_id, recipe in RECIPES.items():
    existing = next((skill for skill in SKILLS if skill["id"] == skill_id), None)
    details = {
        "executable": True,
        "capabilities": recipe["capabilities"],
        "parameters": {
            "arguments": "Map capability IDs to their exact reviewed arguments.",
            "tools": "Map capability IDs to canonical source tool names; do not put tool names in provider arguments.",
            "account_ids": "Map capability IDs to explicit scoped account selections.",
            "start/end": "Explicit offset-aware range; yesterday is resolved from site timezone.",
            "artifacts": "Optional existing capability-to-artifact map in this session.",
        },
        "evidence_required": [
            "Input kinds, units and sources",
            "Coverage and missing intervals",
            "Derived artifact lineage",
        ],
    }
    if existing:
        existing.update(details)
    else:
        SKILLS.append(
            {
                "id": skill_id,
                "intent": skill_id.replace("-", " "),
                "sequence": [
                    "Resolve reviewed sources",
                    "Execute with normal policies",
                    "Process bounded local artifacts",
                ],
                "pitfalls": ["Do not infer causation or physical control authority."],
                **details,
            }
        )
