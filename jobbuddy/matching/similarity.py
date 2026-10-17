"""Stage 2: embedding similarity between Mike's profile and each job description."""
from __future__ import annotations

from typing import Any

from jobbuddy.embeddings import Embedder
from jobbuddy.profile import Profile


def profile_text(profile: Profile) -> str:
    bullets = " ".join(b.text for _, _, b in profile.iter_bullets())
    return " ".join([profile.headline or "", profile.summary or "", " ".join(profile.targets.titles),
                     " ".join(profile.skills), bullets])


def job_text(job: Any, max_chars: int = 4000) -> str:
    return f"{job.title}\n{job.company_name or ''}\n{job.location or ''}\n{(job.description or '')[:max_chars]}"


def embed_profile(embedder: Embedder, profile: Profile) -> list[float]:
    return embedder.embed([profile_text(profile)])[0]
