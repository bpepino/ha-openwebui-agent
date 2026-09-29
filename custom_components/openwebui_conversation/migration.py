"""Pure migration policy, separately testable from Home Assistant lifecycle."""

from .const import DEFAULT_OPTIONS


def migrate_options(data: dict, options: dict) -> dict:
    """Preserve legacy values and retain retired settings for inspection."""
    merged = {**DEFAULT_OPTIONS, **options}
    for key in ("timeout", "verify_ssl", "chat_model", "strip_markdown"):
        if key not in options and key in data:
            merged[key] = data[key]
    if "web_search" not in options:
        merged["web_search"] = options.get(
            "search_enabled", data.get("search_enabled", False)
        )
    # Upstream's omitted strip-markdown option meant False.
    if "strip_markdown" not in options and "strip_markdown" not in data:
        merged["strip_markdown"] = False
    if "timeout" not in options and "timeout" not in data:
        merged["timeout"] = 60
    return merged
