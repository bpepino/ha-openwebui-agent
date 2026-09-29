# Implementation report — Open WebUI Agent 2.0.0-beta.1

Historical report: the initial beta recommendations below are superseded by **2.0.0 stable** after user confirmation that beta.3 resolved conversation history. The stable release uses the separate `openwebui_agent` domain and directory; the old-domain preservation and in-place migration described below apply only to the betas. See the [changelog](../CHANGELOG.md) for current release notes and validation.

This records the initial beta.1 implementation and validation. Subsequent beta.2 resource errors, discovery caching, timing diagnostics and user testing are recorded in the [changelog](../CHANGELOG.md); current behavior is documented in the [README](../README.md).

Beta.2 follow-up validation (2026-09-29): **81 tests passed** on Linux/Python 3.14.7/HA 2026.6.0; **69 portable tests passed** on Windows. Ruff lint and formatting passed (27 Python files); official hassfest passed with **1 integration, 0 invalid**. This covers the HA chat-log correction, voice controls, retention lifecycle and thinking request payload. The new satellite/retention/thinking behavior still needs live acceptance testing; working tools and memory on Open WebUI 0.11.4 were user-reported with beta.1. HACS validation remains delegated to the configured GitHub workflow after the maintainer pushes.

## 1. Architecture

Assist uses Home Assistant's ConversationEntity/ChatLog lifecycle to obtain a conversation ID and send the user's original text to the integration's state manager. A dedicated async client creates or continues a saved Open WebUI chat, submits a native streaming completion, polls server tasks, and reads the exact assistant placeholder after completion. Open WebUI alone discovers/executes tools and performs all model/tool rounds. Only final assistant prose reaches Assist/TTS; no Home Assistant LLM API, entity tool registration or service executor exists.

Per-entry mappings isolate HA conversations and serialize concurrent turns. Model changes, reload/restart, idle eviction or uncertain failure start a new remote chat. Deleted chat references recover once. Remote chats are retained. Structured assistant output is sent back as context on follow-up turns and remains available in Open WebUI.

## 2. Meaningful files changed

All integration paths below are under `custom_components/openwebui_conversation/`.

| Files | Change |
| --- | --- |
| `client.py` (new) | Async HTTP, exact resource discovery/defaults, saved trees, native requests, polling, final response filtering |
| `state.py` (new) | Typed conversation references, bounded mappings, per-conversation serialization and unload cancellation |
| `migration.py` (new) | Testable migration of upstream option semantics |
| `config_schema.py` (new) | Shared translated dynamic selectors |
| `diagnostics.py` (new) | Allowlisted diagnostic metadata with no secrets/messages/URLs |
| `__init__.py` | Entry setup/authentication, migration, reload and unload |
| `conversation.py` | Thin current-HA Assist adapter and translated errors |
| `config_flow.py` | Connection/model/features/tools/terminal setup, refreshed options and reauthentication |
| `const.py`, `exceptions.py` | New identity, defaults and sanitized typed errors |
| `manifest.json` | Display name/version, verified fork URLs, current maintainer |
| `strings.json`, `translations/en.json` | All new labels, selectors, warnings and error messages |
| `api.py`, `coordinator.py`, `helpers.py`, `message.py` (deleted) | Remove obsolete completion transport, health polling, unused exposed-entity helper and duplicate history model |

Repository files:

- `README.md`: rewritten installation, architecture, options, migration, limitations and troubleshooting.
- `CHANGELOG.md`, `CONTRIBUTING.md`, `SECURITY.md`: new public-project documentation.
- `docs/api-research.md`: exact source revisions, API/default semantics and third-party client/license assessment.
- `docs/testing.md`: live acceptance procedures.
- `docs/development.md`: reproducible environments, validation and probe instructions.
- `docs/implementation-report.md`: this report.
- `tests/conftest.py`, `tests/test_client.py`, `tests/test_migration.py`: portable real-client protocol/state tests using a simulated HTTP server.
- `tests/ha/conftest.py`, `tests/ha/test_integration.py`: real HA fixture tests for flows, migration, Assist, diagnostics, setup/unload and reauth.
- `pytest.ini`, `requirements-test.txt`, `requirements-ha-test.txt`: isolated test configurations.
- `requirements.txt`, `.python-version`, `.devcontainer.json`: align development with HA 2026.6.0/Python 3.14.2+.
- `.ruff.toml`: current configuration layout, ignored research environments and test fixture docstring exceptions.
- `.gitignore`: exclude local virtual environments and research downloads.
- `hacs.json`: fork display name, HA minimum, source-directory installation without a required release ZIP.
- `.github/workflows/lint.yml`: real Linux HA tests plus Ruff/format checks.
- `.github/ISSUE_TEMPLATE/bug.yml`, `.github/ISSUE_TEMPLATE/feature_request.yml`, `.github/pull_request_template.md`: useful sanitized reporting and fork links.
- `scripts/probe.py`: read-only discovery by default; an explicitly requested prompt creates a native test chat.
- `scripts/setup`, `scripts/lint`, `scripts/develop`: test dependencies, formatting and corrected module search path.

