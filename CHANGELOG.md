# Changelog

## 3.3.1 — unreleased

Security fix for `openwebui_agent.ask_action`, after a review of the gateway's side.

- **A gateway token is required, also in open mode.** The gateway now refuses `ask_action` without the Home Assistant client's own token (a yes on the satellite runs the plan, so no device on the network may ask anonymously). Without a configured token the action fails in Home Assistant with a clear message and nothing is sent.
- The gateway also refuses steps whose `data` the spoken question would not say (`data_not_spoken`), answers a yes or no as the satellite rather than the owner, and refuses browser requests (`application/json` only, no foreign `Origin`). Nothing changes in what this integration sends.

Validation: the portable tests passed on Windows with Python 3.12 (a call without a token sends nothing). Ruff lint and format passed. Not run: the Home Assistant fixture tests (Linux only), hassfest, HACS validation, a live gateway.

## 3.3.0 — unreleased

Follows the gateway's automation store. Gateway entries only; everything in 3.2 stays.

- **Ask before acting.** New service `openwebui_agent.ask_action` (`question`, `steps`, `satellite`, `area`, `context`) for the store's question recipes (bedtime sweep, window open while heating or AC runs, water heater on too long). It hands the gateway a proposed plan over the gateway link (`POST /household/v1/ask_action`, the gateway token); the gateway checks it, asks on the satellite with `ask_question` and runs it only on a yes. Home Assistant never runs it.
- The fields are checked before anything is sent: plain entity ids, `domain.service` (or a bare service such as `turn_off`, split per entity domain), at most 12 steps and 40 entities, plain values only and nothing that still looks like a template.
- The gateway's answer is the action's response (`asked`, `skipped` with the reason); a refusal (`not_exposed`, `prohibited`, `proactive_questions_off`, ...) fails the action so the automation's trace shows it.
- The service's texts in English and Greek.

