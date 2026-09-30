# Contributing

Use a separate branch and add the smallest useful connector or capability.
Install `uv sync --all-extras`. Run `uv run ruff check .`, `uv run ruff format
--check .`, `uv run mypy`, `uv run pytest -q` and `uv build` before submitting.

A connector needs a manifest, official documentation links, credential behavior,
measurement semantics and meaningful success/failure tests. Public live tests
belong in opt-in probes, never in the offline test suite. Private integrations
must say when only contract fixtures were tested. Include numerical assertions
for local models, rather than merely asserting an output exists.

Never add real account identifiers, credentials, private telemetry or copied data
without permission. Keep physical-control actions separate and denied by default.
Document licence notices for any upstream code copied into the repository.

See [connector development](docs/connector-development.md). Issue reports should
include version, tool name and sanitized structured error, never credential values.
