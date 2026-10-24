from datetime import UTC, datetime

from jobpilot.agents.extract import JobRequirements
from jobpilot.agents.match import (
    WEIGHTS,
    experience_fit,
    hard_filters,
    location_fit,
    weighted_score,
)
from jobpilot.profile.preferences import SearchPreferences

PREFS = SearchPreferences(locations=["Bengaluru"], work_modes=["remote", "hybrid"],
                          exclude_keywords=["clearance"])


def req(**kw: object) -> JobRequirements:
    base: dict = dict(job_id="job_1", task_id="t", created_at=datetime(2026, 10, 24, tzinfo=UTC),
                      required_skills=["Python"], preferred_skills=[],
                      experience_min_years=3, experience_max_years=None, seniority="senior",
                      work_mode="hybrid", location="Bengaluru", role_family="genai_llm",
                      summary="Build LLM apps.", extraction_confidence=0.9)
    return JobRequirements(**{**base, **kw})


def test_weights_exact() -> None:
    assert WEIGHTS == {"skill_fit": 0.30, "experience_fit": 0.25, "project_fit": 0.20,
                       "role_seniority_fit": 0.15, "location_fit": 0.10}


def test_weighted_score_exact() -> None:
    dims = {"skill_fit": 80, "experience_fit": 100, "project_fit": 60,
            "role_seniority_fit": 70, "location_fit": 100}
    # brief listed 80.5; 0.3*80+0.25*100+0.2*60+0.15*70+0.1*100 = 81.5
    assert weighted_score(dims) == 81.5


def test_location_fit_rules() -> None:
    assert location_fit("Bengaluru, India", "hybrid", PREFS) == 100
    assert location_fit("Berlin", "remote", PREFS) == 100
    assert location_fit(None, "hybrid", PREFS) == 60
    assert location_fit("Berlin", "onsite", PREFS) == 0
    assert location_fit("Berlin", "hybrid", PREFS) == 0


def test_experience_fit_rules() -> None:
    assert experience_fit(None, 1.0) == 100
    assert experience_fit(3, 4.0) == 100
    assert experience_fit(5, 3.0) == 50
    assert experience_fit(10, 1.0) == 0


def test_hard_filters() -> None:
    job = {"title": "GenAI Engineer", "location": "Bengaluru", "work_mode": "hybrid"}
    assert hard_filters(job, req(), PREFS, candidate_years=4.0) == []
    assert hard_filters(job, req(seniority="manager"), PREFS, candidate_years=4.0) == ["seniority"]
    assert hard_filters(job, req(seniority="lead"), PREFS, candidate_years=4.0) == []
    far = {**job, "location": "Berlin", "work_mode": "onsite"}
    assert hard_filters(far, req(location="Berlin", work_mode="onsite"), PREFS,
                        candidate_years=4.0) == ["location"]
    assert hard_filters({**job, "title": "Engineer (Clearance required)"}, req(), PREFS,
                        candidate_years=4.0) == ["excluded_keyword"]
    assert hard_filters(job, req(summary="no clearances needed"), PREFS,
                        candidate_years=4.0) == []
    assert hard_filters(job, req(experience_min_years=8), PREFS,
                        candidate_years=4.0) == ["experience_gap"]
    assert hard_filters(job, req(experience_min_years=7.9), PREFS, candidate_years=4.0) == []
