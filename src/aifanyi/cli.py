import argparse
import json
import os

from alembic import command
from alembic.config import Config
from sqlalchemy import select

from .config import get_settings
from .db import Database, Record
from .errors import require
from .models import Gateway
from .profiles import initial_roles, probe
from .store import add_record, patch
from .workflow import checkpointer


def initialize(db, settings, oidc_subject=None):
    if settings.environment == "production":
        require(
            bool(oidc_subject), "BOOTSTRAP_IDENTITY_REQUIRED", "生产初始化需要 --oidc-subject（issuer|sub）。"
        )
    with db.session() as s:
        owner = oidc_subject or "principal_dev"
        principal = s.scalar(select(Record).where(Record.kind == "principal", Record.owner == owner))
        if principal is None:
            principal = add_record(
                s,
                "principal",
                "tenant_default",
                "",
                owner,
                {
                    "enabled": True,
                    "projects": {"project_default": ["user", "reviewer", "editor"]},
                    "roles": ["admin", "enterprise_editor"],
                },
                id="principal_default",
            )
        if s.get(Record, "project_default") is None:
            add_record(
                s,
                "project",
                "tenant_default",
                "",
                principal.id,
                {"name": "默认翻译项目", "require_human": False, "default_profile_id": "profile_default"},
                id="project_default",
            )
        if s.get(Record, "profile_default") is None:
            languages = ["zh-CN", "en", "ja"]
            add_record(
                s,
                "profile",
                "tenant_default",
                "project_default",
                principal.id,
                {
                    "name": "Hy-MT2-7B + existing Qwen 3.5",
                    "roles": initial_roles(settings),
                    "language_pairs": [[a, b] for a in languages for b in languages if a != b],
                    "status": "draft",
                },
                id="profile_default",
            )


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate")
    init = sub.add_parser("init")
    init.add_argument("--oidc-subject", default=os.environ.get("AIFANYI_BOOTSTRAP_OIDC_SUBJECT"))
    sub.add_parser("worker")
    configure = sub.add_parser("configure-models")
    configure.add_argument("--profile", default="profile_default")
    validate = sub.add_parser("validate-models")
    validate.add_argument("--profile", default="profile_default")
    grant = sub.add_parser("grant-principal")
    grant.add_argument("--subject", required=True, help="Exact issuer|sub from the trusted OIDC provider")
    grant.add_argument("--project", default="project_default")
    grant.add_argument("--role", action="append", choices=["user", "reviewer", "editor"], default=[])
    grant.add_argument("--client-id")
    args = parser.parse_args()
    settings = get_settings()
    db = Database(settings.database_url)
    if args.command == "migrate":
        command.upgrade(Config("alembic.ini"), "head")
        with checkpointer(settings) as cp:
            if hasattr(cp, "setup"):
                cp.setup()
        print("Database and persistent checkpoints initialized.")
    elif args.command == "init":
        initialize(db, settings, args.oidc_subject)
        print("Initialized project_default; model profile remains draft until a real probe passes.")
    elif args.command == "configure-models":
        with db.session() as s:
            row = s.get(Record, args.profile)
            require(
                row and row.kind == "profile" and row.data["status"] == "draft",
                "INVALID_TRANSITION",
                "仅允许初始化 draft 配置；已验证配置应新建版本。",
            )
            patch(row, roles=initial_roles(settings))
        print("Draft model configuration updated from environment. No model calls executed.")
    elif args.command == "validate-models":
        with db.session() as s:
            row = s.get(Record, args.profile)
            require(row and row.data["status"] == "draft", "INVALID_TRANSITION", "请选择 draft 模型配置。")
        report = probe(row, Gateway(db, settings))
        with db.session() as s:
            live = s.get(Record, args.profile)
            require(live.version == row.version, "REVISION_CONFLICT", "验证期间配置已变化。")
            patch(live, status="validated", validation=report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
    elif args.command == "grant-principal":
        require(
            args.subject.startswith(settings.oidc_issuer + "|") and bool(settings.oidc_issuer),
            "INVALID_IDENTITY",
            "身份必须属于配置的 OIDC issuer。",
        )
        with db.session() as s:
            project = s.get(Record, args.project)
            require(project and project.kind == "project", "RESOURCE_NOT_FOUND", "项目不存在。", 404)
            if args.client_id:
                client = s.get(Record, args.client_id)
                require(
                    client
                    and client.kind == "client"
                    and client.project == args.project
                    and client.tenant == project.tenant
                    and client.data.get("enabled"),
                    "INVALID_IDENTITY",
                    "机器身份的调用系统未登记或停用。",
                )
            row = s.scalar(select(Record).where(Record.kind == "principal", Record.owner == args.subject))
            if not row:
                row = add_record(
                    s,
                    "principal",
                    project.tenant,
                    "",
                    args.subject,
                    {"enabled": True, "projects": {}, "roles": [], "client_id": args.client_id},
                )
            require(
                row.tenant == project.tenant and row.data.get("client_id") == args.client_id,
                "INVALID_IDENTITY",
                "不能改变现有身份的租户或调用系统。",
            )
            patch(
                row,
                projects={**row.data["projects"], args.project: list(dict.fromkeys(["user", *args.role]))},
            )
            add_record(
                s,
                "audit",
                project.tenant,
                project.id,
                "operator_cli",
                {"action": "grant_project_roles", "principal_id": row.id, "roles": args.role},
            )
        print("Principal project roles updated and audited.")
    elif args.command == "worker":
        from .worker import poll

        poll(settings)


if __name__ == "__main__":
    main()
