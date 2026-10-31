"""Ollama protocol integration tests use explicit local test doubles; no key."""

import time
from types import SimpleNamespace

import pytest

from metric_detective.provider import OllamaProvider, ProviderError, ToolDecision


def decision():
    return ToolDecision(
        tool="decompose_revenue_change", reason_code="ticket_change", evidence_ids=["ev_test"]
    )


def context():
    return {"evidence": [], "allowed_actions": [decision().model_dump(mode="json")]}


def test_real_integration_requests_schema_and_measures_usage(monkeypatch):
    captured = {}

    class ChatDouble:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def with_structured_output(self, schema, **kwargs):
            assert schema["properties"]["action_id"]["enum"] == ["a0"]
            assert schema["additionalProperties"] is False
            assert kwargs == {"method": "json_schema", "include_raw": True}
            return self

        def invoke(self, messages):
            assert messages[0][0] == "system"
            return {
                "parsed": {"action_id": "a0"},
                "parsing_error": None,
                "raw": SimpleNamespace(usage_metadata={"input_tokens": 111, "output_tokens": 22}),
            }

    monkeypatch.setattr("langchain_ollama.ChatOllama", ChatDouble)
    provider = OllamaProvider(model="test-model", api_key="test-secret")
    result = provider.select(context(), remaining_tokens=20000, timeout=2, max_retries=0)
    assert result.input_tokens == 111
    assert result.output_tokens == 22
    assert result.calls == 1
    assert result.decision == decision()
    assert 0 < result.prompt_bytes < 1600
    assert captured["num_predict"] == 64
    assert captured["reasoning"] is False
    assert captured["client_kwargs"]["headers"]["Authorization"] == "Bearer test-secret"
    assert "test-secret" not in str(result)


def test_budget_admission_prevents_network_call():
    with pytest.raises(ProviderError, match="token_budget_exhausted") as exc:
        OllamaProvider().select(context(), remaining_tokens=256, timeout=2, max_retries=0)
    assert exc.value.calls == 0


def test_unimplemented_provider_configuration_is_explicitly_rejected(monkeypatch):
    monkeypatch.setenv("MD_LLM_PROVIDER", "openai")
    with pytest.raises(ProviderError, match="unsupported_provider"):
        OllamaProvider()


def test_retries_rebuild_clients_with_remaining_deadline_and_no_secret_log(monkeypatch):
    timeouts = []

    class ChatDouble:
        def __init__(self, **kwargs):
            timeouts.append(kwargs["client_kwargs"]["timeout"])

        def with_structured_output(self, *args, **kwargs):
            return self

        def invoke(self, *args):
            time.sleep(0.01)
            raise RuntimeError("https://private.example/secret-key=test-secret")

    monkeypatch.setattr("langchain_ollama.ChatOllama", ChatDouble)
    with pytest.raises(ProviderError) as exc:
        OllamaProvider().select(context(), remaining_tokens=20000, timeout=1, max_retries=1)
    assert exc.value.calls == 2
    assert timeouts[1] < timeouts[0]
    assert "test-secret" not in str(exc.value)
    assert "private.example" not in str(exc.value)


def test_invalid_schema_retries_are_bounded(monkeypatch):
    class ChatDouble:
        def __init__(self, **kwargs):
            pass

        def with_structured_output(self, *args, **kwargs):
            return self

        def invoke(self, *args):
            return {
                "parsed": None,
                "parsing_error": ValueError("bad schema"),
                "raw": SimpleNamespace(usage_metadata={"input_tokens": 10, "output_tokens": 5}),
            }

    monkeypatch.setattr("langchain_ollama.ChatOllama", ChatDouble)
    with pytest.raises(ProviderError) as exc:
        OllamaProvider().select(context(), remaining_tokens=20000, timeout=2, max_retries=2)
    assert exc.value.calls == 3
    assert exc.value.input_tokens == 30
    assert exc.value.output_tokens == 15


def test_missing_usage_is_not_reported_as_measured_zero(monkeypatch):
    class ChatDouble:
        def __init__(self, **kwargs):
            pass

        def with_structured_output(self, *args, **kwargs):
            return self

        def invoke(self, *args):
            return {
                "parsed": {"action_id": "a0"},
                "parsing_error": None,
                "raw": SimpleNamespace(usage_metadata=None, response_metadata={}),
            }

    monkeypatch.setattr("langchain_ollama.ChatOllama", ChatDouble)
    with pytest.raises(ProviderError, match="provider_usage_missing") as exc:
        OllamaProvider().select(context(), remaining_tokens=20000, timeout=2, max_retries=0)
    assert exc.value.calls == 1


def test_model_action_choice_selects_tool_and_exact_parameters(monkeypatch):
    long_label = "categoría-" * 22
    captured_messages = []

    class ChatDouble:
        def __init__(self, **kwargs):
            pass

        def with_structured_output(self, schema, **kwargs):
            assert schema["properties"]["action_id"]["enum"] == ["a0", "a1"]
            return self

        def invoke(self, messages):
            captured_messages.extend(messages)
            return {
                "parsed": {"action_id": "a1"},
                "parsing_error": None,
                "raw": SimpleNamespace(usage_metadata={"input_tokens": 100, "output_tokens": 9}),
            }

    monkeypatch.setattr("langchain_ollama.ChatOllama", ChatDouble)
    packet = context()
    packet["allowed_actions"].append(
        {
            "tool": "inspect_daily_pattern",
            "filters": {"channel": long_label},
            "reason_code": "daily_pattern",
            "evidence_ids": ["ev_segment"],
        }
    )
    result = OllamaProvider().select(packet, remaining_tokens=12000, timeout=2, max_retries=0)
    assert result.decision.tool == "inspect_daily_pattern"
    assert result.decision.filters == {"channel": long_label}
    assert result.decision.evidence_ids == ["ev_segment"]
    assert long_label not in captured_messages[1][1]
    assert "…#" in captured_messages[1][1]


@pytest.mark.parametrize(
    "parsed", [{"action_id": "unknown"}, {"action_id": "a0", "sql": "DROP TABLE orders"}]
)
def test_invalid_model_action_never_executes_or_uses_a_fallback(monkeypatch, parsed):
    class ChatDouble:
        def __init__(self, **kwargs):
            pass

        def with_structured_output(self, *args, **kwargs):
            return self

        def invoke(self, *args):
            return {
                "parsed": parsed,
                "parsing_error": None,
                "raw": SimpleNamespace(usage_metadata={"input_tokens": 100, "output_tokens": 9}),
            }

    monkeypatch.setattr("langchain_ollama.ChatOllama", ChatDouble)
    with pytest.raises(ProviderError, match="provider_invalid_structured_output") as exc:
        OllamaProvider().select(context(), remaining_tokens=12000, timeout=2, max_retries=0)
    assert exc.value.calls == 1


@pytest.mark.parametrize(
    "url", ["file:///private", "http://user:secret@example.test", "ftp://example.test"]
)
def test_no_credentials_in_provider_url(url):
    with pytest.raises(ProviderError, match="invalid_provider_url"):
        OllamaProvider(base_url=url)
