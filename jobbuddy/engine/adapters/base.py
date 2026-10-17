"""ATS adapters: deterministic, per-platform knowledge (field keys, navigation buttons, confirmations)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class ATSAdapter:
    name: str = "generic"
    url_patterns: tuple[str, ...] = ()
    # exact control keys (automation id / name / id) -> profile key or special token
    key_rules: dict[str, str] = field(default_factory=dict)
    # regex on control key -> profile key (for indexed names like urls[LinkedIn])
    key_regex_rules: list[tuple[str, str]] = field(default_factory=list)
    next_automation_ids: set[str] = field(default_factory=set)
    submit_automation_ids: set[str] = field(default_factory=set)
    confirmation_patterns: tuple[str, ...] = (
        r"thank you for (applying|your application)", r"application (has been )?(received|submitted)",
        r"we('ve| have) received your application",
    )

    def matches(self, url: str) -> bool:
        return any(re.search(p, url or "", re.I) for p in self.url_patterns)

    def rule_for_key(self, key: str | None) -> str | None:
        if not key:
            return None
        if key in self.key_rules:
            return self.key_rules[key]
        for pat, target in self.key_regex_rules:
            if re.search(pat, key, re.I):
                return target
        return None


def _registry() -> list[ATSAdapter]:
    from jobbuddy.engine.adapters.ashby import ADAPTER as ashby
    from jobbuddy.engine.adapters.bamboohr import ADAPTER as bamboohr
    from jobbuddy.engine.adapters.generic import ADAPTER as generic
    from jobbuddy.engine.adapters.greenhouse import ADAPTER as greenhouse
    from jobbuddy.engine.adapters.lever import ADAPTER as lever
    from jobbuddy.engine.adapters.workday import ADAPTER as workday

    return [greenhouse, lever, workday, ashby, bamboohr, generic]


def get_adapter(name: str | None) -> ATSAdapter:
    for a in _registry():
        if a.name == name:
            return a
    return _registry()[-1]


def detect_adapter(url: str, hint: str | None = None) -> ATSAdapter:
    if hint and hint not in ("generic", None) and not hint.endswith("_alert"):
        return get_adapter(hint)
    for a in _registry():
        if a.matches(url):
            return a
    return get_adapter("generic")
