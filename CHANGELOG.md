# Changelog

## 3.0.0 — unreleased

Home Assistant now talks only to the home AI gateway, through this one integration. Nothing else is needed in Home Assistant.

- **One gateway, three entities.** A gateway entry provides the conversation agent, a speech-to-text entity (`POST /v1/audio/transcriptions`) and a text-to-speech entity (`POST /v1/audio/speech`), grouped on one **AI gateway** device that shows the gateway's version and links to its dashboard. Speech-to-text uploads WAV in the configured language and uses the gateway's repaired transcript (`fixed_greek`) when present, else `text`. Text-to-speech sends the configured voice (empty: the gateway's speech profile voice) and format (MP3, WAV or Opus).
- **Plain setup.** Add integration → **AI gateway** → URL, optional token, language → a **test step** showing the gateway's version, health and whether it runs in open mode. Health comes from `GET /health`; access is checked with a read-only probe of the assistant API. **Reconfigure** changes the URL, token or language; reauthentication asks for a token when the gateway starts requiring one or rejects the saved one.
- **Open mode.** The token is optional. Without one, no `Authorization` header is sent.
- **Options:** turn timeout, voice and audio format only. 2.1.0's SSL verification, request timeout, Continue listening and Strip Markdown keep their saved values but are no longer shown.
- **Turns.** The satellite's device ID and area name are resolved from the entity, device and area registries (a satellite entity without a device ID now sends its device). HA's conversation ID maps to the gateway's `conversation_id`; `new_chat` replaces the mapping. A `still_running` answer with a `turn_id` is polled at `GET /assistant/v1/turns/{turn_id}` every second until the turn timeout; `expects_reply` sets `continue_conversation`. Nothing is retried.
- The gateway client now uses Home Assistant's shared httpx client.
- **Direct Open WebUI mode is deprecated** and will be removed in 3.1. It stays behind **Open WebUI directly (deprecated)** in the setup menu; existing direct entries keep working and get a repair notice. Its documentation moved to [docs/direct-mode.md](docs/direct-mode.md).
- `iot_class` is now `local_polling`: there is no cloud service.

Validation: 179 tests passed on Linux with Python 3.14.10 and Home Assistant 2026.6.0 (`pytest-homeassistant-custom-component` 0.13.336), 35 of them Home Assistant fixture tests with respx simulating the gateway (config flow and test step, open mode without a token, tokens, reauth, reconfigure, options, Assist turns with `still_running` polling, conversation mapping and `continue_conversation`, STT, TTS, diagnostics, direct-mode deprecation). The 144 portable tests also passed on Windows with Python 3.12. Ruff lint and format passed. Not run: hassfest, HACS validation, and any test against a live gateway. The gateway checkout inspected for this change (version 18.0.0) does not yet answer `still_running` with a `turn_id`; it answers a deadline with `504` and `still_running: true`, which this version reports as "still working".

## 2.1.0 — unreleased (superseded by 3.0.0)

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
