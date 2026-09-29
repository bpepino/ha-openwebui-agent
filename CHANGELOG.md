# Changelog

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
