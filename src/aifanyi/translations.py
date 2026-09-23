from sqlalchemy import select
from sqlalchemy.orm import object_session

from . import knowledge
from .db import Job, Record, Segment, now, uid
from .errors import require
from .quality import hard_checks, requires_human
from .schemas import TextInput, TranslateRequest
from .security import get_job, get_record, project_access, role_required
from .store import add_record, digest, enqueue, event, patch


def create_job(s, actor, request: TranslateRequest, settings, parent=None, inherit=None, run_id=None):
    project = project_access(s, actor, request.project_id)
    profile_id = (
        project.data.get("default_profile_id", "profile_default")
        if request.model_profile_id == "profile_default"
        else request.model_profile_id
    )
    profile = get_record(s, actor, profile_id, "profile")
    allowed = (
        {"published"} if settings.environment == "production" else {"validated", "evaluated", "published"}
    )
    require(
        profile.data["status"] in allowed,
        "MODEL_PROFILE_NOT_READY",
        "请先验证模型配置；生产配置还需质量评测。",
        409,
    )
    pairs = profile.data.get("publication", {}).get(
        "accepted_language_pairs", profile.data.get("language_pairs", [])
    )
    if settings.environment == "production":
        require(
            request.domain in profile.data["publication"]["risk_scope"],
            "MODEL_CAPABILITY_MISMATCH",
            "该业务领域未在模型发布范围内。",
            422,
        )
    require(
        request.source_language == "auto" or [request.source_language, request.target_language] in pairs,
        "MODEL_CAPABILITY_MISMATCH",
        "模型组合不支持此语言方向。",
        422,
    )
    input_data = request.input.model_dump()
    items = (
        [{"client_item_id": "text", "text": request.input.text, "context": request.context}]
        if isinstance(request.input, TextInput)
        else [{**x.model_dump(), "context": request.context + "\n" + x.context} for x in request.input.items]
    )
    require(any(x["text"].strip() for x in items), "INVALID_INPUT", "原文不能为空。", 422)
    source = add_record(
        s,
        "source",
        actor.tenant,
        request.project_id,
        actor.id,
        {
            "items": items,
            "input": input_data,
            "sha256": digest(input_data),
            "parent_source": parent.source_id if parent else None,
        },
    )
    snap = s.get(Record, inherit) if inherit else knowledge.snapshot(s, actor, request, profile)
    require(snap is not None, "SNAPSHOT_UNAVAILABLE", "知识快照不可用。")
    if parent:
        tr = get_record(s, actor, parent.translation_id, "translation")
        require(tr.data["latest_revision"] == parent.revision, "REVISION_CONFLICT", "译文已有更新版本。")
        revision = parent.revision + 1
        patch(tr, latest_revision=revision)
    else:
        revision = 1
        tr = add_record(s, "translation", actor.tenant, request.project_id, actor.id, {"latest_revision": 1})
    callback = None
    if request.callback_endpoint_code:
        callback = get_record(s, actor, request.callback_endpoint_code, "endpoint")
        require(
            callback.data.get("client_id") == actor.client_id and callback.data.get("enabled"),
            "ACTION_FORBIDDEN",
            "回调端点未授权或停用。",
            403,
        )
    job = Job(
        id=uid("job"),
        tenant=actor.tenant,
        project=request.project_id,
        owner=actor.id,
        translation_id=tr.id,
        revision=revision,
        source_id=source.id,
        snapshot_id=snap.id,
        data={
            "request": request.model_dump(),
            "parent_job_id": parent.id if parent else None,
            "run_id": run_id,
            "cycle": 0,
            "attempt": 0,
            "quality_status": "pending",
            "requires_human": any(requires_human(project, x["text"], request.domain) for x in items),
            "callback_endpoint_id": callback.id if callback else None,
        },
    )
    s.add(job)
    s.flush()
    enqueue(s, "job", job.id, "0")
    event(s, job, "translation.queued", {"job_id": job.id, "status": "queued", "revision": revision})
    return job


def job_summary(job):
    session = object_session(job)
    if session is not None:
        session.flush()
    return {
        "job_id": job.id,
        "translation_id": job.translation_id,
        "revision": job.revision,
        "status": job.status,
        "stage": job.stage,
        "state_version": job.version,
        "quality_status": job.data.get("quality_status", "pending"),
        "knowledge_snapshot_id": job.snapshot_id,
        "status_url": f"/v1/translation-jobs/{job.id}",
        "error": job.data.get("error"),
        "coverage": job.data.get("coverage", {}),
    }


