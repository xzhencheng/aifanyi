import hashlib
import json
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import object_session

from .db import Event, Idempotency, Message, Record, Work, now, uid
from .errors import require


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256((value if isinstance(value, str) else canonical(value)).encode()).hexdigest()


def timestamp(value=None):
    return (
        datetime.fromtimestamp(now() if value is None else value, timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def add_record(s, kind, tenant, project, owner, data, id=None):
    r = Record(id=id or uid(kind), kind=kind, tenant=tenant, project=project, owner=owner, data=data)
    s.add(r)
    s.flush()
    return r


def patch(row, **values):
    row.data = {**row.data, **values}


def enqueue(s, kind, target, suffix="", data=None):
    key = f"{kind}:{target}:{suffix}"
    if not s.get(Work, key):
        s.add(Work(id=key, kind=kind, target=target, data=data or {}))


def event(s, row, type, payload):
    # Caller holds the aggregate lock (or exclusive worker lease) for this subject.
    sequence = (s.scalar(select(func.max(Event.sequence)).where(Event.subject_id == row.id)) or 0) + 1
    id = uid("evt")
    body = {
        "event_id": id,
        "event": type,
        "subject_id": row.id,
        "sequence": sequence,
        "state_version": row.version,
        "occurred_at": timestamp(),
        **payload,
    }
    e = Event(
        id=id,
        tenant=row.tenant,
        project=row.project,
        subject_id=row.id,
        sequence=sequence,
        type=type,
        body=body,
        raw_body=canonical(body),
    )
    s.add(e)
    s.flush()
    return e


def assistant_message(s, conversation, run_id, blocks):
    sequence = (
        s.scalar(select(func.max(Message.sequence)).where(Message.conversation_id == conversation.id)) or 0
    ) + 1
    msg = Message(
        conversation_id=conversation.id,
        sequence=sequence,
        role="assistant",
        content="",
        data={"run_id": run_id, "blocks": blocks},
    )
    s.add(msg)
    patch(conversation, active_run_id=None)
    s.flush()
    return msg


def cached_response(s, actor, key, path, body):
    require(bool(key) and len(key) <= 200, "IDEMPOTENCY_KEY_REQUIRED", "写请求需要 Idempotency-Key。", 422)
    found = s.get(Idempotency, digest([actor.tenant, actor.id, path, key]))
    if found and found.expires > now():
        require(found.body_hash == digest(body), "IDEMPOTENCY_CONFLICT", "相同请求键对应不同内容。")
        return found.response
    return None


def idempotent(s, actor, key, path, body, fn):
    require(bool(key) and len(key) <= 200, "IDEMPOTENCY_KEY_REQUIRED", "写请求需要 Idempotency-Key。", 422)
    scope = digest([actor.tenant, actor.id, path, key])
    found = s.get(Idempotency, scope)
    hashed = digest(body)
    if found and found.expires > now():
        require(found.body_hash == hashed, "IDEMPOTENCY_CONFLICT", "相同请求键对应不同内容。")
        return found.response
    if found:
        s.delete(found)
        s.flush()
    response = fn()
    s.add(Idempotency(key=scope, body_hash=hashed, response=response, expires=now() + 7 * 86400))
    s.flush()
    return response


def public_record(r):
    session = object_session(r)
    if session is not None:
        session.flush()
    return {**r.data, "id": r.id, "version": r.version, "created_at": timestamp(r.created)}
