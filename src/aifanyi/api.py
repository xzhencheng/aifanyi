import asyncio
import base64
from pathlib import Path
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import Field
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import StaleDataError
from starlette.routing import Match

from . import conversation, knowledge, profiles, translations
from .callbacks import validate_url
from .config import get_settings
from .db import Database, Event, Job, Message, Record, Segment, Work, now
from .errors import DomainError, require
from .models import Gateway
from .schemas import (
    ChatRequest,
    Clarification,
    ConversationCreate,
    Feedback,
    KnowledgeCreate,
    ProfileRequest,
    PublishProfile,
    Reason,
    ReleaseRequest,
    Retranslation,
    ReviewDecisions,
    Strict,
    SyncRequest,
    TextInput,
    TranslateRequest,
)
from .security import Auth, get_job, get_record, project_access, role_required
from .store import add_record, cached_response, canonical, idempotent, patch, public_record


class VersionAction(Strict):
    expected_version: int
    reason: str = Field(min_length=1)


class EntryVersion(Strict):
    expected_version: int
    content: dict
    source_refs: list[str] = Field(min_length=1)


class ClientCreate(Strict):
    project_id: str
    name: str


class EndpointCreate(Strict):
    project_id: str
    url: str
    key_id: str
    secret_env: str
    enabled: bool = True
    expires: float
    events: list[str] = [
        "translation.succeeded",
        "translation.failed",
        "translation.review_required",
        "translation.canceled",
    ]


class EvaluationCreate(Strict):
    project_id: str
    report: dict


def cursor_value(cursor):
    if not cursor:
        return ""
    try:
        return base64.urlsafe_b64decode(cursor.encode()).decode()
    except Exception:
        raise DomainError("INVALID_CURSOR", "分页游标无效。", 422) from None


def page(rows, limit, serializer):
    more = len(rows) > limit
    rows = rows[:limit]
    return {
        "items": [serializer(x) for x in rows],
        "next_cursor": base64.urlsafe_b64encode(rows[-1].id.encode()).decode() if more else None,
    }


