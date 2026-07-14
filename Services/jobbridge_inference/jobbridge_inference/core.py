from __future__ import annotations

import math
import json
import os
import time
import uuid
from collections import Counter
from threading import Lock
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd

from .reference import ReferenceRepository


PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_DIR.parents[2]
DEFAULT_MODEL_PATH = PROJECT_ROOT / "Models" / "lightgbm_jobseeker_preference_v1" / "jobbridge_preference_model.joblib"
DEFAULT_RANKER_MODEL_PATH = PROJECT_ROOT / "Models" / "lightgbm_posting_ranker_v1" / "lightgbm_posting_ranker.joblib"
DEFAULT_DATA_DIR = PROJECT_ROOT / "Data" / "demo"
DEFAULT_LIVE_POSTINGS_PATH = PROJECT_ROOT / "Data" / "processed" / "live_job_postings" / "job_postings_live.json"
DEFAULT_STANDARD_WORKPLACE_PATH = PROJECT_ROOT / "Data" / "05_standard_workplaces" / "standard_workplaces_20251231.csv"
MODEL_VERSION = "lightgbm_jobseeker_preference_v1"
RANKER_MODEL_VERSION = "lightgbm_posting_ranker_v1"
KST = timezone(timedelta(hours=9))
REMOTE_WORK_KEYWORDS = ("재택", "원격", "재택병행", "자택", "비대면")
VISUAL_REVIEW_TERMS = (
    "안내",
    "동선",
    "주차",
    "운전",
    "배송",
    "배달",
    "검수",
    "검사",
    "진열",
    "순찰",
    "경비",
)


def load_project_env() -> None:
    env_path = PROJECT_ROOT / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_project_env()


@dataclass(frozen=True)
class RuntimePaths:
    model_path: Path
    data_dir: Path
    ranker_model_path: Path = DEFAULT_RANKER_MODEL_PATH

    @classmethod
    def from_env(cls) -> "RuntimePaths":
        return cls(
            model_path=Path(os.getenv("JOBBRIDGE_MODEL_PATH", str(DEFAULT_MODEL_PATH))),
            data_dir=Path(os.getenv("JOBBRIDGE_DATA_DIR", str(DEFAULT_DATA_DIR))),
            ranker_model_path=Path(os.getenv("JOBBRIDGE_RANKER_MODEL_PATH", str(DEFAULT_RANKER_MODEL_PATH))),
        )


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def clean_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    if pd.isna(value):
        return None
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    return value


def safe_text(value: Any, fallback: str = "") -> str:
    value = clean_value(value)
    if value is None:
        return fallback
    text = str(value).strip()
    return text if text else fallback


def age_group_for(age: int | float | None) -> str:
    if age is None:
        return ""
    age = int(age)
    if age < 20:
        return "under_20"
    if age >= 70:
        return "70_plus"
    return f"{(age // 10) * 10}s"


def bucket_for_job_class(job_class: str | None) -> str:
    text = job_class or ""
    if any(token in text for token in ("보건", "의료", "사회복지", "종교", "돌봄")):
        return "보건·복지"
    if "정보통신" in text:
        return "IT·데이터"
    if any(token in text for token in ("예술", "디자인", "방송")):
        return "디자인·콘텐츠"
    if any(token in text for token in ("서비스", "돌봄", "음식", "여행", "숙박", "경호", "경비")):
        return "서비스"
    if any(
        token in text
        for token in ("생산", "제조", "설치", "정비", "건설", "채굴", "운전", "운송", "농림어업")
    ):
        return "제조·생산"
    return "사무·행정"


def parse_wage_preference(label: str | None) -> tuple[float | None, float | None]:
    if not label:
        return None, None
    if "180만원 미만" in label:
        return 0, 1_800_000
    if "180~220만원" in label:
        return 1_800_000, 2_200_000
    if "220~260만원" in label:
        return 2_200_000, 2_600_000
    if "260만원 이상" in label:
        return 2_600_000, None
    return None, None


def monthly_wage_equivalent(wage_type: Any, wage_amount: Any) -> float | None:
    amount = clean_value(wage_amount)
    if amount is None:
        return None
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return None
    wage_type_text = safe_text(wage_type)
    if "시급" in wage_type_text:
        return amount * 209
    if "일급" in wage_type_text:
        return amount * 22
    if "연봉" in wage_type_text:
        return amount / 12
    return amount


def wage_matches_preference(wage_type: Any, wage_amount: Any, preference: str | None) -> bool:
    low, high = parse_wage_preference(preference)
    if low is None and high is None:
        return True
    monthly = monthly_wage_equivalent(wage_type, wage_amount)
    if monthly is None:
        return False
    if low is not None and monthly < low:
        return False
    if high is not None and monthly > high:
        return False
    return True


def format_wage(wage_type: Any, wage_amount: Any) -> str:
    wage_type_text = safe_text(wage_type, "임금")
    amount = clean_value(wage_amount)
    if amount is None:
        return "임금 협의"
    try:
        return f"{wage_type_text} {float(amount):,.0f}원"
    except (TypeError, ValueError):
        return f"{wage_type_text} {amount}"


def format_recruit_deadline(value: Any) -> str:
    text = safe_text(value)
    if not text:
        return ""
    try:
        end_date = datetime.fromisoformat(text[:10]).date()
    except ValueError:
        return f"{text} 마감"
    days_left = (end_date - current_service_date()).days
    if days_left < 0:
        return "마감"
    if days_left == 0:
        return "오늘마감"
    if days_left <= 30:
        return f"D-{days_left}"
    return f"{end_date.isoformat()} 마감"


def format_recruit_status(value: Any) -> str:
    text = safe_text(value)
    if not text:
        return "채용중"
    try:
        end_date = datetime.fromisoformat(text[:10]).date()
    except ValueError:
        return "채용중"
    return "마감" if (end_date - current_service_date()).days < 0 else "채용중"


def current_service_date():
    return datetime.now(KST).date()


def parse_recruit_end_date(value: Any):
    text = safe_text(value)
    if not text:
        return None
    try:
        return datetime.fromisoformat(text[:10]).date()
    except ValueError:
        return None


def is_current_recruit_end(value: Any, today=None) -> bool:
    end_date = parse_recruit_end_date(value)
    if end_date is None:
        return True
    return end_date >= (today or current_service_date())


def normalize_company_name(value: Any) -> str:
    text = safe_text(value).lower()
    for token in ("주식회사", "(주)", "㈜", "의료법인", "재단법인", "사단법인", "사회복지법인"):
        text = text.replace(token, "")
    for char in (" ", "\t", "\n", "·", ".", ",", "-", "_", "(", ")", "（", "）"):
        text = text.replace(char, "")
    return text


