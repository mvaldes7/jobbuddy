"""Field mapper: deterministic layers first, LLM last.

Order per field (first hit wins):
  1. human override (edits from the approval flow)                     confidence 1.00
  2. ATS adapter key rule (exact automation-id / name / id)            confidence 0.98
  3. learned mapping (form_field_mappings, by normalized label)        stored confidence
  4. generic label regex rules                                         0.90 (x option-match score)
  5. on-box agent answers (field_questions, by field_id / label)       0.85 (x option-match score)
  6. open-ended question answered at tailoring time (applications.answers)  0.85
  7. optional model fallback (library only; CI passes llm=None)        capped at 0.80
Anything still empty becomes a field_question for the agent (see pipeline.queue_field_questions).
Sensitive fields (authorization, sponsorship, salary, EEO, background) never come from the LLM.
TODO values in profile.yaml produce an empty value with confidence 0 and a review item.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Callable, Iterable, Literal

from pydantic import BaseModel, Field, ValidationError

from jobbuddy.engine.adapters.base import ATSAdapter
from jobbuddy.engine.models import FieldMapping, FormField, Option
from jobbuddy.llm.client import LLMClient, chat_json
from jobbuddy.profile import Profile, is_todo
from jobbuddy.tailoring.questions import sensitive_key

log = logging.getLogger(__name__)


def normalize_label(label: str) -> str:
    s = (label or "").lower()
    s = re.sub(r"\(\s*(optional|required)\s*\)", " ", s)
    s = s.replace("*", " ")
    s = re.sub(r"[^a-z0-9?/&+ ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


# (label regex, profile key | special token)
LABEL_RULES: list[tuple[str, str]] = [
    # Checked first: these labels often mention other keywords ("...LinkedIn, website, referral?")
    (r"how did you (hear|find|learn)|where did you (hear|find|see)|^source$|referral source|referred by(?!.*\b(employee|their name|name of|who referred))", "application_answers.how_did_you_hear"),
    (r"phone (device )?type|type of phone", "__unmapped__"),
    (r"^(legal )?first name|^given name|^first$", "personal.first_name"),
    (r"^(legal )?last name|^surname|^family name|^last$", "personal.last_name"),
    (r"preferred (first )?name|nickname|name you go by", "personal.preferred_name"),
    (r"^middle (name|initial)", "personal.middle_initial"),
    (r"^(full |legal |your )?(full )?name$|^full legal name", "personal.full_name"),
    (r"e ?mail", "personal.email"),
    (r"phone|mobile|cell", "personal.phone"),
    (r"linkedin", "personal.linkedin"),
    (r"website|portfolio|personal (site|url)|github", "personal.website"),
    (r"cover letter", "__cover_letter__"),
    (r"resume|\bcv\b|curriculum vitae", "__file_resume__"),
    (r"^city$|current city|city of residence", "personal.location.city"),
    (r"^state\b|province|^region$", "personal.location.state"),
    (r"^country( of residence)?$|country/region", "personal.location.country"),
    (r"zip|postal", "personal.location.postal_code"),
    (r"^(current )?location$|where are you (currently )?(located|based)|^location city$", "__location__"),
    (r"current (company|employer)|most recent (company|employer)|^company$|^organization$", "experience.0.company"),
    (r"current (job )?title|most recent (job )?title|^title$", "experience.0.roles.0.title"),
    (r"years of (relevant |professional )?experience|how many years", "years_experience"),
    (r"^(school|university|college)( name)?$", "education.0.school"),
    (r"^degree", "education.0.degree"),
    (r"(legally )?authori[sz]ed to work|eligible to work|work authori[sz]ation|legally (able|permitted)", "application_answers.work_authorization"),
    (r"sponsor", "application_answers.requires_sponsorship"),
    (r"salary|compensation|desired pay|pay expectation|expected pay", "application_answers.salary_expectation_text"),
    (r"start date|when can you start|notice period|available to start|date available", "application_answers.start_date"),
    (r"relocat", "application_answers.willing_to_relocate"),
    (r"hispanic|latino", "application_answers.eeo.hispanic_latino"),
    (r"gender|^sex$", "application_answers.eeo.gender"),
    (r"race|ethnicit", "application_answers.eeo.race_ethnicity"),
    (r"veteran", "application_answers.eeo.veteran_status"),
    (r"disabilit", "application_answers.eeo.disability_status"),
    (r"sexual orientation", "application_answers.eeo.sexual_orientation"),
    (r"pronoun", "application_answers.eeo.pronouns"),
    (r"previously (worked|been employed)|former employee|worked (here|for us) before", "__previous_worker__"),
    (r"privacy|i (agree|acknowledge|consent|certify)|terms (and|&) conditions|^acknowledg", "__consent__"),
]

DECLINE_PHRASES = ("decline", "prefer not", "don t wish", "do not wish", "not wish to", "choose not", "don t want",
                   "do not want", "not to say", "not to answer", "not to disclose", "rather not", "wish to answer")
CAREERS_PHRASES = ("company website", "careers page", "career site", "company careers", "corporate website",
                   "website", "career page", "job board")

OPEN_ENDED_HINT = re.compile(r"\?|why|describe|tell us|explain|what (excites|interests|makes)|share|how would you", re.I)


class LLMFieldValue(BaseModel):
    field_id: str
    value: str | bool | list[str] | None
    confidence: float = Field(ge=0, le=1)
    source: Literal["profile", "inferred", "unknown"] = "inferred"


class LLMFieldBatch(BaseModel):
    mappings: list[LLMFieldValue]


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", (s or "").lower())).strip()


def yes_no(value: Any) -> str | None:
    v = _norm(str(value))
    if re.match(r"^(yes|y|true|i am|authorized|i do)\b", v):
        return "yes"
    if re.match(r"^(no|n|false|i am not|not)\b", v):
        return "no"
    return None


def match_option(target: Any, options: list[Option]) -> tuple[Option | None, float]:
    """Pick the option that best expresses `target`. Returns (option, score 0..1)."""
    opts = [o for o in options if (o.label or "").strip() and not re.match(r"^(select|choose|--|please select)", o.label.strip(), re.I)]
    if not opts or target is None:
        return None, 0.0
    t = _norm(str(target))
    for o in opts:
        if _norm(o.label) == t or (o.value and _norm(o.value) == t):
            return o, 1.0
    if any(p in t for p in DECLINE_PHRASES):
        for o in opts:
            if any(p in _norm(o.label) for p in DECLINE_PHRASES) or "self identify" in _norm(o.label):
                return o, 0.95
    yn = yes_no(target)
    if yn:
        for o in opts:
            if yes_no(o.label) == yn:
                return o, 0.9
    if "careers page" in t or "company website" in t:
        for phrase in CAREERS_PHRASES:
            for o in opts:
                if phrase in _norm(o.label):
                    return o, 0.85
    best, score = None, 0.0
    for o in opts:
        lab = _norm(o.label)
        s = SequenceMatcher(None, t, lab).ratio()
        if t and (t in lab or lab in t):
            s = max(s, 0.8)
        if s > score:
            best, score = o, s
    return (best, score) if score >= 0.6 else (None, score)


@dataclass
class MapperContext:
    profile: Profile
    adapter: ATSAdapter
    job_title: str = ""
    company: str = ""
    learned: dict[str, Any] = field(default_factory=dict)          # label_normalized -> FormFieldMapping row
    overrides: dict[str, Any] = field(default_factory=dict)        # field_id or label_normalized -> value
    cover_letter_text: Callable[[], str | None] | None = None      # lazily generates the letter
    has_cover_letter_file: bool = False
    answer_question: Callable[[str, int | None], Any] | None = None  # -> tailoring.questions.Answer
    llm: LLMClient | None = None
    llm_confidence_cap: float = 0.8
    agent_answers: dict[str, Any] = field(default_factory=dict)    # field_id or label_normalized -> answer
    agent_answer_confidence: float = 0.85
    how_did_you_hear_default: str = "Company careers page"


def needs_cover_letter(fields: Iterable[FormField]) -> bool:
    for f in fields:
        hay = " ".join(filter(None, [f.label, f.name, f.html_id, f.automation_id])).lower()
        if "cover" in hay and "letter" in hay or re.search(r"cover_?letter|coverletter", hay):
            return True
    return False


class FieldMapper:
    def __init__(self, ctx: MapperContext) -> None:
        self.ctx = ctx
        self.used_learned: list[Any] = []

    # ---------- value resolution ----------
    def _location(self) -> str | None:
        loc = self.ctx.profile.personal.location
        parts = [p for p in [loc.city, loc.state] if p and not is_todo(p)]
        return ", ".join(parts) or None

    def _resolve_key(self, f: FormField, key: str, base_conf: float, source: str) -> FieldMapping:
        p = self.ctx.profile
        if key == "__file_resume__":
            if f.type == "file":
                return FieldMapping(field_id=f.field_id, value="__resume__", file_kind="resume",
                                    confidence=base_conf, source=source, profile_key="documents.resume")
            return FieldMapping(field_id=f.field_id, value=None, confidence=0.3, source=source,
                                reason="resume text field: paste manually if needed")
        if key in ("__cover_letter__", "__file_cover_letter__", "__text_cover_letter__"):
            if f.type == "file" and key != "__text_cover_letter__":
                return FieldMapping(field_id=f.field_id, value="__cover_letter__", file_kind="cover_letter",
                                    confidence=base_conf, source=source, profile_key="documents.cover_letter")
            text = self.ctx.cover_letter_text() if self.ctx.cover_letter_text else None
            if not text:
                return FieldMapping(field_id=f.field_id, value=None, confidence=0.0, source=source,
                                    reason="cover letter could not be generated")
            if f.max_length and len(text) > f.max_length:
                text = text[: f.max_length]
            return FieldMapping(field_id=f.field_id, value=text, confidence=min(base_conf, 0.85), source=source,
                                profile_key="generated.cover_letter", reason="generated cover letter: review tone")
        if key == "__unmapped__":
            return FieldMapping(field_id=f.field_id, value=None, confidence=0.0, source=source,
                                suggestions=[o.label for o in f.options][:8], reason="choose manually")
        if key == "__location__":
            v = self._location()
            return self._finish(f, v, base_conf, source, "personal.location")
        if key == "__previous_worker__":
            return self._finish(f, "No", 0.6, source, None,
                                reason="assumed 'No' (not on resume as a former employee): confirm")
        if key == "__consent__":
            if f.type in ("checkbox",):
                return FieldMapping(field_id=f.field_id, value=True, confidence=0.6, source=source,
                                    reason="consent/acknowledgement: Mike must agree personally")
            return self._finish(f, None, 0.0, source, None, reason="consent question: answer manually")
        if key == "application_answers.how_did_you_hear":
            val = p.get(key) or self.ctx.how_did_you_hear_default
            return self._finish(f, val, base_conf, source, key)
        val = p.get(key)
        if key == "application_answers.eeo.race_ethnicity" and f.options and \
                yes_no(p.get("application_answers.eeo.hispanic_latino") or "") == "yes":
            # Combined race/ethnicity lists (Ashby, Lever) offer "Hispanic or Latino" next to "White (Not Hispanic
            # or Latino)": never pick a "Not Hispanic" option for someone whose profile says Hispanic/Latino = Yes.
            hisp = next((o for o in f.options if re.search(r"hispanic|latin", o.label, re.I)
                         and not re.search(r"\bnot\b", o.label, re.I)), None)
            if hisp is not None:
                return FieldMapping(field_id=f.field_id, value=[hisp.label] if f.type == "checkbox_group" else hisp.label,
                                    confidence=min(base_conf, 0.7), source=source, profile_key=key,
                                    reason="combined race/ethnicity list: chose Hispanic/Latino per profile; confirm")
        if key == "years_experience" and val is not None:
            val = str(val)
        if is_todo(val):
            return FieldMapping(field_id=f.field_id, value=None, confidence=0.0, source="profile_todo",
                                profile_key=key, suggestions=[str(val)],
                                reason=f"{key} is a TODO in profile.yaml")
        if val is None or val == "":
            return FieldMapping(field_id=f.field_id, value=None, confidence=0.0, source=source, profile_key=key,
                                reason=f"no value for {key} in profile")
        return self._finish(f, val, base_conf, source, key)

    def _finish(self, f: FormField, value: Any, conf: float, source: str, key: str | None,
                reason: str | None = None) -> FieldMapping:
        if value is None:
            return FieldMapping(field_id=f.field_id, value=None, confidence=0.0, source=source,  # type: ignore[arg-type]
                                profile_key=key, reason=reason or "no value")
        if f.type in ("select", "custom_select", "radio", "checkbox_group") and f.options:
            opt, score = match_option(value, f.options)
            if opt is None:
                return FieldMapping(field_id=f.field_id, value=None, confidence=0.0, source=source,  # type: ignore[arg-type]
                                    profile_key=key, suggestions=[o.label for o in f.options][:8],
                                    reason=f"no option matches '{str(value)[:60]}'")
            out = [opt.label] if f.type == "checkbox_group" else opt.label
            return FieldMapping(field_id=f.field_id, value=out, confidence=round(conf * score, 3),  # type: ignore[arg-type]
                                source=source, profile_key=key, reason=reason)
        if f.type == "checkbox":
            yn = yes_no(value)
            return FieldMapping(field_id=f.field_id, value=(yn == "yes") if yn else bool(value),  # type: ignore[arg-type]
                                confidence=conf, source=source, profile_key=key, reason=reason)
        if isinstance(value, (list, dict)):
            value = json.dumps(value) if isinstance(value, dict) else ", ".join(map(str, value))
        return FieldMapping(field_id=f.field_id, value=str(value), confidence=conf, source=source,  # type: ignore[arg-type]
                            profile_key=key, reason=reason)

    # ---------- layers ----------
    def _override(self, f: FormField) -> FieldMapping | None:
        ov = self.ctx.overrides
        for k in (f.field_id, f.label_normalized):
            if k in ov:
                m = self._finish(f, ov[k], 1.0, "human", None, reason="human edit")
                if m.value is None and ov[k] not in (None, ""):
                    m = FieldMapping(field_id=f.field_id, value=ov[k], confidence=1.0, source="human")
                return m
        return None

    def _adapter(self, f: FormField) -> FieldMapping | None:
        for key in (f.automation_id, f.name, f.html_id, f.container_automation_id and
                    f.container_automation_id.removeprefix("formField-")):
            target = self.ctx.adapter.rule_for_key(key)
            if target:
                return self._resolve_key(f, target, 0.98, "adapter")
        return None

    def _learned(self, f: FormField) -> FieldMapping | None:
        row = self.ctx.learned.get(f.label_normalized)
        if row is None:
            return None
        self.used_learned.append(row)
        if row.profile_key:
            m = self._resolve_key(f, row.profile_key, float(row.confidence), "learned")
        else:
            m = self._finish(f, row.static_value, float(row.confidence), "learned", None)
        return m

    def _agent(self, f: FormField) -> FieldMapping | None:
        aa = self.ctx.agent_answers
        for k in (f.field_id, f.label_normalized):
            if k and k in aa:
                m = self._finish(f, aa[k], self.ctx.agent_answer_confidence, "agent", None,
                                 reason="answered by on-box agent")
                return m
        return None

    def _rule(self, f: FormField) -> FieldMapping | None:
        lab = f.label_normalized
        if not lab:
            return None
        for pat, key in LABEL_RULES:
            if re.search(pat, lab):
                # Long free-text questions that merely mention a keyword are not simple fields.
                if f.type == "textarea" and key.startswith("personal.") and len(lab) > 40:
                    continue
                return self._resolve_key(f, key, 0.9, "rule")
        return None

    def _question(self, f: FormField) -> FieldMapping | None:
        if f.type not in ("textarea", "text") or not self.ctx.answer_question:
            return None
        if f.type == "text" and not OPEN_ENDED_HINT.search(f.label or ""):
            return None
        if len(f.label or "") < 12:
            return None
        ans = self.ctx.answer_question(f.label, f.max_length)
        if ans is None:
            return None  # unknown: becomes a field_question for the agent
        return FieldMapping(field_id=f.field_id, value=ans.text, confidence=float(ans.confidence),
                            source="question" if ans.source in ("llm", "agent") else "profile_todo"
                            if ans.text is None else "rule",
                            reason="; ".join(ans.notes) or None)

    def map_fields(self, fields: list[FormField]) -> list[FieldMapping]:
        results: dict[str, FieldMapping] = {}
        pending: list[FormField] = []
        for f in fields:
            if f.type == "password":
                results[f.field_id] = FieldMapping(field_id=f.field_id, value=None, confidence=0.0,
                                                   reason="password fields are never filled")
                continue
            m = self._override(f) or self._adapter(f) or self._learned(f) or self._rule(f) or self._agent(f)
            if m is None and sensitive_key(f.label or ""):
                m = FieldMapping(field_id=f.field_id, value=None, confidence=0.0, source="none",
                                 reason="sensitive question: answer manually",
                                 suggestions=[o.label for o in f.options][:8])
            if m is None:
                try:
                    m = self._question(f)
                except Exception as exc:
                    log.warning("question answering failed: %s", type(exc).__name__)
                    m = None
            if m is None:
                pending.append(f)
            else:
                results[f.field_id] = m
        if pending:
            for m in self._llm_fallback(pending):
                results[m.field_id] = m
        return [results.get(f.field_id) or FieldMapping(field_id=f.field_id, reason="unmapped") for f in fields]

    def _llm_fallback(self, fields: list[FormField]) -> list[FieldMapping]:
        if self.ctx.llm is None:
            return [FieldMapping(field_id=f.field_id, reason="unmapped (no LLM configured)",
                                 suggestions=[o.label for o in f.options][:8]) for f in fields]
        from jobbuddy.matching.rubric import profile_brief

        p = self.ctx.profile
        safe_facts = {
            "name": p.personal.full_name, "city": p.personal.location.city, "linkedin": p.personal.linkedin,
            **profile_brief(p),
        }
        spec = [{"field_id": f.field_id, "label": f.label, "type": f.type, "required": f.required,
                 "options": [o.label for o in f.options][:40]} for f in fields]
        messages = [
            {"role": "system", "content": (
                "Map job-application form fields to the candidate's facts. Use ONLY the facts given. "
                "For select/radio fields the value MUST be one of the listed options, verbatim. "
                "If the facts do not answer a field, return value null and confidence 0. Never guess legal, "
                "salary, demographic or authorization answers. Reply JSON: "
                '{"mappings": [{"field_id": str, "value": str|bool|list|null, "confidence": 0..1, '
                '"source": "profile"|"inferred"|"unknown"}]}')},
            {"role": "user", "content": json.dumps({"job": {"title": self.ctx.job_title, "company": self.ctx.company},
                                                    "facts": safe_facts, "fields": spec}, ensure_ascii=False)},
        ]
        try:
            batch = chat_json(self.ctx.llm, messages, LLMFieldBatch, purpose="field_map", max_tokens=900)
        except (ValueError, ValidationError, Exception) as exc:
            log.warning("LLM field mapping failed: %s", type(exc).__name__)
            return [FieldMapping(field_id=f.field_id, reason="LLM mapping failed") for f in fields]
        by_id = {f.field_id: f for f in fields}
        out: dict[str, FieldMapping] = {}
        for item in batch.mappings:
            f = by_id.get(item.field_id)
            if f is None:
                continue  # hallucinated field id
            if sensitive_key(f.label or ""):
                out[f.field_id] = FieldMapping(field_id=f.field_id, reason="sensitive: LLM answer discarded")
                continue
            conf = min(item.confidence, self.ctx.llm_confidence_cap)
            if item.source == "unknown" or item.value in (None, ""):
                out[f.field_id] = FieldMapping(field_id=f.field_id, confidence=0.0, source="llm",
                                               reason="LLM could not answer",
                                               suggestions=[o.label for o in f.options][:8])
                continue
            m = self._finish(f, item.value, conf, "llm", None, reason=f"LLM ({item.source})")
            out[f.field_id] = m
        for f in fields:
            out.setdefault(f.field_id, FieldMapping(field_id=f.field_id, reason="LLM returned nothing"))
        return list(out.values())


def low_confidence_items(fields: list[FormField], mappings: list[FieldMapping], threshold: float) -> list[dict[str, Any]]:
    """Fields to show Mike in the approval message, each with suggestions."""
    by_id = {m.field_id: m for m in mappings}
    items = []
    for f in fields:
        m = by_id.get(f.field_id)
        if m is None:
            continue
        empty = m.value in (None, "", [])
        if m.confidence >= threshold and not empty:
            continue
        if empty and not f.required and m.source in ("none",) and not m.suggestions:
            continue  # optional field we simply leave blank
        sugg = list(m.suggestions)
        if not sugg and f.options:
            sugg = [o.label for o in f.options][:8]
        items.append({"field_id": f.field_id, "label": f.label, "type": f.type, "required": f.required,
                      "value": m.value if not isinstance(m.value, str) else m.value[:200],
                      "confidence": m.confidence, "source": m.source, "reason": m.reason, "suggestions": sugg})
    return items