def create_app(settings=None, db=None, gateway=None):
    settings = settings or get_settings()
    db = db or Database(settings.database_url)
    gateway = gateway or Gateway(db, settings)
    auth = Auth(settings, db)
    app = FastAPI(title="AI 翻译 Agent", version="0.1.0")
    app.state.db, app.state.settings, app.state.gateway = db, settings, gateway

    @app.middleware("http")
    async def request_id(request, call_next):
        request.state.request_id = "req_" + uuid4().hex
        if (
            request.url.path.startswith("/v1/")
            and any(route.matches(request.scope)[0] is Match.FULL for route in app.routes)
            and request.method in {"POST", "PUT", "PATCH"}
            and request.headers.get("content-type", "").split(";", 1)[0].lower() != "application/json"
        ):
            response = JSONResponse(
                status_code=415,
                content={
                    "error": {
                        "code": "UNSUPPORTED_MEDIA_TYPE",
                        "message": "当前接口只接受 JSON 文本请求。",
                        "request_id": request.state.request_id,
                    }
                },
            )
        else:
            response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["Cache-Control"] = "no-store" if request.url.path.startswith("/v1") else "no-cache"
        return response

    @app.exception_handler(DomainError)
    async def domain_error(request, exc):
        return JSONResponse(
            status_code=exc.status,
            content={
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "request_id": request.state.request_id,
                    "retryable": exc.status in {429, 503},
                    "details": exc.details,
                }
            },
        )

    @app.exception_handler(RequestValidationError)
    async def invalid(request, exc):
        errors = exc.errors()
        code = "INVALID_REQUEST"
        if any("INVALID_LANGUAGE_PAIR" in str(e.get("msg")) for e in errors):
            code = "INVALID_LANGUAGE_PAIR"
        if request.url.path == "/v1/translations" and any(e["type"] == "string_too_long" for e in errors):
            code = "SYNC_INPUT_TOO_LARGE"
        if any(e["type"] == "union_tag_invalid" for e in errors):
            code = "UNSUPPORTED_INPUT_KIND"
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": code,
                    "message": "请求字段、范围或类型不符合接口要求。",
                    "request_id": request.state.request_id,
                    "retryable": False,
                    "details": {"fields": [list(e["loc"]) for e in errors]},
                }
            },
        )

    @app.exception_handler(IntegrityError)
    @app.exception_handler(StaleDataError)
    async def conflict(request, exc):
        return JSONResponse(
            status_code=409,
            content={
                "error": {
                    "code": "REVISION_CONFLICT",
                    "message": "并发状态已变化，请按原请求键重试或刷新。",
                    "request_id": request.state.request_id,
                    "retryable": True,
                }
            },
        )

    def actor(authorization: str | None = Header(default=None)):
        return auth.authenticate(authorization)

    def write(a, key, path, body, fn):
        for attempt in range(3):
            try:
                with db.session() as s:
                    return idempotent(s, a, key, path, body, lambda: fn(s))
            except (IntegrityError, StaleDataError):
                if attempt == 2:
                    raise

    @app.get("/health/live")
    def live():
        return {"status": "ok", "version": "0.1.0"}

    @app.get("/health/ready")
    def ready():
        with db.session() as s:
            s.execute(text("SELECT 1 FROM records LIMIT 1"))
        return {"database": "ready", "models": "validate profiles separately"}

    @app.get("/v1/me")
    def me(a=Depends(actor)):
        return {"id": a.id, "projects": a.projects, "roles": a.roles, "environment": settings.environment}

    @app.post("/v1/translation-jobs", status_code=202)
    def create_translation(
        body: TranslateRequest, a=Depends(actor), idempotency_key: str | None = Header(default=None)
    ):
        return write(
            a,
            idempotency_key,
            "/translation-jobs",
            body.model_dump(),
            lambda s: translations.job_summary(translations.create_job(s, a, body, settings)),
        )

    @app.post("/v1/translations")
    async def sync_translation(
        body: SyncRequest, a=Depends(actor), idempotency_key: str | None = Header(default=None)
    ):
        req = TranslateRequest(
            **body.model_dump(exclude={"text"}), input=TextInput(kind="text", text=body.text)
        )
        created = write(
            a,
            idempotency_key,
            "/translations",
            body.model_dump(),
            lambda s: translations.job_summary(translations.create_job(s, a, req, settings)),
        )
        end = now() + settings.sync_wait_seconds
        while now() < end:
            with db.session() as s:
                job = get_job(s, a, created["job_id"])
                if job.status == "succeeded":
                    return translations.result(s, a, job.id)
                if job.status == "awaiting_review":
                    return {
                        **translations.job_summary(job),
                        "draft": True,
                        "segments": [
                            {"id": x.id, "source_text": x.raw_text, **x.data}
                            for x in translations.segments(s, job)
                        ],
                    }
                if job.status == "failed":
                    raise DomainError(
                        job.data.get("error", {}).get("code", "MODEL_UNAVAILABLE"),
                        "翻译执行失败。",
                        502,
                        created,
                    )
            await asyncio.sleep(0.25)
        raise DomainError("SYNC_WAIT_TIMEOUT", "等待超时，任务仍可查询。", 504, created)

    @app.get("/v1/translation-jobs/{job_id}")
    def get_translation_job(job_id: str, a=Depends(actor)):
        with db.session() as s:
            return translations.job_summary(get_job(s, a, job_id))

    @app.get("/v1/translation-jobs/{job_id}/segments")
    def get_segments(job_id: str, cursor: str = "", limit: int = 20, a=Depends(actor)):
        require(1 <= limit <= 100, "INVALID_REQUEST", "分页上限为 100。", 422)
        with db.session() as s:
            get_job(s, a, job_id)
            rows = list(
                s.scalars(
                    select(Segment)
                    .where(Segment.job_id == job_id, Segment.id > cursor_value(cursor))
                    .order_by(Segment.id)
                    .limit(limit + 1)
                )
            )
            return page(
                rows,
                limit,
                lambda x: {
                    "id": x.id,
                    "index": x.index,
                    "source_text": x.raw_text,
                    "locator": x.locator,
                    "status": x.status,
                    **x.data,
                },
            )

    @app.get("/v1/translation-jobs/{job_id}/result")
    def get_result(job_id: str, a=Depends(actor)):
        with db.session() as s:
            return translations.result(s, a, job_id)

    @app.get("/v1/translations/{translation_id}")
    def get_translation(translation_id: str, revision: int | None = None, a=Depends(actor)):
        with db.session() as s:
            tr = get_record(s, a, translation_id, "translation")
            job = s.scalar(
                select(Job).where(
                    Job.translation_id == tr.id, Job.revision == (revision or tr.data["latest_revision"])
                )
            )
            require(job is not None, "RESOURCE_NOT_FOUND", "版本不存在。", 404)
            return {
                **translations.job_summary(job),
                "source_id": job.source_id,
                "segments": [
                    {"id": x.id, "index": x.index, "source_text": x.raw_text, "status": x.status, **x.data}
                    for x in translations.segments(s, job)
                ],
            }

    @app.post("/v1/translation-jobs/{job_id}/cancel", status_code=202)
    def cancel(
        job_id: str, body: Reason, a=Depends(actor), idempotency_key: str | None = Header(default=None)
    ):
        return write(
            a,
            idempotency_key,
            f"/translation-jobs/{job_id}/cancel",
            body.model_dump(),
            lambda s: translations.cancel_job(s, a, job_id, body.reason),
        )

    @app.post("/v1/translation-jobs/{job_id}/retry", status_code=202)
    def retry(
        job_id: str, body: Reason, a=Depends(actor), idempotency_key: str | None = Header(default=None)
    ):
        return write(
            a,
            idempotency_key,
            f"/translation-jobs/{job_id}/retry",
            body.model_dump(),
            lambda s: translations.retry_job(s, a, job_id, body.expected_version),
        )

    @app.post("/v1/translations/{translation_id}/retranslations", status_code=202)
    def retranslate(
        translation_id: str,
        body: Retranslation,
        a=Depends(actor),
        idempotency_key: str | None = Header(default=None),
    ):
        return write(
            a,
            idempotency_key,
            f"/translations/{translation_id}/retranslations",
            body.model_dump(),
            lambda s: translations.job_summary(
                translations.retranslate(s, a, translation_id, body, settings)
            ),
        )

    @app.post("/v1/translations/{translation_id}/feedback", status_code=201)
    def feedback(
        translation_id: str,
        body: Feedback,
        a=Depends(actor),
        idempotency_key: str | None = Header(default=None),
    ):
        return write(
            a,
            idempotency_key,
            f"/translations/{translation_id}/feedback",
            body.model_dump(),
            lambda s: public_record(translations.feedback(s, a, translation_id, body)),
        )

    @app.post("/v1/translation-jobs/{job_id}/review-decisions")
    def review(
        job_id: str,
        body: ReviewDecisions,
        a=Depends(actor),
        idempotency_key: str | None = Header(default=None),
    ):
        return write(
            a,
            idempotency_key,
            f"/translation-jobs/{job_id}/review-decisions",
            body.model_dump(),
            lambda s: translations.review_decisions(s, a, job_id, body),
        )

    @app.post("/v1/agent/conversations", status_code=201)
    def new_conversation(
        body: ConversationCreate, a=Depends(actor), idempotency_key: str | None = Header(default=None)
    ):
        def create(s):
            c = conversation.create_conversation(s, a, body)
            return {"conversation_id": c.id, "version": c.version, "defaults": c.data["defaults"]}

        return write(a, idempotency_key, "/agent/conversations", body.model_dump(), create)

    @app.get("/v1/agent/conversations")
    def list_conversations(project_id: str, cursor: str = "", limit: int = 20, a=Depends(actor)):
        require(1 <= limit <= 100, "INVALID_REQUEST", "分页上限为 100。", 422)
        with db.session() as s:
            project_access(s, a, project_id)
            rows = list(
                s.scalars(
                    select(Record)
                    .where(
                        Record.kind == "conversation",
                        Record.tenant == a.tenant,
                        Record.project == project_id,
                        Record.owner == a.id,
                        Record.id > cursor_value(cursor),
                    )
                    .order_by(Record.id)
                    .limit(limit + 1)
                )
            )
            return page(rows, limit, public_record)

    @app.get("/v1/agent/conversations/{conversation_id}/messages")
    def get_messages(conversation_id: str, cursor: str = "", limit: int = 100, a=Depends(actor)):
        require(1 <= limit <= 100, "INVALID_REQUEST", "分页上限为 100。", 422)
        with db.session() as s:
            conv = get_record(s, a, conversation_id, "conversation")
            after = 0
            if cursor:
                prior = s.get(Message, cursor_value(cursor))
                require(prior and prior.conversation_id == conv.id, "INVALID_CURSOR", "游标无效。", 422)
                after = prior.sequence
            rows = list(
                s.scalars(
                    select(Message)
                    .where(Message.conversation_id == conv.id, Message.sequence > after)
                    .order_by(Message.sequence)
                    .limit(limit + 1)
                )
            )
            return {
                **page(
                    rows,
                    limit,
                    lambda m: {
                        "id": m.id,
                        "sequence": m.sequence,
                        "role": m.role,
                        "content": m.content,
                        **m.data,
                    },
                ),
                "version": conv.version,
                "active_run_id": conv.data.get("active_run_id"),
                "defaults": conv.data["defaults"],
            }

    @app.post("/v1/agent/messages", status_code=202)
    def message(body: ChatRequest, a=Depends(actor), idempotency_key: str | None = Header(default=None)):
        return write(
            a,
            idempotency_key,
            "/agent/messages",
            body.model_dump(),
            lambda s: conversation.submit_message(s, a, body),
        )

    @app.get("/v1/agent/runs/{run_id}")
    def get_run(run_id: str, a=Depends(actor)):
        with db.session() as s:
            run = get_record(s, a, run_id, "run")
            conv = get_record(s, a, run.data["conversation_id"], "conversation")
            return {
                "run_id": run.id,
                "version": run.version,
                "conversation_id": conv.id,
                "conversation_version": conv.version,
                "status": run.data["status"],
                "question": run.data.get("question"),
                "job_id": run.data.get("job_id"),
                "blocks": run.data.get("blocks", []),
                "intent": (run.data.get("plan") or {}).get("intent"),
            }

    @app.post("/v1/agent/runs/{run_id}/responses", status_code=202)
    def response(
        run_id: str, body: Clarification, a=Depends(actor), idempotency_key: str | None = Header(default=None)
    ):
        return write(
            a,
            idempotency_key,
            f"/agent/runs/{run_id}/responses",
            body.model_dump(),
            lambda s: conversation.respond(s, a, run_id, body),
        )

    @app.post("/v1/agent/runs/{run_id}/cancel", status_code=202)
    def cancel_run(
        run_id: str, body: Reason, a=Depends(actor), idempotency_key: str | None = Header(default=None)
    ):
        def action(s):
            run = get_record(s, a, run_id, "run")
            require(run.version == body.expected_run_version, "REVISION_CONFLICT", "运行状态已变化。")
            require(run.data["status"] not in {"completed", "failed", "canceled"})
            job = s.get(Job, run.data["job_id"]) if run.data.get("job_id") else None
            conversation.finish_run(s, run, [{"type": "status", "text": "已取消。"}], "canceled")
            if job and job.status in {"queued", "running", "awaiting_review"}:
                translations.cancel_job(s, a, job.id, body.reason)
            return {"run_id": run.id, "status": "canceled"}

        return write(a, idempotency_key, f"/agent/runs/{run_id}/cancel", body.model_dump(), action)

    @app.get("/v1/agent/runs/{run_id}/events")
    async def events(
        run_id: str, request: Request, last_event_id: str | None = Header(default=None), a=Depends(actor)
    ):
        with db.session() as s:
            get_record(s, a, run_id, "run")
            start = 0
            if last_event_id:
                prior = s.get(Event, last_event_id)
                require(prior and prior.subject_id == run_id, "INVALID_EVENT_CURSOR", "事件游标无效。")
                require(
                    prior.created >= now() - settings.event_retention_days * 86400,
                    "EVENT_CURSOR_EXPIRED",
                    "事件游标已过期，请查询当前运行状态。",
                    410,
                )
                start = prior.sequence

        async def stream():
            sequence, heartbeat, reauth = start, now(), now()
            stream_actor = a
            while not await request.is_disconnected():
                if now() - reauth > 15:
                    try:
                        stream_actor = auth.authenticate(request.headers.get("authorization"))
                        with db.session() as s:
                            get_record(s, stream_actor, run_id, "run")
                    except DomainError:
                        return
                    reauth = now()
                with db.session() as s:
                    run = get_record(s, stream_actor, run_id, "run")
                    rows = list(
                        s.scalars(
                            select(Event)
                            .where(
                                Event.subject_id == run_id,
                                Event.sequence > sequence,
                                Event.created >= now() - settings.event_retention_days * 86400,
                            )
                            .order_by(Event.sequence)
                            .limit(100)
                        )
                    )
                    state = run.data["status"]
                    payloads = [(e.id, e.sequence, e.type, canonical(e.body)) for e in rows]
                for id, seq, type, body in payloads:
                    sequence = seq
                    yield f"id: {id}\nevent: {type}\ndata: {body}\n\n"
                if state in {"completed", "failed", "canceled"} and len(payloads) < 100:
                    return
                if now() - heartbeat >= 15:
                    yield ": heartbeat\n\n"
                    heartbeat = now()
                await asyncio.sleep(0.5)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"X-Accel-Buffering": "no", "Cache-Control": "no-store"},
        )

    @app.post("/v1/knowledge/entries", status_code=201)
    def knowledge_create(
        body: KnowledgeCreate, a=Depends(actor), idempotency_key: str | None = Header(default=None)
    ):
        return write(
            a,
            idempotency_key,
            "/knowledge/entries",
            body.model_dump(),
            lambda s: public_record(knowledge.create_knowledge(s, a, body)),
        )

    @app.get("/v1/knowledge/entries")
    def knowledge_list(project_id: str, cursor: str = "", limit: int = 20, a=Depends(actor)):
        require(1 <= limit <= 100, "INVALID_REQUEST", "分页上限为 100。", 422)
        with db.session() as s:
            project_access(s, a, project_id)
            rows = list(
                s.scalars(
                    select(Record)
                    .where(
                        Record.tenant == a.tenant,
                        Record.project == project_id,
                        Record.kind == "knowledge",
                        Record.id > cursor_value(cursor),
                    )
                    .order_by(Record.id)
                )
            )
            rows = [r for r in rows if r.data["scope"] != "personal" or r.owner == a.id]
            return page(rows[: limit + 1], limit, public_record)

    @app.post("/v1/knowledge/entries/{entry_id}/versions", status_code=201)
    def knowledge_version(
        entry_id: str,
        body: EntryVersion,
        a=Depends(actor),
        idempotency_key: str | None = Header(default=None),
    ):
        def action(s):
            old = get_record(s, a, entry_id, "knowledge")
            require(old.version == body.expected_version, "REVISION_CONFLICT", "版本已变化。")
            req = KnowledgeCreate(
                project_id=old.project,
                **{
                    k: old.data[k]
                    for k in ["kind", "scope", "source_language", "target_language", "locked", "domain"]
                },
                content=body.content,
                source_refs=body.source_refs,
            )
            new = knowledge.create_knowledge(s, a, req)
            patch(
                new, entry_id=old.data["entry_id"], entry_version=old.data["entry_version"] + 1, parent=old.id
            )
            return public_record(new)

        return write(a, idempotency_key, f"/knowledge/entries/{entry_id}/versions", body.model_dump(), action)

    @app.get("/v1/knowledge/entries/{entry_id}/versions")
    def knowledge_versions(entry_id: str, a=Depends(actor)):
        with db.session() as s:
            old = get_record(s, a, entry_id, "knowledge")
            rows = s.scalars(
                select(Record).where(
                    Record.kind == "knowledge", Record.tenant == a.tenant, Record.project == old.project
                )
            )
            return {"items": [public_record(r) for r in rows if r.data["entry_id"] == old.data["entry_id"]]}

    @app.post("/v1/knowledge/proposals/{entry_id}/{action}")
    def knowledge_action(
        entry_id: str,
        action: str,
        body: VersionAction,
        a=Depends(actor),
        idempotency_key: str | None = Header(default=None),
    ):
        require(action in {"submit", "approve", "reject"}, "RESOURCE_NOT_FOUND", "操作不存在。", 404)

        def change(s):
            r = get_record(s, a, entry_id, "knowledge")
            knowledge.approve(s, a, r, body.expected_version, action, body.reason)
            s.flush()
            return public_record(r)

        return write(
            a, idempotency_key, f"/knowledge/proposals/{entry_id}/{action}", body.model_dump(), change
        )

    @app.post("/v1/knowledge/releases", status_code=201)
    def release(body: ReleaseRequest, a=Depends(actor), idempotency_key: str | None = Header(default=None)):
        return write(
            a,
            idempotency_key,
            "/knowledge/releases",
            body.model_dump(),
            lambda s: public_record(knowledge.release(s, a, body)),
        )

    @app.post("/v1/knowledge/releases/{release_id}/rollback", status_code=201)
    def rollback_release(
        release_id: str, body: Reason, a=Depends(actor), idempotency_key: str | None = Header(default=None)
    ):
        return write(
            a,
            idempotency_key,
            f"/knowledge/releases/{release_id}/rollback",
            body.model_dump(),
            lambda s: public_record(knowledge.rollback(s, a, release_id, body.reason)),
        )

    @app.get("/v1/feedback/{feedback_id}")
    def feedback_detail(feedback_id: str, a=Depends(actor)):
        with db.session() as s:
            return public_record(get_record(s, a, feedback_id, "feedback"))

    @app.post("/v1/feedback/{feedback_id}/knowledge-proposals", status_code=201)
    def propose_feedback(
        feedback_id: str,
        body: KnowledgeCreate,
        a=Depends(actor),
        idempotency_key: str | None = Header(default=None),
    ):
        def action(s):
            proposal = get_record(s, a, feedback_id, "feedback")
            require(
                proposal.project == body.project_id and proposal.data["scope"] == body.scope,
                "INVALID_KNOWLEDGE",
                "提案必须沿用用户确认的项目和作用范围。",
                422,
            )
            req = body.model_copy(
                update={"source_refs": list(dict.fromkeys([*body.source_refs, proposal.id]))}
            )
            entry = knowledge.create_knowledge(s, a, req)
            patch(proposal, status="proposed", knowledge_entry_id=entry.id)
            return public_record(entry)

        return write(
            a, idempotency_key, f"/feedback/{feedback_id}/knowledge-proposals", body.model_dump(), action
        )

    @app.post("/v1/model-profiles", status_code=201)
    def new_profile(
        body: ProfileRequest, a=Depends(actor), idempotency_key: str | None = Header(default=None)
    ):
        role_required(a, "admin")
        profiles.check_roles(body.roles, gateway)

        def create(s):
            project_access(s, a, body.project_id)
            r = add_record(
                s,
                "profile",
                a.tenant,
                body.project_id,
                a.id,
                {**body.model_dump(exclude={"project_id"}), "status": "draft"},
            )
            return public_record(r)

        return write(a, idempotency_key, "/model-profiles", body.model_dump(), create)

    @app.get("/v1/model-profiles")
    def model_profiles(project_id: str, a=Depends(actor)):
        role_required(a, "admin")
        with db.session() as s:
            project_access(s, a, project_id)
            return {
                "items": [
                    public_record(x)
                    for x in s.scalars(
                        select(Record).where(
                            Record.kind == "profile", Record.project == project_id, Record.tenant == a.tenant
                        )
                    )
                ]
            }

    @app.post("/v1/model-profiles/{profile_id}/validate")
    def validate_profile(
        profile_id: str,
        body: VersionAction,
        a=Depends(actor),
        idempotency_key: str | None = Header(default=None),
    ):
        role_required(a, "admin")
        with db.session() as s:
            cached = cached_response(
                s, a, idempotency_key, f"/model-profiles/{profile_id}/validate", body.model_dump()
            )
            if cached is not None:
                return cached
            row = get_record(s, a, profile_id, "profile")
            require(
                row.version == body.expected_version and row.data["status"] != "published",
                "REVISION_CONFLICT",
                "配置版本已变化或已发布。",
            )
        report = profiles.probe(row, gateway)

        def save(s):
            live = get_record(s, a, profile_id, "profile")
            require(live.version == body.expected_version, "REVISION_CONFLICT", "验证期间配置已变化。")
            patch(live, status="validated", validation=report)
            s.flush()
            return public_record(live)

        return write(a, idempotency_key, f"/model-profiles/{profile_id}/validate", body.model_dump(), save)

    @app.post("/v1/model-profiles/{profile_id}/publish")
    def publish_profile(
        profile_id: str,
        body: PublishProfile,
        a=Depends(actor),
        idempotency_key: str | None = Header(default=None),
    ):
        role_required(a, "admin")

        def action(s):
            r = get_record(s, a, profile_id, "profile")
            profiles.publish(s, a, r, body)
            s.flush()
            return public_record(r)

        return write(a, idempotency_key, f"/model-profiles/{profile_id}/publish", body.model_dump(), action)

    @app.post("/v1/model-profiles/{profile_id}/activate")
    def activate_profile(
        profile_id: str,
        body: VersionAction,
        a=Depends(actor),
        idempotency_key: str | None = Header(default=None),
    ):
        role_required(a, "admin")

        def action(s):
            r = get_record(s, a, profile_id, "profile")
            require(r.version == body.expected_version, "REVISION_CONFLICT", "模型配置版本已变化。")
            allowed = (
                {"published"}
                if settings.environment == "production"
                else {"validated", "evaluated", "published"}
            )
            require(r.data["status"] in allowed, "MODEL_PROFILE_NOT_READY", "配置尚未获准使用。")
            project = project_access(s, a, r.project)
            previous = project.data.get("default_profile_id")
            patch(project, default_profile_id=r.id)
            add_record(
                s,
                "audit",
                a.tenant,
                r.project,
                a.id,
                {
                    "action": "activate_profile",
                    "previous": previous,
                    "profile_id": r.id,
                    "reason": body.reason,
                },
            )
            return {"project_id": project.id, "default_profile_id": r.id, "previous_profile_id": previous}

        return write(a, idempotency_key, f"/model-profiles/{profile_id}/activate", body.model_dump(), action)

    @app.post("/v1/evaluation-reports", status_code=201)
    def record_evaluation(
        body: EvaluationCreate, a=Depends(actor), idempotency_key: str | None = Header(default=None)
    ):
        role_required(a, "reviewer", body.project_id)

        def create(s):
            project_access(s, a, body.project_id)
            return public_record(
                add_record(
                    s, "evaluation", a.tenant, body.project_id, a.id, {**body.report, "approved_by": a.id}
                )
            )

        return write(a, idempotency_key, "/evaluation-reports", body.model_dump(), create)

    @app.get("/v1/evaluations/{id}/report")
    def evaluation_report(id: str, a=Depends(actor)):
        with db.session() as s:
            r = get_record(s, a, id, "evaluation")
            role_required(a, "reviewer", r.project)
            return public_record(r)

    @app.post("/v1/clients", status_code=201)
    def create_client(
        body: ClientCreate, a=Depends(actor), idempotency_key: str | None = Header(default=None)
    ):
        role_required(a, "admin")

        def create(s):
            project_access(s, a, body.project_id)
            return public_record(
                add_record(s, "client", a.tenant, body.project_id, a.id, {"name": body.name, "enabled": True})
            )

        return write(a, idempotency_key, "/clients", body.model_dump(), create)

    @app.post("/v1/clients/{client_id}/callback-endpoints", status_code=201)
    def endpoint(
        client_id: str,
        body: EndpointCreate,
        a=Depends(actor),
        idempotency_key: str | None = Header(default=None),
    ):
        role_required(a, "admin")
        require(
            body.secret_env.startswith("AIFANYI_CALLBACK_") and body.expires > now(),
            "INVALID_REQUEST",
            "回调密钥引用或有效期无效。",
            422,
        )
        validate_url(body.url, settings)

        def create(s):
            client = get_record(s, a, client_id, "client")
            require(client.project == body.project_id, "ACTION_FORBIDDEN", "项目不匹配。", 403)
            return public_record(
                add_record(
                    s,
                    "endpoint",
                    a.tenant,
                    body.project_id,
                    a.id,
                    {**body.model_dump(), "client_id": client.id},
                )
            )

        return write(
            a, idempotency_key, f"/clients/{client_id}/callback-endpoints", body.model_dump(), create
        )

    @app.get("/v1/clients")
    def clients(project_id: str, a=Depends(actor)):
        role_required(a, "admin")
        with db.session() as s:
            project_access(s, a, project_id)
            return {
                "items": [
                    public_record(x)
                    for x in s.scalars(
                        select(Record).where(
                            Record.kind == "client", Record.tenant == a.tenant, Record.project == project_id
                        )
                    )
                ]
            }

    @app.post("/v1/clients/{client_id}/disable")
    def disable_client(
        client_id: str, body: Reason, a=Depends(actor), idempotency_key: str | None = Header(default=None)
    ):
        role_required(a, "admin")

        def action(s):
            client = get_record(s, a, client_id, "client")
            patch(client, enabled=False)
            add_record(
                s,
                "audit",
                a.tenant,
                client.project,
                a.id,
                {"action": "disable_client", "target": client.id, "reason": body.reason},
            )
            s.flush()
            return public_record(client)

        return write(a, idempotency_key, f"/clients/{client_id}/disable", body.model_dump(), action)

    @app.get("/v1/clients/{client_id}/callback-endpoints")
    def endpoints(client_id: str, a=Depends(actor)):
        role_required(a, "admin")
        with db.session() as s:
            client = get_record(s, a, client_id, "client")
            rows = s.scalars(
                select(Record).where(
                    Record.kind == "endpoint", Record.tenant == a.tenant, Record.project == client.project
                )
            )
            return {"items": [public_record(x) for x in rows if x.data["client_id"] == client_id]}

    @app.post("/v1/callback-endpoints/{endpoint_id}/disable")
    def disable_endpoint(
        endpoint_id: str, body: Reason, a=Depends(actor), idempotency_key: str | None = Header(default=None)
    ):
        role_required(a, "admin")

        def action(s):
            ep = get_record(s, a, endpoint_id, "endpoint")
            patch(ep, enabled=False)
            add_record(
                s,
                "audit",
                a.tenant,
                ep.project,
                a.id,
                {"action": "disable_endpoint", "target": ep.id, "reason": body.reason},
            )
            s.flush()
            return public_record(ep)

        return write(
            a, idempotency_key, f"/callback-endpoints/{endpoint_id}/disable", body.model_dump(), action
        )

    @app.get("/v1/callback-deliveries")
    def deliveries(project_id: str, cursor: str = "", limit: int = 20, a=Depends(actor)):
        role_required(a, "admin")
        require(1 <= limit <= 100, "INVALID_REQUEST", "分页上限为 100。", 422)
        with db.session() as s:
            project_access(s, a, project_id)
            endpoints = {
                x.id
                for x in s.scalars(
                    select(Record).where(
                        Record.kind == "endpoint", Record.tenant == a.tenant, Record.project == project_id
                    )
                )
            }
            rows = [
                x
                for x in s.scalars(
                    select(Work)
                    .where(Work.kind == "callback", Work.id > cursor_value(cursor))
                    .order_by(Work.id)
                )
                if x.data["endpoint_id"] in endpoints
            ]
            return page(
                rows[: limit + 1],
                limit,
                lambda x: {
                    "id": x.id,
                    "event_id": x.target,
                    "status": x.status,
                    "attempts": x.attempts,
                    "error": x.last_error,
                    "available_at": x.available_at,
                },
            )

    @app.post("/v1/callback-deliveries/{id}/redeliver", status_code=202)
    def redeliver(
        id: str, body: Reason, a=Depends(actor), idempotency_key: str | None = Header(default=None)
    ):
        role_required(a, "admin")

        def retry_delivery(s):
            w = s.get(Work, id)
            require(w and w.kind == "callback", "RESOURCE_NOT_FOUND", "投递不存在。", 404)
            ep = get_record(s, a, w.data["endpoint_id"], "endpoint")
            require(ep.data["enabled"] and w.status == "dead")
            w.status, w.available_at, w.attempts = "pending", now(), 0
            return {"event_id": w.target, "status": w.status}

        return write(
            a, idempotency_key, f"/callback-deliveries/{id}/redeliver", body.model_dump(), retry_delivery
        )

    dist = Path(settings.chat_dist)
    if dist.exists():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

        @app.get("/", include_in_schema=False)
        def chat():
            return FileResponse(dist / "index.html")

    return app


app = create_app()
