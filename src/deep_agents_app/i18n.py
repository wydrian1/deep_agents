"""Language packs: system prompts and user-facing messages live in data files, not code.

To add a language, add ``prompts/<code>.md`` and ``messages/<code>.json``.
Missing message keys fall back to English.
"""

from __future__ import annotations

import json
from functools import lru_cache
from importlib import resources

DEFAULT_LANGUAGE = "en"


def available_languages() -> list[str]:
    prompts = {p.name.removesuffix(".md") for p in resources.files(__package__).joinpath("prompts").iterdir() if p.name.endswith(".md")}
    messages = {p.name.removesuffix(".json") for p in resources.files(__package__).joinpath("messages").iterdir() if p.name.endswith(".json")}
    return sorted(prompts & messages)


def resolve_language(code: str | None, fallback: str = DEFAULT_LANGUAGE) -> str:
    languages = available_languages()
    if code and code in languages:
        return code
    return fallback if fallback in languages else DEFAULT_LANGUAGE


@lru_cache
def _messages(language: str) -> dict[str, str]:
    text = resources.files(__package__).joinpath("messages", f"{language}.json").read_text(encoding="utf-8")
    return json.loads(text)


def message(key: str, language: str = DEFAULT_LANGUAGE, **values: object) -> str:
    template = _messages(language).get(key) or _messages(DEFAULT_LANGUAGE).get(key) or key
    return template.format(**values) if values else template


@lru_cache
def system_prompt(language: str) -> str:
    return resources.files(__package__).joinpath("prompts", f"{language}.md").read_text(encoding="utf-8").strip()
