import pytest

from deep_agents_app.config import Settings
from deep_agents_app.llm import gateway
from deep_agents_app.runs.errors import classify_error


def settings(**values) -> Settings:
    return Settings(_env_file=None, database_url="postgresql://u:p@db/x", **values)  # type: ignore[arg-type]


def test_model_targets_the_litellm_gateway():
    model = gateway.build_model(
        settings(llm_gateway_url="http://litellm.internal:4000/v1", llm_api_key="sk-secret",
                 llm_params_json='{"parallel_tool_calls": false}', llm_max_retries=1),
        "claude-alias", "run-1",
    )
    assert model.model_name == "claude-alias"
    assert str(model.openai_api_base).startswith("http://litellm.internal:4000/v1")
    assert model.max_retries == 1
    assert model.extra_body == {"metadata": {"run_id": "run-1"}}
    assert model.model_kwargs == {"parallel_tool_calls": False}
    assert "sk-secret" not in repr(model)


def test_missing_gateway_or_model_is_an_explicit_error():
    with pytest.raises(gateway.GatewayNotConfigured):
        gateway.build_model(settings(), "m", "r")
    with pytest.raises(gateway.ModelNotConfigured):
        gateway.resolve_model_name(settings(), None)
    assert gateway.resolve_model_name(settings(default_model="d"), None) == "d"
    assert gateway.resolve_model_name(settings(default_model="d"), " other ") == "other"


class _Status(Exception):
    def __init__(self, code):
        self.status_code = code


@pytest.mark.parametrize(
    ("error", "key"),
    [(_Status(429), "error_rate_limited"), (_Status(401), "error_gateway_auth"), (_Status(400), "error_gateway_request"),
     (_Status(502), "error_gateway_unavailable"), (TimeoutError(), "error_gateway_unavailable"), (ValueError("x"), "run_failed")],
)
def test_errors_map_to_message_keys(error, key):
    assert classify_error(error) == key