def segments(s, job):
    return list(s.scalars(select(Segment).where(Segment.job_id == job.id).order_by(Segment.index)))


def result(s, actor, job_id):
    job = get_job(s, actor, job_id)
    require(job.status == "succeeded", "RESULT_NOT_READY", "结果尚未通过全部检查。")
    require(job.data.get("result_id"), "ARTIFACT_EXPIRED", "结果已清理。", 410)
    row = s.get(Record, job.data["result_id"])
    require(row is not None, "ARTIFACT_EXPIRED", "结果已清理。", 410)
    return {**job_summary(job), **row.data}


def set_status(s, job, status, error=None):
    job.status = status
    patch(job, error=error)
    s.flush()
    if status in {"succeeded", "awaiting_review", "failed", "canceled"}:
        etype = "translation.review_required" if status == "awaiting_review" else f"translation.{status}"
        e = event(
            s,
            job,
            etype,
            {
                "job_id": job.id,
                "translation_id": job.translation_id,
                "status": status,
                "revision": job.revision,
                "quality_status": job.data.get("quality_status"),
                "result": {"kind": "text", "result_url": f"/v1/translation-jobs/{job.id}/result"},
            },
        )
        endpoint = job.data.get("callback_endpoint_id")
        if endpoint:
            enqueue(s, "callback", e.id, endpoint, {"endpoint_id": endpoint})
        if job.data.get("run_id"):
            from .conversation import job_changed

            job_changed(s, job)


def cancel_job(s, actor, job_id, reason):
    job = get_job(s, actor, job_id)
    require(job.status not in {"succeeded", "failed", "canceled"})
    patch(job, cancel_reason=reason)
    set_status(s, job, "canceled")
    return job_summary(job)


def retry_job(s, actor, job_id, expected_version):
    job = get_job(s, actor, job_id)
    require(job.version == expected_version, "REVISION_CONFLICT", "任务版本已变化。")
    require(job.status == "failed")
    require(
        s.get(Record, job.source_id) and s.get(Record, job.snapshot_id),
        "SNAPSHOT_UNAVAILABLE",
        "原文或快照已清理。",
    )
    patch(job, attempt=job.data["attempt"] + 1, error=None)
    job.status = "queued"
    enqueue(s, "job", job.id, f"retry{job.data['attempt']}")
    return job_summary(job)


def retranslate(s, actor, translation_id, request, settings, run_id=None):
    tr = get_record(s, actor, translation_id, "translation")
    require(
        tr.data["latest_revision"] == request.base_revision, "REVISION_CONFLICT", "请使用当前最新译文版本。"
    )
    parent = s.scalar(select(Job).where(Job.translation_id == tr.id, Job.revision == request.base_revision))
    require(parent is not None and parent.status in {"succeeded", "awaiting_review"})
    source_segments = segments(s, parent)
    ids = {x.id for x in source_segments}
    require(set(request.segment_ids) <= ids, "RESOURCE_NOT_FOUND", "目标片段不存在。", 404)
    data = parent.data["request"]
    child = create_job(
        s,
        actor,
        TranslateRequest.model_validate(data),
        settings,
        parent=parent,
        inherit=parent.snapshot_id if request.knowledge_mode == "inherit" else None,
        run_id=run_id,
    )
    patch(
        child,
        instruction=request.instruction,
        target_indices=[x.index for x in source_segments if x.id in request.segment_ids],
    )
    return child


def feedback(s, actor, translation_id, request):
    tr = get_record(s, actor, translation_id, "translation")
    job = s.scalar(select(Job).where(Job.translation_id == tr.id, Job.revision == request.revision))
    require(job is not None, "RESOURCE_NOT_FOUND", "译文版本不存在。", 404)
    require(
        set(request.segment_ids) <= {x.id for x in segments(s, job)},
        "RESOURCE_NOT_FOUND",
        "片段不存在。",
        404,
    )
    return add_record(
        s,
        "feedback",
        actor.tenant,
        tr.project,
        actor.id,
        {**request.model_dump(), "translation_id": translation_id, "job_id": job.id, "status": "draft"},
    )


