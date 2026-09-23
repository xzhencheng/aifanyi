import re
from collections import Counter

from .db import uid
from .errors import require

PROTECTED = re.compile(
    r"\{\{[^{}]+\}\}|\$\{[^{}]+\}|%[sd]|(?<![A-Za-z0-9])[A-Z]+[-_]\d+[A-Za-z0-9_-]*(?![A-Za-z0-9])|[-+]?\d+(?:[.,]\d+)*"
)


def hard_checks(segment_id, source, target, knowledge):
    issues = []
    source_values = Counter(PROTECTED.findall(source))
    target_values = Counter(PROTECTED.findall(target))
    if source_values != target_values:
        issues.append(
            {
                "id": uid("issue"),
                "segment_id": segment_id,
                "type": "protected_content",
                "severity": "major",
                "source_span": None,
                "target_span": None,
                "explanation": "数字、型号或占位符集合不一致。",
                "status": "open",
                "origin": "deterministic",
                "hard": True,
            }
        )
    if not target.strip():
        issues.append(
            {
                "id": uid("issue"),
                "segment_id": segment_id,
                "type": "malformed_output",
                "severity": "major",
                "explanation": "译文为空。",
                "status": "open",
                "origin": "deterministic",
                "hard": True,
            }
        )
    for term in knowledge.get("terms", []):
        c = term["content"]
        variants = [c["target"], *c.get("variants", [])]
        if not any(x in target for x in variants):
            issues.append(
                {
                    "id": uid("issue"),
                    "segment_id": segment_id,
                    "type": "terminology",
                    "severity": "major",
                    "explanation": f"缺少适用术语译法：{c['source']} → {c['target']}",
                    "evidence_refs": [term["id"]],
                    "status": "open",
                    "origin": "deterministic",
                    "hard": bool(term.get("locked")),
                }
            )
    # Pair units with their numeric value; preserving just the set of numbers is insufficient.
    aliases = {
        "kg": "kg",
        "千克": "kg",
        "公斤": "kg",
        "キログラム": "kg",
        "mm": "mm",
        "毫米": "mm",
        "ミリメートル": "mm",
        "cm": "cm",
        "厘米": "cm",
        "センチメートル": "cm",
        "m/s": "m/s",
        "米/秒": "m/s",
        "メートル/秒": "m/s",
        "v": "v",
        "伏": "v",
        "伏特": "v",
        "ボルト": "v",
        "hz": "hz",
        "赫兹": "hz",
        "ヘルツ": "hz",
        "kw": "kw",
        "千瓦": "kw",
        "キロワット": "kw",
        "℃": "°c",
        "°c": "°c",
        "摄氏度": "°c",
        "%": "%",
        "％": "%",
    }
    pattern = (
        r"([-+]?\d+(?:[.,]\d+)*)\s*("
        + "|".join(re.escape(x) for x in sorted(aliases, key=len, reverse=True))
        + r")(?![A-Za-z])"
    )

    def quantities(value):
        return Counter((number, aliases[unit.lower()]) for number, unit in re.findall(pattern, value, re.I))

    if quantities(source) != quantities(target):
        issues.append(
            {
                "id": uid("issue"),
                "segment_id": segment_id,
                "type": "protected_content",
                "severity": "major",
                "explanation": "数值与单位的对应关系不一致；一期不自动做单位换算。",
                "status": "open",
                "origin": "deterministic",
                "hard": True,
            }
        )
    return issues


def validate_review(review, inputs, allowed_refs):
    expected = {x["segment_id"]: x for x in inputs}
    ids = review.reviewed_segment_ids
    require(
        len(ids) == len(set(ids)) and set(ids) == set(expected),
        "REVIEW_INVALID",
        "审校未完整覆盖目标片段。",
        502,
    )
    for issue in review.issues:
        require(issue.segment_id in expected, "REVIEW_INVALID", "审校引用未知片段。", 502)
        row = expected[issue.segment_id]
        require(set(issue.evidence_refs) <= set(allowed_refs), "REVIEW_INVALID", "审校引用未知知识。", 502)
        for span, key in [(issue.source_span, "source_text"), (issue.target_span, "target_text")]:
            if span:
                raw = row[key]
                require(
                    0 <= span.start < span.end <= len(raw) and raw[span.start : span.end] == span.quote,
                    "REVIEW_INVALID",
                    "审校引用与原文/译文不一致。",
                    502,
                )
        if issue.type == "omission":
            valid = issue.source_span is not None
        elif issue.type == "addition":
            valid = issue.target_span is not None
        elif issue.type == "source_ambiguity":
            valid = issue.source_span is not None
        else:
            valid = issue.source_span is not None and issue.target_span is not None
        require(valid, "REVIEW_INVALID", "审校问题缺少必要证据定位。", 502)


def annotated_issues(review):
    seen = set()
    result = []
    for item in review.issues:
        d = item.model_dump()
        key = (item.segment_id, item.type, str(d["source_span"]), str(d["target_span"]))
        if key not in seen:
            result.append({"id": uid("issue"), **d, "status": "open", "origin": "semantic", "hard": False})
            seen.add(key)
    return result


def requires_human(project, text, domain):
    # A server-side minimum. Users/models cannot request a lower risk.
    patterns = [
        "维修",
        "检修",
        "复位",
        "断电",
        "maintenance",
        "reset",
        "保守",
        "点検",
        *project.data.get("risk_patterns", []),
    ]
    return bool(
        project.data.get("require_human", False)
        or domain == "maintenance"
        or any(p.casefold() in text.casefold() for p in patterns)
    )


def split_text(text, max_chars=700):
    """Exact source slices; never normalize/reconstruct source. Prefer paragraph/sentence boundaries."""
    start = 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        if end < len(text):
            matches = list(re.finditer(r"\n|[。！？]|[.!?](?:\s|$)", text[start:end]))
            if matches:
                end = start + matches[-1].end()
            else:
                spaces = list(re.finditer(r"\s+", text[start:end]))
                if spaces:
                    end = start + spaces[-1].end()
                else:
                    # Keep a long unbroken token intact; the actual tokenizer budget will reject oversize input.
                    boundary = re.search(r"\s|[。！？]", text[end:])
                    end = end + boundary.end() if boundary else len(text)
        raw = text[start:end]
        if raw.strip():
            yield start, end, raw
        start = end
