from __future__ import annotations

import math
import re

from jsonschema import Draft202012Validator

from .models import EnergyError, Handler, Tool, Toolkit


class Registry:
    def __init__(self) -> None:
        self.toolkits: dict[str, Toolkit] = {}
        self.tools: dict[str, Tool] = {}
        self.handlers: dict[str, Handler] = {}

    def add_toolkit(self, toolkit: Toolkit) -> None:
        if toolkit.id in self.toolkits:
            raise ValueError(f"Duplicate toolkit: {toolkit.id}")
        self.toolkits[toolkit.id] = toolkit

    def add(self, tool: Tool, handler: Handler) -> None:
        if tool.toolkit not in self.toolkits or tool.name in self.tools:
            raise ValueError(f"Unknown toolkit or duplicate tool: {tool.name}")
        Draft202012Validator.check_schema(tool.input_schema)
        self.tools[tool.name] = tool
        self.handlers[tool.name] = handler

    def get(self, name: str) -> Tool:
        if name not in self.tools:
            raise EnergyError("tool_not_found", "Tool does not exist; search the registry first.")
        return self.tools[name]

    def search(self, query: str, allowed: set[str] | None = None, limit: int = 5) -> list[Tool]:
        words = set(re.findall(r"[a-z0-9]+", query.lower()))
        synonyms = {
            "electricity": "energy",
            "use": "consumption",
            "used": "consumption",
            "yesterday": "consumption",
            "spike": "anomaly",
            "cleanest": "carbon",
            "cheapest": "tariff",
            "charging": "battery",
            "solar": "pv",
        }
        words |= {synonyms[w] for w in list(words) if w in synonyms}
        scored = []
        token_sets = {
            t.name: set(
                re.findall(r"[a-z0-9]+", " ".join([t.name, t.description, *t.capabilities]).lower())
            )
            for t in self.tools.values()
        }
        frequencies = {w: sum(w in tokens for tokens in token_sets.values()) for w in words}
        for tool in self.tools.values():
            if allowed is not None and tool.toolkit not in allowed:
                continue
            text = " ".join([tool.name, tool.description, *tool.capabilities]).lower()
            tokens = set(re.findall(r"[a-z0-9]+", text))
            score = sum(
                1 + math.log((len(token_sets) + 1) / (frequencies[w] + 1)) for w in words & tokens
            )
            name_tokens = set(re.findall(r"[a-z0-9]+", tool.name.lower()))
            score += 2 * len(words & name_tokens)
            score += sum(3 for c in tool.capabilities if c in query.lower())
            if score:
                scored.append((score, tool.name, tool))
        return [t for _, _, t in sorted(scored, key=lambda s: (-s[0], s[1]))[:limit]]
