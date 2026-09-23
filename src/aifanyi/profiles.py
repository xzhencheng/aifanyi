from .db import Record
from .errors import require
from .models import messages
from .quality import validate_review
from .schemas import Plan, Repair, Review
from .store import add_record, patch


def initial_roles(settings):
    hy = {
        "base_url": settings.hymt_base_url,
        "model": settings.hymt_model,
        "revision": settings.hymt_revision,
        "secret_env": "AIFANYI_HYMT_API_KEY",
        "precision": "bf16",
        "max_model_len": 8192,
        "max_output_tokens": 4096,
        "concurrency": settings.hymt_concurrency,
        "template_version": "hymt-v1",
        "sampling": {"temperature": 0.7, "top_p": 0.6, "top_k": 20, "repetition_penalty": 1.05},
    }
    q = {
        "base_url": settings.qwen_base_url,
        "model": settings.qwen_model,
        "revision": settings.qwen_revision,
        "secret_env": "AIFANYI_QWEN_API_KEY",
        "precision": settings.qwen_precision,
        "max_model_len": settings.qwen_max_model_len,
        "max_output_tokens": settings.qwen_max_output_tokens,
        "concurrency": settings.qwen_concurrency,
        "thinking": settings.qwen_thinking,
        "thinking_field": settings.qwen_thinking_field,
        "sampling": {"temperature": 0.2, "top_p": 0.8},
    }
    return {
        "translator": hy,
        "reviewer": {**q, "template_version": "review-v1"},
        "repairer": {**q, "template_version": "repair-v1"},
        "conversation": {**q, "template_version": "conversation-v1", "max_output_tokens": 2048},
    }


def check_roles(roles, gateway):
    require(
        set(roles) == {"translator", "reviewer", "repairer", "conversation"},
        "MODEL_CAPABILITY_MISMATCH",
        "模型配置必须包含四个角色。",
        422,
    )
    allowed = {
        "base_url",
        "model",
        "revision",
        "secret_env",
        "precision",
        "max_model_len",
        "max_output_tokens",
        "concurrency",
        "template_version",
        "sampling",
        "thinking",
        "thinking_field",
    }
    required = {
        "base_url",
        "model",
        "revision",
        "secret_env",
        "precision",
        "max_model_len",
        "max_output_tokens",
        "concurrency",
        "template_version",
    }
    for c in roles.values():
        require(
            required <= set(c) and not set(c) - allowed,
            "MODEL_CAPABILITY_MISMATCH",
            "模型配置字段不完整或不允许。",
            422,
        )
        require(
            isinstance(c["concurrency"], int) and 1 <= c["concurrency"] <= 128,
            "MODEL_CAPABILITY_MISMATCH",
            "并发配额无效。",
            422,
        )
        require(
            isinstance(c["max_output_tokens"], int)
            and isinstance(c["max_model_len"], int)
            and 0 < c["max_output_tokens"] < c["max_model_len"],
            "MODEL_CAPABILITY_MISMATCH",
            "上下文预算无效。",
            422,
        )
        require(
            c["secret_env"] in {"AIFANYI_HYMT_API_KEY", "AIFANYI_QWEN_API_KEY"},
            "MODEL_CAPABILITY_MISMATCH",
            "只允许登记的模型 Secret 引用。",
            422,
        )
        require(
            set(c.get("sampling", {}))
            <= {"temperature", "top_p", "top_k", "repetition_penalty", "presence_penalty"},
            "MODEL_CAPABILITY_MISMATCH",
            "采样参数不允许。",
            422,
        )
        gateway.endpoint(c)
    q = roles["reviewer"]
    require(
        all(
            roles[r]["base_url"] == q["base_url"]
            and roles[r]["model"] == q["model"]
            and roles[r]["concurrency"] == q["concurrency"]
            for r in ["repairer", "conversation"]
        ),
        "MODEL_CAPABILITY_MISMATCH",
        "Qwen 三个角色必须使用同端点与共享配额。",
        422,
    )


