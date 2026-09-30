"""Provider schema formatting preserves the original JSON Schema and optionality."""

import re
from copy import deepcopy
from typing import Literal

from .models import Json


def format_tools(
    tools: list[Json], provider: Literal["openai", "openai-responses", "anthropic"]
) -> list[Json]:
    formatted = []
    aliases: set[str] = set()
    for tool in tools:
        name = provider_name(tool["name"])
        if name in aliases:
            raise ValueError("Provider tool aliases collide")
        aliases.add(name)
        description, inputs = tool["description"], deepcopy(tool["input_schema"])
        if provider == "anthropic":
            formatted.append({"name": name, "description": description, "input_schema": inputs})
        elif provider == "openai-responses":
            formatted.append(
                {
                    "type": "function",
                    "name": name,
                    "description": description,
                    "parameters": inputs,
                    "strict": False,
                }
            )
        elif provider == "openai":
            formatted.append(
                {
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": description,
                        "parameters": inputs,
                        "strict": False,
                    },
                }
            )
        else:
            raise ValueError("Unknown provider")
    return formatted


def provider_name(name: str) -> str:
    alias = re.sub(r"[^a-zA-Z0-9_-]", "_", name)
    if not alias or len(alias) > 64:
        raise ValueError("Tool name cannot be represented by this provider")
    return alias


def resolve_provider_name(tools: list[Json], name: str) -> str:
    matches = [t["name"] for t in tools if provider_name(t["name"]) == name]
    if len(matches) != 1:
        raise ValueError("Unknown or ambiguous provider tool alias")
    return matches[0]
