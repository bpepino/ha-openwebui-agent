# API research and architecture decisions

Research date: 2026-09-29. This is source/documentation verification, not a live-server compatibility certification.

## Authoritative references

- [Server-Side Tool Calling / Path A](https://docs.openwebui.com/reference/server-side-tool-calling/)
- [Backend-Controlled API Flow](https://docs.openwebui.com/reference/api-flow/)
- [API Endpoints](https://docs.openwebui.com/reference/api-endpoints/)
- Open WebUI source revision [`8bd8b4fac5e059578ac0c74b3c18d11139f88b7d`](https://github.com/open-webui/open-webui/tree/8bd8b4fac5e059578ac0c74b3c18d11139f88b7d), whose package.json reports **0.11.4**.

The documented APIs are evolving. The earliest release supporting every required behavior was not established. We detect `/api/version` for diagnostics, do not reject newer/unknown versions numerically, and validate endpoint responses. A missing saved-chat/task API or non-task completion acceptance is an explicit incompatibility error. No live Open WebUI version was tested in this work.

## Endpoints used

| Method / endpoint | Purpose |
| --- | --- |
| GET `/api/version` | Diagnostic server version; optional if missing/forbidden |
| GET `/api/models` | Authentication validation and friendly model discovery including `info.meta` |
| GET `/api/v1/tools/` | Accessible Workspace, MCP and server-side OpenAPI resources |
| GET `/api/v1/terminals/` | Accessible terminals and chat contexts |
| POST `/api/v1/chats/new` | Save initial user node plus assistant placeholder |
| GET `/api/v1/chats/{chat_id}` | Read current tree and exact final assistant message |
| POST `/api/v1/chats/{chat_id}` | Save follow-up tree before submission |
| POST `/api/chat/completions` | Start the native streaming server task |
| GET `/api/tasks/chat/{chat_id}` | Wait for no active task IDs |
| DELETE `/api/v1/chats/{chat_id}` | Remove only tracked idle Assist chats when history retention is off |

All use Bearer authentication and the HA-owned aiohttp session. URLs retain optional reverse-proxy prefixes. Redirects are rejected, preventing accidental credential forwarding to another endpoint. No caller-supplied `tools` key is emitted, including for an empty custom selection. `tool_ids: []` is a different field and does not disable built-in injection.

The completion includes `model`, the active branch's `messages`, `stream: true`, `chat_id`, assistant `id`, a stable per-conversation `session_id`, `params.function_calling: native`, the four documented feature flags, `tool_ids`, optional `terminal_id`, and all three background generation flags set false. A session UUID follows documented HTTP Path A; it does not implement a socket connection.

The created tree includes camelCase `history.currentId`, `history.messages`, UUID IDs, `parentId`, `childrenIds`, timestamps, user model list, assistant model/name/index and `done: false`. Every follow-up links through the previous successful assistant. Structured assistant `output` is carried into subsequent model context, as in the browser. Final speech excludes reasoning, tool events and sources; only the final assistant prose is used. The original message stays in Open WebUI.

**Beta.3 correction:** the completion also sends `user_message` (the current saved user node) and `parent_id` (the prior assistant ID or null). The inspected backend's existing-chat path recreates assistant placeholders with `parentId = metadata.user_message_id`; omitting `user_message` overwrites the parent with null even when a correct tree was saved beforehand. Supplying it also lets middleware load the authoritative branch from the database. The original mock did not reproduce this overwrite and therefore missed the history loss; regression tests now reproduce it. See [the backend placeholder handling](https://github.com/open-webui/open-webui/blob/8bd8b4fac5e059578ac0c74b3c18d11139f88b7d/backend/open_webui/main.py).

## Discovery and browser defaults

The inspected [`routers/tools.py`](https://github.com/open-webui/open-webui/blob/8bd8b4fac5e059578ac0c74b3c18d11139f88b7d/backend/open_webui/routers/tools.py) aggregates local Workspace tools, OpenAPI IDs (`server:...`) and MCP IDs (`server:mcp:...`) in **one** accessible-user list. The integration does not need admin connection endpoints, infer IDs or acquire server credentials. MCP entries may carry `authenticated: false`.

[`Chat.svelte`](https://github.com/open-webui/open-webui/blob/8bd8b4fac5e059578ac0c74b3c18d11139f88b7d/src/lib/components/chat/Chat.svelte) reads `model.info.meta.toolIds`, deduplicates against the tools list, and handles OAuth in the browser. It explicitly sends these as `tool_ids`. The backend native tool resolver consumes those IDs; it does not automatically reproduce browser selection state. This applies to Workspace/MCP/server-side OpenAPI selections alike.

The integration reproduces the explicit metadata-to-ID selection. Since beta.2 it filters partially stale model defaults against available tools with a warning, matching the browser. It reports an error when all selected defaults are missing, an explicitly selected custom ID is missing, or a selected tool explicitly requires OAuth. Configure the server connection and authorize it in the browser. Browser-local `direct_server:` resources need frontend execution and produce a specific error when selected through saved metadata.

Discovery results are cached for 60 seconds during conversations, as the official guide recommends caching discovered IDs. Configuration flows explicitly refresh. A failed turn clears the cache without replaying any completion. Server-side authorization remains authoritative for every request.

The same browser reads `info.meta.terminalId` and checks availability and model capability. The integration explicitly sends it only when the user chooses model-default terminal mode. `/api/v1/terminals/` supplies IDs and `contexts`; `contexts.chat: false` is excluded. Saved-chat-scoped terminals receive the real chat ID.

User-local browser selections, attached files, skill/filter selection, per-user browser variables/location, and interactive confirmations are outside this beta. Backend-managed capabilities remain governed by Open WebUI; no invented feature flags are added.

## Existing Python client assessment

Examined [Fu-Jie/openwebui-chat-client](https://github.com/Fu-Jie/openwebui-chat-client), revision [`123243c989f3a93113f7f6a3d46838ca1b0bb08d`](https://github.com/Fu-Jie/openwebui-chat-client/tree/123243c989f3a93113f7f6a3d46838ca1b0bb08d), package **0.1.24**. The project has substantial stateful chat, file and management APIs and ongoing development; advertised tool support alone does not establish compatibility with current Path A.

| Requirement | Inspected async implementation |
| --- | --- |
| Stateful chats and tool IDs | Present |
| Native saved assistant ID in completion | Missing from `_ask` / `_ask_stream` payloads |
| Stable `session_id` and built-in features | Missing from those payloads |
| Streaming | `_ask_stream` reads SSE deltas and writes response content itself |
| Native async task polling | No `/api/tasks/chat/` implementation in inspected async chat path |
| Ordinary `chat()` | Sends `stream: false` and reads `choices[0].message.content` |
| HA session reuse | Creates/owns `httpx.AsyncClient`, rather than using HA aiohttp |
| Package dependencies | `requests`, `python-dotenv`, `httpx` |

Decision: **reference research only; no dependency and no copied code**. Wrapping the current library would still require replacing the native completion lifecycle, adding another transport and much broader functionality. Our `OpenWebUIClient` remains a narrow replaceable abstraction. Re-evaluate the library if a future release implements native Path A, session reuse and typed error semantics.

License metadata says **GPL-3.0-only**. Its included GPL section 13 expressly permits combination with AGPLv3, retaining GPL terms for the GPL-covered part and applying AGPL network-interaction requirements to the combined work. That is not permission to remove notices or simply relabel copied GPL code. No combination is shipped here, and the original project AGPL license remains unchanged. See the library's [license](https://github.com/Fu-Jie/openwebui-chat-client/blob/123243c989f3a93113f7f6a3d46838ca1b0bb08d/LICENSE).

## Home Assistant boundaries

HA 2026.6.0's ConversationEntity already creates a chat session/log before `_async_handle_message`. The integration uses that hook instead of the upstream obsolete direct `async_get_chat_log` call. Config-entry ID remains the entity unique ID. No HA LLM API or exposed-entity helper remains.

The state manager holds remote references, with a 128-entry idle LRU, per-conversation locks and unload cancellation. HA's satellite entity already retains its active conversation ID; its chat-session helper expires idle sessions after about five minutes. The adapter returns `chat_log.continue_conversation`, including HA's question punctuation handling, rather than inventing a separate device session or question detector.

When history retention is off, a dedicated cleaner tracks only chat IDs and 15-minute expiry times in HA Store. It records IDs before submission, extends expiry after each turn, and serializes cleanup against chat use. A task check defers deletion while remote work runs. Cleanup resumes after restart; conversation continuation mappings do not. The client never enumerates old user chats for deletion. Enabling retention cancels pending cleanup. Memory records remain managed separately by Open WebUI.

Thinking defaults to model/provider settings. Disabled adds only `params.reasoning_effort: none`, passed by Open WebUI's parameter handling and documented by [NInfer](https://github.com/Neroued/ninfer/blob/master/docs/serving.md). It does not reorder tools, alter earlier messages, freeze time variables or change model-side cache settings. Prompt/KV reuse remains a provider responsibility; conflicting server reasoning overrides must be resolved in the model configuration.