`LICENSE` is unchanged. Existing release and official HACS/hassfest workflows are retained; no workflow was triggered remotely.

## 3. API implementation

| Endpoint | Use |
| --- | --- |
| GET `/api/version` | Diagnostic server version |
| GET `/api/models` | Auth validation and all visible base/Workspace model metadata |
| GET `/api/v1/tools/` | Accessible Workspace/MCP/server-side OpenAPI discovery |
| GET `/api/v1/terminals/` | Terminal discovery and chat-context filtering |
| POST `/api/v1/chats/new` | Initial linked user/assistant tree |
| POST `/api/v1/chats/{id}` | Follow-up tree update |
| POST `/api/chat/completions` | Native streaming submission with chat, assistant and session IDs |
| GET `/api/tasks/chat/{id}` | Async polling until task IDs are empty |
| GET `/api/v1/chats/{id}` | Read current tree and final assistant message |

The request never includes an OpenAI `tools` property. It includes `tool_ids`, four supported feature flags, optional terminal, native function-calling mode and disabled title/tag/follow-up generation. Both structural acceptance metadata and completed message state are checked. Unexecuted function syntax is rejected diagnostically, never executed. Request and completion timeouts are separate; mutations are not automatically retried.

## 4. Model-default tools

The inspected Open WebUI browser reads `info.meta.toolIds` and explicitly sends selected IDs. Its `/api/v1/tools/` endpoint already aggregates Workspace, MCP and server-side OpenAPI resources. This integration follows that behavior, preserving exact IDs and requiring access/OAuth readiness. It reads `info.meta.terminalId` only when model-default terminal mode is selected and sends it explicitly. Missing defaults produce an error instead of silently removing them. Browser-local selections, direct tool connections, skills/filters and confirmation dialogs are not imported.

