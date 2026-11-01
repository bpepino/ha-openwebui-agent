# Manual acceptance tests

## Gateway mode (3.2.0)

These need a running AI gateway on your LAN and an Assist satellite (or the Assist dialog with a microphone). They have not been run against a live gateway as part of the 3.x changes; the automated tests simulate the gateway at the HTTP layer. Record the integration, Home Assistant and gateway versions with your results.

1. **Open mode setup.** Add the integration, choose **AI gateway**, enter the gateway URL and leave the token empty. The test step must show the version the gateway dashboard shows, and "open mode". Submit: one **AI gateway** device appears with a conversation, a speech-to-text and a text-to-speech entity.
2. **Tokens required.** Turn on **Require tokens** in the gateway and reload the entry. Home Assistant must ask for a token. A wrong token stays on the form with an error; the token of an API client of kind `ha` is accepted and the entry loads. Turn the setting off again if you use open mode.
3. **Whole pipeline.** In **Settings → Voice assistants** select **AI gateway** as conversation agent, speech-to-text and text-to-speech, and turn off **Prefer handling commands locally**. Say "Turn on the office light." The gateway's request log must show one transcription, one turn carrying the satellite's device and area, and one speech request with the configured voice and format. The light must actually change.
4. **Transcript and speaker.** Speak a sentence in the household language. The Assist pipeline debug view must show the transcript's `plain_text` (not the repaired `fixed_greek`), the gateway's transcription log must show the pipeline's language as a base code (`en`, not `en-US`), and the gateway must greet or attribute the identified speaker on the turn that follows.
5. **Follow-up.** Ask something that makes the agent ask back. The satellite must listen again without a wake word. Answer; the gateway must continue the same conversation (same chat in Open WebUI).
6. **Long turn.** Set the turn timeout to 10 seconds and ask for something that takes about 15 seconds. The gateway must receive the turn only once, answer `504` with a `turn_id`, and then show `GET /assistant/v1/turns/{turn_id}` about every 2 seconds until the answer, which Assist speaks. Ask for something that takes longer than 20 seconds: you must hear "I'm still working on that", and the gateway must still finish the turn.
7. **Options.** Change the voice and the audio format in **Configure**. The next answer must use them; a sentence spoken before must not replay in the old voice.
8. **Failures.** Stop the gateway: Assist must say it can't reach the gateway, and a Home Assistant restart must keep retrying the entry instead of asking for a token. Start the gateway again: the entry recovers.
9. **Diagnostics.** Download diagnostics: they must show the gateway's version and access mode and the household event counts, and contain neither the URL nor any token.
10. **Token scope.** Give the entry the token of a client without the `assistant:turns` scope. Reloading must ask for a token (reauthentication), not retry forever.
11. **Exposed entities.** Expose a light in an area and leave a switch unexposed. `curl -H "Authorization: Bearer <HA token>" "http://<home-assistant>:8123/api/oikos?area=1&caps=1&assistant=conversation"` must list the light with its area name and not the switch. The gateway's household page must show the same entities.
12. **Household events.** Let a person tracked under **Configure → Household events** come home: the gateway's event log must show one `person_arrived_home` whose `event_id` starts with `ha-person_arrived_home-`. Leave an exposed door open longer than the configured time: one `door_open_long`. In open mode the request carries no `Authorization` header; with **Require tokens** on, events need an `events` token, and without one diagnostics count them as failed with HTTP 401.
13. **Open mode from health.** With a gateway that reports `open_mode` in `GET /health`, the test step must say "open mode" or "tokens required" accordingly, and diagnostics must show `token_needed`.
14. **Degraded answer at the gateway.** Turn on the gateway's degraded mode and stop its Open WebUI. Ask a satellite "turn on the office light": you must hear "Assistant offline, basic commands only." and the light must change; the satellite must not keep listening. The conversation entity's `degraded` attribute must be `true`, an `openwebui_agent_degraded` event with `source: gateway` must appear (Developer tools → Events), and diagnostics must count one degraded answer. The next normal answer sets the attribute back to `false`.
15. **Fallback here.** Stop the gateway. With **Fall back to Home Assistant when the gateway is unreachable** off you hear "I can't reach the assistant gateway right now." Turn it on and ask again: you must hear "Assistant offline, basic commands only." followed by Home Assistant's own answer, and an event with `source: home_assistant`. A wrong token (401) or a slow turn (still working) must never fall back.
16. **Streamed answer.** Ask a Voice PE something with a long answer (more than one sentence). The Assist dialog's debug view must show `tts_start_streaming`, the gateway's log one speech request per sentence, and the satellite must start speaking before the answer is complete. Listen for gaps or cuts between sentences. If playback waits for the whole answer or has gaps, turn **Speak while the answer streams** off and check that the whole answer is spoken in one request.
17. **Listening again.** Ask something the agent answers with a question: the satellite must listen without the wake word. Answer with a statement: it must not. With the gateway's follow-up switch off, it must never listen again, even after a question.
18. **Approval.** Ask for an action that needs approval: the satellite must ask the question (for example "Close Kitchen blinds?") after the answer, without its follow-up microphone opening first. Answer "yes": the action runs; "no" or silence: it does not.
19. **Gateway question.** With the gateway's proactive questions on, let it ask on a satellite (`start_conversation`). Answer it: the gateway's log must show the turn with that satellite's `device_id` and no `extra_instructions`.

