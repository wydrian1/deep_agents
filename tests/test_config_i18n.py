import json
from importlib import resources

import pytest
from pydantic import ValidationError

from deep_agents_app import i18n
from deep_agents_app.config import Settings


def settings(**values) -> Settings:
    return Settings(_env_file=None, database_url="postgresql://u:p@db.internal:5432/x", **values)  # type: ignore[arg-type]


def test_urls_are_converted_for_each_driver():
    s = settings()
    assert s.sqlalchemy_url.startswith("postgresql+psycopg://u:p@db.internal")
    assert s.psycopg_url.startswith("postgresql://u:p@db.internal")


@pytest.mark.parametrize("bad", ["mysql://x/y", "not a url"])
def test_database_url_must_be_postgres(bad):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, database_url=bad)  # type: ignore[arg-type]


def test_schema_name_is_restricted():
    with pytest.raises(ValidationError):
        settings(database_schema='x"; DROP SCHEMA public; --')


def test_gateway_and_database_hosts_are_always_blocked():
    s = settings(llm_gateway_url="http://litellm.internal:4000/v1", web_blocked_hosts="Corp.Example, intranet")
    assert {"litellm.internal", "db.internal", "corp.example", "intranet"} <= set(s.blocked_host_suffixes)


def test_auth_is_off_by_default_and_needs_full_config_when_on():
    assert settings().auth_enabled is False
    with pytest.raises(ValidationError, match="OAUTH_CLIENT_ID"):
        settings(auth_enabled=True)
    with pytest.raises(ValidationError, match="SESSION_SECRET"):
        settings(
            auth_enabled=True, oauth_authorize_url="https://i/a", oauth_token_url="https://i/t",
            oauth_userinfo_url="https://i/u", oauth_client_id="c", oauth_client_secret="s",
            oauth_redirect_url="https://app/auth/callback", oauth_allowed_emails="a@b.c", session_secret="short",
        )


def test_llm_params_must_be_a_json_object():
    assert settings(llm_params_json='{"parallel_tool_calls": false}').llm_params == {"parallel_tool_calls": False}
    with pytest.raises(ValidationError):
        settings(llm_params_json="[1]")


def test_languages_have_prompt_and_messages():
    assert {"en", "hu"} <= set(i18n.available_languages())
    assert i18n.resolve_language("xx", "hu") == "hu"
    assert i18n.resolve_language("hu", "en") == "hu"
    assert "Hungarian" not in i18n.system_prompt("en") and i18n.system_prompt("hu")


def test_every_language_has_every_english_message_key():
    root = resources.files("deep_agents_app").joinpath("messages")
    english = set(json.loads(root.joinpath("en.json").read_text(encoding="utf-8")))
    for language in i18n.available_languages():
        assert english == set(json.loads(root.joinpath(f"{language}.json").read_text(encoding="utf-8"))), language


def test_message_formats_and_falls_back_to_english():
    assert "3" in i18n.message("busy", "hu", limit=3)
    assert i18n.message("does_not_exist") == "does_not_exist"