def review_decisions(s, actor, job_id, request):
    job = get_job(s, actor, job_id)
    role_required(actor, "reviewer", job.project)
    require(
        job.version == request.expected_version and job.revision == request.revision,
        "REVISION_CONFLICT",
        "任务或译文版本已变化。",
    )
    require(job.status == "awaiting_review")
    rows = {x.id: x for x in segments(s, job)}
    replaced = False
    for decision in request.decisions:
        seg = rows.get(decision.segment_id)
        require(seg is not None, "RESOURCE_NOT_FOUND", "片段不存在。", 404)
        d = dict(seg.data)
        require(d.get("revision", 0) == decision.segment_revision, "REVISION_CONFLICT", "候选译文已变化。")
        issues = [dict(x) for x in d.get("issues", [])]
        if decision.action == "replace_translation":
            replaced = True
            require(
                bool(decision.replacement_text and decision.replacement_text.strip()),
                "INVALID_INPUT",
                "缺少修订译文。",
                422,
            )
            revision = d.get("revision", 0) + 1
            candidate = add_record(
                s,
                "candidate",
                job.tenant,
                job.project,
                actor.id,
                {
                    "job_id": job.id,
                    "segment_id": seg.id,
                    "revision": revision,
                    "text": decision.replacement_text,
                    "origin": "human",
                    "parent": d.get("candidate_id"),
                },
            )
            d.update(
                text=decision.replacement_text,
                candidate_id=candidate.id,
                revision=revision,
                reviewed=False,
                human_approved=False,
                repairs=0,
                issues=[],
                decisions={},
                regression=False,
            )
            seg.status = "checking"
        elif decision.action in {"mark_false_positive", "accept_minor"}:
            issue = next((x for x in issues if x["id"] == decision.issue_id), None)
            require(issue is not None, "RESOURCE_NOT_FOUND", "问题不存在。", 404)
            require(not issue.get("hard"), "QUALITY_GATE_BLOCKED", "硬性保护错误必须修正译文。")
            if decision.action == "accept_minor":
                require(issue["severity"] == "minor", "QUALITY_GATE_BLOCKED", "不能接受真实严重错误。")
            require(bool(decision.evidence_refs), "EVIDENCE_REQUIRED", "裁决需要原文或知识证据引用。", 422)
            allowed = {job.source_id, *d.get("knowledge", {}).get("evidence_refs", [])}
            require(set(decision.evidence_refs) <= allowed, "INVALID_EVIDENCE", "证据引用不可用。", 422)
            issue["status"] = (
                "false_positive" if decision.action == "mark_false_positive" else "accepted_minor"
            )
            d.update(issues=issues)
        elif decision.action == "approve_segment":
            require(
                d.get("reviewed") and not any(x["status"] == "open" for x in issues),
                "QUALITY_GATE_BLOCKED",
                "请先处理问题并完成该版审校。",
            )
            require(
                not hard_checks(seg.id, seg.raw_text, d["text"], d.get("knowledge", {})),
                "QUALITY_GATE_BLOCKED",
                "确定性检查仍未通过。",
            )
            d["human_approved"] = True
            d["regression"] = False
            seg.status = "accepted"
        seg.data = d
        add_record(
            s,
            "decision",
            job.tenant,
            job.project,
            actor.id,
            {**decision.model_dump(), "job_id": job.id, "created_at": now()},
        )
    if request.finalize:
        for seg in rows.values():
            if (
                seg.status == "needs_review"
                and seg.data.get("text")
                and not any(x["status"] == "open" for x in seg.data.get("issues", []))
            ):
                seg.status = "checking"
                patch(seg, regression=False)
        consistency = (
            job.data.get("consistency_checked", False)
            and not replaced
            and all(not any(i["status"] == "open" for i in x.data.get("issues", [])) for x in rows.values())
        )
        patch(job, cycle=job.data["cycle"] + 1, quality_status="pending", consistency_passed=consistency)
        job.status = "queued"
        enqueue(s, "job", job.id, f"review{job.data['cycle']}")
        run = s.get(Record, job.data.get("run_id")) if job.data.get("run_id") else None
        if run and run.data["status"] == "awaiting_review":
            patch(run, status="running")
    return job_summary(job)
