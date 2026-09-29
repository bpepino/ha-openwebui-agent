"""Upstream options are preserved rather than silently discarded."""

from owui_protocol.migration import migrate_options


def test_legacy_migration():
    """Retain model, transport, voice settings and inactive legacy phrases."""
    options = {
        "chat_model": "custom-model",
        "timeout": 90,
        "verify_ssl": False,
        "strip_markdown": True,
        "search_enabled": True,
        "search_sentences": "search {query}",
        "search_result_prefix": "Found:",
    }
    result = migrate_options(
        {"api_key": "private", "base_url": "https://host"}, options
    )
    assert all(result[k] == v for k, v in options.items())
    assert result["web_search"] and result["memory"]
    assert "api_key" not in result


def test_old_defaults_and_data_values():
    """Do not enable formerly disabled search or Markdown removal on upgrade."""
    result = migrate_options({"timeout": 45, "verify_ssl": False}, {})
    assert result["timeout"] == 45 and result["verify_ssl"] is False
    assert result["web_search"] is False and result["strip_markdown"] is False
    assert migrate_options({}, {})["timeout"] == 60
