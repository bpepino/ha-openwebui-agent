# Changelog

## 2.1.0 — unreleased

- Add **gateway mode**: a new connection choice that sends Assist turns to a home AI gateway (`POST /assistant/v1/turns`) with an API client token, instead of talking to Open WebUI directly. The gateway owns the model, tools, features, thinking, speaker identity, conversation continuation and chat retention; setup and Configure show only the answer timeout, request timeout, SSL verification, Continue listening and Strip Markdown.
- Gateway turns forward HA's conversation ID, language, device ID (or satellite entity ID), the device's area name and `extra_system_prompt`. Speech uses the gateway's TTS-ready `speech_text`; the Assist chat log gets `display_text`. The gateway's `expects_reply` also keeps satellites listening; stop phrases still win.
- Map every gateway error code to this integration's error keys, so existing translations keep working. New short, speakable messages for an unreachable gateway, a rejected token, a wrong URL, non-speech, an unavailable voice pipeline, an invalid request, gateway configuration and internal errors, and "still working" when the deadline passes after the gateway accepted a turn (never retried).
- Setup validates the URL and token with `GET /assistant/v1/health`; a revoked token starts reauthentication. Diagnostics show the backend and the gateway's health (no URL or token).
- Direct Open WebUI mode is unchanged and remains the default; existing entries keep working without changes. An unknown error key now falls back to English, then to the gateway's own message, instead of failing.

Validation: 128 portable tests passed (57 new gateway-client tests against a simulated gateway over real HTTP), Ruff lint/format passed, and the request bodies were validated with the gateway's own `TurnBody` model. New Home Assistant fixture tests for gateway mode (`tests/ha/test_gateway.py`) were added but not run locally (Windows, no Python 3.14); CI runs them. No live gateway acceptance test yet.

## 2.0.0 — 2026-09-29

- Promote the tested beta.3 agent protocol to the first stable release.
- Give the fork its own `openwebui_agent` domain, component directory and release ZIP so it can coexist with upstream's `openwebui_conversation`. Existing upstream/beta entries require one-time setup of the new integration and selection in Assist; see the README migration steps.
- Open WebUI owns native tool execution, memory and conversation context; Home Assistant provides the Assist interface.
- Includes linked multi-turn history, voice conversation controls, optional thinking suppression, idle chat cleanup and resource diagnostics from the beta series below.

Validation: 84 Home Assistant/protocol tests passed, including setup alongside an existing upstream entry using the same connection. Ruff lint/format passed; hassfest reported 1 integration with 0 invalid. The user installed beta.3 and confirmed the conversation-history issue was resolved; custom tools and memory were also reported working on Open WebUI 0.11.4. The new domain's live HACS transition, other providers, Code Interpreter, Image Generation and terminals still require setup-specific acceptance testing.

## 2.0.0-beta.3 — development history

- Send the current `user_message` and `parent_id` with each completion, matching the current Open WebUI browser/backend contract. Without these fields, the backend overwrote assistant parent links with null and later turns lost earlier user messages.
- Preserve the last successful conversation branch after a completed response is rejected as an unexecuted tool call. A later user request retains device context without replaying the failed action or including its raw tool text. Timeouts and uncertain server failures still invalidate the mapping.
- Model the backend's placeholder overwrite in the protocol tests and cover the light-on / thanks / light-off sequence and context after rejected output.

Validation: 83 full Home Assistant/protocol tests passed, 71 portable tests passed, Ruff lint/format passed, and hassfest reported 1 integration with 0 invalid. A reported raw XML tool response was confirmed not to execute; provider tool parsing remains a separate live investigation, not a claimed fix in this version.

## 2.0.0-beta.2 — development history

- Distinguish missing tools, required MCP OAuth authorization, browser-local connections and terminal errors; identify the selected resource that failed.
- Filter partially stale model defaults against accessible tools, matching the browser. Explicit custom selections remain exact; an entirely unavailable selection still reports an error.
- Cache discovery for 60 seconds during conversations, avoiding repeated catalogue requests. Configuration discovery always refreshes; failed turns invalidate the cache without retrying actions. Open WebUI still authorizes every completion.
- Add per-phase timings to debug logs and sanitized diagnostics, separating discovery, chat persistence, submission and completion waiting.
- Correct the HA chat-log API so assistant replies are recorded and question-mark follow-ups work. Add session-scoped "let's talk" / "end conversation" controls and an Always continue option.
- Add automatic deletion of newly tracked chats after 15 minutes idle, with durable cleanup deadlines, active-task protection and a Keep chat history option. Older untracked chats are unaffected.
- Add Thinking: Model default / Disabled. Disabled sends the NInfer-supported `reasoning_effort: none`; provider support is required.
- Test request-prefix stability, cleanup races/restarts/failures, discovery expiry/concurrency/failures and translated resource errors.

Early user testing on Open WebUI 0.11.4 reports working custom tool selection and memory with beta.1. Initial slow requests included substantial model-server prefill and partial cache reuse; later requests were reported faster. The precise cache behavior has not been reproduced locally, and this update does not claim to fix model-side KV caching.

## 2.0.0-beta.1 — development history

- Relaunch the public fork as Open WebUI Agent while preserving the internal `openwebui_conversation` domain and upstream attribution.
- Replace ordinary completion/search-trigger logic with saved Open WebUI chats, native streaming task submission, polling and final-message retrieval.
- Keep multi-turn chat/session mappings isolated and serialize concurrent turns per conversation.
- Discover friendly model names and accessible Workspace/MCP/server-side OpenAPI tools; resolve model tool defaults explicitly.
- Add Memory/Web Search/Code Interpreter/Image Generation flags and optional terminal discovery/defaults.
- Migrate upstream transport/model/voice settings and search enablement without discarding legacy options.
- Add sanitized diagnostics, translated failures, unload cancellation and raw-tool diagnostic protection.
- Add mocked protocol tests and real Home Assistant fixture tests, API research and manual acceptance instructions.

Compatibility: HA 2026.6.0+. Open WebUI current documented Path A, inspected source reporting 0.11.4; minimum release not established. No live server/tool/provider acceptance tests have been performed. Code Interpreter, Image Generation and terminal behavior require local acceptance testing. Conversation mappings do not survive integration reload or HA restart; remote chats remain saved.

These beta entries record validation at the time; see 2.0.0 above for the stable release status.
