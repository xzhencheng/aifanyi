from sqlalchemy import select

from .db import Record
from .errors import DomainError, require
from .security import get_record, project_access, role_required
from .store import add_record, digest, patch


def validate_content(request):
    c = request.content
    required = {
        "term": {"source", "target", "concept_id", "definition"},
        "tm": {"source", "target", "context_fingerprint"},
        "policy": {"key", "value"},
        "domain_excerpt": {"text"},
    }[request.kind]
    require(required <= set(c), "INVALID_KNOWLEDGE", "知识内容缺少必要字段。", 422)
    require(len(str(c)) <= 20000, "INPUT_LIMIT_EXCEEDED", "知识条目过长。", 413)
    text_fields = required - {"value"}
    require(
        all(isinstance(c[k], str) and c[k].strip() for k in text_fields),
        "INVALID_KNOWLEDGE",
        "知识文本字段必须是非空字符串。",
        422,
    )
    require(
        isinstance(c.get("variants", []), list)
        and all(isinstance(x, str) and x for x in c.get("variants", [])),
        "INVALID_KNOWLEDGE",
        "术语变体必须是非空字符串列表。",
        422,
    )
    require(
        request.source_language != request.target_language,
        "INVALID_LANGUAGE_PAIR",
        "源语言和目标语言必须不同。",
        422,
    )
    if request.locked:
        require(request.scope == "enterprise", "INVALID_KNOWLEDGE", "只有企业项可以锁定。", 422)


def create_knowledge(s, actor, request):
    project_access(s, actor, request.project_id)
    if request.scope == "enterprise":
        role_required(actor, "enterprise_editor")
    if request.scope == "project":
        role_required(actor, "editor", request.project_id)
    validate_content(request)
    data = request.model_dump(exclude={"project_id"})
    data.update(status="draft", entry_id="", entry_version=1, human_approved=False)
    r = add_record(s, "knowledge", actor.tenant, request.project_id, actor.id, data)
    patch(r, entry_id=r.id)
    return r


def approve(s, actor, entry, expected_version, action, reason):
    require(entry.version == expected_version, "REVISION_CONFLICT", "知识版本已变化。")
    scope = entry.data["scope"]
    if scope == "enterprise":
        role_required(actor, "enterprise_editor")
    elif scope == "project":
        role_required(actor, "reviewer", entry.project)
    else:
        require(entry.owner == actor.id, "ACTION_FORBIDDEN", "只能维护本人的个人项。", 403)
    current = entry.data["status"]
    transitions = {
        "submit": ({"draft", "rejected"}, "submitted"),
        "approve": ({"submitted"}, "approved"),
        "reject": ({"submitted"}, "rejected"),
    }
    allowed, status = transitions[action]
    require(current in allowed)
    patch(entry, status=status, human_approved=action == "approve")
    add_record(
        s,
        "audit",
        actor.tenant,
        entry.project,
        actor.id,
        {"action": action, "target": entry.id, "reason": reason},
    )


def release(s, actor, request):
    project_access(s, actor, request.project_id)
    entries = [get_record(s, actor, id, "knowledge") for id in request.entry_version_refs]
    require(
        all(r.project == request.project_id for r in entries), "INVALID_KNOWLEDGE", "条目项目不匹配。", 422
    )
    for r in entries:
        scope = r.data["scope"]
        if scope == "enterprise":
            role_required(actor, "enterprise_editor")
        elif scope == "project":
            role_required(actor, "reviewer", r.project)
        else:
            require(r.owner == actor.id, "ACTION_FORBIDDEN", "个人范围不匹配。", 403)
        require(r.data["status"] == "approved", "KNOWLEDGE_NOT_APPROVED", "条目必须先经审核。")
    published = add_record(
        s,
        "release",
        actor.tenant,
        request.project_id,
        actor.id,
        {"entry_version_refs": request.entry_version_refs, "reason": request.reason},
    )
    for r in entries:
        # Retire the earlier published version of this logical entry, retain the immutable content.
        for old in s.scalars(select(Record).where(Record.kind == "knowledge", Record.tenant == actor.tenant)):
            if (
                old.id != r.id
                and old.data.get("entry_id") == r.data["entry_id"]
                and old.data.get("status") == "published"
            ):
                patch(old, status="retired")
        patch(r, status="published", release_id=published.id)
    # Revalidate TM against every effective term when reused; term-set hash is also frozen in snapshot.
    return published


