import hashlib
import hmac
import socket

import httpx
import pytest

from aifanyi.callbacks import PinnedTransport, deliver, sign, validate_url
from aifanyi.db import Job, Record, Work, now
from aifanyi.errors import DomainError
from aifanyi.knowledge import resolve
from aifanyi.store import add_record, digest, event
from conftest import job_request, post


def publish_entry(c, content, kind="term", scope="enterprise", locked=False):
    entry = post(
        c,
        "/v1/knowledge/entries",
        {
            "project_id": "project_default",
            "kind": kind,
            "scope": scope,
            "source_language": "zh-CN",
            "target_language": "en",
            "content": content,
            "source_refs": ["business-manual:test"],
            "locked": locked,
        },
    ).json()
    for action in ["submit", "approve"]:
        response = post(
            c,
            f"/v1/knowledge/proposals/{entry['id']}/{action}",
            {"expected_version": entry["version"], "reason": "业务审核"},
        )
        assert response.status_code == 200, response.text
        entry = response.json()
    r = post(
        c,
        "/v1/knowledge/releases",
        {"project_id": "project_default", "entry_version_refs": [entry["id"]], "reason": "发布审核版本"},
    )
    assert r.status_code == 201, r.text
    return entry, r.json()


def test_locked_enterprise_term_overrides_personal_and_tm_is_still_reviewed(env):
    c, runner, g, db, _ = env
    term = {"source": "待机", "target": "standby", "concept_id": "standby", "definition": "设备等待运行"}
    published, _ = publish_entry(c, term, locked=True)
    publish_entry(c, {**term, "target": "idle"}, scope="personal")
    source = "设备处于待机状态。"
    publish_entry(
        c,
        {"source": source, "target": "The device is in standby mode.", "context_fingerprint": digest("")},
        kind="tm",
    )
    job = post(c, "/v1/translation-jobs", job_request(source)).json()
    runner.drain()
    assert c.get(job["status_url"]).json()["status"] == "succeeded"
    assert g.calls == ["reviewer"]
    seg = c.get(job["status_url"] + "/segments").json()["items"][0]
    assert seg["knowledge"]["terms"][0]["id"] == published["id"]


def test_knowledge_rollback_creates_new_release_without_changing_old_snapshot(env):
    c, runner, g, db, _ = env
    entry, release = publish_entry(
        c, {"source": "待机", "target": "standby", "concept_id": "standby", "definition": "等待运行"}
    )
    job = post(c, "/v1/translation-jobs", job_request()).json()
    with db.session() as s:
        frozen = s.get(Record, s.get(Job, job["job_id"]).snapshot_id).data
    rollback = post(c, f"/v1/knowledge/releases/{release['id']}/rollback", {"reason": "恢复已审核版本"})
    assert rollback.status_code == 201, rollback.text
    assert rollback.json()["id"] != release["id"]
    runner.drain()
    with db.session() as s:
        assert s.get(Record, s.get(Job, job["job_id"]).snapshot_id).data == frozen
        assert s.get(Record, entry["id"]).data["status"] == "retired"


def test_same_priority_term_conflict_does_not_guess():
    base = {
        "kind": "term",
        "scope": "project",
        "source_language": "zh-CN",
        "content": {"source": "控制", "target": "control"},
    }
    entries = [{"id": "a", **base}, {"id": "b", **base, "content": {"source": "控制", "target": "govern"}}]
    with pytest.raises(DomainError) as error:
        resolve(entries, "控制设备", "zh-CN", "")
    assert error.value.code == "KNOWLEDGE_CONFLICT"


def test_callback_signature_binds_exact_bytes():
    raw = '{"text":"中文","event_id":"e1"}'
    expected = hmac.new(b"secret", ("123.e1." + raw).encode(), hashlib.sha256).hexdigest()
    assert sign("secret", "123", "e1", raw) == expected
    assert sign("secret", "123", "e1", raw + " ") != expected


def test_callback_pins_approved_address_and_retains_hostname_tls(env, monkeypatch):
    settings = env[4].model_copy(
        update={"callback_allowed_hosts": ["callback.example"], "callback_allowed_cidrs": ["10.40.0.0/16"]}
    )
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.40.1.9", 443))],
    )
    observed = {}

    def base(self, request):
        observed.update(
            host=request.url.host, host_header=request.headers["Host"], sni=request.extensions["sni_hostname"]
        )
        return httpx.Response(204)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", base)
    with httpx.Client(transport=PinnedTransport(settings), trust_env=False) as client:
        assert client.post("https://callback.example/event", content=b"{}").status_code == 204
    assert observed == {"host": "10.40.1.9", "host_header": "callback.example", "sni": "callback.example"}
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))],
    )
    with pytest.raises(DomainError):
        validate_url("https://callback.example/event", settings)


def test_disabled_callback_never_connects_or_changes_successful_job(env, monkeypatch):
    c, runner, g, db, settings = env
    job = post(c, "/v1/translation-jobs", job_request()).json()
    runner.drain()
    with db.session() as s:
        j = s.get(Job, job["job_id"])
        system = add_record(s, "client", j.tenant, j.project, j.owner, {"enabled": True})
        ep = add_record(
            s,
            "endpoint",
            j.tenant,
            j.project,
            j.owner,
            {"enabled": False, "client_id": system.id, "expires": now() + 3600},
        )
        e = event(s, j, "translation.succeeded", {"job_id": j.id})
        work = Work(id="callback-disabled", kind="callback", target=e.id, data={"endpoint_id": ep.id})
        s.add(work)
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda *a, **k: pytest.fail("Disabled callback must not resolve DNS")
    )
    with pytest.raises(DomainError) as error:
        deliver(db, settings, work)
    assert error.value.code == "CALLBACK_DISABLED"
    runner.drain()
    assert c.get(job["status_url"]).json()["status"] == "succeeded"
    with db.session() as s:
        assert s.get(Work, work.id).status == "dead"


def test_quality_report_cannot_publish_unproven_profile(env):
    c, _, _, db, _ = env
    report = post(
        c, "/v1/evaluation-reports", {"project_id": "project_default", "report": {"human_reviewed": True}}
    ).json()
    with db.session() as s:
        version = s.get(Record, "profile_default").version
    response = post(
        c,
        "/v1/model-profiles/profile_default/publish",
        {
            "expected_version": version,
            "evaluation_report_id": report["id"],
            "max_major_segment_rate": 0.01,
            "min_auto_coverage": 0.8,
            "min_reviewer_recall": 0.9,
            "max_reviewer_false_positive_rate": 0.1,
            "accepted_language_pairs": [["zh-CN", "en"]],
            "risk_scope": ["general"],
        },
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "EVALUATION_INCOMPLETE"
