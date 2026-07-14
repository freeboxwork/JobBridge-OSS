from __future__ import annotations

from pathlib import Path

from jobbridge_inference.core import JobBridgeInferenceService


ROOT = Path(__file__).resolve().parents[1]


def test_open_model_and_demo_data_are_present() -> None:
    assert (
        ROOT
        / "Models"
        / "lightgbm_jobseeker_preference_v1"
        / "jobbridge_preference_model.joblib"
    ).is_file()
    assert (ROOT / "Data" / "demo" / "job_postings_normalized.csv").is_file()


def test_health_loads_open_model_and_synthetic_jobs(monkeypatch) -> None:
    monkeypatch.setenv("JOBBRIDGE_LIVE_JOBS_ENABLED", "false")
    monkeypatch.setenv("JOBBRIDGE_RECOMMENDATION_LOGGING_ENABLED", "false")
    service = JobBridgeInferenceService()
    health = service.health()
    assert health["ok"] is True
    assert health["modelVersion"] == "lightgbm_jobseeker_preference_v1"
    assert health["postingSource"] == "static_csv"
    assert health["postingRows"] == 10
    assert health["postingRankerAvailable"] is False


def test_recommendation_returns_explainable_results(monkeypatch) -> None:
    monkeypatch.setenv("JOBBRIDGE_LIVE_JOBS_ENABLED", "false")
    monkeypatch.setenv("JOBBRIDGE_RECOMMENDATION_LOGGING_ENABLED", "false")
    service = JobBridgeInferenceService()
    result = service.recommend(
        {
            "modelFeatures": {
                "sido": "경기",
                "sigungu": "수원시",
                "age": 32,
                "disability_type": "청각장애",
                "severity": "경증",
            },
            "scoringPreferences": {
                "desired_job_class": "경영·행정·사무직",
                "desired_wage": "월 220~260만원",
            },
        }
    )
    assert result["modelVersion"] == "lightgbm_jobseeker_preference_v1"
    assert result["predictedJobClasses"]
    assert result["recs"]
    assert result["recs"][0]["title"]
    assert "diagnostics" in result
