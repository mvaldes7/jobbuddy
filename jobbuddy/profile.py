"""Candidate profile: the single source of truth for every claim jobbuddy makes.

Load order: CANDIDATE_PROFILE_JSON env var (CI secret) -> profile/profile.yaml.
Values beginning with "TODO" are unknown and must never be submitted.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterator

import yaml
from pydantic import BaseModel, ConfigDict, Field

from jobbuddy.config import PROJECT_ROOT

TODO_PREFIX = "TODO"


def is_todo(value: Any) -> bool:
    return isinstance(value, str) and value.strip().upper().startswith(TODO_PREFIX)


class _Loose(BaseModel):
    model_config = ConfigDict(extra="allow")


class Location(_Loose):
    city: str | None = None
    state: str | None = None
    country: str | None = None
    postal_code: str | None = None


class Personal(_Loose):
    full_name: str
    first_name: str
    last_name: str
    middle_initial: str | None = None
    preferred_name: str | None = None
    email: str
    phone: str | None = None
    linkedin: str | None = None
    website: str | None = None
    location: Location = Field(default_factory=Location)


class Bullet(_Loose):
    id: str
    text: str


class Role(_Loose):
    title: str
    official_title: str | None = None
    start: str | None = None
    end: str | None = None
    note: str | None = None
    summary: str | None = None
    bullets: list[Bullet] = Field(default_factory=list)
    also_held: list[dict[str, Any]] = Field(default_factory=list)


class Experience(_Loose):
    company: str
    location: str | None = None
    start: str | None = None
    end: str | None = None
    roles: list[Role] = Field(default_factory=list)


class Education(_Loose):
    degree: str
    school: str
    concentration: str | None = None
    minor: str | None = None
    location: str | None = None
    year: int | None = None
    notes: str | None = None


class Certification(_Loose):
    name: str
    issuer: str | None = None
    year: int | None = None


class Voice(_Loose):
    sample_path: str | None = None
    sample_text: str | None = None
    personal_hook: str | None = None
    traits: list[str] = Field(default_factory=list)


class EEO(_Loose):
    gender: str = "Decline to answer"
    race_ethnicity: str = "Decline to answer"
    hispanic_latino: str = "Decline to answer"
    veteran_status: str = "Decline to answer"
    disability_status: str = "Decline to answer"
    sexual_orientation: str = "Decline to answer"
    pronouns: str = "Decline to answer"


class ApplicationAnswers(_Loose):
    work_authorization: str | None = None
    requires_sponsorship: str | None = None
    salary_floor: str | int | None = None
    salary_expectation_text: str | None = None
    start_date: str | None = None
    willing_to_relocate: str | None = None
    how_did_you_hear: str | None = None
    eeo_reviewed: bool = False
    eeo: EEO = Field(default_factory=EEO)


class Targets(_Loose):
    titles_confirmed: bool = False
    titles: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    remote_ok: bool = True


class Profile(_Loose):
    schema_version: int = 1
    personal: Personal
    headline: str | None = None
    years_experience: int | None = None
    summary: str | None = None
    experience: list[Experience] = Field(default_factory=list)
    education: list[Education] = Field(default_factory=list)
    certifications: list[Certification] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    voice: Voice = Field(default_factory=Voice)
    application_answers: ApplicationAnswers = Field(default_factory=ApplicationAnswers)
    targets: Targets = Field(default_factory=Targets)

    # ---------- fact helpers ----------
    def iter_bullets(self) -> Iterator[tuple[Experience, Role, Bullet]]:
        for exp in self.experience:
            for role in exp.roles:
                for b in role.bullets:
                    yield exp, role, b

    def bullet_by_id(self) -> dict[str, Bullet]:
        return {b.id: b for _, _, b in self.iter_bullets()}

    def fact_corpus(self) -> str:
        """All verifiable resume text concatenated: the validator checks claims against this."""
        parts: list[str] = [self.headline or "", self.summary or "", str(self.years_experience or "")]
        for exp in self.experience:
            parts += [exp.company, exp.location or ""]
            for role in exp.roles:
                parts += [role.title, role.official_title or "", role.note or "", role.summary or ""]
                parts += [str(h.get("title", "")) for h in role.also_held]
                parts += [b.text for b in role.bullets]
        for ed in self.education:
            parts += [ed.degree, ed.school, ed.concentration or "", ed.minor or "", ed.notes or "", str(ed.year or "")]
        for c in self.certifications:
            parts += [c.name, c.issuer or "", str(c.year or "")]
        parts += self.skills
        return "\n".join(p for p in parts if p)

    def get(self, dotted: str, default: Any = None) -> Any:
        """Look up 'personal.location.city' style keys. TODO values come back as-is (check is_todo)."""
        node: Any = self
        for part in dotted.split("."):
            if isinstance(node, BaseModel):
                if part in type(node).model_fields:
                    node = getattr(node, part)
                elif node.model_extra and part in node.model_extra:
                    node = node.model_extra[part]
                else:
                    return default
            elif isinstance(node, dict):
                if part not in node:
                    return default
                node = node[part]
            elif isinstance(node, list) and part.isdigit():
                idx = int(part)
                if idx >= len(node):
                    return default
                node = node[idx]
            else:
                return default
        return node

    def todo_fields(self) -> list[str]:
        out: list[str] = []

        def walk(prefix: str, node: Any) -> None:
            if isinstance(node, dict):
                for k, v in node.items():
                    walk(f"{prefix}.{k}" if prefix else k, v)
            elif isinstance(node, list):
                for i, v in enumerate(node):
                    walk(f"{prefix}.{i}", v)
            elif is_todo(node):
                out.append(prefix)

        walk("", self.model_dump())
        if not self.application_answers.eeo_reviewed:
            out.append("application_answers.eeo (eeo_reviewed is false; defaults are 'Decline to answer')")
        if not self.targets.titles_confirmed:
            out.append("targets.titles (titles_confirmed is false)")
        return out

    def current_role(self) -> tuple[Experience, Role] | None:
        for exp in self.experience:
            for role in exp.roles:
                return exp, role
        return None

    def pii_literals(self) -> list[str]:
        """Strings the log filter masks: name, email, phone, LinkedIn, address parts."""
        p = self.personal
        loc = p.location
        vals = [p.full_name, f"{p.first_name} {p.last_name}", p.last_name, p.email, p.phone or "",
                p.linkedin or "", loc.city or "", loc.postal_code or "",
                (p.model_extra or {}).get("address_line1") or ""]
        if p.phone:
            digits = "".join(ch for ch in p.phone if ch.isdigit())
            vals.append(digits)
        return [v for v in vals if v and not is_todo(v)]


def _default_profile_path() -> Path:
    return PROJECT_ROOT / "profile" / "profile.yaml"


def load_profile(path: str | Path | None = None, *, env: dict[str, str] | None = None) -> Profile:
    env = os.environ if env is None else env
    raw_json = env.get("CANDIDATE_PROFILE_JSON")
    if raw_json and path is None:
        return Profile.model_validate(json.loads(raw_json))
    p = Path(path) if path else _default_profile_path()
    if not p.exists():
        raise FileNotFoundError(
            f"No profile found. Set CANDIDATE_PROFILE_JSON or create {p} (see profile/profile.example.yaml)."
        )
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    return Profile.model_validate(data)


def profile_to_json(profile: Profile) -> str:
    return json.dumps(profile.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":"))


def load_style_sample(profile: Profile, session: Any | None = None) -> str:
    """Cover-letter voice sample: inline text -> DB document -> local file. Empty string if none."""
    if profile.voice.sample_text:
        return profile.voice.sample_text.strip()
    if session is not None:
        from jobbuddy.db.repository import latest_document_text

        txt = latest_document_text(session, "cover_letter_sample")
        if txt:
            return txt
    if profile.voice.sample_path:
        sp = Path(profile.voice.sample_path)
        if not sp.is_absolute():
            sp = PROJECT_ROOT / sp
        if sp.exists():
            return sp.read_text(encoding="utf-8").strip()
    return ""
