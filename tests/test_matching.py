from datetime import datetime, timedelta, timezone

import pytest

from jobbuddy.config import MatchingConfig
from jobbuddy.fetchers.base import JobPosting
from jobbuddy.llm.client import BudgetExceeded
from jobbuddy.matching.filters import apply_filters
from jobbuddy.matching.rubric import RubricResult, score_job
from jobbuddy.matching.similarity import embed_profile, job_text

CFG = MatchingConfig(title_keywords_any=["product solution architect", "technical product manager", "product manager"],
                     title_exclude=["intern"], locations_any=["san francisco", "bay area"],
                     excluded_keywords=["ts/sci"], max_posting_age_days=30, min_salary=150000)


def job(**kw):
    base = dict(source="greenhouse", external_job_id="1", title="Senior Product Solution Architect",
                location="San Francisco, CA", description="clinical trial software", company_name="Acme")
    base.update(kw)
    return JobPosting(**base)


def test_filters_pass_and_fail_reasons(profile):
    assert apply_filters(job(), CFG, profile).passed
    r = apply_filters(job(title="Product Manager Intern"), CFG, profile)
    assert not r.passed and any("intern" in x for x in r.reasons)
    assert not apply_filters(job(location="Boston, MA"), CFG, profile).passed
    assert apply_filters(job(location="Remote - US", remote=True), CFG, profile).passed
    assert not apply_filters(job(location="Remote - UK", remote=True), CFG, profile).passed
    assert not apply_filters(job(salary_max=120000), CFG, profile).passed
    old = datetime.now(timezone.utc) - timedelta(days=45)
    assert not apply_filters(job(posted_at=old), CFG, profile).passed
    assert not apply_filters(job(description="Requires TS/SCI clearance"), CFG, profile).passed
    assert not apply_filters(job(title="Staff Accountant"), CFG, profile).passed


def test_embedding_similarity_ranks_relevant_job_higher(profile, embedder):
    pv = embed_profile(embedder, profile)
    good = job(description="Lead AI agent adoption and LLM application development for clinical trial software, "
                           "KNIME data pipelines, JIRA user stories, 21 CFR Part 11 compliance")
    bad = job(title="Line Cook", description="Prepare food in a busy restaurant kitchen, grill and fryer")
    from jobbuddy.embeddings import cosine

    vg, vb = embedder.embed([job_text(good), job_text(bad)])
    assert cosine(pv, vg) > cosine(pv, vb)


def test_llm_rubric_validates_and_clamps(profile, fake_llm_factory):
    llm = fake_llm_factory({"score": '```json\n{"score": 140, "reasons": ["strong domain fit"], "gaps": []}\n```'})
    r = score_job(llm, profile, job())
    assert isinstance(r, RubricResult) and r.score == 100 and r.reasons == ["strong domain fit"]
    # contact details never go to the LLM
    prompt = llm.calls[0]["messages"][1]["content"]
    assert profile.personal.email not in prompt and profile.personal.phone not in prompt


def test_llm_rubric_retries_on_bad_json(profile, fake_llm_factory):
    replies = iter(["not json at all", '{"score": 77, "reasons": ["ok"]}'])
    llm = fake_llm_factory({"score": lambda m: next(replies)})
    assert score_job(llm, profile, job()).score == 77
    assert len(llm.calls) == 2


def test_budget_cap(profile, fake_llm_factory):
    llm = fake_llm_factory({"score": '{"score": 50}'}, budget=10)
    with pytest.raises(BudgetExceeded):
        score_job(llm, profile, job())
