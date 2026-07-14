from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import joblib


VISUAL_DISABILITY = "시각장애"
SEVERE = "중증"
REMOTE_WORK_KEYWORDS = ("재택", "원격", "재택병행", "자택", "비대면")
VISUAL_REVIEW_TERMS = ("안내", "동선", "주차", "운전", "배송", "배달", "검수", "검사", "진열", "순찰", "경비")

FEATURE_COLUMNS = [
    "age",
    "same_sido",
    "same_sigungu",
    "job_class_model_score",
    "monthly_wage",
    "has_wage",
    "title_total_count",
    "title_disability_count",
    "title_profile_count",
    "class_profile_count",
    "class_profile_share",
    "is_standard_workplace",
    "has_remote_keyword",
    "visual_review_penalty",
]

CATEGORICAL_COLUMNS = [
    "age_group",
    "disability_type",
    "severity",
    "profile_sido",
    "profile_sigungu",
    "posting_sido",
    "posting_sigungu",
    "posting_job_class",
    "employment_type",
    "wage_type",
    "job_title",
]


def clean_text(value: Any, fallback: str = "") -> str:
    if value is None or pd.isna(value):
        return fallback
    text = str(value).strip()
    return text if text else fallback


def normalize_company_name(value: Any) -> str:
    text = clean_text(value).lower()
    for token in ("주식회사", "(주)", "㈜", "의료법인", "재단법인", "사단법인", "사회복지법인"):
        text = text.replace(token, "")
    for char in (" ", "\t", "\n", "·", ".", ",", "-", "_", "(", ")", "（", "）"):
        text = text.replace(char, "")
    return text


def monthly_wage_equivalent(wage_type: Any, wage_amount: Any) -> float:
    if wage_amount is None or pd.isna(wage_amount):
        return 0.0
    try:
        amount = float(wage_amount)
    except (TypeError, ValueError):
        return 0.0
    wage_type_text = clean_text(wage_type)
    if "시급" in wage_type_text:
        return amount * 209
    if "일급" in wage_type_text:
        return amount * 22
    if "연봉" in wage_type_text:
        return amount / 12
    return amount


def read_csv(path: Path, *, encoding: str = "utf-8-sig") -> pd.DataFrame:
    return pd.read_csv(path, encoding=encoding)


