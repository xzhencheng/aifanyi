from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Language = Literal["zh-CN", "en", "ja"]
SourceLanguage = Literal["auto", "zh-CN", "en", "ja"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TextInput(Strict):
    kind: Literal["text"]
    text: str = Field(min_length=1, max_length=100000)


class BatchItem(Strict):
    client_item_id: str = Field(min_length=1, max_length=100)
    text: str = Field(min_length=1, max_length=100000)
    context: str = Field(default="", max_length=10000)


class BatchInput(Strict):
    kind: Literal["batch"]
    items: list[BatchItem] = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def unique_items(self):
        if len({x.client_item_id for x in self.items}) != len(self.items):
            raise ValueError("Duplicate client_item_id")
        if sum(len(x.text) + len(x.context) for x in self.items) > 100000:
            raise ValueError("Batch text and context exceed 100000 codepoints")
        return self


class TranslateRequest(Strict):
    project_id: str
    source_language: SourceLanguage = "auto"
    target_language: Language
    input: Annotated[TextInput | BatchInput, Field(discriminator="kind")]
    domain: str = Field(default="general", max_length=100)
    context: str = Field(default="", max_length=10000)
    model_profile_id: str = "profile_default"
    callback_endpoint_code: str | None = None

    @model_validator(mode="after")
    def valid_pair(self):
        if self.source_language == self.target_language:
            raise ValueError("INVALID_LANGUAGE_PAIR")
        total = (
            len(self.input.text)
            if isinstance(self.input, TextInput)
            else sum(len(x.text) + len(x.context) for x in self.input.items)
        )
        if total + len(self.context) > 100000:
            raise ValueError("INPUT_LIMIT_EXCEEDED")
        return self


class SyncRequest(Strict):
    project_id: str
    source_language: SourceLanguage = "auto"
    target_language: Language
    text: str = Field(min_length=1, max_length=1000)
    domain: str = "general"
    model_profile_id: str = "profile_default"


class Span(Strict):
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    quote: str


IssueType = Literal[
    "mistranslation",
    "omission",
    "addition",
    "terminology",
    "negation",
    "condition",
    "sequence",
    "value_relation",
    "entity",
    "reference",
    "inconsistency",
    "language_mismatch",
    "source_ambiguity",
    "protected_content",
    "structure",
    "malformed_output",
]


class Issue(Strict):
    segment_id: str
    type: IssueType
    severity: Literal["critical", "major", "minor"]
    source_span: Span | None = None
    target_span: Span | None = None
    explanation: str = Field(min_length=1)
    evidence_refs: list[str] = []
    suggested_action: str = "repair"


class Review(Strict):
    reviewed_segment_ids: list[str]
    issues: list[Issue]
    style_suggestions: list[str] = []


class Repair(Strict):
    segment_id: str
    text: str = Field(min_length=1)
    issue_refs: list[str]


class TargetRef(Strict):
    translation_id: str
    base_revision: int = Field(ge=1)
    segment_ids: list[str] = Field(min_length=1)


class SourceSpan(Strict):
    start: int = Field(ge=0)
    end: int = Field(ge=0)


class ConversationCreate(Strict):
    project_id: str
    defaults: dict = {}

    @model_validator(mode="after")
    def allowed_defaults(self):
        if set(self.defaults) - {"target_language", "source_language", "domain", "style"}:
            raise ValueError("Unsupported conversation default")
        if self.defaults.get("target_language") not in (None, "zh-CN", "en", "ja"):
            raise ValueError("Invalid target language")
        return self


class ChatRequest(Strict):
    conversation_id: str
    client_message_id: str = Field(min_length=1, max_length=100)
    expected_conversation_version: int = Field(ge=1)
    message: str = Field(min_length=1, max_length=20000)
    source_language: SourceLanguage | None = None
    target_language: Language | None = None
    source_spans: list[SourceSpan] | None = None
    target_ref: TargetRef | None = None
    feedback_scope: Literal["once", "personal", "project", "enterprise"] | None = None


class Plan(Strict):
    intent: Literal["translate", "explain", "retranslate", "feedback", "query", "clarify", "unsupported"]
    source_spans: list[Span] = []
    source_language: SourceLanguage = "auto"
    target_language: Language | None = None
    target_ref: TargetRef | None = None
    instruction: str = ""
    question: str = ""
    feedback_scope: Literal["once", "personal", "project", "enterprise"] | None = None
    feedback_text: str = ""


class Clarification(Strict):
    question_id: str
    message: str = Field(min_length=1, max_length=20000)
    expected_run_version: int
    expected_conversation_version: int


class Reason(Strict):
    reason: str = Field(min_length=1, max_length=1000)
    expected_version: int | None = None
    expected_run_version: int | None = None


class Retranslation(Strict):
    base_revision: int
    segment_ids: list[str] = Field(min_length=1)
    instruction: str = Field(min_length=1, max_length=5000)
    knowledge_mode: Literal["inherit", "latest"] = "inherit"


class Feedback(Strict):
    revision: int
    segment_ids: list[str] = Field(min_length=1)
    category: Literal["term", "sentence", "policy", "context", "source", "format", "style"]
    scope: Literal["once", "personal", "project", "enterprise"]
    before: str
    after: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class Decision(Strict):
    segment_id: str
    segment_revision: int
    action: Literal["replace_translation", "mark_false_positive", "accept_minor", "approve_segment"]
    reason: str = Field(min_length=1)
    issue_id: str | None = None
    evidence_refs: list[str] = []
    replacement_text: str | None = None


class ReviewDecisions(Strict):
    revision: int
    expected_version: int
    decisions: list[Decision] = Field(min_length=1)
    finalize: bool = False


class KnowledgeCreate(Strict):
    project_id: str
    kind: Literal["term", "tm", "policy", "domain_excerpt"]
    scope: Literal["personal", "project", "enterprise"]
    source_language: Language
    target_language: Language
    content: dict
    source_refs: list[str] = Field(min_length=1)
    locked: bool = False
    domain: str = "general"


class ReleaseRequest(Strict):
    project_id: str
    entry_version_refs: list[str] = Field(min_length=1)
    reason: str = Field(min_length=1)


class ProfileRequest(Strict):
    project_id: str
    name: str
    roles: dict
    language_pairs: list[list[str]]


class PublishProfile(Strict):
    expected_version: int
    evaluation_report_id: str
    max_major_segment_rate: float = Field(ge=0, le=1)
    min_auto_coverage: float = Field(ge=0, le=1)
    min_reviewer_recall: float = Field(ge=0, le=1)
    max_reviewer_false_positive_rate: float = Field(ge=0, le=1)
    accepted_language_pairs: list[list[str]]
    risk_scope: list[str]


class Explanation(Strict):
    text: str = Field(min_length=1)
    evidence_refs: list[str]
