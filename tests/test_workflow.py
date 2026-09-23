from sqlalchemy import select

from aifanyi.db import Job, Record, Work
from aifanyi.store import patch
from conftest import job_request, post


def test_translation_runs_full_review_and_survives_duplicate_dispatch(env):
    c, runner, gateway, db, _ = env
    created = post(c, "/v1/translation-jobs", job_request()).json()
    assert created["status"] == "queued"
    assert c.get(f"/v1/translation-jobs/{created['job_id']}/result").status_code == 409
    runner.drain()
    result = c.get(f"/v1/translation-jobs/{created['job_id']}/result").json()
    assert result["status"] == "succeeded", result
    assert result["result"]["text"] == gateway.translation
    assert gateway.calls == ["translator", "reviewer"]
    with db.session() as s:
        s.add(Work(id="duplicate", kind="job", target=created["job_id"]))
    runner.drain()
    assert gateway.calls == ["translator", "reviewer"]


def test_missing_review_coverage_never_publishes(env):
    c, runner, gateway, *_ = env
    gateway.reviews = [{"reviewed_segment_ids": [], "issues": []}]
    job = post(c, "/v1/translation-jobs", job_request()).json()
    runner.drain()
    status = c.get(job["status_url"]).json()
    assert status["status"] == "failed", status
    assert status["error"]["code"] == "REVIEW_INVALID"
    assert c.get(job["status_url"] + "/result").status_code == 409


def test_nonexistent_evidence_is_rejected(env):
    c, runner, gateway, *_ = env

    def bad(payload):
        id = payload["segments"][0]["segment_id"]
        return {
            "reviewed_segment_ids": [id],
            "issues": [
                {
                    "segment_id": id,
                    "type": "mistranslation",
                    "severity": "major",
                    "source_span": {"start": 0, "end": 2, "quote": "错误"},
                    "target_span": {"start": 0, "end": 3, "quote": "The"},
                    "explanation": "invalid evidence",
                }
            ],
        }

    gateway.reviews = [bad]
    job = post(c, "/v1/translation-jobs", job_request()).json()
    runner.drain()
    assert c.get(job["status_url"]).json()["status"] == "failed"


def test_qwen_outage_cannot_return_hymt_as_final(env):
    c, runner, gateway, *_ = env
    gateway.failure_role = "reviewer"
    job = post(c, "/v1/translation-jobs", job_request()).json()
    runner.drain()
    assert c.get(job["status_url"]).json()["status"] == "failed"
    assert c.get(job["status_url"] + "/result").status_code == 409


def test_hard_number_change_repair_limit_then_review(env):
    c, runner, gateway, *_ = env
    job = post(c, "/v1/translation-jobs", job_request("设备额定载重为 1000 kg。", domain="general")).json()
    runner.drain()
    state = c.get(job["status_url"]).json()
    assert state["status"] == "awaiting_review", state
    assert gateway.calls.count("repairer") == 2
    assert c.get(job["status_url"] + "/result").status_code == 409
    seg = c.get(job["status_url"] + "/segments").json()["items"][0]
    attempt = post(
        c,
        job["status_url"] + "/review-decisions",
        {
            "revision": 1,
            "expected_version": state["state_version"],
            "decisions": [
                {
                    "segment_id": seg["id"],
                    "segment_revision": seg["revision"],
                    "action": "approve_segment",
                    "reason": "accept",
                }
            ],
            "finalize": True,
        },
    )
    assert attempt.status_code == 409


def test_high_risk_needs_real_approval_and_revision(env):
    c, runner, gateway, *_ = env
    job = post(c, "/v1/translation-jobs", job_request(domain="maintenance")).json()
    runner.drain()
    state = c.get(job["status_url"]).json()
    assert state["status"] == "awaiting_review"
    seg = c.get(job["status_url"] + "/segments").json()["items"][0]
    r = post(
        c,
        job["status_url"] + "/review-decisions",
        {
            "revision": 1,
            "expected_version": state["state_version"],
            "decisions": [
                {
                    "segment_id": seg["id"],
                    "segment_revision": seg["revision"],
                    "action": "approve_segment",
                    "reason": "业务审核完成",
                }
            ],
            "finalize": True,
        },
    )
    assert r.status_code == 200, r.text
    runner.drain()
    assert c.get(job["status_url"] + "/result").json()["quality_status"] == "human_approved"


def test_snapshot_is_frozen_when_knowledge_changes(env):
    c, runner, gateway, db, _ = env
    job = post(c, "/v1/translation-jobs", job_request()).json()
    with db.session() as s:
        row = s.get(Job, job["job_id"])
        snapshot = s.get(Record, row.snapshot_id)
        original = snapshot.data
        patch(s.get(Record, "profile_default"), name="changed after dispatch")
    runner.drain()
    with db.session() as s:
        assert s.get(Record, row.snapshot_id).data == original


def test_cancel_prevents_delayed_work(env):
    c, runner, gateway, *_ = env
    job = post(c, "/v1/translation-jobs", job_request()).json()
    assert post(c, job["status_url"] + "/cancel", {"reason": "cancel"}).status_code == 202
    runner.drain()
    assert c.get(job["status_url"]).json()["status"] == "canceled"
    assert gateway.calls == []


def test_idempotency_and_sync_timeout_keep_one_job(env):
    c, _, _, db, _ = env
    body = {
        "project_id": "project_default",
        "source_language": "zh-CN",
        "target_language": "en",
        "text": "设备待机。",
    }
    first = post(c, "/v1/translations", body, "one")
    second = post(c, "/v1/translations", body, "one")
    assert first.status_code == second.status_code == 504
    assert first.json()["error"]["details"]["job_id"] == second.json()["error"]["details"]["job_id"]
    assert post(c, "/v1/translations", {**body, "text": "不同内容"}, "one").status_code == 409
    with db.session() as s:
        assert len(list(s.scalars(select(Job)))) == 1
