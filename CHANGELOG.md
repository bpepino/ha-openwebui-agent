# Changelog

## 2.0.0-beta.3 — unreleased

- Send the current `user_message` and `parent_id` with each completion, matching the current Open WebUI browser/backend contract. Without these fields, the backend overwrote assistant parent links with null and later turns lost earlier user messages.
- Preserve the last successful conversation branch after a completed response is rejected as an unexecuted tool call. A later user request retains device context without replaying the failed action or including its raw tool text. Timeouts and uncertain server failures still invalidate the mapping.
- Model the backend's placeholder overwrite in the protocol tests and cover the light-on / thanks / light-off sequence and context after rejected output.

Validation: 83 full Home Assistant/protocol tests passed, 71 portable tests passed, Ruff lint/format passed, and hassfest reported 1 integration with 0 invalid. A reported raw XML tool response was confirmed not to execute; provider tool parsing remains a separate live investigation, not a claimed fix in this version.

## 2.0.0-beta.2 — unreleased

- Distinguish missing tools, required MCP OAuth authorization, browser-local connections and terminal errors; identify the selected resource that failed.
- Filter partially stale model defaults against accessible tools, matching the browser. Explicit custom selections remain exact; an entirely unavailable selection still reports an error.
- Cache discovery for 60 seconds during conversations, avoiding repeated catalogue requests. Configuration discovery always refreshes; failed turns invalidate the cache without retrying actions. Open WebUI still authorizes every completion.
- Add per-phase timings to debug logs and sanitized diagnostics, separating discovery, chat persistence, submission and completion waiting.
- Correct the HA chat-log API so assistant replies are recorded and question-mark follow-ups work. Add session-scoped "let's talk" / "end conversation" controls and an Always continue option.
- Add automatic deletion of newly tracked chats after 15 minutes idle, with durable cleanup deadlines, active-task protection and a Keep chat history option. Older untracked chats are unaffected.
- Add Thinking: Model default / Disabled. Disabled sends the NInfer-supported `reasoning_effort: none`; provider support is required.
- Test request-prefix stability, cleanup races/restarts/failures, discovery expiry/concurrency/failures and translated resource errors.

Early user testing on Open WebUI 0.11.4 reports working custom tool selection and memory with beta.1. Initial slow requests included substantial model-server prefill and partial cache reuse; later requests were reported faster. The precise cache behavior has not been reproduced locally, and this update does not claim to fix model-side KV caching.

## 2.0.0-beta.1 — unreleased

- Relaunch the public fork as Open WebUI Agent while preserving the internal `openwebui_conversation` domain and upstream attribution.
- Replace ordinary completion/search-trigger logic with saved Open WebUI chats, native streaming task submission, polling and final-message retrieval.
- Keep multi-turn chat/session mappings isolated and serialize concurrent turns per conversation.
- Discover friendly model names and accessible Workspace/MCP/server-side OpenAPI tools; resolve model tool defaults explicitly.
- Add Memory/Web Search/Code Interpreter/Image Generation flags and optional terminal discovery/defaults.
- Migrate upstream transport/model/voice settings and search enablement without discarding legacy options.
- Add sanitized diagnostics, translated failures, unload cancellation and raw-tool diagnostic protection.
- Add mocked protocol tests and real Home Assistant fixture tests, API research and manual acceptance instructions.

Compatibility: HA 2026.6.0+. Open WebUI current documented Path A, inspected source reporting 0.11.4; minimum release not established. No live server/tool/provider acceptance tests have been performed. Code Interpreter, Image Generation and terminal behavior require local acceptance testing. Conversation mappings do not survive integration reload or HA restart; remote chats remain saved.

No release has been published.