def read_standard_workplaces(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    for encoding in ("cp949", "utf-8-sig", "utf-8"):
        try:
            return pd.read_csv(path, encoding=encoding)
        except UnicodeDecodeError:
            continue
    return pd.DataFrame()


def build_profile_model_scores(
    seekers: pd.DataFrame,
    model_path: Path,
) -> tuple[dict[int, np.ndarray], dict[str, int], bool]:
    if not model_path.exists():
        return {}, {}, False
    artifact = joblib.load(model_path)
    model = artifact["model"]
    label_encoder = artifact["label_encoder"]
    feature_columns = list(artifact["feature_columns"])
    categorical_columns = list(artifact["categorical_columns"])

    missing_columns = sorted(set(feature_columns) - set(seekers.columns))
    if missing_columns:
        raise ValueError(f"Profile model feature columns missing from seekers: {missing_columns}")

    X = seekers[feature_columns].copy()
    for column in categorical_columns:
        X[column] = X[column].astype("string").fillna("unknown").astype("category")
    for column in feature_columns:
        if column not in categorical_columns:
            X[column] = pd.to_numeric(X[column], errors="coerce").fillna(0)

    proba = model.predict_proba(X)
    class_index = {str(name): idx for idx, name in enumerate(label_encoder.classes_)}
    score_rows = {
        int(seeker_id): proba[row_index]
        for row_index, seeker_id in enumerate(seekers["seeker_id"].astype(int).to_numpy())
    }
    return score_rows, class_index, True


def standard_company_keys(standard_workplaces: pd.DataFrame) -> set[str]:
    if standard_workplaces.empty:
        return set()
    column = "사업체명" if "사업체명" in standard_workplaces.columns else standard_workplaces.columns[1]
    return {
        normalized
        for normalized in standard_workplaces[column].map(normalize_company_name)
        if normalized
    }


def is_standard_company(company: Any, standard_keys: set[str]) -> int:
    normalized = normalize_company_name(company)
    if not normalized or not standard_keys:
        return 0
    if normalized in standard_keys:
        return 1
    for key in standard_keys:
        if len(key) >= 4 and (key in normalized or normalized in key):
            return 1
    return 0


def has_remote_keyword(row: pd.Series) -> int:
    text = " ".join(
        clean_text(row.get(column))
        for column in ("job_title", "company_name", "employment_type", "address_raw", "recruit_period_raw")
    )
    return int(any(keyword in text for keyword in REMOTE_WORK_KEYWORDS))


def visual_review_penalty(disability_type: str, severity: str, job_title: str) -> int:
    if disability_type != VISUAL_DISABILITY or severity != SEVERE:
        return 0
    return 16 if any(term in job_title for term in VISUAL_REVIEW_TERMS) else 0


def make_count_maps(seekers: pd.DataFrame) -> dict[str, dict[Any, int]]:
    clean = seekers.dropna(subset=["desired_job_title", "target_job_class_candidate"]).copy()
    for column in ("desired_job_title", "disability_type", "severity", "age_group", "target_job_class_candidate"):
        clean[column] = clean[column].map(clean_text)

    profile_group = ["disability_type", "severity", "age_group"]
    class_counts = clean.groupby([*profile_group, "target_job_class_candidate"]).size()
    profile_counts = clean.groupby(profile_group).size()
    return {
        "title_total": clean.groupby("desired_job_title").size().to_dict(),
        "title_disability": clean.groupby(["desired_job_title", "disability_type"]).size().to_dict(),
        "title_profile": clean.groupby(["desired_job_title", "disability_type", "severity"]).size().to_dict(),
        "class_profile": class_counts.to_dict(),
        "profile_total": profile_counts.to_dict(),
    }


def posting_feature_row(
    seeker: pd.Series,
    posting: pd.Series,
    relevance: int,
    label_source: str,
    counts: dict[str, dict[Any, int]],
    standard_keys: set[str],
    profile_model_scores: dict[int, np.ndarray],
    profile_model_class_index: dict[str, int],
) -> dict[str, Any]:
    job_title = clean_text(posting.get("job_title"), "unknown")
    disability_type = clean_text(seeker.get("disability_type"), "unknown")
    severity = clean_text(seeker.get("severity"), "unknown")
    age_group = clean_text(seeker.get("age_group"), "unknown")
    posting_job_class = clean_text(posting.get("target_job_class_candidate"), "unknown")
    seeker_id = int(seeker.get("seeker_id"))
    score_row = profile_model_scores.get(seeker_id)
    score_index = profile_model_class_index.get(posting_job_class)
    job_class_model_score = float(score_row[score_index]) if score_row is not None and score_index is not None else 0.0
    profile_key = (disability_type, severity, age_group)
    class_key = (*profile_key, posting_job_class)
    profile_total = int(counts["profile_total"].get(profile_key, 0))
    class_profile_count = int(counts["class_profile"].get(class_key, 0))
    class_profile_share = class_profile_count / profile_total if profile_total else 0.0

    return {
        "group_id": seeker_id,
        "seeker_id": seeker_id,
        "posting_id": int(posting.get("posting_id")),
        "relevance": int(relevance),
        "label_source": label_source,
        "age": int(seeker.get("age")) if not pd.isna(seeker.get("age")) else 0,
        "age_group": age_group,
        "disability_type": disability_type,
        "severity": severity,
        "profile_sido": clean_text(seeker.get("sido"), "unknown"),
        "profile_sigungu": clean_text(seeker.get("sigungu"), "unknown"),
        "posting_sido": clean_text(posting.get("sido"), "unknown"),
        "posting_sigungu": clean_text(posting.get("sigungu"), "unknown"),
        "posting_job_class": posting_job_class,
        "employment_type": clean_text(posting.get("employment_type"), "unknown"),
        "wage_type": clean_text(posting.get("wage_type"), "unknown"),
        "job_title": job_title,
        "same_sido": int(clean_text(seeker.get("sido")) == clean_text(posting.get("sido"))),
        "same_sigungu": int(clean_text(seeker.get("sigungu")) == clean_text(posting.get("sigungu"))),
        "job_class_model_score": job_class_model_score,
        "monthly_wage": monthly_wage_equivalent(posting.get("wage_type"), posting.get("wage_amount")),
        "has_wage": int(not pd.isna(posting.get("wage_amount"))),
        "title_total_count": int(counts["title_total"].get(job_title, 0)),
        "title_disability_count": int(counts["title_disability"].get((job_title, disability_type), 0)),
        "title_profile_count": int(counts["title_profile"].get((job_title, disability_type, severity), 0)),
        "class_profile_count": class_profile_count,
        "class_profile_share": class_profile_share,
        "is_standard_workplace": is_standard_company(posting.get("company_name"), standard_keys),
        "has_remote_keyword": has_remote_keyword(posting),
        "visual_review_penalty": visual_review_penalty(disability_type, severity, job_title),
        "desired_job_title": clean_text(seeker.get("desired_job_title"), "unknown"),
        "company_name": clean_text(posting.get("company_name"), "unknown"),
    }


def local_sort(frame: pd.DataFrame, seeker: pd.Series) -> pd.DataFrame:
    if frame.empty:
        return frame
    df = frame.copy()
    df["_same_sigungu"] = (df["sigungu"].fillna("unknown") == clean_text(seeker.get("sigungu"), "unknown")).astype(int)
    df["_same_sido"] = (df["sido"].fillna("unknown") == clean_text(seeker.get("sido"), "unknown")).astype(int)
    df["_has_wage"] = df["wage_amount"].notna().astype(int)
    return df.sort_values(["_same_sigungu", "_same_sido", "_has_wage", "posting_id"], ascending=[False, False, False, True])


def build_dataset(args: argparse.Namespace) -> tuple[pd.DataFrame, dict[str, Any]]:
    data_dir = Path(args.data_dir)
    seekers = read_csv(data_dir / "job_seekers_normalized.csv")
    postings = read_csv(data_dir / "job_postings_normalized.csv")
    standard_workplaces = read_standard_workplaces(Path(args.standard_workplaces))
    standard_keys = standard_company_keys(standard_workplaces)

    seekers = seekers.dropna(subset=["seeker_id", "desired_job_title", "target_job_class_candidate", "age", "age_group", "disability_type", "severity"]).copy()
    seekers = seekers[seekers["is_official_disability_type"].astype(bool)].copy()
    postings = postings.dropna(subset=["posting_id", "job_title", "target_job_class_candidate"]).copy()
    postings["posting_id"] = postings["posting_id"].astype(int)

    if args.max_seekers and args.max_seekers > 0 and len(seekers) > args.max_seekers:
        seekers = seekers.sample(n=args.max_seekers, random_state=args.random_state).sort_values("seeker_id")

    counts = make_count_maps(seekers)
    profile_model_scores, profile_model_class_index, profile_model_available = build_profile_model_scores(
        seekers,
        Path(args.profile_model),
    )
    postings_by_title = {str(title): df for title, df in postings.groupby("job_title", dropna=False)}
    postings_by_class = {str(job_class): df for job_class, df in postings.groupby("target_job_class_candidate", dropna=False)}
    posting_indices = postings.index.to_numpy()
    rng = np.random.default_rng(args.random_state)
    rows: list[dict[str, Any]] = []
    group_count = 0

    for _, seeker in seekers.iterrows():
        desired_title = clean_text(seeker.get("desired_job_title"))
        target_class = clean_text(seeker.get("target_job_class_candidate"))
        if not desired_title or not target_class:
            continue

        selected: list[tuple[pd.Series, int, str]] = []
        used_posting_ids: set[int] = set()

        exact = postings_by_title.get(desired_title, pd.DataFrame())
        for _, posting in local_sort(exact, seeker).head(args.exact_positive_per_group).iterrows():
            used_posting_ids.add(int(posting["posting_id"]))
            selected.append((posting, 3, "exact_title"))

        same_class = postings_by_class.get(target_class)
        if same_class is not None and not same_class.empty:
            same_class = same_class[~same_class["posting_id"].isin(used_posting_ids)]
            same_label = 2 if selected else 3
            for _, posting in local_sort(same_class, seeker).head(args.same_class_positive_per_group).iterrows():
                used_posting_ids.add(int(posting["posting_id"]))
                selected.append((posting, same_label, "same_job_class"))

        if not selected:
            continue

        negative_pool = postings[
            (postings["target_job_class_candidate"] != target_class)
            & (~postings["posting_id"].isin(used_posting_ids))
        ]
        if len(negative_pool) > 0:
            take = min(args.negative_per_group, len(negative_pool))
            negative_positions = rng.choice(negative_pool.index.to_numpy(), size=take, replace=False)
            for _, posting in postings.loc[negative_positions].iterrows():
                selected.append((posting, 0, "other_job_class"))

        if len(selected) < 2:
            continue
        group_count += 1
        for posting, relevance, label_source in selected:
            rows.append(
                posting_feature_row(
                    seeker,
                    posting,
                    relevance,
                    label_source,
                    counts,
                    standard_keys,
                    profile_model_scores,
                    profile_model_class_index,
                )
            )

    dataset = pd.DataFrame(rows).sort_values(["group_id", "relevance"], ascending=[True, False]).reset_index(drop=True)
    summary = {
        "rows": int(len(dataset)),
        "groups": int(group_count),
        "seekers_used": int(seekers["seeker_id"].nunique()),
        "postings_used": int(dataset["posting_id"].nunique()) if len(dataset) else 0,
        "standard_workplace_rows": int(len(standard_workplaces)),
        "standard_company_keys": int(len(standard_keys)),
        "feature_columns": FEATURE_COLUMNS,
        "categorical_columns": CATEGORICAL_COLUMNS,
        "profile_model": str(Path(args.profile_model)),
        "profile_model_score_available": bool(profile_model_available),
        "profile_model_score_seekers": int(len(profile_model_scores)),
        "label_distribution": dataset["relevance"].value_counts().sort_index().to_dict() if len(dataset) else {},
        "label_sources": dataset["label_source"].value_counts().to_dict() if len(dataset) else {},
        "weak_label_policy": {
            "3": "seeker desired job title exactly appears in an active posting, or same-class fallback when no exact posting exists",
            "2": "posting maps to the same broad target job class as the seeker desired title",
            "0": "sampled posting from a different broad target job class",
        },
    }
    return dataset, summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare JobBridge posting-level weak ranking dataset.")
    parser.add_argument("--data-dir", default="Data/processed/profile_contract_v1")
    parser.add_argument("--standard-workplaces", default="Data/05_standard_workplaces/standard_workplaces_20251231.csv")
    parser.add_argument("--profile-model", default="Models/lightgbm_profile_contract_v1/lightgbm_job_class_baseline.joblib")
    parser.add_argument("--out", default="Data/processed/profile_contract_v1/job_ranking_dataset.csv")
    parser.add_argument("--summary-out", default="Data/processed/profile_contract_v1/job_ranking_dataset_summary.json")
    parser.add_argument("--exact-positive-per-group", type=int, default=1)
    parser.add_argument("--same-class-positive-per-group", type=int, default=2)
    parser.add_argument("--negative-per-group", type=int, default=4)
    parser.add_argument("--max-seekers", type=int, default=0)
    parser.add_argument("--random-state", type=int, default=42)
    args = parser.parse_args()

    dataset, summary = build_dataset(args)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    dataset.to_csv(out, index=False, encoding="utf-8-sig")
    summary["dataset"] = str(out)
    summary_path = Path(args.summary_out)
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