class JobBridgeInferenceService:
    def __init__(self, paths: RuntimePaths | None = None) -> None:
        self.paths = paths or RuntimePaths.from_env()
        self._loaded = False
        self.artifact: dict[str, Any] | None = None
        self.model: Any = None
        self.label_encoder: Any = None
        self.feature_columns: list[str] = []
        self.categorical_columns: list[str] = []
        self.ranker_artifact: dict[str, Any] | None = None
        self.ranker_model: Any = None
        self.ranker_feature_columns: list[str] = []
        self.ranker_numeric_columns: list[str] = []
        self.ranker_categorical_columns: list[str] = []
        self.ranker_category_levels: dict[str, list[str]] = {}
        self.ranker_load_error = ""
        self.training = pd.DataFrame()
        self.postings = pd.DataFrame()
        self.posting_source = "not_loaded"
        self.live_postings_error = ""
        self.seekers = pd.DataFrame()
        self.standard_workplaces = pd.DataFrame()
        self.priors_sido = pd.DataFrame()
        self.priors_sigungu = pd.DataFrame()
        self.priors_national = pd.DataFrame()
        self._desired_title_total: dict[str, int] = {}
        self._desired_title_disability: dict[tuple[str, str], int] = {}
        self._desired_title_profile: dict[tuple[str, str, str], int] = {}
        self._class_profile_count: dict[tuple[str, str, str, str], int] = {}
        self._profile_total: dict[tuple[str, str, str], int] = {}
        self._standard_company_by_norm: dict[str, str] = {}
        self._load_lock = Lock()
        self._postings_refresh_lock = Lock()
        self._postings_lock = Lock()
        self._postings_refreshed_at = 0.0
        self._postings_refresh_attempted_at = 0.0
        self._postings_refreshed_at_iso = ""

    def load(self) -> None:
        if self._loaded:
            return
        with self._load_lock:
            if self._loaded:
                return
            if not self.paths.model_path.exists():
                raise FileNotFoundError(f"Model file not found: {self.paths.model_path}")
            if not self.paths.data_dir.exists():
                raise FileNotFoundError(f"Data directory not found: {self.paths.data_dir}")

            self.artifact = joblib.load(self.paths.model_path)
            self.model = self.artifact["model"]
            self.label_encoder = self.artifact["label_encoder"]
            self.feature_columns = list(self.artifact["feature_columns"])
            self.categorical_columns = list(self.artifact["categorical_columns"])
            self._load_ranker()
            self.training = self._read_csv("training_dataset.csv")
            postings = self._read_postings()
            loaded_at = time.monotonic()
            with self._postings_refresh_lock:
                with self._postings_lock:
                    self.postings = postings
                    if self.posting_source == "supabase_live":
                        self._postings_refreshed_at = loaded_at
                        self._postings_refresh_attempted_at = loaded_at
                        self._postings_refreshed_at_iso = utc_now_iso()
            self.seekers = self._read_csv("job_seekers_normalized.csv")
            self.standard_workplaces = self._read_standard_workplaces()
            self.priors_sido = self._read_csv("market_priors_by_sido.csv")
            self.priors_sigungu = self._read_csv("market_priors_by_sigungu.csv")
            self.priors_national = self._read_csv("market_priors_national.csv")
            self._prepare_fit_evidence()
            self._prepare_standard_workplaces()
            self._loaded = True

    def reset_loaded_state(self) -> None:
        """Invalidate only live postings after a sync; keep models and reference CSVs loaded."""
        with self._postings_refresh_lock:
            with self._postings_lock:
                self.live_postings_error = ""
                self._postings_refreshed_at = 0.0
                self._postings_refresh_attempted_at = 0.0
                self._postings_refreshed_at_iso = ""

    def _read_csv(self, name: str) -> pd.DataFrame:
        path = self.paths.data_dir / name
        if not path.exists():
            return pd.DataFrame()
        return pd.read_csv(path, encoding="utf-8-sig")

    def _live_jobs_enabled(self) -> bool:
        value = os.getenv("JOBBRIDGE_LIVE_JOBS_ENABLED", "false").strip().lower()
        return value in {"1", "true", "yes", "y", "on"}

    def _live_jobs_refresh_ttl_seconds(self) -> float:
        raw = os.getenv("JOBBRIDGE_LIVE_JOBS_TTL_SECONDS", "30").strip()
        try:
            value = float(raw or "30")
        except ValueError:
            value = 30.0
        return max(0.0, min(value, 60.0))

    def _postings_state_snapshot(self) -> tuple[pd.DataFrame, str, str]:
        with self._postings_lock:
            return self.postings.copy(), self.posting_source, self.live_postings_error

    def refresh_live_postings(self, force: bool = False) -> bool:
        """Refresh only the live posting frame without reloading models or CSV datasets."""
        request_started_at = time.monotonic()
        self.load()
        if not self._live_jobs_enabled():
            return False

        with self._postings_refresh_lock:
            if self._postings_refresh_attempted_at >= request_started_at:
                return False
            ttl_seconds = self._live_jobs_refresh_ttl_seconds()
            if (
                not force
                and self._postings_refreshed_at > 0
                and request_started_at - self._postings_refreshed_at < ttl_seconds
            ):
                return False

            try:
                from .persistence import SupabaseRecorder

                recorder = SupabaseRecorder.from_env()
                if not recorder.enabled:
                    raise RuntimeError("Supabase live postings are not configured")
                limit = int(os.getenv("JOBBRIDGE_LIVE_JOBS_DB_LIMIT", "1000"))
                rows = recorder.fetch_active_live_postings(limit=limit)
                postings = self._postings_frame(rows)
                completed_at = time.monotonic()
                with self._postings_lock:
                    self.postings = postings
                    self.posting_source = "supabase_live"
                    self.live_postings_error = ""
                    self._postings_refreshed_at = completed_at
                    self._postings_refreshed_at_iso = utc_now_iso()
                return True
            except Exception as exc:
                with self._postings_lock:
                    self.live_postings_error = f"Supabase live posting refresh failed: {type(exc).__name__}: {exc}"
                return False
            finally:
                self._postings_refresh_attempted_at = time.monotonic()

    def _read_postings(self) -> pd.DataFrame:
        self.live_postings_error = ""
        if not self._live_jobs_enabled():
            self.posting_source = "static_csv"
            return self._read_csv("job_postings_normalized.csv")

        limit = int(os.getenv("JOBBRIDGE_LIVE_JOBS_DB_LIMIT", "1000"))
        try:
            from .persistence import SupabaseRecorder

            recorder = SupabaseRecorder.from_env()
            if recorder.enabled:
                rows = recorder.fetch_active_live_postings(limit=limit)
                if rows:
                    self.posting_source = "supabase_live"
                    return self._postings_frame(rows)
                self.live_postings_error = "Supabase returned no active live postings"
        except Exception as exc:
            self.live_postings_error = f"Supabase live posting load failed: {type(exc).__name__}: {exc}"

        snapshot = Path(os.getenv("JOBBRIDGE_LIVE_JOBS_SNAPSHOT", str(DEFAULT_LIVE_POSTINGS_PATH)))
        if snapshot.exists():
            try:
                rows = json.loads(snapshot.read_text(encoding="utf-8"))
                if isinstance(rows, dict):
                    rows = rows.get("items") or rows.get("jobs") or []
                self.posting_source = "local_live_snapshot"
                return self._postings_frame(rows)
            except Exception as exc:
                message = f"Local live snapshot load failed: {type(exc).__name__}: {exc}"
                self.live_postings_error = f"{self.live_postings_error}; {message}" if self.live_postings_error else message

        self.posting_source = "live_empty"
        return pd.DataFrame()

    def _postings_frame(self, rows: list[dict[str, Any]]) -> pd.DataFrame:
        df = pd.DataFrame(rows)
        if df.empty:
            return df
        rename_map = {
            "company_address": "address_raw",
            "salary_type": "wage_type",
            "salary_amount": "wage_amount",
            "recruit_start_date": "recruit_start",
            "recruit_end_date": "recruit_end",
            "env_hand_work": "env_handwork",
            "env_listen_talk": "env_lstn_talk",
            "env_stand_walk": "env_stnd_walk",
        }
        for old, new in rename_map.items():
            if old in df.columns and new not in df.columns:
                df[new] = df[old]
        if "posting_id" not in df.columns:
            if "source_posting_key" in df.columns:
                df["posting_id"] = df["source_posting_key"]
            elif "external_key" in df.columns:
                df["posting_id"] = df["external_key"]
        if "wage_amount" in df.columns:
            df["wage_amount"] = pd.to_numeric(df["wage_amount"], errors="coerce")
        if "is_active" in df.columns:
            active = df["is_active"].map(lambda value: str(value).lower() not in {"false", "0", "nan", "none"})
            df = df[active].copy()
        df = self._current_postings(df)
        return df

    def _current_postings(self, rows: pd.DataFrame) -> pd.DataFrame:
        if rows.empty or "recruit_end" not in rows.columns:
            return rows.copy()
        today = current_service_date()
        current = rows["recruit_end"].map(lambda value: is_current_recruit_end(value, today))
        return rows[current].copy()

    def _read_standard_workplaces(self) -> pd.DataFrame:
        if not DEFAULT_STANDARD_WORKPLACE_PATH.exists():
            return pd.DataFrame()
        for encoding in ("cp949", "utf-8-sig", "utf-8"):
            try:
                return pd.read_csv(DEFAULT_STANDARD_WORKPLACE_PATH, encoding=encoding)
            except UnicodeDecodeError:
                continue
        return pd.DataFrame()

    def _load_ranker(self) -> None:
        self.ranker_artifact = None
        self.ranker_model = None
        self.ranker_feature_columns = []
        self.ranker_numeric_columns = []
        self.ranker_categorical_columns = []
        self.ranker_category_levels = {}
        self.ranker_load_error = ""
        if not self.paths.ranker_model_path.exists():
            return
        try:
            artifact = joblib.load(self.paths.ranker_model_path)
            self.ranker_artifact = artifact
            self.ranker_model = artifact["model"]
            self.ranker_feature_columns = list(artifact["feature_columns"])
            self.ranker_numeric_columns = list(artifact.get("numeric_columns") or [])
            self.ranker_categorical_columns = list(artifact["categorical_columns"])
            self.ranker_category_levels = {
                str(key): [str(value) for value in values]
                for key, values in (artifact.get("category_levels") or {}).items()
            }
        except Exception as exc:
            self.ranker_load_error = f"{type(exc).__name__}: {exc}"
            self.ranker_artifact = None
            self.ranker_model = None

    def health(self) -> dict[str, Any]:
        started = time.perf_counter()
        self.load()
        postings, posting_source, live_postings_error = self._postings_state_snapshot()
        current_postings = self._current_postings(postings)
        return {
            "ok": True,
            "modelVersion": MODEL_VERSION,
            "modelPath": str(self.paths.model_path),
            "postingRankerVersion": (self.ranker_artifact or {}).get("model_version") or RANKER_MODEL_VERSION,
            "postingRankerPath": str(self.paths.ranker_model_path),
            "postingRankerAvailable": self.ranker_model is not None,
            "postingRankerLoadError": self.ranker_load_error,
            "dataDir": str(self.paths.data_dir),
            "postingRows": int(len(current_postings)),
            "loadedPostingRows": int(len(postings)),
            "postingSource": posting_source,
            "livePostingsError": live_postings_error,
            "livePostingsRefreshedAt": self._postings_refreshed_at_iso or None,
            "standardWorkplaceRows": int(len(self.standard_workplaces)),
            "classes": int(len(self.label_encoder.classes_)),
            "loadCheckMs": round((time.perf_counter() - started) * 1000, 2),
        }

    def recommend(self, request: dict[str, Any]) -> dict[str, Any]:
        started = time.perf_counter()
        self.load()
        self.refresh_live_postings(force=True)
        model_payload = self._extract_payload(request)
        features = model_payload["modelFeatures"]
        preferences = model_payload["scoringPreferences"]

        feature_df = pd.DataFrame([features])
        for column in self.categorical_columns:
            feature_df[column] = feature_df[column].astype("string").fillna("unknown").astype("category")
        for column in self.feature_columns:
            if column not in self.categorical_columns:
                feature_df[column] = pd.to_numeric(feature_df[column], errors="coerce")

        proba = self.model.predict_proba(feature_df[self.feature_columns])[0]
        predictions = self._rank_predictions(proba, features, preferences, top_k=5)
        recs = self._build_recommendations(predictions[:3], features, preferences)
        connected_jobs = self._connected_jobs(predictions, features, preferences, limit=5)
        top_prediction = predictions[0]
        top_prob_pct = int(round(top_prediction["probability"] * 100))
        top_prior = top_prediction["marketPrior"]
        has_desired_job = bool(preferences.get("desired_job_class"))
        has_desired_wage = bool(preferences.get("desired_wage"))

        factors = [
            {"label": "AI 예측 신뢰", "pct": max(45, min(95, 52 + top_prob_pct))},
            {"label": "지역 채용 신호", "pct": self._prior_factor_pct(top_prior)},
            {"label": "직무 선호 보정", "pct": 88 if has_desired_job else 70},
            {"label": "임금 조건 보정", "pct": 82 if has_desired_wage else 70},
        ]

        report = {
            "requestId": str(uuid.uuid4()),
            "generatedAt": utc_now_iso(),
            "modelVersion": MODEL_VERSION,
            "source": "lightgbm",
            "profile": request.get("profile") or request.get("clientProfile") or {},
            "modelPayload": model_payload,
            "recommendationBucket": bucket_for_job_class(top_prediction["jobClass"]),
            "predictedJobClasses": predictions,
            "recs": recs,
            "factors": factors,
            "connectedJobs": connected_jobs,
            "challengeRecs": [],
            "diagnostics": {
                "latencyMs": round((time.perf_counter() - started) * 1000, 2),
                "modelFeatures": self.feature_columns,
                "postingRankerAvailable": self.ranker_model is not None,
                "postingRankerVersion": (self.ranker_artifact or {}).get("model_version") or RANKER_MODEL_VERSION,
                "postprocessing": "desired_job_class and desired_wage are scoring preferences; posting fit is reviewed with seeker-title evidence and disability/severity guardrails",
            },
        }
        challenge_recs, challenge_diagnostics = self._challenge_recs_for_report(
            request,
            model_payload,
            predictions,
        )
        report["challengeRecs"] = challenge_recs
        report["diagnostics"]["challengeRecommendation"] = challenge_diagnostics
        report["diagnostics"]["latencyMs"] = round((time.perf_counter() - started) * 1000, 2)
        return report

    def _challenge_recs_for_report(
        self,
        request: dict[str, Any],
        model_payload: dict[str, Any],
        predictions: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        challenge_request = {
            "profile": request.get("profile") or request.get("clientProfile") or {},
            "modelFeatures": model_payload.get("modelFeatures") or {},
            "scoringPreferences": model_payload.get("scoringPreferences") or {},
            "modelContext": {
                "predictedJobClasses": predictions[:5],
                "scorePolicy": "modelCandidateScore is not a positive challenge ranking weight",
            },
        }
        try:
            challenge_report = ReferenceRepository.from_env().challenge_recommendations(challenge_request)
        except Exception as exc:
            return [], {
                "status": "error",
                "version": "challenge_xai_contract_v1",
                "error": f"{type(exc).__name__}: {exc}",
            }
        challenge_recs = list(challenge_report.get("challengeRecs") or [])
        diagnostics = dict(challenge_report.get("diagnostics") or {})
        return challenge_recs, {
            "status": "ready" if challenge_recs else "empty",
            "version": diagnostics.get("challengeRecommendationVersion") or "challenge_xai_contract_v1",
            "source": challenge_report.get("source"),
            "count": len(challenge_recs),
            "fallbackUsed": bool((challenge_report.get("fallback") or {}).get("used")),
            "fallbackReason": safe_text((challenge_report.get("fallback") or {}).get("reason")),
            "referenceDbStatus": diagnostics.get("referenceDbStatus"),
            "ncsMappingMode": diagnostics.get("ncsMappingMode") or "reference_cache_unreviewed",
            "scorePolicy": diagnostics.get("scorePolicy"),
        }

    def live_jobs_for_ui(self, limit: int = 500, force_refresh: bool = True) -> dict[str, Any]:
        started = time.perf_counter()
        self.load()
        self.refresh_live_postings(force=force_refresh)
        postings, posting_source, live_postings_error = self._postings_state_snapshot()
        rows = self._current_postings(postings)
        available_rows_count = int(len(rows))
        expired_filtered_count = int(max(0, len(postings) - available_rows_count))
        if rows.empty:
            return {
                "generatedAt": utc_now_iso(),
                "source": posting_source,
                "jobs": [],
                "diagnostics": {
                    "latencyMs": round((time.perf_counter() - started) * 1000, 2),
                    "postingRows": 0,
                    "loadedRows": int(len(postings)),
                    "expiredFilteredRows": expired_filtered_count,
                    "livePostingsError": live_postings_error,
                    "livePostingsRefreshedAt": self._postings_refreshed_at_iso or None,
                },
            }
        if "recruit_end" in rows.columns:
            rows["_sort_recruit_end"] = pd.to_datetime(rows["recruit_end"], errors="coerce")
            rows = rows.sort_values(["_sort_recruit_end", "posting_id"], ascending=[True, True], na_position="last")
        rows = rows.head(max(1, min(int(limit), 1000)))
        jobs = []
        for _, row in rows.iterrows():
            job_class = safe_text(row.get("target_job_class_candidate"), safe_text(row.get("job_title"), "채용공고"))
            prediction = {
                "jobClass": job_class,
                "bucket": bucket_for_job_class(job_class),
                "probability": 0.0,
                "relativeModelScore": 0.0,
                "marketPrior": {},
                "preferenceBoost": 0.0,
            }
            job = self._posting_to_ui_job(row, prediction, {})
            job["linked"] = False
            job["score"] = None
            job["showScore"] = False
            job["statusLabel"] = format_recruit_status(row.get("recruit_end"))
            jobs.append(job)
        return {
            "generatedAt": utc_now_iso(),
            "source": posting_source,
            "jobs": jobs,
            "diagnostics": {
                "latencyMs": round((time.perf_counter() - started) * 1000, 2),
                "postingRows": available_rows_count,
                "loadedRows": int(len(postings)),
                "expiredFilteredRows": expired_filtered_count,
                "returnedRows": len(jobs),
                "livePostingsError": live_postings_error,
                "livePostingsRefreshedAt": self._postings_refreshed_at_iso or None,
            },
        }

    def reference_summary(self) -> dict[str, Any]:
        return ReferenceRepository.from_env().summary()

    def capabilities(self, request: dict[str, Any] | None = None) -> dict[str, Any]:
        return ReferenceRepository.from_env().capabilities(request or {})

    def ncs_capabilities(self, request: dict[str, Any] | None = None) -> dict[str, Any]:
        return ReferenceRepository.from_env().ncs_capabilities(request or {})

    def challenge_recommendations(self, request: dict[str, Any]) -> dict[str, Any]:
        return ReferenceRepository.from_env().challenge_recommendations(request)

    def _capability_catalog_admin_status(self) -> dict[str, Any]:
        gate_profile = {"disabilityType": "시각장애", "severity": "중증"}
        try:
            payload = self.capabilities({"profile": gate_profile})
            counts = payload.get("counts") or {}
            selectable = int(counts.get("suitable") or 0) + int(counts.get("caution") or 0)
            source = safe_text(payload.get("source"), "unknown")
            return {
                "status": "ready" if int(counts.get("items") or 0) > 0 else "empty",
                "source": source,
                "usesSupabase": source == "supabase_capability_catalog_v1",
                "version": safe_text(payload.get("version"), "capability_catalog_v1"),
                "gateProfile": gate_profile,
                "counts": {
                    "categories": int(counts.get("categories") or 0),
                    "groups": int(counts.get("groups") or 0),
                    "items": int(counts.get("items") or 0),
                    "suitable": int(counts.get("suitable") or 0),
                    "caution": int(counts.get("caution") or 0),
                    "blocked": int(counts.get("blocked") or 0),
                    "selectable": selectable,
                },
                "fallbackReason": safe_text((payload.get("meta") or {}).get("fallbackReason")),
                "secretsExposed": False,
            }
        except Exception as exc:
            return {
                "status": "error",
                "source": "unavailable",
                "usesSupabase": False,
                "version": "capability_catalog_v1",
                "gateProfile": gate_profile,
                "counts": {
                    "categories": 0,
                    "groups": 0,
                    "items": 0,
                    "suitable": 0,
                    "caution": 0,
                    "blocked": 0,
                    "selectable": 0,
                },
                "fallbackReason": f"{type(exc).__name__}: {exc}",
                "secretsExposed": False,
            }

    def admin_status(self, persistence_status: dict[str, Any] | None = None) -> dict[str, Any]:
        started = time.perf_counter()
        self.load()
        self.refresh_live_postings(force=False)
        postings, posting_source, live_postings_error = self._postings_state_snapshot()
        current_postings = self._current_postings(postings)
        snapshot_path = Path(os.getenv("JOBBRIDGE_LIVE_JOBS_SNAPSHOT", str(DEFAULT_LIVE_POSTINGS_PATH)))
        snapshot = self._live_snapshot_status(snapshot_path)
        live_jobs_enabled = self._live_jobs_enabled()
        persistence = persistence_status or {}
        capability_catalog = self._capability_catalog_admin_status()
        return {
            "ok": True,
            "generatedAt": utc_now_iso(),
            "service": {
                "modelVersion": MODEL_VERSION,
                "modelLoaded": self.model is not None,
                "modelPath": str(self.paths.model_path),
                "dataDir": str(self.paths.data_dir),
                "classCount": int(len(self.label_encoder.classes_)),
                "postingSource": posting_source,
                "liveJobsEnabled": live_jobs_enabled,
                "livePostingsError": live_postings_error,
                "livePostingsRefreshedAt": self._postings_refreshed_at_iso or None,
            },
            "liveJobs": {
                "runtime": {
                    "activePostingRows": int(len(current_postings)),
                    "loadedPostingRows": int(len(postings)),
                    "source": posting_source,
                    "usesSupabase": posting_source == "supabase_live",
                    "usesLocalSnapshot": posting_source == "local_live_snapshot",
                    "usesStaticCsv": posting_source == "static_csv",
                },
                "snapshot": snapshot,
                "collectionNote": snapshot.get("collectionNote"),
            },
            "recommendation": {
                "profileModelVersion": MODEL_VERSION,
                "postingRankerVersion": (self.ranker_artifact or {}).get("model_version") or RANKER_MODEL_VERSION,
                "postingRankerAvailable": self.ranker_model is not None,
                "postingRankerPath": str(self.paths.ranker_model_path),
                "postingRankerLoadError": self.ranker_load_error,
                "recommendationLogging": persistence,
                "capabilityCatalog": capability_catalog,
            },
            "capabilityCatalog": capability_catalog,
            "visitors": {
                "status": "not_collected",
                "message": "로컬 MVP에서는 방문자/사용량 분석을 수집하지 않습니다. 추후 Supabase analytics 또는 별도 events 테이블이 필요합니다.",
                "fakeCountsGenerated": False,
            },
            "diagnostics": {
                "latencyMs": round((time.perf_counter() - started) * 1000, 2),
                "standardWorkplaceRows": int(len(self.standard_workplaces)),
            },
        }

    def _live_snapshot_status(self, path: Path) -> dict[str, Any]:
        status: dict[str, Any] = {
            "path": str(path),
            "exists": path.exists(),
            "format": "missing",
            "metadata": {},
            "totalRows": 0,
            "activeRows": 0,
            "inactiveRows": 0,
            "currentRowsAsOfToday": 0,
            "expiredRowsAsOfToday": 0,
            "mappedRows": 0,
            "unmappedRows": 0,
            "environmentDetailRows": 0,
            "latestFetchedAt": None,
            "latestLastSeenAt": None,
            "sourceSystems": [],
            "preferredEndpointBreakdown": [],
            "endpointCoverageBreakdown": [],
            "mergedFromMultipleEndpointsRows": 0,
            "collectionNote": "live snapshot file was not found; status falls back to runtime health/live-jobs data.",
        }
        if not path.exists():
            return status

        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            status["format"] = "unreadable"
            status["error"] = f"{type(exc).__name__}: {exc}"
            status["collectionNote"] = "live snapshot file exists but could not be parsed."
            return status

        metadata: dict[str, Any] = {}
        if isinstance(raw, dict):
            metadata = dict(raw.get("metadata") or raw.get("summary") or {})
            rows = raw.get("items") or raw.get("jobs") or raw.get("rows") or []
            status["format"] = "object"
        elif isinstance(raw, list):
            rows = raw
            status["format"] = "array"
        else:
            rows = []
            status["format"] = type(raw).__name__

        if not isinstance(rows, list):
            rows = []

        today = current_service_date()
        active_rows = [row for row in rows if isinstance(row, dict) and self._row_is_active(row)]
        expired_rows = [row for row in active_rows if self._row_is_expired(row, today)]
        mapped_rows = [
            row
            for row in active_rows
            if safe_text(row.get("target_job_class_candidate")) and safe_text(row.get("job_class_mapping_method")) != "unmapped"
        ]
        preferred_counter: Counter[str] = Counter()
        coverage_counter: Counter[str] = Counter()
        source_system_counter: Counter[str] = Counter()
        merged_rows = 0

        for row in active_rows:
            preferred = safe_text(row.get("source_endpoint"), "unknown")
            preferred_counter[preferred] += 1
            source_system_counter[safe_text(row.get("source_system"), "unknown")] += 1
            endpoints = row.get("source_endpoints")
            if isinstance(endpoints, list) and endpoints:
                if len(set(str(endpoint) for endpoint in endpoints)) > 1:
                    merged_rows += 1
                for endpoint in endpoints:
                    coverage_counter[safe_text(endpoint, "unknown")] += 1
            else:
                coverage_counter[preferred] += 1

        fetched_values = [
            safe_text(row.get("fetched_at"))
            for row in rows
            if isinstance(row, dict) and safe_text(row.get("fetched_at"))
        ]
        seen_values = [
            safe_text(row.get("last_seen_at"))
            for row in rows
            if isinstance(row, dict) and safe_text(row.get("last_seen_at"))
        ]
        status.update(
            {
                "metadata": metadata,
                "totalRows": len(rows),
                "activeRows": len(active_rows),
                "inactiveRows": max(0, len(rows) - len(active_rows)),
                "currentRowsAsOfToday": max(0, len(active_rows) - len(expired_rows)),
                "expiredRowsAsOfToday": len(expired_rows),
                "mappedRows": len(mapped_rows),
                "unmappedRows": max(0, len(active_rows) - len(mapped_rows)),
                "environmentDetailRows": sum(1 for row in active_rows if bool(row.get("has_environment_detail"))),
                "latestFetchedAt": max(fetched_values) if fetched_values else metadata.get("fetched_at"),
                "latestLastSeenAt": max(seen_values) if seen_values else metadata.get("last_seen_at"),
                "sourceSystems": self._counter_items(source_system_counter),
                "preferredEndpointBreakdown": self._counter_items(preferred_counter),
                "endpointCoverageBreakdown": self._counter_items(coverage_counter),
                "mergedFromMultipleEndpointsRows": merged_rows,
                "excludedExpiredRowsFromCollector": metadata.get("excluded_expired_rows"),
                "mergedRowsFromCollector": metadata.get("merged_rows"),
                "collectionNote": (
                    "snapshot metadata found; collector summary values are included when present."
                    if metadata
                    else "snapshot has no metadata wrapper; counts are inferred from the current local snapshot rows."
                ),
            }
        )
        return status

    @staticmethod
    def _row_is_active(row: dict[str, Any]) -> bool:
        value = row.get("is_active", True)
        return str(value).strip().lower() not in {"false", "0", "nan", "none", ""}

    @staticmethod
    def _row_is_expired(row: dict[str, Any], today: Any) -> bool:
        text = safe_text(row.get("recruit_end"))
        if not text:
            return False
        try:
            return datetime.fromisoformat(text[:10]).date() < today
        except ValueError:
            return False

    @staticmethod
    def _counter_items(counter: Counter[str]) -> list[dict[str, Any]]:
        return [
            {"name": name, "count": int(count)}
            for name, count in sorted(counter.items(), key=lambda item: (-item[1], item[0]))
        ]

    def _extract_payload(self, request: dict[str, Any]) -> dict[str, Any]:
        model_features = dict(request.get("modelFeatures") or request.get("model_features") or {})
        preferences = dict(request.get("scoringPreferences") or request.get("scoring_preferences") or {})
        if "payload" in request and isinstance(request["payload"], dict):
            model_features.update(request["payload"].get("modelFeatures") or {})
            preferences.update(request["payload"].get("scoringPreferences") or {})

        required = ["sido", "age", "disability_type", "severity"]
        missing = [name for name in required if model_features.get(name) in (None, "")]
        if missing:
            raise ValueError(f"Missing required model feature(s): {', '.join(missing)}")

        model_features["sigungu"] = model_features.get("sigungu") or "unknown"
        model_features["age"] = int(model_features["age"])
        model_features["age_group"] = model_features.get("age_group") or age_group_for(model_features["age"])
        return {
            "modelFeatures": {
                "sido": safe_text(model_features.get("sido")),
                "sigungu": safe_text(model_features.get("sigungu"), "unknown"),
                "age": model_features["age"],
                "age_group": safe_text(model_features.get("age_group")),
                "disability_type": safe_text(model_features.get("disability_type")),
                "severity": safe_text(model_features.get("severity")),
            },
            "scoringPreferences": {
                "desired_job_class": clean_value(preferences.get("desired_job_class")),
                "desired_wage": clean_value(preferences.get("desired_wage")),
            },
        }

    def _rank_predictions(
        self,
        proba: np.ndarray,
        features: dict[str, Any],
        preferences: dict[str, Any],
        top_k: int,
    ) -> list[dict[str, Any]]:
        desired_class = preferences.get("desired_job_class")
        top_indices = list(proba.argsort()[-max(top_k * 2, top_k) :][::-1])
        if desired_class:
            class_matches = np.where(self.label_encoder.classes_ == desired_class)[0]
            if len(class_matches) and int(class_matches[0]) not in top_indices:
                top_indices.append(int(class_matches[0]))
        predictions = []
        top_probability = float(proba[top_indices[0]]) if len(top_indices) else 0.0
        for class_idx in top_indices:
            job_class = self.label_encoder.inverse_transform([class_idx])[0]
            probability = float(proba[class_idx])
            prior = self._market_prior(features, job_class)
            preference_boost = 0.0
            if desired_class and desired_class == job_class:
                preference_boost += 0.45
            elif desired_class and bucket_for_job_class(desired_class) == bucket_for_job_class(job_class):
                preference_boost += 0.08
            market_boost = min(0.08, prior["postingShare"] * 0.18 + prior["seekerShare"] * 0.04)
            adjusted = probability + preference_boost + market_boost
            relative = probability / top_probability if top_probability else 0.0
            predictions.append(
                {
                    "jobClass": job_class,
                    "probability": round(probability, 6),
                    "adjustedScore": round(float(adjusted), 6),
                    "relativeModelScore": round(float(relative), 6),
                    "bucket": bucket_for_job_class(job_class),
                    "marketPrior": prior,
                    "preferenceBoost": round(preference_boost, 4),
                }
            )
        return sorted(predictions, key=lambda item: item["adjustedScore"], reverse=True)[:top_k]

    def _market_prior(self, features: dict[str, Any], job_class: str) -> dict[str, Any]:
        sido = features.get("sido")
        sigungu = features.get("sigungu") or "unknown"
        row = self._find_prior_row(self.priors_sigungu, job_class, sido=sido, sigungu=sigungu)
        level = "sigungu"
        if row is None:
            row = self._find_prior_row(self.priors_sido, job_class, sido=sido)
            level = "sido"
        if row is None:
            row = self._find_prior_row(self.priors_national, job_class)
            level = "national"
        if row is None:
            return {
                "level": "none",
                "postingCount": 0,
                "seekerCount": 0,
                "postingShare": 0.0,
                "seekerShare": 0.0,
            }
        return {
            "level": level,
            "postingCount": int(clean_value(row.get("posting_count")) or 0),
            "seekerCount": int(clean_value(row.get("seeker_count")) or 0),
            "postingShare": float(clean_value(row.get("posting_share_in_region")) or 0.0),
            "seekerShare": float(clean_value(row.get("seeker_share_in_region")) or 0.0),
            "postingWageMedian": clean_value(row.get("posting_wage_median")),
            "desiredWageMedian": clean_value(row.get("desired_wage_median")),
        }

    def _prepare_fit_evidence(self) -> None:
        self._desired_title_total = {}
        self._desired_title_disability = {}
        self._desired_title_profile = {}
        self._class_profile_count = {}
        self._profile_total = {}
        required = {"desired_job_title", "disability_type", "severity"}
        if self.seekers.empty or not required.issubset(self.seekers.columns):
            return
        evidence = self.seekers[list(required)].copy()
        for column in required:
            evidence[column] = evidence[column].map(safe_text)
        evidence = evidence[evidence["desired_job_title"] != ""]
        self._desired_title_total = {
            str(key): int(value)
            for key, value in evidence.groupby("desired_job_title").size().to_dict().items()
        }
        self._desired_title_disability = {
            (str(key[0]), str(key[1])): int(value)
            for key, value in evidence.groupby(["desired_job_title", "disability_type"]).size().to_dict().items()
        }
        self._desired_title_profile = {
            (str(key[0]), str(key[1]), str(key[2])): int(value)
            for key, value in evidence.groupby(["desired_job_title", "disability_type", "severity"]).size().to_dict().items()
        }
        class_required = {"target_job_class_candidate", "disability_type", "severity", "age_group"}
        if class_required.issubset(self.seekers.columns):
            class_evidence = self.seekers[list(class_required)].copy()
            for column in class_required:
                class_evidence[column] = class_evidence[column].map(safe_text)
            profile_group = ["disability_type", "severity", "age_group"]
            self._profile_total = {
                (str(key[0]), str(key[1]), str(key[2])): int(value)
                for key, value in class_evidence.groupby(profile_group).size().to_dict().items()
            }
            self._class_profile_count = {
                (str(key[0]), str(key[1]), str(key[2]), str(key[3])): int(value)
                for key, value in class_evidence.groupby([*profile_group, "target_job_class_candidate"]).size().to_dict().items()
            }

    def _prepare_standard_workplaces(self) -> None:
        self._standard_company_by_norm = {}
        if self.standard_workplaces.empty or "사업체명" not in self.standard_workplaces.columns:
            return
        for value in self.standard_workplaces["사업체명"].dropna():
            name = safe_text(value)
            normalized = normalize_company_name(name)
            if normalized:
                self._standard_company_by_norm[normalized] = name

    def _standard_workplace_info(self, row: pd.Series | None) -> dict[str, Any]:
        if row is None or not self._standard_company_by_norm:
            return {"isStandard": False, "matchedName": ""}
        company = safe_text(row.get("company_name"))
        normalized = normalize_company_name(company)
        if not normalized:
            return {"isStandard": False, "matchedName": ""}
        matched = self._standard_company_by_norm.get(normalized)
        if matched:
            return {"isStandard": True, "matchedName": matched}
        for standard_key, standard_name in self._standard_company_by_norm.items():
            if len(standard_key) >= 4 and (standard_key in normalized or normalized in standard_key):
                return {"isStandard": True, "matchedName": standard_name}
        return {"isStandard": False, "matchedName": ""}

    def _remote_work_info(self, row: pd.Series | None) -> dict[str, Any]:
        if row is None:
            return {"hasRemoteKeyword": False, "matchedKeyword": ""}
        text = " ".join(
            safe_text(row.get(column))
            for column in ("job_title", "company_name", "employment_type", "address_raw", "recruit_period_raw")
        )
        for keyword in REMOTE_WORK_KEYWORDS:
            if keyword in text:
                return {"hasRemoteKeyword": True, "matchedKeyword": keyword}
        return {"hasRemoteKeyword": False, "matchedKeyword": ""}

    def _profile_evidence(self, features: dict[str, Any], job_class: str) -> dict[str, Any]:
        empty = {
            "scope": "none",
            "similarCount": 0,
            "jobClassCount": 0,
            "jobClassSharePct": 0,
            "exactCount": 0,
            "sidoCount": 0,
            "nationalCount": 0,
            "rank": None,
        }
        required = {"disability_type", "severity", "age_group", "sido", "sigungu", "target_job_class"}
        if self.training.empty or not required.issubset(self.training.columns):
            return empty

        base = self.training[
            (self.training["disability_type"] == features.get("disability_type"))
            & (self.training["severity"] == features.get("severity"))
            & (self.training["age_group"] == features.get("age_group"))
        ]
        if base.empty:
            return empty

        sido = features.get("sido")
        sigungu = features.get("sigungu") or "unknown"
        sido_rows = base[base["sido"] == sido] if sido else base.iloc[0:0]
        has_sigungu = bool(sigungu and sigungu != "unknown")
        if has_sigungu:
            exact_rows = sido_rows[sido_rows["sigungu"] == sigungu]
            scope = "시군구"
        else:
            exact_rows = sido_rows
            scope = "시도"

        scoped = exact_rows
        if len(scoped) < 30 and len(sido_rows) >= 30:
            scoped = sido_rows
            scope = "시도"
        elif len(scoped) < 30:
            scoped = base
            scope = "전국"

        counts = scoped["target_job_class"].value_counts()
        job_count = int(counts.get(job_class, 0))
        similar_count = int(len(scoped))
        share = int(round(job_count / similar_count * 100)) if similar_count else 0
        rank = None
        if job_count:
            rank = int(list(counts.index).index(job_class) + 1)
        return {
            "scope": scope,
            "similarCount": similar_count,
            "jobClassCount": job_count,
            "jobClassSharePct": share,
            "exactCount": int(len(exact_rows)),
            "sidoCount": int(len(sido_rows)),
            "nationalCount": int(len(base)),
            "rank": rank,
        }

    def _accessibility_review(
        self,
        row: pd.Series | None,
        features: dict[str, Any],
        standard_info: dict[str, Any],
    ) -> dict[str, Any]:
        profile_sigungu = safe_text(features.get("sigungu"), "unknown")
        has_profile_sigungu = bool(profile_sigungu and profile_sigungu != "unknown")
        same_sigungu = bool(row is not None and has_profile_sigungu and safe_text(row.get("sigungu")) == profile_sigungu)
        same_sido = bool(row is not None and safe_text(row.get("sido")) == safe_text(features.get("sido")))
        remote_info = self._remote_work_info(row)
        score = 55
        if same_sido:
            score += 10
        if same_sigungu:
            score += 15
        if standard_info.get("isStandard"):
            score += 10
        if remote_info.get("hasRemoteKeyword"):
            score += 10
        return {
            "score": max(40, min(95, score)),
            "sameSido": same_sido,
            "sameSigungu": same_sigungu,
            **remote_info,
        }

    def _posting_fit_review(self, row: pd.Series | None, features: dict[str, Any]) -> dict[str, Any]:
        title = safe_text(row.get("job_title")) if row is not None else ""
        disability = safe_text(features.get("disability_type"))
        severity = safe_text(features.get("severity"))
        title_total = int(self._desired_title_total.get(title, 0))
        disability_count = int(self._desired_title_disability.get((title, disability), 0))
        profile_count = int(self._desired_title_profile.get((title, disability, severity), 0))
        level = "ok"
        penalty = 0
        boost = 0
        notes: list[str] = []

        visual_review = disability == "시각장애" and any(term in title for term in VISUAL_REVIEW_TERMS)
        if disability == "시각장애" and severity == "중증":
            if profile_count == 0 and disability_count > 0:
                level = "review" if visual_review else "caution"
                penalty = max(penalty, 16 if visual_review else 8)
                notes.append(
                    f"세부 희망직무 데이터에서 '{title}'은 시각장애 {disability_count}건이 모두 경증이고 중증 사례는 0건입니다."
                )
            elif profile_count == 0 and title_total >= 20 and visual_review:
                level = "review"
                penalty = max(penalty, 14)
                notes.append(
                    f"세부 희망직무 데이터에서 '{title}'은 전체 {title_total}건 중 시각장애 중증 사례가 확인되지 않았습니다."
                )
            elif visual_review:
                level = "caution"
                penalty = max(penalty, 6)
                notes.append("직무명에 안내·이동·현장 확인 성격이 포함되어 시각장애 중증은 업무조정 확인이 필요합니다.")

        if visual_review and not any("업무조정" in note for note in notes):
            notes.append("직무명에 안내·이동·현장 확인 성격이 포함되어 보조공학·업무조정 가능 여부를 확인해야 합니다.")
        if profile_count > 0:
            boost = max(boost, min(8, 2 + int(math.log2(profile_count + 1))))
            notes.append(f"같은 장애유형·중증도에서 이 세부 직무 희망 사례 {profile_count}건이 확인됩니다.")
        elif disability_count > 0:
            boost = max(boost, min(4, 1 + int(math.log2(disability_count + 1))))

        return {
            "level": level,
            "penalty": penalty,
            "boost": boost,
            "notes": notes[:3],
            "evidence": {
                "desiredTitleCount": title_total,
                "disabilityTitleCount": disability_count,
                "profileTitleCount": profile_count,
            },
        }

    def _ranker_feature_frame(
        self,
        df: pd.DataFrame,
        features: dict[str, Any],
        job_class_model_score: float,
    ) -> pd.DataFrame:
        disability = safe_text(features.get("disability_type"), "unknown")
        severity = safe_text(features.get("severity"), "unknown")
        age_group = safe_text(features.get("age_group"), "unknown")
        profile_sido = safe_text(features.get("sido"), "unknown")
        profile_sigungu = safe_text(features.get("sigungu"), "unknown")
        profile_key = (disability, severity, age_group)
        profile_total = int(self._profile_total.get(profile_key, 0))

        rows: list[dict[str, Any]] = []
        for _, row in df.iterrows():
            job_title = safe_text(row.get("job_title"), "unknown")
            posting_job_class = safe_text(row.get("target_job_class_candidate"), "unknown")
            class_key = (*profile_key, posting_job_class)
            class_profile_count = int(self._class_profile_count.get(class_key, 0))
            class_profile_share = class_profile_count / profile_total if profile_total else 0.0
            standard_info = self._standard_workplace_info(row)
            remote_info = self._remote_work_info(row)
            fit_review = self._posting_fit_review(row, features)
            posting_sido = safe_text(row.get("sido"), "unknown")
            posting_sigungu = safe_text(row.get("sigungu"), "unknown")
            has_profile_sigungu = bool(profile_sigungu and profile_sigungu != "unknown")

            rows.append(
                {
                    "age": clean_value(features.get("age")) or 0,
                    "age_group": age_group,
                    "disability_type": disability,
                    "severity": severity,
                    "profile_sido": profile_sido,
                    "profile_sigungu": profile_sigungu,
                    "posting_sido": posting_sido,
                    "posting_sigungu": posting_sigungu,
                    "posting_job_class": posting_job_class,
                    "employment_type": safe_text(row.get("employment_type"), "unknown"),
                    "wage_type": safe_text(row.get("wage_type"), "unknown"),
                    "job_title": job_title,
                    "same_sido": int(profile_sido == posting_sido),
                    "same_sigungu": int(has_profile_sigungu and profile_sigungu == posting_sigungu),
                    "job_class_model_score": float(job_class_model_score or 0.0),
                    "monthly_wage": monthly_wage_equivalent(row.get("wage_type"), row.get("wage_amount")) or 0.0,
                    "has_wage": int(clean_value(row.get("wage_amount")) is not None),
                    "title_total_count": int(self._desired_title_total.get(job_title, 0)),
                    "title_disability_count": int(self._desired_title_disability.get((job_title, disability), 0)),
                    "title_profile_count": int(self._desired_title_profile.get((job_title, disability, severity), 0)),
                    "class_profile_count": class_profile_count,
                    "class_profile_share": class_profile_share,
                    "is_standard_workplace": int(bool(standard_info.get("isStandard"))),
                    "has_remote_keyword": int(bool(remote_info.get("hasRemoteKeyword"))),
                    "visual_review_penalty": int(fit_review.get("penalty") or 0),
                }
            )

        frame = pd.DataFrame(rows)
        for column in self.ranker_feature_columns:
            if column not in frame.columns:
                frame[column] = "unknown" if column in self.ranker_categorical_columns else 0
        return frame

    def _prepare_ranker_feature_matrix(self, frame: pd.DataFrame) -> pd.DataFrame:
        X = frame[self.ranker_feature_columns].copy()
        for column in self.ranker_categorical_columns:
            values = X[column].astype("string").fillna("unknown")
            levels = self.ranker_category_levels.get(column)
            if levels:
                allowed = set(levels)
                values = values.where(values.isin(allowed), "unknown")
                X[column] = pd.Categorical(values, categories=levels)
            else:
                X[column] = values.astype("category")
        for column in self.ranker_feature_columns:
            if column not in self.ranker_categorical_columns:
                X[column] = pd.to_numeric(X[column], errors="coerce").fillna(0)
        return X

    def _confidence_for(self, score: int, fit_review: dict[str, Any]) -> str:
        if fit_review.get("level") == "review":
            return "review"
        if score >= 78:
            return "high"
        if score >= 62:
            return "medium"
        return "low"

    def _find_prior_row(
        self,
        df: pd.DataFrame,
        job_class: str,
        sido: str | None = None,
        sigungu: str | None = None,
    ) -> pd.Series | None:
        if df.empty or "target_job_class" not in df:
            return None
        mask = df["target_job_class"] == job_class
        if sido is not None and "sido" in df:
            mask &= df["sido"] == sido
        if sigungu is not None and "sigungu" in df:
            mask &= df["sigungu"] == sigungu
        result = df[mask]
        if result.empty:
            return None
        return result.iloc[0]

    def _build_recommendations(
        self,
        predictions: list[dict[str, Any]],
        features: dict[str, Any],
        preferences: dict[str, Any],
    ) -> list[dict[str, Any]]:
        recs = []
        used_posting_ids: set[str] = set()
        for idx, prediction in enumerate(predictions, start=1):
            posting = self._best_posting(
                prediction["jobClass"],
                features,
                preferences,
                used_posting_ids,
                prediction.get("probability", 0.0),
            )
            if posting is not None:
                posting_id = safe_text(posting.get("posting_id"))
                if posting_id:
                    used_posting_ids.add(posting_id)
                title = safe_text(posting.get("job_title"), prediction["jobClass"])
                company = safe_text(posting.get("company_name"), "채용공고")
                region = self._posting_region(posting, features)
                employment = safe_text(posting.get("employment_type"), "고용형태 확인")
                wage = format_wage(posting.get("wage_type"), posting.get("wage_amount"))
            else:
                title = prediction["jobClass"]
                company = "학습 데이터 기반 직무군"
                region = self._feature_region(features)
                employment = "추천 직무군"
                wage = "임금 데이터 없음"
            fit_review = self._posting_fit_review(posting, features)
            score_adjustment = int(fit_review.get("boost") or 0) - int(fit_review.get("penalty") or 0)
            score = max(45, min(97, self._recommendation_score(prediction, idx) + score_adjustment))
            reason_details = self._reason_details(prediction, features, preferences, posting, fit_review)
            standard_info = self._standard_workplace_info(posting)
            recs.append(
                {
                    "rank": idx,
                    "title": title,
                    "company": company,
                    "region": region,
                    "employment": employment,
                    "wage": wage,
                    "score": score,
                    "confidence": self._confidence_for(score, fit_review),
                    "predictedJobClass": prediction["jobClass"],
                    "probability": prediction["probability"],
                    "fitReview": fit_review,
                    "standard": bool(standard_info.get("isStandard")),
                    "standardWorkplace": standard_info,
                    "reasonDetails": reason_details,
                    "reasons": self._reasons(prediction, features, preferences, posting, fit_review, reason_details),
                }
            )
        return recs

    def _connected_jobs(
        self,
        predictions: list[dict[str, Any]],
        features: dict[str, Any],
        preferences: dict[str, Any],
        limit: int,
    ) -> list[dict[str, Any]]:
        jobs: list[dict[str, Any]] = []
        used: set[str] = set()
        for prediction in predictions:
            rows = self._candidate_postings(
                prediction["jobClass"],
                features,
                preferences,
                prediction.get("probability", 0.0),
            ).head(limit)
            for _, row in rows.iterrows():
                posting_id = safe_text(row.get("posting_id"))
                if posting_id in used:
                    continue
                used.add(posting_id)
                jobs.append(self._posting_to_ui_job(row, prediction, features))
                if len(jobs) >= limit:
                    return jobs
        return jobs

    def _best_posting(
        self,
        job_class: str,
        features: dict[str, Any],
        preferences: dict[str, Any],
        used_posting_ids: set[str],
        job_class_model_score: float = 0.0,
    ) -> pd.Series | None:
        rows = self._candidate_postings(job_class, features, preferences, job_class_model_score)
        if rows.empty:
            return None
        for _, row in rows.iterrows():
            if safe_text(row.get("posting_id")) not in used_posting_ids:
                return row
        return rows.iloc[0]

    def _candidate_postings(
        self,
        job_class: str,
        features: dict[str, Any],
        preferences: dict[str, Any],
        job_class_model_score: float = 0.0,
    ) -> pd.DataFrame:
        postings, _, _ = self._postings_state_snapshot()
        postings = self._current_postings(postings)
        if postings.empty or "target_job_class_candidate" not in postings:
            return pd.DataFrame()
        df = postings[postings["target_job_class_candidate"] == job_class].copy()
        if df.empty:
            return df
        desired_wage = preferences.get("desired_wage")
        profile_sigungu = safe_text(features.get("sigungu"), "unknown")
        has_profile_sigungu = bool(profile_sigungu and profile_sigungu != "unknown")
        if "sigungu" in df:
            df["_same_sigungu"] = ((df["sigungu"] == profile_sigungu) & has_profile_sigungu).astype(int)
        else:
            df["_same_sigungu"] = 0
        df["_same_sido"] = (df.get("sido") == features.get("sido")).astype(int)
        if desired_wage:
            df["_wage_match"] = df.apply(
                lambda row: int(wage_matches_preference(row.get("wage_type"), row.get("wage_amount"), desired_wage)),
                axis=1,
            )
        else:
            df["_wage_match"] = 0
        df["_has_wage"] = df["wage_amount"].notna().astype(int) if "wage_amount" in df else 0
        fit_reviews = df.apply(lambda row: self._posting_fit_review(row, features), axis=1)
        df["_fit_penalty"] = fit_reviews.map(lambda item: int(item.get("penalty") or 0))
        df["_fit_boost"] = fit_reviews.map(lambda item: int(item.get("boost") or 0))
        df["_standard_boost"] = df.apply(
            lambda row: int(self._standard_workplace_info(row).get("isStandard", False)) * (2 if features.get("severity") == "중증" else 1),
            axis=1,
        )
        df["_heuristic_score"] = (
            df["_same_sigungu"] * 20
            + df["_same_sido"] * 10
            + df["_wage_match"] * 2
            + df["_has_wage"]
            + df["_fit_boost"]
            + df["_standard_boost"]
            - df["_fit_penalty"]
        )
        df["_ranker_score"] = 0.0
        df["_ranker_norm"] = 0.0
        if self.ranker_model is not None and self.ranker_feature_columns:
            ranker_frame = self._ranker_feature_frame(df, features, job_class_model_score)
            ranker_X = self._prepare_ranker_feature_matrix(ranker_frame)
            df["_ranker_score"] = self.ranker_model.predict(ranker_X[self.ranker_feature_columns])
            score_min = float(df["_ranker_score"].min())
            score_max = float(df["_ranker_score"].max())
            if score_max > score_min:
                df["_ranker_norm"] = (df["_ranker_score"] - score_min) / (score_max - score_min)
        df["_sort_score"] = df["_heuristic_score"] + df["_ranker_norm"] * 10
        return df.sort_values(["_sort_score", "_ranker_score", "posting_id"], ascending=[False, False, True])

    def _posting_to_ui_job(self, row: pd.Series, prediction: dict[str, Any], features: dict[str, Any]) -> dict[str, Any]:
        title = safe_text(row.get("job_title"), prediction["jobClass"])
        company = safe_text(row.get("company_name"), "채용공고")
        region = self._posting_region(row, {})
        wage = format_wage(row.get("wage_type"), row.get("wage_amount"))
        recruit_period = safe_text(row.get("recruit_period_raw"), "채용기간 확인 필요")
        recruit_start = safe_text(row.get("recruit_start"))
        recruit_end = safe_text(row.get("recruit_end"))
        deadline_label = format_recruit_deadline(recruit_end) or recruit_period
        career = safe_text(row.get("required_career"), "경력 확인 필요")
        education = safe_text(row.get("required_education"), "학력 확인 필요")
        registered_date = safe_text(row.get("registered_date"))
        offer_registered_date = safe_text(row.get("offer_registered_date"))
        contact = safe_text(row.get("contact_phone"))
        agency = safe_text(row.get("agency_name"))
        fit_review = self._posting_fit_review(row, features)
        standard_info = self._standard_workplace_info(row)
        score_adjustment = int(fit_review.get("boost") or 0) - int(fit_review.get("penalty") or 0)
        score = max(45, min(97, self._recommendation_score(prediction, 1) + score_adjustment))
        prefs = [
            f"모델 예측 직무군: {prediction['jobClass']}",
            f"임금 조건: {wage}",
            "KEAD 실시간 구인 API 기반 연결",
        ]
        if agency:
            prefs.append(f"접수기관: {agency}")
        if contact:
            prefs.append(f"문의처: {contact}")
        prefs.extend(fit_review.get("notes") or [])
        environment_specs = (
            (
                "bothHands",
                "양손작업",
                "hand",
                "env_both_hands",
                {
                    "한손작업 가능": ("한손작업 가능", "요구 낮음", "jb-env-meter--low", "width:33%;", 1),
                    "한손보조작업 가능": ("한손 보조 가능", "확인 필요", "jb-env-meter--mid", "width:66%;", 2),
                    "양손작업 가능": ("양손작업 가능", "요구 높음", "jb-env-meter--high", "width:100%;", 3),
                },
            ),
            (
                "eyesight",
                "시력",
                "eye",
                "env_eyesight",
                {
                    "일상적 활동 가능": ("일상 활동 가능", "요구 낮음", "jb-env-meter--low", "width:33%;", 1),
                    "비교적 큰 인쇄물을 읽을 수 있음": ("큰 글씨 판독", "확인 필요", "jb-env-meter--mid", "width:66%;", 2),
                    "아주 작은 글씨를 읽을 수 있음": ("작은 글씨 판독", "요구 높음", "jb-env-meter--high", "width:100%;", 3),
                },
            ),
            (
                "handwork",
                "손작업",
                "mouse-pointer-2",
                "env_handwork",
                {
                    "큰 물품 조립가능": ("큰 물품 조립", "요구 낮음", "jb-env-meter--low", "width:33%;", 1),
                    "작은 물품 조립가능": ("작은 물품 조립", "확인 필요", "jb-env-meter--mid", "width:66%;", 2),
                    "정밀한 작업가능": ("정밀 작업 가능", "요구 높음", "jb-env-meter--high", "width:100%;", 3),
                },
            ),
            (
                "liftPower",
                "드는 힘",
                "dumbbell",
                "env_lift_power",
                {
                    "5Kg 이내의 물건을 다룰 수 있음": ("5kg 이내", "요구 낮음", "jb-env-meter--low", "width:33%;", 1),
                    "5~20Kg의 물건을 다룰 수 있음": ("5~20kg", "확인 필요", "jb-env-meter--mid", "width:66%;", 2),
                    "20Kg 이상의 물건을 다룰 수 있음": ("20kg 이상", "요구 높음", "jb-env-meter--high", "width:100%;", 3),
                },
            ),
            (
                "listenTalk",
                "듣고 말하기",
                "ear",
                "env_lstn_talk",
                {
                    "듣고 말하는 작업 어려움": ("어려움 있음", "요구 낮음", "jb-env-meter--low", "width:33%;", 1),
                    "간단한 듣고 말하기 가능": ("간단한 의사소통", "확인 필요", "jb-env-meter--mid", "width:66%;", 2),
                    "듣고 말하기에 어려움 없음": ("어려움 없음", "요구 높음", "jb-env-meter--high", "width:100%;", 3),
                },
            ),
            (
                "standWalk",
                "서거나 걷기",
                "footprints",
                "env_stnd_walk",
                {
                    "서거나 걷는 일 어려움": ("걷기 어려움", "요구 낮음", "jb-env-meter--low", "width:33%;", 1),
                    "일부 서서하는 작업 가능": ("일부 서서 작업", "확인 필요", "jb-env-meter--mid", "width:66%;", 2),
                    "오랫동안 가능": ("오랫동안 가능", "요구 높음", "jb-env-meter--high", "width:100%;", 3),
                },
            ),
        )
        environment = []
        environment_items = []
        for key, label, icon, column, value_map in environment_specs:
            value = safe_text(row.get(column))
            if value:
                environment.append(f"{label}: {value}")
            mapped = value_map.get(value)
            if mapped:
                display_value, level_text, level_class, width_style, level = mapped
                environment_items.append(
                    {
                        "key": key,
                        "label": label,
                        "icon": icon,
                        "rawValue": value,
                        "displayValue": display_value,
                        "provided": True,
                        "level": level,
                        "levelText": level_text,
                        "levelClass": level_class,
                        "widthStyle": width_style,
                    }
                )
            else:
                environment_items.append(
                    {
                        "key": key,
                        "label": label,
                        "icon": icon,
                        "rawValue": value,
                        "displayValue": value or "정보 없음",
                        "provided": bool(value),
                        "level": 0,
                        "levelText": "정보 없음",
                        "levelClass": "jb-env-meter--missing",
                        "widthStyle": "width:0%;",
                    }
                )
        source_label = "KEAD 작업환경 포함 실시간 API" if environment else "KEAD 실시간 구인 API"
        return {
            "id": f"api-{safe_text(row.get('posting_id'), uuid.uuid4().hex)}",
            "title": title,
            "company": company,
            "region": region,
            "jobType": prediction["bucket"],
            "employment": safe_text(row.get("employment_type"), "고용형태 확인"),
            "wage": wage,
            "wageType": safe_text(row.get("wage_type")),
            "wageAmount": clean_value(row.get("wage_amount")),
            "standard": bool(standard_info.get("isStandard")),
            "linked": True,
            "score": score,
            "showScore": True,
            "statusLabel": format_recruit_status(recruit_end),
            "fitReview": fit_review,
            "standardWorkplace": standard_info,
            "fitBadgeLabel": "업무조정 검토" if fit_review.get("level") == "review" else "",
            "summary": f"{prediction['jobClass']} 직무군과 연결된 실제 채용공고입니다.",
            "duties": [
                safe_text(row.get("reference_small"), title),
                safe_text(row.get("required_career"), "경력 조건 확인 필요"),
                safe_text(row.get("required_education"), "학력 조건 확인 필요"),
                "세부 직무 내용은 원천 공고 확인이 필요합니다.",
            ],
            "location": safe_text(row.get("address_raw"), region),
            "hours": safe_text(row.get("work_time"), "근무시간 원문 확인 필요"),
            "recruitPeriod": recruit_period,
            "recruitStart": recruit_start,
            "recruitEnd": recruit_end,
            "deadlineLabel": deadline_label,
            "career": career,
            "education": education,
            "registeredDate": registered_date,
            "offerRegisteredDate": offer_registered_date,
            "contact": contact,
            "agency": agency,
            "sourceLabel": source_label,
            "environment": environment,
            "environmentItems": environment_items,
            "envBothHands": safe_text(row.get("env_both_hands")),
            "envEyesight": safe_text(row.get("env_eyesight")),
            "envHandWork": safe_text(row.get("env_handwork")),
            "envLiftPower": safe_text(row.get("env_lift_power")),
            "envLstnTalk": safe_text(row.get("env_lstn_talk")),
            "envStndWalk": safe_text(row.get("env_stnd_walk")),
            "hasEnvironmentDetail": bool(environment),
            "hasEnvironment": bool(environment),
            "prefs": prefs,
        }

    def _posting_region(self, row: pd.Series, features: dict[str, Any]) -> str:
        sido = safe_text(row.get("sido"), safe_text(features.get("sido"), "지역 미상"))
        sigungu = safe_text(row.get("sigungu"))
        if sigungu and sigungu != "unknown":
            return f"{sido} {sigungu}"
        return sido

    def _feature_region(self, features: dict[str, Any]) -> str:
        sido = safe_text(features.get("sido"), "지역 미상")
        sigungu = safe_text(features.get("sigungu"))
        if sigungu and sigungu != "unknown":
            return f"{sido} {sigungu}"
        return f"{sido} 전체"

    def _prior_factor_pct(self, prior: dict[str, Any]) -> int:
        posting_share = float(prior.get("postingShare") or 0)
        seeker_share = float(prior.get("seekerShare") or 0)
        posting_count = int(prior.get("postingCount") or 0)
        return max(45, min(94, int(round(58 + posting_share * 35 + seeker_share * 12 + min(posting_count, 20)))))

    def _recommendation_score(self, prediction: dict[str, Any], rank: int) -> int:
        relative = float(prediction.get("relativeModelScore") or 0)
        prior = prediction.get("marketPrior") or {}
        preference = float(prediction.get("preferenceBoost") or 0)
        prior_pct = self._prior_factor_pct(prior)
        score = 56 + relative * 24 + (prior_pct - 50) * 0.18 + preference * 45 - (rank - 1) * 4
        return max(55, min(97, int(round(score))))

    def _reason_details(
        self,
        prediction: dict[str, Any],
        features: dict[str, Any],
        preferences: dict[str, Any],
        posting: pd.Series | None,
        fit_review: dict[str, Any],
    ) -> list[dict[str, Any]]:
        evidence = self._profile_evidence(features, prediction["jobClass"])
        standard_info = self._standard_workplace_info(posting)
        access = self._accessibility_review(posting, features, standard_info)
        region = self._feature_region(features)
        job_class = prediction["jobClass"]
        desired_class = safe_text(preferences.get("desired_job_class"), "전체 직종")
        desired_wage = safe_text(preferences.get("desired_wage"), "전체 임금")
        wage = format_wage(posting.get("wage_type"), posting.get("wage_amount")) if posting is not None else "임금 데이터 없음"
        reference = safe_text(posting.get("reference_small")) if posting is not None else ""
        if not reference:
            reference = safe_text(posting.get("reference_mid")) if posting is not None else ""
        if not reference:
            reference = job_class

        type_lines = [
            f"{features.get('disability_type')}({features.get('severity')}) · {features.get('age_group')} · {region} 조건을 {evidence['scope']} 범위의 취업 사례와 비교했습니다.",
            f"유사 사례 {evidence['similarCount']}건 중 '{job_class}' 사례 {evidence['jobClassCount']}건({evidence['jobClassSharePct']}%)을 확인했습니다.",
        ]
        if evidence.get("rank"):
            type_lines.append(f"해당 조건에서 '{job_class}'은 취업 직무군 순위 {evidence['rank']}위입니다.")
        if fit_review.get("notes"):
            type_lines.extend(fit_review["notes"][:1])

        standard_lines = []
        if standard_info.get("isStandard"):
            standard_lines.extend(
                [
                    f"'{standard_info['matchedName']}'이 장애인 표준사업장 목록과 매칭되었습니다.",
                    "표준사업장은 장애인 다수고용 인증 사업장으로 편의시설·고용 의지 측면에서 우선 검토할 수 있습니다.",
                ]
            )
        else:
            standard_lines.extend(
                [
                    "현재 공고의 사업장은 표준사업장 목록과 직접 매칭되지 않았습니다.",
                    "표준사업장 여부, 편의시설, 직무조정 가능성은 공고 담당기관 또는 사업장 확인이 필요합니다.",
                ]
            )
        standard_lines.append("표준사업장이 재택근무를 자동 보장하지는 않습니다.")

        access_lines = []
        if access["sameSigungu"]:
            access_lines.append("희망 시군구와 같은 지역 공고라 이동 부담을 낮게 평가했습니다.")
        elif access["sameSido"]:
            access_lines.append("희망 시도 안의 공고라 지역 접근성을 일부 반영했습니다.")
        else:
            access_lines.append("희망지역과 떨어진 공고라 실제 통근 가능성 확인이 필요합니다.")
        if access["hasRemoteKeyword"]:
            access_lines.append(f"공고 텍스트에서 '{access['matchedKeyword']}' 키워드를 확인했습니다.")
        else:
            access_lines.append("공공 구인 데이터에 재택근무 전용 필드가 없어 재택 가능 여부는 개별 확인이 필요합니다.")

        data_lines = [
            f"정확 조건({features.get('disability_type')}·{features.get('severity')}·{features.get('age_group')}·{region}) 취업 사례는 {evidence['exactCount']}건입니다.",
        ]
        if evidence["exactCount"] < 30:
            data_lines.append(
                f"표본이 적어 {evidence['scope']} 범위의 유사 사례 {evidence['similarCount']}건을 보완 참조했습니다."
            )
        else:
            data_lines.append("정확 조건 표본이 30건 이상이라 같은 조건의 사례를 우선 반영했습니다.")

        return [
            {
                "title": "① 유형적합도",
                "scoreLabel": f"{min(30, 16 + min(14, evidence['jobClassSharePct'] // 3))}/30",
                "lines": type_lines[:3],
            },
            {
                "title": "② 표준사업장·편의제공 검토",
                "scoreLabel": "확인 필요" if not standard_info.get("isStandard") else "우선 검토",
                "lines": standard_lines,
            },
            {
                "title": "③ 접근성 스코어",
                "scoreLabel": f"{access['score']}/100",
                "lines": access_lines,
            },
            {
                "title": "④ 희망부합도",
                "scoreLabel": "보정 기준",
                "lines": [
                    f"희망직종: {desired_class} / 예측 직무군: {job_class}",
                    f"희망임금: {desired_wage} / 공고 임금: {wage}",
                    "희망직종과 희망임금은 모델 입력이 아니라 최종 점수 보정·필터링 기준으로만 사용했습니다.",
                ],
            },
            {
                "title": "⚠ 데이터 안내",
                "scoreLabel": "투명성",
                "lines": data_lines,
            },
            {
                "title": "💡 NCS·직무 역량 안내",
                "scoreLabel": "요약",
                "lines": [
                    f"현재 공고의 세부 직무 기준: {reference}",
                    "기본 6개 입력만으로는 전공·자격증·경력 역량 매칭을 확정할 수 없습니다.",
                    "이력서 또는 자격 정보를 추가하면 역량 기반 분석으로 확장할 수 있습니다.",
                ],
            },
        ]

    def _reasons(
        self,
        prediction: dict[str, Any],
        features: dict[str, Any],
        preferences: dict[str, Any],
        posting: pd.Series | None,
        fit_review: dict[str, Any] | None = None,
        reason_details: list[dict[str, Any]] | None = None,
    ) -> list[str]:
        if reason_details:
            return [
                f"{block['title']}: {block['lines'][0]}"
                for block in reason_details
                if block.get("lines")
            ][:5]
        probability_pct = round(float(prediction["probability"]) * 100, 1)
        region = self._feature_region(features)
        prior = prediction["marketPrior"]
        reasons = [
            f"LightGBM 모델이 입력 프로필에서 '{prediction['jobClass']}' 직무군을 상위 후보로 예측했습니다. (모델 확률 {probability_pct}%)",
            f"{region} 기준 공고 {prior.get('postingCount', 0)}건, 구직 수요 {prior.get('seekerCount', 0)}건의 지역 신호를 함께 반영했습니다.",
        ]
        desired_class = preferences.get("desired_job_class")
        desired_wage = preferences.get("desired_wage")
        if desired_class:
            reasons.append("희망직종은 모델 입력이 아니라 최종 점수 보정으로만 사용했습니다.")
        if desired_wage:
            reasons.append(f"희망임금 '{desired_wage}' 조건에 맞는 공고를 우선 연결했습니다.")
        if fit_review and fit_review.get("level") != "ok":
            reasons.extend(fit_review.get("notes") or [])
        if posting is not None:
            reasons.append("실제 공공 구인공고와 직무군 매핑이 확인된 항목입니다.")
        return reasons[:5]
