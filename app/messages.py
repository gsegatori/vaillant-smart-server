"""Internationalization and message catalogs for English and Italian.

Provides global language state and translation helper for strings that were
originally in Italian (quota error detail and server logs).
"""
from __future__ import annotations

from typing import Any

SUPPORTED_LANGUAGES = ("en", "it")
DEFAULT_LANGUAGE = "en"

MESSAGES: dict[str, dict[str, str]] = {
    "en": {
        # HTTPException detail message (originally in Italian)
        "quota_exceeded": "Vaillant API quota exceeded, replenish in {time}",
        # Server & operational logs (originally in Italian)
        "log_env_credentials_missing": "VAILLANT_USER or VAILLANT_PASSWORD not set in .env",
        "log_quota_window_expired": "Quota window expired (was {time}), AUTO-ENABLE upstream",
        "log_quota_lockout_unparseable": "Quota lockout without parseable replenish_in ({replenish}); upstream left unchanged",
        "log_quota_lockout_auto_disable": "Auto-DISABLE upstream due to Vaillant quota (Vaillant says {replenish}, resuming at {resume} UTC with +{buffer}s buffer){extra}",
        "log_was_already_off": " (was already OFF)",
        "log_upstream_enabled_manual": "upstream ENABLED (manual override of quota auto-resume)",
        "log_upstream_disabled": "upstream DISABLED (cache-only mode, auto-resume cancelled)",
    },
    "it": {
        # HTTPException detail message
        "quota_exceeded": "Quota API Vaillant esaurita, replenish in {time}",
        # Server & operational logs
        "log_env_credentials_missing": "VAILLANT_USER o VAILLANT_PASSWORD non impostati nel .env",
        "log_quota_window_expired": "Quota window scaduta (era {time}), AUTO-ENABLE upstream",
        "log_quota_lockout_unparseable": "Quota lockout senza replenish_in parsabile ({replenish}); upstream lasciato com'e'",
        "log_quota_lockout_auto_disable": "Auto-DISABLE upstream per quota Vaillant (Vaillant dice {replenish}, riprendo alle {resume} UTC con +{buffer}s buffer){extra}",
        "log_was_already_off": " (era gia' OFF)",
        "log_upstream_enabled_manual": "upstream ENABLED (manual override del quota auto-resume)",
        "log_upstream_disabled": "upstream DISABLED (cache-only mode, auto-resume cancellato)",
    },
}

_current_language: str = DEFAULT_LANGUAGE
_cur_lang_msgs: dict[str, str] = MESSAGES[DEFAULT_LANGUAGE]


def set_current_language(lang: str) -> None:
    """Sets the server-wide active language and updates _cur_lang_msgs dictionary."""
    global _current_language, _cur_lang_msgs
    if lang and lang.lower() in SUPPORTED_LANGUAGES:
        _current_language = lang.lower()
        _cur_lang_msgs = MESSAGES[_current_language]


def get_current_language() -> str:
    """Returns the server-wide active language."""
    return _current_language


def get_message(key: str, **kwargs: Any) -> str:
    """Returns a translated message for `key` using the _cur_lang_msgs dictionary."""
    template = _cur_lang_msgs.get(key, MESSAGES[DEFAULT_LANGUAGE].get(key, key))
    if kwargs:
        return template.format(**kwargs)
    return template


_ = get_message