## Direct Open WebUI mode (deprecated)

These tests require your own configured Home Assistant and Open WebUI instance. User testing confirmed tools, memory and the beta.3 conversation-history fix on Open WebUI 0.11.4; the full matrix below has not been independently run against a live server. Save the integration, HA, Open WebUI, model/provider versions and results. Compare with the same Open WebUI API-key user in the browser.

## Setup

1. Install this component locally and restart HA 2026.6.0+.
2. Add the integration with **Open WebUI directly (deprecated)**. Configure an API key and select the same model you use successfully in Open WebUI.
3. Choose model-default tools or select your discovered server tools explicitly. Authorize MCP OAuth in the browser if needed.
4. Select Open WebUI Agent in an Assist pipeline. Turn off **Prefer handling commands locally** so HA cannot satisfy the test before calling Open WebUI.
5. Enable integration debug logs and open the Open WebUI chat list. Do not publish unsanitized server logs.
6. If keeping upstream installed, verify its `openwebui_conversation` entry remains separate from the new `openwebui_agent` entry. Confirm HACS installs each repository into its own folder and Assist uses the intended entity.

## A. Basic completion

Say: **Hello, what can you do?**

Expected: a new chat titled Home Assistant, a completed assistant message, and matching final speech in Assist. It must not speak task metadata, sources JSON or reasoning. The probe in development.md can test the same API without HA.

## B. Primary home-control criterion

1. In Open WebUI's browser, confirm **Turn on the office light.** successfully calls your existing `ha_control_device` tool and operates the intended device. This name is an example acceptance fixture, not recognized by integration code.
2. Put the device back into the test's starting state.
3. In the selected HA Assist pipeline, say **Turn on the office light.**
4. Inspect the newly saved Open WebUI chat and server/tool logs: the model chooses the function, Open WebUI runs it, the result returns to the model, and a final assistant message is persisted.
5. Confirm the real device state and that Assist speaks only the final answer, for example **The office light is on.**

A natural-language claim alone is not proof the action ran. Any raw tool-call output or failure to perform the action fails acceptance. The integration intentionally reports unexecuted tool protocols as an error rather than executing them.

## C. Native Web Search

1. Configure and test Open WebUI's search provider and the API user's/model's search capability.
2. Enable Web Search in the integration.
3. Ask **Search the web for today's weather in Athens and summarize it.** Then test a natural current-information question without the words “search the web”.
4. Inspect the saved chat for an actual search tool execution and current sources. Compare Assist's final summary with the persisted answer; it should not contain raw search events/source JSON.

Model choice determines whether to search. A plausible answer without a tool trace does not pass a search-execution test.

## D. Open WebUI Memory

1. Enable Memory globally, for the user/model, and in the integration.
2. Ask **Remember that my preferred desk-light brightness is 40 percent.**
3. Confirm a memory-write tool execution and inspect the user's Memory UI in Open WebUI. Do not accept conversational acknowledgment alone.
4. Start a completely new Assist conversation and ask **What desk-light brightness do I prefer?**
5. Confirm memory retrieval, rather than same-chat context, supplies the answer.
6. Remove the test memory in Open WebUI when finished.

## E. Multi-turn and isolation

1. In one Assist conversation, say **My test word for this conversation is marigold. Do not save it to memory.**
2. Continue that same conversation: **What was my test word?**
3. Verify one Open WebUI chat/session, two user nodes, two assistant nodes and correct parent/child links.
4. Start a separate conversation; verify a different chat and session. Disable Memory for this test if needed to avoid cross-conversation recall by Open WebUI.
5. Delete the first saved chat, then continue its HA conversation. Confirm a new chat is created once, without a loop.
6. Reload the integration or restart HA and continue: the integration creates a fresh chat; it does not promise restart continuity.