Validation: the portable tests passed on Windows with Python 3.12 (236 passed; the service's field checks and the forwarding with respx). One Home Assistant fixture test was added (the service validates, forwards with the token and raises on a refusal); it was not run here (Linux only). Ruff lint and format passed. Not run: hassfest, HACS validation, and any test against a live gateway or satellite.

## 3.2.0 — unreleased

Follows the gateway's Phase 5 (the direct voice path). Gateway entries only; everything in 3.1 stays.

- **Listening again is the gateway's decision.** The satellite keeps listening without the wake word exactly when the answer's `continue_conversation` is true (falls back to `expects_reply` when a gateway leaves it out), and never after a degraded answer. Home Assistant's own question-mark guess, "let's talk" and the hidden 2.1.0 "always continue" no longer apply to gateway entries, so a pending approval's microphone never collides with the follow-up.
- **Streamed answers.** Turns ask for `response.stream`. Each `delta` sentence goes into Home Assistant's chat log as it arrives (`ChatLog.async_add_delta_content_stream`); the agent declares streaming support. `final` gives the flags and the full answer; an `error` event, or a stream that breaks after `status` named a `turn_id`, is followed up like the 504. A gateway that answers JSON is read as before.
- **Streamed speech.** The text-to-speech entity takes streamed text (`async_stream_tts_audio`), cuts it into sentences with the gateway's rule (ported with its tests) and requests each sentence as MP3 as soon as it is complete. New option **Speak while the answer streams** (on); off, every answer is one request in the configured format, which is the fallback the gateway's brief describes.
- **Questions and approvals** from the gateway (`ask_question`, `start_conversation`) need nothing here; HA-side prompts (`extra_system_prompt`) are no longer forwarded into turns, so the gateway's own question context is the only one.
- Diagnostics show whether speech streams and the last turn's path.

Checked against Home Assistant 2026.6.0 (the brief's VERIFY items):

- `ChatLog.async_add_delta_content_stream(agent_id, stream)` exists with that name and signature: holds.
- `TextToSpeechEntity.async_stream_tts_audio(TTSAudioRequest)` with `TTSAudioRequest(language, options, message_gen)` and `TTSAudioResponse(extension, data_gen)`; streaming input is used when `async_supports_streaming_input()` is true: holds.
- The voice pipeline starts text-to-speech before the agent finishes once more than 60 streamed characters arrived (`STREAM_RESPONSE_CHARS = 60`), and sends ESPHome satellites `tts_start_streaming`: holds. Shorter answers get one text-to-speech request with the final speech.
- Per-sentence MP3 concatenated through Home Assistant's own ffmpeg conversion (the command `tts` runs, to WAV and FLAC at 16 kHz): all audio decoded, ffmpeg exits 0, about 50 ms of codec padding at each sentence boundary: holds for Home Assistant.
- A `start_conversation` reply reaches the pipeline's conversation agent with the satellite's `device_id` (and `satellite_id`): holds.
- Not checkable here: whether Voice PE firmware plays the stream progressively, and how the sentence boundaries sound on it. If either fails, turn **Speak while the answer streams** off.

Validation: 285 tests passed on Linux with Python 3.14 and Home Assistant 2026.6.0 (`pytest-homeassistant-custom-component` 0.13.336): 223 portable tests (streamed turns with and without deltas, error events, broken streams followed up, JSON answers, per-sentence speech, the ported sentence rule) and 62 Home Assistant fixture tests (deltas reaching the chat log and its listener once, the gateway's `continue_conversation` against question marks, `expects_reply` and degraded answers, per-sentence MP3 requested before the rest of the text exists, one request with the option off, no `extra_system_prompt` in turns). The portable tests also passed on Windows with Python 3.12. Ruff lint and format passed. Not run: hassfest, HACS validation, and any test against a live gateway or satellite.

## 3.1.0 — unreleased

Follows the gateway's Phase 4. Gateway entries only.

- **Degraded answers from the gateway.** When the gateway's degraded mode is on and its assistant upstream is offline, it answers with "Assistant offline, basic commands only." and Home Assistant's own agent, marked `degraded: true`. Home Assistant speaks it as usual, never keeps listening after it and never retries it.
- **Fallback while the gateway is unreachable** (new option, off by default): on a connection or DNS failure or a 5xx from a proxy, but never on a `4xx` or a `504 still_running`, Home Assistant's own agent (`conversation.home_assistant`) answers the same text, conversation, language and satellite, after the translated "Assistant offline, basic commands only." With the option off, the gateway's unreachable message is spoken as before.
- Both degraded answers are shown the same way: a `degraded` attribute on the conversation entity, an `openwebui_agent_degraded` event (`source`, `device_id`, `satellite_id`) and diagnostics.
- **Health.** The test step and diagnostics read `open_mode` from `GET /health` and say whether a token is needed; diagnostics also show the gateway's degraded mode (`enabled`, `offline`). Gateways that do not report `open_mode` are probed as before.
- The turn follow-up (`GET /assistant/v1/turns/{turn_id}`) and the household event push are unchanged. Newer gateways also derive the same events themselves with the same IDs and drop duplicates.
- Direct Open WebUI mode is still available and deprecated; its notices no longer name 3.1 as the removal version.

Validation: 263 tests passed on Linux with Python 3.14 and Home Assistant 2026.6.0 (`pytest-homeassistant-custom-component` 0.13.336): 209 portable tests and 54 Home Assistant fixture tests with respx simulating the gateway (a degraded gateway answer with its attribute, event and diagnostics; the fallback on a connection failure and a proxy 502, where Home Assistant's agent really answered a built-in intent; no fallback with the option off, on a 401, a 504 `still_running` or a gateway error envelope; `open_mode` read from health). The portable tests also passed on Windows with Python 3.12. Ruff lint and format passed. Not run: hassfest, HACS validation, and any test against a live gateway.

## 3.0.1 — unreleased

Fixes after an independent review of 3.0.0. Gateway entries only.

- **Exposed entities and household events.** The gateway's household reviews read the entities you exposed to Assist at `GET /api/oikos` (read-only, needs a Home Assistant token; same query parameters and response shape as the gateway's former component, including `entities` and the `push` status) and receive household events from this integration at `POST /household/v1/events`: `person_arrived_home`, `person_left_home`, `door_open_long`, `device_unavailable`, `device_available` and, optionally, `state_changed`, each with a deterministic event ID, retries and throttled warnings. The event rules (`events.py`) are the gateway's pure module, unchanged. In open mode events are sent without a token; an `events` token is sent only when set. New collapsed **Household events** section in **Configure**: send events (on), persons, door open too long after (10 min), device unavailable after (2 min), every state change (off), events token. Diagnostics show the push counts and whether an events token is set, never the token. The manifest now depends on `http` and loads after `person`.
- **Speech-to-text** returns `plain_text`, else `text`, and never the repaired `fixed_greek`: the gateway identifies the speaker and picks the answer language by matching the turn against `text`/`plain_text`. The upload uses the base code of the Assist pipeline's language (`en` for `en-US`), and the configured language only when the pipeline has none.
- **Long turns** follow the gateway's contract: the `turn_id` comes from the 504 `still_running` envelope and `GET /assistant/v1/turns/{turn_id}` is asked every 2 s for at most the turn timeout. `done` speaks the result's `speech_text`, `failed` its error; 404 (unknown or expired turn), a failed follow-up or the end of the wait keep the "still working" answer. The guessed 200 `still_running` reply of 3.0.0 is gone.
- **403 at setup** (a token without the `assistant:turns` scope) now starts reauthentication instead of endless retries. A configured token is checked in open mode too.
- The gateway's `display_text` is shown as it is: it is already plain, so the second Markdown pass is skipped in gateway mode. The hidden 2.1.0 Strip Markdown option no longer applies to gateway entries.

Validation: 238 tests passed on Linux with Python 3.14 and Home Assistant 2026.6.0 (`pytest-homeassistant-custom-component` 0.13.336): 193 portable tests (including the gateway's event-module tests, ported) and 45 Home Assistant fixture tests with respx simulating the gateway (`/api/oikos` with exposed and unexposed entities and area names, `person_arrived_home` with its deterministic ID, no `Authorization` header in open mode and an events token when set, retries on 5xx, `door_open_long` timers, clean unload, the 504 follow-up, 403 reauthentication). The portable tests also passed on Windows with Python 3.12. Ruff lint and format passed. Not run: hassfest, HACS validation, and any test against a live gateway.

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
