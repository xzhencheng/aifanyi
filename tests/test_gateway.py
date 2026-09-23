import json

import httpx
import pytest
from sqlalchemy import select

from aifanyi.db import Budget, ModelCall
from aifanyi.errors import DomainError
from aifanyi.models import Gateway, messages
from aifanyi.profiles import initial_roles
from aifanyi.schemas import Review


def gateway(env, monkeypatch, replies, token_count=10):
    _, _, _, db, settings = env
    monkeypatch.setattr("aifanyi.models.time.sleep", lambda _: None)
    sent = []

    def handle(request):
        if request.url.path == "/tokenize":
            return httpx.Response(200, json={"count": token_count})
        sent.append(json.loads(request.content))
        value = replies.pop(0)
        if isinstance(value, Exception):
            raise value
        if isinstance(value, int):
            return httpx.Response(value)
        content, finish = value
        return httpx.Response(
            200,
            json={
                "model": "qwen-test",
                "choices": [{"finish_reason": finish, "message": {"content": content}}],
            },
        )

    return Gateway(db, settings, httpx.MockTransport(handle)), initial_roles(settings)["reviewer"], sent


def test_invalid_schema_retry_is_bounded_across_resume(env, monkeypatch):
    g, config, sent = gateway(env, monkeypatch, [("not-json", "stop"), ("{}", "stop")])
    for _ in range(2):
        with pytest.raises(DomainError) as error:
            g.generate(config, "reviewer", messages("review", {}, Review), ["one"], ["segment"], Review)
        assert error.value.code == "MODEL_OUTPUT_INVALID"
    assert len(sent) == 2
    with env[3].session() as s:
        assert s.get(Budget, "segment").used == 2


def test_transient_retries_record_uncertain_outcomes_and_do_not_exceed_two(env, monkeypatch):
    g, config, sent = gateway(env, monkeypatch, [httpx.ReadTimeout("timeout"), 503, 429])
    for _ in range(2):
        with pytest.raises(DomainError):
            g.generate(config, "reviewer", messages("review", {}), ["outage"], ["segment"])
    assert len(sent) == 3
    with env[3].session() as s:
        assert {r.status for r in s.scalars(select(ModelCall))} == {"outcome_unknown"}


@pytest.mark.parametrize("reply", [("partial", "length"), ("<think>not final</think>", "stop"), ("", "stop")])
def test_truncated_reasoning_and_empty_output_are_not_results(env, monkeypatch, reply):
    g, config, sent = gateway(env, monkeypatch, [reply, reply])
    with pytest.raises(DomainError) as error:
        g.generate(config, "reviewer", messages("review", {}), ["bad"], ["segment"])
    assert error.value.code == "MODEL_OUTPUT_INVALID"
    assert len(sent) == 2


def test_cached_response_and_segment_budget_are_persistent(env, monkeypatch):
    g, config, sent = gateway(env, monkeypatch, [("valid", "stop")])
    args = (config, "reviewer", messages("review", {}), ["cached"], ["segment"])
    assert g.generate(*args) == g.generate(*args) == "valid"
    assert len(sent) == 1
    with env[3].session() as s:
        s.get(Budget, "segment").used = 12
    with pytest.raises(DomainError) as error:
        g.generate(config, "reviewer", messages("review", {}), ["new"], ["segment"])
    assert error.value.code == "MODEL_BUDGET_EXHAUSTED"
    assert len(sent) == 1


def test_actual_token_budget_blocks_generation(env, monkeypatch):
    g, config, sent = gateway(env, monkeypatch, [], token_count=16000)
    with pytest.raises(DomainError) as error:
        g.generate(config, "reviewer", messages("review", {}), ["oversize"], ["segment"])
    assert error.value.code == "MODEL_INPUT_TOO_LARGE"
    assert not sent


def test_hymt_plain_output_and_qwen_json_are_different_protocols(env, monkeypatch):
    g, config, sent = gateway(
        env, monkeypatch, [("translation", "stop"), ('{"reviewed_segment_ids":[],"issues":[]}', "stop")]
    )
    g.generate(
        initial_roles(env[4])["translator"],
        "translator",
        [{"role": "user", "content": "翻译"}],
        ["hy"],
        ["s"],
    )
    g.generate(config, "reviewer", messages("review", {}, Review), ["qwen"], ["s"], Review)
    assert "response_format" not in sent[0]
    assert sent[1]["response_format"] == {"type": "json_object"}