## F. Multiple tool rounds

Use a prompt your model can satisfy only by inspecting state and then acting based on that result. Confirm multiple sequential Open WebUI tool calls, a final assistant answer after the last result, and only that final answer in Assist.

## G. Optional features

- Code Interpreter: choose a server engine such as Jupyter, enable the flag, request a calculation that requires code, and inspect its tool trace. Browser Pyodide is unsupported.
- Terminal: select a discovered server/default, request a harmless read-only command in its permitted workspace, and verify execution in Open WebUI. Check chat-scoped terminal isolation if configured.
- Image Generation: enable it and request a test image. Inspect the image in Open WebUI; HA's conversation adapter only returns text, or an error if no text exists.

## H. Failures and cleanup

Test invalid key, permission denial, unreachable server, missing model, removed tool/default terminal and a deliberately short timeout. Each should give a useful error without leaking credentials. Cancel/unload during a slow run and ensure local polling stops. An accepted server task can keep running: inspect it before retrying any action. Delete test chats manually if desired; integration uninstall never deletes remote history.

## I. Latency and prompt-cache checks

1. Compare the same short, harmless request in the same Workspace model, with the same tools and features. Measure until the **complete** answer in both interfaces; browser streaming can appear quicker. Use typed Assist first to exclude speech recognition and playback.
2. Repeat within one conversation and compare a fresh conversation separately. Chat/session IDs remain stable within a mapped Assist conversation. Reloading the integration starts a new mapping.
3. Download integration diagnostics after a successful turn. Compare the phase durations in `last_turn_timing_seconds`. Short-interval repeat turns should avoid discovery requests. Default polling can add roughly zero to two seconds plus HTTP latency; setting one second reduces that contribution but does not accelerate model execution.
4. If the provider logs slow prefill, compare prompt tokens, cached tokens and time to first token between the browser and Assist. Compare reasoning effort too: browser-local Chat Controls are not imported; shared parameters belong in Open WebUI's Workspace model. Large tool catalogues and long reasoning output can both add latency.
5. If cached tokens vary, inspect system prompts for changing date/time variables and filters or memory context that modify the prompt. The integration does not insert dates, new IDs or device-state dumps into model message text. Open WebUI builds the final provider request, so integration payloads alone cannot prove provider-prefix equality. Cache eviction or checkpoint policy can also affect reuse; a cache miss alone does not establish a client bug.

Timings and provider token/cache counts are enough for initial diagnosis; keep prompt contents and credentials private. Freezing current-time values or disabling memory changes behavior and is not necessary just to collect measurements.

References: [Open WebUI native agent flow](https://docs.openwebui.com/reference/server-side-tool-calling/), [Open WebUI dynamic-time caching discussion](https://github.com/open-webui/open-webui/issues/28527), [NInfer context-cache architecture](https://github.com/Neroued/ninfer/blob/master/docs/maintainer/resource-scheduling-and-context-cache.md).

## J. Voice context, cleanup and thinking

1. With local command handling disabled for this test pipeline, use the same satellite to request a light action, then immediately say "turn it back off". Verify one Open WebUI chat, linked history, and the correct device. Start a separate conversation/satellite and verify it does not inherit the first conversation.
2. Make an ambiguous request that causes the model to ask a question. Verify the satellite listens for the answer and the next request reuses the chat. Actual interpretation still depends on the selected model.
3. Leave Keep chat history off. After 15 minutes idle plus up to one cleanup interval, verify the new test chat disappears. A follow-up resets its deletion timer; a running task prevents cleanup. Restart HA while a deadline is pending and confirm deletion resumes. Older untracked chats must remain.
4. Enable Keep chat history before a pending deadline; confirm the chat remains. Disabling it again tracks only newly created chats, not old history.
5. Select Thinking: Disabled with NInfer. Verify its request log reports disabled thinking and no automatic `default->xhigh` reasoning selection. Compare repeated requests after warm-up; the first request after changing thinking mode may require a new prefill. Return to Model default to inherit server settings.
6. Say "let's talk" and confirm the satellite keeps listening after a statement without a question mark. Say "end conversation" and confirm it stops. A separate HA session must not inherit that voice mode. Test Always continue separately; stop commands and errors should still end automatic listening.