The additional Fu-Jie research found that async client 0.1.24 does not implement the required assistant-ID/session/task-polling flow. It remains a reference only; no dependency or copied code is included. Its GPLv3 section 13 permits AGPLv3 combination with obligations retained, but protocol/session/dependency compatibility determined this decision. See [the full assessment](api-research.md#existing-python-client-assessment).

## 5. Config migration

Entry version 1 migrates to 2.1. Credentials, URL, entry identity/title, explicit model, timeout, SSL and Markdown values are retained. An omitted upstream timeout retains its former 60-second behavior; new installs use 30 seconds. Search enablement becomes native Web Search; old trigger phrases/prefix/language options remain stored but inactive. Upstream omitted Markdown/search values retain False. A missing model selection requires selecting a discovered model instead of guessing an ID. HA 2026.6.0+ is now explicit.

## 6. Internal domain

`openwebui_conversation` is preserved to avoid breaking config entries, entity identity, installation paths and HACS upgrades. The public repository name is `ha-openwebui-agent`; display name is `Open WebUI Agent`.

## 7. Validation results

Final results are recorded after the final audit. Tests use simulated Open WebUI HTTP responses and real HA fixtures; they do not operate real devices.

- Full pytest, Linux / Python 3.14.7 / HA 2026.6.0: **57 passed**.
- Portable protocol/state/migration suite, Windows / Python 3.12.14: **47 passed**.
- Ruff 0.16.9: **all checks passed**.
- Ruff formatting: **25 files already formatted**.
- Official hassfest from HA 2026.6.0 source: **1 integration; 0 invalid integrations**, all applicable validators passed.
- `git diff --check`: passed.
- Probe help and import smoke check: passed. No live API calls were made by the probe.
- HACS action: **not run locally**. The official action uses Docker/GitHub context; Docker is not installed here, and these changes are intentionally unpublished. Its workflow remains configured for the maintainer's later push. Local JSON/layout inspection is not represented as a full HACS pass.

Initial test runs exposed missing test-environment dependencies/fixtures, which were corrected. No known remaining automated-test failure is accepted or hidden. The original repository had no test suite, and its Python 3.11 workflow conflicted with its pinned HA 2026.6.0 dependency; this was corrected.

## 8. Primary manual test

Use the same API-key user/model that already runs `ha_control_device` successfully in Open WebUI. Select this integration in an Assist pipeline and disable Prefer handling commands locally. Say **Turn on the office light.** Inspect the saved Open WebUI chat/tool logs for model selection → server tool execution → result → final model response, and confirm the actual device state. Assist must receive only the final natural-language answer. Any raw tool protocol fails acceptance. This physical-device test is **not yet performed**.

## 9. Web Search test

Configure the server search provider and user/model permissions; enable the integration's Web Search flag. Ask for current Athens weather and inspect a real search execution and sources in Open WebUI. Repeat with a natural current-information question without a trigger phrase. Assist must speak only the summary. Model behavior cannot be proved from a plausible response alone.

## 10. Memory test

Enable Memory on server/user/model and integration. Ask it to remember a harmless test preference. Verify the write in the Open WebUI Memory UI, start a new HA conversation, ask for the preference, and verify retrieval. Delete the test memory afterward. This tests Open WebUI memory rather than same-chat context.

## 11. Multi-turn test

Give a harmless temporary test word and ask for it in the same Assist conversation. Verify the same Open WebUI chat/session and linked messages. Start another HA conversation and verify a distinct chat/session. Delete a mapped chat and check one-time recovery. Reload HA and verify the documented fresh-chat behavior. Full steps for these tests and error/terminal/code/image cases are in [testing.md](testing.md).

## 12. Known limitations

- No live Open WebUI/provider/device test yet; optional features require acceptance testing.
- Mappings are in memory, with 128 idle references; no restart/reload continuity.
- No file uploads, attachment selection, media rendering, browser-local tools, real WebSocket or interactive browser confirmations.
- Browser Pyodide code execution is unsupported; use a server engine such as Jupyter.
- Other backend built-ins remain server governed; separate calendar/notes/automation/sub-agent APIs and browser-selected skills/filters are not implemented.
- Only final prose is spoken; sources/artifacts remain in Open WebUI. No-text output is an actionable error.
- Concurrent editing of the same remote chat in the browser is unsupported.
- Timeout/unload does not guarantee remote action cancellation. Inspect saved chats before repeating actions.
- The voice pipeline may impose a shorter timeout than the integration.
- Only English translations were present upstream; English is updated, and fallback is used for errors in other languages.

## 13. Open WebUI compatibility

Targets current official documented Path A and source revision `8bd8b4fac5e059578ac0c74b3c18d11139f88b7d`, reporting version 0.11.4. The earliest fully compatible release was not established; no numeric minimum is fabricated. The client detects version for diagnostics and checks required API behavior without rejecting future versions. **Live tested version: none.**

## 14. Readiness

- **Local testing:** ready, with passing automated checks and acceptance procedures.
- **Public beta:** candidate after the maintainer completes the live primary/search/memory/multi-turn tests and remote HACS validation. Not yet acceptance-certified.
- **Normal public use:** not yet; gather live provider/tool/version evidence first.

## 15. Manual GitHub work

No remote changes were made. The verified remote owner is `bpepino`, so manifest, README and issue links already use that owner; change them only if ownership changes.

- Repository: **ha-openwebui-agent**.
- Display name: **Open WebUI Agent**.
- Suggested description: **Home Assistant HACS conversation integration that turns Assist into a thin client for Open WebUI, with server-side agent execution, native tools, MCP/OpenAPI tools, memory, web search, and multi-turn conversations.**
- Topics: `home-assistant`, `homeassistant`, `hacs`, `open-webui`, `openwebui`, `assist`, `conversation`, `llm`, `agent`, `mcp`, `tool-calling`, `home-automation`.
- Enable Issues and private vulnerability reporting. Discussions are optional for setup/help questions.
- Review/push the local branch yourself, inspect CI including HACS validation, then publish a prerelease only after acceptance tests.
- HACS default-catalog submission remains a separate future decision.

## 16. First release recommendation

Version: **2.0.0-beta.1** (already in the local manifest).

Title: **Open WebUI Agent 2.0.0-beta.1 — native server-side agent beta**.

Suggested release notes: This fork turns Assist into a thin client for Open WebUI's saved-chat native agent flow, adds dynamic model/tool discovery and model defaults, native feature toggles, optional terminals, multi-turn conversations, migrations and diagnostics. Requires HA 2026.6.0+. Keeps the upstream domain and AGPL attribution. Mappings reset on reload/restart; browser-only features and non-text rendering are limited. Include the actual Open WebUI/provider versions and acceptance results before publishing; do not claim universal tool support.

## 17. Local commits

At the user's subsequent request, the changes were committed locally on **feature/openwebui-full-agent**:

1. `880e6f6` — `feat: add Open WebUI native agent execution and configuration`
2. `test: cover native agent protocol and Home Assistant lifecycle`
3. `docs: relaunch integration as Open WebUI Agent`

The second commit includes development/CI alignment; the third includes this report. No push, PR, release, additional GitHub repository or HACS submission was created. The user will push and perform live Home Assistant acceptance testing.
