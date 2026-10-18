from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

FieldType = Literal["text", "email", "tel", "url", "number", "date", "textarea", "select", "custom_select",
                    "radio", "checkbox", "checkbox_group", "file", "password", "unknown"]
MappingSource = Literal["adapter", "learned", "rule", "agent", "llm", "question", "profile_todo", "human", "none"]


class Option(BaseModel):
    value: str | None = None
    label: str
    selector: str | None = None


class FormField(BaseModel):
    field_id: str                 # stable key: automation id > name > id > generated jb id
    jb_id: str                    # data-jb-id stamped by the inspector (valid for the current page state)
    frame_index: int = 0
    frame_url: str | None = None
    selector: str
    tag: str
    type: FieldType
    label: str
    label_source: str = "none"
    name: str | None = None
    html_id: str | None = None
    automation_id: str | None = None
    container_automation_id: str | None = None
    placeholder: str | None = None
    required: bool = False
    options: list[Option] = Field(default_factory=list)
    accept: str | None = None
    multiple: bool = False
    max_length: int | None = None
    current_value: Any = None
    in_shadow: bool = False

    @property
    def label_normalized(self) -> str:
        from jobbuddy.engine.field_mapper import normalize_label

        return normalize_label(self.label)


class Button(BaseModel):
    text: str
    jb_id: str
    selector: str
    frame_index: int = 0
    automation_id: str | None = None
    type: str | None = None
    disabled: bool = False
    kind: Literal["submit", "next", "back", "other"] = "other"


class PageSnapshot(BaseModel):
    url: str
    title: str = ""
    fields: list[FormField] = Field(default_factory=list)
    buttons: list[Button] = Field(default_factory=list)
    error_texts: list[str] = Field(default_factory=list)
    unhandled_required: list[str] = Field(default_factory=list)  # required labels with no recognised control


class FieldMapping(BaseModel):
    field_id: str
    value: str | bool | list[str] | None = None
    confidence: float = Field(default=0.0, ge=0, le=1)
    source: MappingSource = "none"
    profile_key: str | None = None
    file_kind: Literal["resume", "cover_letter"] | None = None
    suggestions: list[str] = Field(default_factory=list)
    reason: str | None = None


class Blocker(BaseModel):
    kind: Literal["captcha", "login", "email_code", "mfa", "rate_limited"]
    evidence: str


class StepRecord(BaseModel):
    index: int
    url: str
    fields: list[FormField]
    mappings: list[FieldMapping]
    errors: list[str] = Field(default_factory=list)
    action: str = ""


class FillResult(BaseModel):
    status: Literal["awaiting_approval", "paused", "failed", "submitted"]
    steps: list[StepRecord] = Field(default_factory=list)
    low_confidence: list[dict[str, Any]] = Field(default_factory=list)
    blockers: list[Blocker] = Field(default_factory=list)
    screenshot_path: str | None = None
    final_url: str | None = None
    error: str | None = None
    submit_button: Button | None = None
    confirmation_text: str | None = None
    required_empty: list[str] = Field(default_factory=list)  # required fields still empty after filling
    # After a Submit click: True = confirmed, False = evidence it did NOT go through, None = unknown.
    likely_submitted: bool | None = None
    submit_evidence: dict[str, Any] = Field(default_factory=dict)  # POST status/code, errors (no PII)

    def filled_state(self) -> dict[str, Any]:
        """Serializable state persisted on the application (no file contents, no screenshots)."""
        return {
            "final_url": self.final_url,
            "steps": [{"index": s.index, "url": s.url, "action": s.action, "errors": s.errors,
                       "fields": [{"field_id": f.field_id, "label": f.label, "type": f.type, "required": f.required,
                                   "options": [o.label for o in f.options][:50]} for f in s.fields],
                       "mappings": [m.model_dump() for m in s.mappings]} for s in self.steps],
        }
