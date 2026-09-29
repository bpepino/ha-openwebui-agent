# Security

Open WebUI API keys are secrets and act with the permissions of their user. Use a dedicated least-privilege user where practical. Model-selected tools and terminals may change devices, data and other real-world resources; configure authorization on Open WebUI and its tool servers.

Avoid unnecessary public exposure of Open WebUI. Use HTTPS over untrusted networks and leave certificate verification enabled. Restrict API endpoint access only after allowing the native chat/task and discovery routes used by the integration.

Do not post API keys, tokens, Authorization headers, private chat/memory contents or tool arguments in issues. Revoke exposed keys. Diagnostics intentionally omit connection URLs, identifiers and conversation contents, but review logs before sharing.

For vulnerabilities, use GitHub's **Report a vulnerability** feature if enabled on this repository. If unavailable, ask the maintainer to enable private vulnerability reporting without disclosing exploit details publicly. No private contact address has been established here; none is invented.

This prerelease has no security support SLA. Upgrade decisions should follow tested compatibility and published security fixes. Cancellation or timeout of an Assist request does not guarantee a previously accepted Open WebUI action has stopped.
