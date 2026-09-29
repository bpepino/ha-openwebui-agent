# Changelog

## 2.0.0-beta.2 — unreleased

- Distinguish missing tools, required MCP OAuth authorization, browser-local connections and terminal errors; identify the selected resource that failed.
- Filter partially stale model defaults against accessible tools, matching the browser. Explicit custom selections remain exact; an entirely unavailable selection still reports an error.
- Cache discovery for 60 seconds during conversations, avoiding repeated catalogue requests. Configuration discovery always refreshes; failed turns invalidate the cache without retrying actions. Open WebUI still authorizes every completion.
- Add per-phase timings to debug logs and sanitized diagnostics, separating discovery, chat persistence, submission and completion waiting.
- Preserve the current HA satellite conversation and forward its question/follow-up signal.
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
