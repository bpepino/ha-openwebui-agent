# Manual acceptance tests

These tests require your own configured Home Assistant and Open WebUI instance. They have **not** been run against a live server during this implementation. Save the integration, HA, Open WebUI, model/provider versions and results. Compare with the same Open WebUI API-key user in the browser.

## Setup

1. Install this component locally and restart HA 2026.6.0+.
2. Configure an API key and select the same model you use successfully in Open WebUI.
3. Choose model-default tools or select your discovered server tools explicitly. Authorize MCP OAuth in the browser if needed.
4. Select Open WebUI Agent in an Assist pipeline. Turn off **Prefer handling commands locally** so HA cannot satisfy the test before calling Open WebUI.
5. Enable integration debug logs and open the Open WebUI chat list. Do not publish unsanitized server logs.

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
6. Reload the integration or restart HA and continue: this beta creates a fresh chat; it does not promise restart continuity.

## F. Multiple tool rounds

Use a prompt your model can satisfy only by inspecting state and then acting based on that result. Confirm multiple sequential Open WebUI tool calls, a final assistant answer after the last result, and only that final answer in Assist.

## G. Optional features

- Code Interpreter: choose a server engine such as Jupyter, enable the flag, request a calculation that requires code, and inspect its tool trace. Browser Pyodide is unsupported.
- Terminal: select a discovered server/default, request a harmless read-only command in its permitted workspace, and verify execution in Open WebUI. Check chat-scoped terminal isolation if configured.
- Image Generation: enable it and request a test image. Inspect the image in Open WebUI; HA's conversation adapter only returns text, or an error if no text exists.

## H. Failures and cleanup

Test invalid key, permission denial, unreachable server, missing model, removed tool/default terminal and a deliberately short timeout. Each should give a useful error without leaking credentials. Cancel/unload during a slow run and ensure local polling stops. An accepted server task can keep running: inspect it before retrying any action. Delete test chats manually if desired; integration uninstall never deletes remote history.
