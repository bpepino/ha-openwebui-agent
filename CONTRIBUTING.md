# Contributing

Start with [development](docs/development.md) and [API research](docs/api-research.md). Keep this integration a thin client: Open WebUI owns all model/tool execution. Preserve the internal HA domain and migrate existing options.

For a bug, include integration/HA/Open WebUI versions, model/provider, feature flags, tool mode, reproduction steps, and whether the same request works as the same user in Open WebUI's browser. Attach sanitized diagnostics and only the relevant sanitized debug logs. Never share keys, tokens, private prompts, memory contents or tool arguments.

Run Ruff, protocol tests and real Home Assistant tests before proposing changes. State which live acceptance tests you performed; do not substitute mocked results for physical-device verification. Describe user-visible behavior and compatibility changes in the changelog.

Contributions remain under this project's AGPL-3.0 license. Preserve upstream and third-party notices where applicable.
