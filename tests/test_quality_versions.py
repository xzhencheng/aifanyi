import pytest

from aifanyi.db import Job, Record
from aifanyi.quality import hard_checks, split_text
from conftest import job_request, post


def test_units_stay_attached_to_their_numeric_values():
    assert hard_checks("s", "电压 220 V，频率 50 Hz。", "Voltage 220 Hz, frequency 50 V.", {})
    assert not hard_checks("s", "载重 1000 千克。", "Capacity 1000 kg.", {})


def test_split_preserves_unicode_offsets_and_does_not_split_a_long_token():
    text = "😀设备。\n" * 300 + "${very_long_variable}"
    pieces = list(split_text(text))
    assert "".join(raw for _, _, raw in pieces) == text
    assert all(text[a:b] == raw for a, b, raw in pieces)
    assert list(split_text("x" * 1200)) == [(0, 1200, "x" * 1200)]


@pytest.mark.parametrize(
    "source,target",
    [("zh-CN", "en"), ("zh-CN", "ja"), ("en", "zh-CN"), ("en", "ja"), ("ja", "zh-CN"), ("ja", "en")],
)
def test_six_directions_enter_same_reviewed_workflow(env, source, target):
    c, runner, g, *_ = env
    job = post(c, "/v1/translation-jobs", job_request(source_language=source, target_language=target)).json()
    runner.drain()
    assert c.get(job["status_url"]).json()["status"] == "succeeded"
    assert g.calls == ["translator", "reviewer"]  # Routing behavior only, not language quality.
    same = post(c, "/v1/translation-jobs", job_request(source_language=source, target_language=source))
    assert same.status_code == 422


def test_local_retranslation_preserves_source_and_untouched_candidates(env):
    c, runner, g, db, _ = env
    request = job_request()
    request["input"] = {
        "kind": "batch",
        "items": [
            {"client_item_id": str(i), "text": text}
            for i, text in enumerate(["设备处于待机状态。", "门处于关闭状态。", "指示灯已亮起。"])
        ],
    }
    parent = post(c, "/v1/translation-jobs", request).json()
    runner.drain()
    rows = sorted(c.get(parent["status_url"] + "/segments").json()["items"], key=lambda x: x["index"])
    before = g.calls.count("translator")
    child = post(
        c,
        f"/v1/translations/{parent['translation_id']}/retranslations",
        {"base_revision": 1, "segment_ids": [rows[1]["id"]], "instruction": "语气更正式。"},
    ).json()
    runner.drain()
    assert c.get(child["status_url"]).json()["status"] == "succeeded"
    assert g.calls.count("translator") == before + 1
    new_rows = sorted(c.get(child["status_url"] + "/segments").json()["items"], key=lambda x: x["index"])
    assert new_rows[0]["candidate_id"] == rows[0]["candidate_id"]
    assert new_rows[2]["candidate_id"] == rows[2]["candidate_id"]
    assert all(r["reviewed"] for r in new_rows)
    with db.session() as s:
        old_job, new_job = s.get(Job, parent["job_id"]), s.get(Job, child["job_id"])
        assert old_job.status == "succeeded" and new_job.revision == 2
        assert old_job.source_id != new_job.source_id
        assert (
            s.get(Record, old_job.source_id).data["input"] == s.get(Record, new_job.source_id).data["input"]
        )
        assert old_job.snapshot_id == new_job.snapshot_id


def test_repair_regression_rolls_back_and_human_replacement_can_resume(env):
    c, runner, g, *_ = env
    g.translation = "Previous candidate."

    def issue(kind):
        def output(payload):
            r = payload["segments"][0]
            return {
                "reviewed_segment_ids": [r["segment_id"]],
                "issues": [
                    {
                        "segment_id": r["segment_id"],
                        "type": kind,
                        "severity": "major",
                        "source_span": {"start": 0, "end": 2, "quote": r["source_text"][:2]},
                        "target_span": {"start": 0, "end": 3, "quote": r["target_text"][:3]},
                        "explanation": "controlled error",
                    }
                ],
            }

        return output

    g.reviews = [issue("mistranslation"), issue("negation")]
    job = post(c, "/v1/translation-jobs", job_request()).json()
    runner.drain()
    state = c.get(job["status_url"]).json()
    seg = c.get(job["status_url"] + "/segments").json()["items"][0]
    assert state["status"] == "awaiting_review"
    assert seg["text"] == "Previous candidate." and seg["regression"]
    assert g.calls.count("repairer") == 1
    change = post(
        c,
        job["status_url"] + "/review-decisions",
        {
            "revision": 1,
            "expected_version": state["state_version"],
            "finalize": True,
            "decisions": [
                {
                    "segment_id": seg["id"],
                    "segment_revision": seg["revision"],
                    "action": "replace_translation",
                    "reason": "对照原文修订",
                    "replacement_text": "Corrected candidate.",
                }
            ],
        },
    )
    assert change.status_code == 200, change.text
    runner.drain()
    assert c.get(job["status_url"] + "/result").json()["result"]["text"] == "Corrected candidate."