def snapshot(s, actor, request, profile):
    entries = []
    for r in s.scalars(select(Record).where(Record.kind == "knowledge", Record.tenant == actor.tenant)):
        d = r.data
        if d.get("status") != "published":
            continue
        if d["scope"] != "enterprise" and r.project != request.project_id:
            continue
        if d["scope"] == "personal" and (r.owner != actor.id or actor.client_id):
            continue
        if d["target_language"] != request.target_language:
            continue
        if request.source_language != "auto" and d["source_language"] != request.source_language:
            continue
        if d.get("domain", "general") not in ("general", request.domain):
            continue
        entries.append({"id": r.id, "version": r.version, **d})
    return add_record(
        s,
        "snapshot",
        actor.tenant,
        request.project_id,
        actor.id,
        {
            "entries": entries,
            "profile": profile.data,
            "profile_id": profile.id,
            "profile_version": profile.version,
            "retriever_version": "lexical-v1",
            "term_set_hash": digest([x for x in entries if x["kind"] == "term"]),
        },
    )


def resolve(entries, text, source_language, context):
    priorities = {"enterprise": 1, "project": 2, "personal": 3}
    candidates = {}
    evidence = []
    for e in entries:
        if e["source_language"] != source_language:
            continue
        c = e["content"]
        if e["kind"] == "term" and c["source"] not in text:
            continue
        if e["kind"] in ("tm", "domain_excerpt"):
            evidence.append(e)
            continue
        key = (e["kind"], c.get("source") if e["kind"] == "term" else c["key"])
        score = 100 if e.get("locked") else priorities[e["scope"]]
        prior = candidates.get(key)
        if prior is None or prior[0] < score:
            candidates[key] = (score, e)
        elif prior[0] == score and prior[1]["content"] != c:
            raise DomainError("KNOWLEDGE_CONFLICT", "同优先级知识存在冲突，需要确认。")
    resolved = [v[1] for v in candidates.values()]
    terms = [x for x in resolved if x["kind"] == "term"]
    tm = next(
        (
            x
            for x in evidence
            if x["kind"] == "tm"
            and x.get("human_approved")
            and x["content"]["source"] == text
            and x["content"]["context_fingerprint"] == digest(context)
            and all(t["content"]["target"] in x["content"]["target"] for t in terms)
        ),
        None,
    )
    return {
        "terms": terms,
        "policies": [x for x in resolved if x["kind"] == "policy"],
        "domain": [x for x in evidence if x["kind"] == "domain_excerpt"][:5],
        "tm": tm,
        "evidence_refs": [x["id"] for x in resolved + evidence],
    }


def rollback(s, actor, release_id, reason):
    from .schemas import KnowledgeCreate, ReleaseRequest

    old_release = get_record(s, actor, release_id, "release")
    refs = []
    for ref in old_release.data["entry_version_refs"]:
        old = get_record(s, actor, ref, "knowledge")
        require(old.data.get("human_approved"), "KNOWLEDGE_NOT_APPROVED", "回滚版本没有审核记录。")
        data = old.data
        request = KnowledgeCreate(
            project_id=old.project,
            **{
                k: data[k]
                for k in [
                    "kind",
                    "scope",
                    "source_language",
                    "target_language",
                    "content",
                    "locked",
                    "domain",
                ]
            },
            source_refs=[*data["source_refs"], old_release.id],
        )
        new = create_knowledge(s, actor, request)
        versions = [
            x.data["entry_version"]
            for x in s.scalars(
                select(Record).where(Record.kind == "knowledge", Record.tenant == actor.tenant)
            )
            if x.data.get("entry_id") == data["entry_id"]
        ]
        patch(
            new,
            entry_id=data["entry_id"],
            entry_version=max(versions) + 1,
            status="approved",
            human_approved=True,
            rollback_of=old.id,
        )
        refs.append(new.id)
    result = release(
        s, actor, ReleaseRequest(project_id=old_release.project, entry_version_refs=refs, reason=reason)
    )
    patch(result, rollback_of=old_release.id)
    add_record(
        s,
        "audit",
        actor.tenant,
        old_release.project,
        actor.id,
        {
            "action": "rollback_knowledge",
            "from_release": old_release.id,
            "new_release": result.id,
            "reason": reason,
        },
    )
    return result
