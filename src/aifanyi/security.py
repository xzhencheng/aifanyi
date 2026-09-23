import secrets
from dataclasses import dataclass

import jwt
from sqlalchemy import select

from .db import Job, Record
from .errors import DomainError, require


@dataclass(frozen=True)
class Actor:
    id: str
    tenant: str
    projects: dict[str, list[str]]
    roles: list[str]
    client_id: str | None = None

    def can(self, role, project=""):
        return role in self.roles or role in self.projects.get(project, [])


class Auth:
    def __init__(self, settings, db):
        self.settings, self.db = settings, db
        self.jwks = jwt.PyJWKClient(settings.oidc_jwks_url, timeout=5) if settings.oidc_jwks_url else None

    def authenticate(self, authorization):
        require(
            bool(authorization and authorization.startswith("Bearer ")), "UNAUTHENTICATED", "请登录。", 401
        )
        token = authorization[7:]
        if self.settings.dev_auth and secrets.compare_digest(token, self.settings.dev_token):
            principal_id = "principal_dev"
        else:
            require(self.jwks is not None, "UNAUTHENTICATED", "身份服务未配置。", 401)
            try:
                key = self.jwks.get_signing_key_from_jwt(token).key
                claims = jwt.decode(
                    token,
                    key,
                    algorithms=self.settings.oidc_algorithms,
                    audience=self.settings.oidc_audience,
                    issuer=self.settings.oidc_issuer,
                    options={"require": ["exp", "iat", "sub"]},
                )
                principal_id = f"{claims['iss']}|{claims['sub']}"
            except (jwt.PyJWTError, OSError, ValueError):
                raise DomainError("UNAUTHENTICATED", "身份凭据无效或过期。", 401) from None
        with self.db.session() as s:
            row = s.scalar(select(Record).where(Record.kind == "principal", Record.owner == principal_id))
            require(
                row is not None and row.data.get("enabled", False),
                "UNAUTHENTICATED",
                "身份未登记或停用。",
                401,
            )
            if row.data.get("client_id"):
                client = s.get(Record, row.data["client_id"])
                require(
                    client and client.tenant == row.tenant and client.data.get("enabled"),
                    "UNAUTHENTICATED",
                    "调用系统已停用。",
                    401,
                )
            return Actor(
                row.id,
                row.tenant,
                row.data.get("projects", {}),
                row.data.get("roles", []),
                row.data.get("client_id"),
            )


def project_access(s, actor, project):
    row = s.get(Record, project)
    require(
        row is not None
        and row.kind == "project"
        and row.tenant == actor.tenant
        and project in actor.projects,
        "RESOURCE_NOT_FOUND",
        "对象不存在或不可访问。",
        404,
    )
    return row


def get_record(s, actor, id, kind=None, write=False):
    row = s.get(Record, id)
    require(
        row is not None and row.tenant == actor.tenant and (kind is None or row.kind == kind),
        "RESOURCE_NOT_FOUND",
        "对象不存在或不可访问。",
        404,
    )
    if row.project:
        project_access(s, actor, row.project)
    if row.kind in {
        "conversation",
        "run",
        "translation",
        "source",
        "snapshot",
        "context",
        "feedback",
        "candidate",
        "result",
    }:
        require(
            row.owner == actor.id
            or (row.kind not in {"conversation", "run"} and actor.can("reviewer", row.project)),
            "RESOURCE_NOT_FOUND",
            "对象不存在或不可访问。",
            404,
        )
    if row.kind == "knowledge" and row.data.get("scope") == "personal":
        require(row.owner == actor.id, "RESOURCE_NOT_FOUND", "对象不存在或不可访问。", 404)
    return row


def get_job(s, actor, id):
    row = s.get(Job, id)
    require(
        row is not None and row.tenant == actor.tenant, "RESOURCE_NOT_FOUND", "任务不存在或不可访问。", 404
    )
    project_access(s, actor, row.project)
    require(
        row.owner == actor.id or actor.can("reviewer", row.project),
        "RESOURCE_NOT_FOUND",
        "任务不存在或不可访问。",
        404,
    )
    return row


def role_required(actor, role, project=""):
    require(actor.can(role, project), "ACTION_FORBIDDEN", "没有此操作权限。", 403)