def probe(profile, gateway):
    roles = profile.data["roles"]
    check_roles(roles, gateway)
    results = {}
    for name, config in roles.items():
        base, headers = gateway.endpoint(config)
        response = gateway.http.get(base + "/models", headers=headers)
        require(
            response.status_code == 200
            and config["model"] in {x["id"] for x in response.json().get("data", [])},
            "MODEL_CAPABILITY_MISMATCH",
            f"{name} 的模型 ID 不在服务列表中。",
            422,
        )
        results[name] = {
            "tokenizer_count": gateway.count_tokens(config, [{"role": "user", "content": "Hello"}]),
            "configured_model": config["model"],
            "revision": config["revision"],
        }
    row = {"segment_id": "probe_001", "source_text": "Hello", "target_text": "你好"}
    gateway.generate(
        roles["reviewer"],
        "reviewer",
        messages("对照原文审校译文，返回 JSON。", {"segments": [row]}, Review),
        [profile.id, profile.version, "probe_review"],
        [f"probe:{profile.id}:{profile.version}:review"],
        Review,
        lambda value: validate_review(value, [row], []),
    )
    gateway.generate(
        roles["translator"],
        "translator",
        [{"role": "user", "content": "将以下文本翻译为中文，只输出译文：Hello"}],
        [profile.id, profile.version, "probe_translate"],
        [f"probe:{profile.id}:{profile.version}:translate"],
    )
    gateway.generate(
        roles["conversation"],
        "conversation",
        messages("解析请求，只输出 JSON。", {"message": "翻译一下"}, Plan),
        [profile.id, profile.version, "probe_plan"],
        [f"probe:{profile.id}:{profile.version}:plan"],
        Plan,
    )
    gateway.generate(
        roles["repairer"],
        "repairer",
        messages(
            "按原文修复译文，只输出 JSON。",
            {
                "segment_id": "probe_001",
                "source_text": "Hello",
                "target_text": "再见",
                "issues": [{"id": "probe_issue", "explanation": "问候误译成告别"}],
            },
            Repair,
        ),
        [profile.id, profile.version, "probe_repair"],
        [f"probe:{profile.id}:{profile.version}:repair"],
        Repair,
    )
    return {"roles": results, "schema_probes": "passed", "business_quality_validated": False}


def publish(s, actor, profile, request):
    require(profile.version == request.expected_version, "REVISION_CONFLICT", "配置版本已变化。")
    report = s.get(Record, request.evaluation_report_id)
    require(
        report
        and report.kind == "evaluation"
        and report.tenant == actor.tenant
        and report.project == profile.project,
        "RESOURCE_NOT_FOUND",
        "评测记录不存在。",
        404,
    )
    require(
        profile.data["status"] in {"validated", "evaluated"},
        "PROFILE_NOT_VALIDATED",
        "请先完成模型能力验证。",
    )
    r = report.data
    required = {
        "dataset_version",
        "annotation_version",
        "split_manifest_hash",
        "profile_id",
        "profile_version",
        "independent_test",
        "human_reviewed",
        "critical_count",
        "major_segment_rate",
        "baseline_major_segment_rate",
        "improvement_ci_high",
        "auto_coverage",
        "reviewer_recall",
        "reviewer_false_positive_rate",
        "term_accuracy",
        "accepted_language_pairs",
        "risk_scope",
        "denominator",
        "approved_by",
        "report_uri",
    }
    require(required <= set(r), "EVALUATION_INCOMPLETE", "独立评测报告字段不完整。")
    require(
        r["profile_id"] == profile.id and r["profile_version"] == profile.version,
        "REVISION_CONFLICT",
        "评测不对应当前配置版本。",
    )
    require(
        r["independent_test"] is True and r["human_reviewed"] is True and r["denominator"] > 0,
        "EVALUATION_INCOMPLETE",
        "需要独立人工盲评证据。",
    )
    require(
        r["critical_count"] == 0
        and r["major_segment_rate"] <= min(request.max_major_segment_rate, r["baseline_major_segment_rate"])
        and r["improvement_ci_high"] < 0
        and r["auto_coverage"] >= request.min_auto_coverage
        and r["reviewer_recall"] >= request.min_reviewer_recall
        and r["reviewer_false_positive_rate"] <= request.max_reviewer_false_positive_rate
        and r["term_accuracy"] >= 0.99,
        "QUALITY_GATE_BLOCKED",
        "评测未达到发布门槛。",
    )
    require(
        request.accepted_language_pairs == r["accepted_language_pairs"]
        and request.risk_scope == r["risk_scope"],
        "EVALUATION_INCOMPLETE",
        "发布范围与评测覆盖不一致。",
    )
    require(
        set(map(tuple, request.accepted_language_pairs)) <= set(map(tuple, profile.data["language_pairs"])),
        "MODEL_CAPABILITY_MISMATCH",
        "发布方向超出配置声明。",
        422,
    )
    require(
        all(
            c["revision"] not in {"", "unverified"} and "unverified" not in c["precision"]
            for c in profile.data["roles"].values()
        ),
        "MODEL_CAPABILITY_MISMATCH",
        "生产发布需固定各模型的真实版本和推理精度。",
    )
    patch(profile, status="published", publication=request.model_dump(), published_by=actor.id)
    add_record(
        s,
        "audit",
        actor.tenant,
        profile.project,
        actor.id,
        {"action": "publish_profile", "profile_id": profile.id, "report": report.id},
    )
