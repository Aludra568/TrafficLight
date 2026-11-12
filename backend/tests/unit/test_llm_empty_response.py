"""Пустой HTTP 200 от провайдера повторяется ограниченно, отказ — не повторяется."""

import json
import time

import httpx
import openai
import pytest

from svetofor.llm.backend import OpenAIBackend
from svetofor.llm.errors import LLMRequestRejectedError, LLMUnavailableError
from tests.support.fake_llm import completion
from tests.unit.test_llm_backend import REQUEST


@pytest.mark.parametrize("empty_count", [1, 2])
def test_empty_choices_recover_without_changing_request(monkeypatch, empty_count):
    requests = []
    waits = []
    monkeypatch.setattr(time, "sleep", waits.append)

    def respond(request):
        requests.append(request.content)
        body = completion('{"ok": true}')
        if len(requests) <= empty_count:
            body["choices"] = []
        return httpx.Response(200, json=body)

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        client = openai.OpenAI(api_key="test", http_client=http)
        adapter = OpenAIBackend(
            base_url="http://unused",
            api_key="test",
            timeout_s=5,
            supports_json_schema=True,
            client=client,
        )
        reply = adapter.complete(REQUEST)
        assert reply.content == '{"ok": true}'
        assert reply.tokens_in == 11 * (empty_count + 1)
        assert reply.tokens_out == 7 * (empty_count + 1)
    assert len(requests) == empty_count + 1
    assert all(request == requests[0] for request in requests)
    assert waits == [1.0, 2.0][:empty_count]


@pytest.mark.parametrize("refusal", [False, True])
def test_empty_choices_are_bounded_and_refusals_are_not_retried(monkeypatch, refusal):
    requests = []
    monkeypatch.setattr(time, "sleep", lambda _: None)

    def respond(request):
        requests.append(request)
        body = completion("", refusal="Cannot answer" if refusal else None)
        if not refusal:
            body["choices"] = []
        return httpx.Response(200, json=body)

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        client = openai.OpenAI(api_key="test", http_client=http)
        adapter = OpenAIBackend(
            base_url="http://unused",
            api_key="test",
            timeout_s=5,
            supports_json_schema=True,
            client=client,
        )
        expected = LLMRequestRejectedError if refusal else LLMUnavailableError
        with pytest.raises(expected):
            adapter.complete(REQUEST)
    assert len(requests) == (1 if refusal else 3)


@pytest.mark.parametrize("reserve", [0, 4000])
def test_empty_choices_with_exhausted_reasoning_use_length_recovery(reserve):
    limits = []

    def respond(request):
        limits.append(json.loads(request.content)["max_tokens"])
        body = completion('{"ok": true}')
        if len(limits) == 1:
            body["choices"] = []
            body["usage"] = {
                "prompt_tokens": 11,
                "completion_tokens": 100,
                "total_tokens": 111,
                "completion_tokens_details": {"reasoning_tokens": 100},
            }
        return httpx.Response(200, json=body)

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        adapter = OpenAIBackend(
            base_url="http://unused",
            api_key="test",
            timeout_s=5,
            supports_json_schema=True,
            reasoning_tokens=reserve,
            client=openai.OpenAI(api_key="test", http_client=http),
        )
        reply = adapter.complete(REQUEST)
    assert limits == ([100, 4100] if reserve else [100])
    assert reply.finish_reason == ("stop" if reserve else "length")
    assert reply.tokens_out == (107 if reserve else 100)
