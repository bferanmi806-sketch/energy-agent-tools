from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping

from jsonschema import Draft202012Validator

from .models import EnergyError, Handler, Tool, Toolkit


class Registry:
    def __init__(self) -> None:
        self.toolkits: dict[str, Toolkit] = {}
        self.tools: dict[str, Tool] = {}
        self.handlers: dict[str, Handler] = {}
        self._indexed_count = -1
        self._tokens: dict[str, set[str]] = {}
        self._postings: dict[str, set[str]] = {}

    def add_toolkit(self, toolkit: Toolkit) -> None:
        if toolkit.id in self.toolkits:
            raise ValueError(f"Duplicate toolkit: {toolkit.id}")
        self.toolkits[toolkit.id] = toolkit

    def add(self, tool: Tool, handler: Handler) -> None:
        if tool.toolkit not in self.toolkits or tool.name in self.tools:
            raise ValueError(f"Unknown toolkit or duplicate tool: {tool.name}")
        encoded = json.dumps(tool.input_schema, allow_nan=False).encode()
        if len(encoded) > 500000:
            raise ValueError("Tool schema exceeds the registration size limit")
        nodes = [(tool.input_schema, 0)]
        count = 0
        while nodes:
            value, depth = nodes.pop()
            count += 1
            if depth > 50 or count > 10000:
                raise ValueError("Tool schema exceeds structural limits")
            if isinstance(value, dict):
                for key in ("$ref", "$dynamicRef", "$recursiveRef"):
                    reference = value.get(key)
                    if reference is not None and (
                        not isinstance(reference, str) or not reference.startswith("#")
                    ):
                        raise ValueError("External schema references are not permitted")
                nodes.extend((nested, depth + 1) for nested in value.values())
            elif isinstance(value, list):
                nodes.extend((nested, depth + 1) for nested in value)
        Draft202012Validator.check_schema(tool.input_schema)
        self.tools[tool.name] = tool
        self.handlers[tool.name] = handler

    def get(self, name: str) -> Tool:
        if name not in self.tools:
            raise EnergyError("tool_not_found", "Tool does not exist; search the registry first.")
        return self.tools[name]

    def search(
        self,
        query: str,
        allowed: set[str] | None = None,
        limit: int = 5,
        *,
        scoped_capabilities: Mapping[str, list[str]] | None = None,
        allowed_tool_names: set[str] | None = None,
    ) -> list[Tool]:
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
        if self._indexed_count != len(self.tools):
            self._tokens = {
                t.name: set(
                    re.findall(
                        r"[a-z0-9]+", " ".join([t.name, t.description, *t.capabilities]).lower()
                    )
                )
                for t in self.tools.values()
            }
            self._postings = {}
            for name, tokens in self._tokens.items():
                for token in tokens:
                    self._postings.setdefault(token, set()).add(name)
            self._indexed_count = len(self.tools)
        overlay_tokens = {
            name: set(re.findall(r"[a-z0-9]+", " ".join(values).lower()))
            for name, values in (scoped_capabilities or {}).items()
            if name in self.tools
        }
        postings = {
            word: self._postings.get(word, set())
            | {name for name, tokens in overlay_tokens.items() if word in tokens}
            for word in words
        }
        frequencies = {word: len(names) for word, names in postings.items()}
        matches = set().union(*postings.values())
        for name in matches:
            if allowed_tool_names is not None and name not in allowed_tool_names:
                continue
            tool = self.tools[name]
            if allowed is not None and tool.toolkit not in allowed:
                continue
            tokens = self._tokens[name] | overlay_tokens.get(name, set())
            score = sum(
                1 + math.log((len(self._tokens) + 1) / (frequencies[w] + 1)) for w in words & tokens
            )
            name_tokens = set(re.findall(r"[a-z0-9]+", tool.name.lower()))
            score += 2 * len(words & name_tokens)
            roles = set(tool.capabilities) | set((scoped_capabilities or {}).get(name, []))
            score += sum(3 for capability in roles if capability in query.lower())
            if score:
                scored.append((score, tool.name, tool))
        return [t for _, _, t in sorted(scored, key=lambda s: (-s[0], s[1]))[:limit]]
