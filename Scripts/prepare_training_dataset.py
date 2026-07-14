from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd


OFFICIAL_DISABILITY_TYPES = {
    "지체장애",
    "뇌병변장애",
    "시각장애",
    "청각장애",
    "언어장애",
    "안면장애",
    "신장장애",
    "심장장애",
    "간장애",
    "호흡기장애",
    "장루요루장애",
    "뇌전증장애",
    "지적장애",
    "정신장애",
    "자폐성장애",
}


REQUIRED_COLUMNS = [
    "순번",
    "취업일자",
    "근무지역",
    "취업직종대분류",
    "연령",
    "장애유형",
    "중증여부",
]


def read_csv_with_fallback(path: Path) -> pd.DataFrame:
    for encoding in ("utf-8-sig", "cp949", "euc-kr"):
        try:
            return pd.read_csv(path, encoding=encoding)
        except UnicodeDecodeError:
            continue
    return pd.read_csv(path)


def normalize_text(series: pd.Series) -> pd.Series:
    return series.astype("string").str.strip().replace({"": pd.NA})


def age_group(age: float | int | None) -> str | None:
    if pd.isna(age):
        return None
    age = int(age)
    if age < 20:
        return "under_20"
    if age >= 70:
        return "70_plus"
    return f"{age // 10 * 10}s"


def split_region(region: str | None) -> tuple[str | None, str | None]:
    if pd.isna(region):
        return None, None
    parts = str(region).strip().split()
    if not parts:
        return None, None
    sido = parts[0]
    sigungu = " ".join(parts[1:]) if len(parts) > 1 else None
    return sido, sigungu


def build_training_dataset(source: Path) -> tuple[pd.DataFrame, dict]:
    df = read_csv_with_fallback(source)

    missing_columns = [col for col in REQUIRED_COLUMNS if col not in df.columns]
    if missing_columns:
        raise ValueError(f"Missing required columns: {missing_columns}")

    raw_rows = len(df)
    prepared = pd.DataFrame()
    prepared["row_id"] = pd.to_numeric(df["순번"], errors="coerce").astype("Int64")
    prepared["employment_date"] = pd.to_datetime(df["취업일자"], errors="coerce")
    prepared["employment_year"] = prepared["employment_date"].dt.year.astype("Int64")
    prepared["employment_month"] = prepared["employment_date"].dt.month.astype("Int64")

    prepared["work_region_raw"] = normalize_text(df["근무지역"])
    region_parts = prepared["work_region_raw"].apply(split_region)
    prepared["sido"] = region_parts.apply(lambda value: value[0])
    prepared["sigungu"] = region_parts.apply(lambda value: value[1])

    prepared["age"] = pd.to_numeric(df["연령"], errors="coerce").astype("Int64")
    prepared["age_group"] = prepared["age"].apply(age_group)

    prepared["disability_type"] = normalize_text(df["장애유형"])
    prepared["is_official_disability_type"] = prepared["disability_type"].isin(
        OFFICIAL_DISABILITY_TYPES
    )
    prepared["severity"] = normalize_text(df["중증여부"])
    prepared["target_job_class"] = normalize_text(df["취업직종대분류"])

    critical_columns = [
        "employment_date",
        "work_region_raw",
        "age",
        "disability_type",
        "severity",
        "target_job_class",
    ]
    before_drop = len(prepared)
    prepared = prepared.dropna(subset=critical_columns).copy()
    dropped_critical_na = before_drop - len(prepared)

    model_columns = [
        "row_id",
        "employment_date",
        "employment_year",
        "employment_month",
        "work_region_raw",
        "sido",
        "sigungu",
        "age",
        "age_group",
        "disability_type",
        "is_official_disability_type",
        "severity",
        "target_job_class",
    ]
    prepared = prepared[model_columns]

    summary = {
        "source_file": str(source),
        "raw_rows": raw_rows,
        "prepared_rows": len(prepared),
        "dropped_critical_na": dropped_critical_na,
        "columns": model_columns,
        "recommended_feature_columns": [
            "sido",
            "sigungu",
            "age",
            "age_group",
            "disability_type",
            "severity",
        ],
        "not_recommended_as_features": [
            "row_id",
            "employment_date",
            "employment_year",
            "employment_month",
            "work_region_raw",
        ],
        "target_column": "target_job_class",
        "target_class_count": int(prepared["target_job_class"].nunique()),
        "disability_type_count": int(prepared["disability_type"].nunique()),
        "severity_values": prepared["severity"].value_counts(dropna=False).to_dict(),
        "non_official_disability_values": (
            prepared.loc[
                ~prepared["is_official_disability_type"], "disability_type"
            ]
            .value_counts()
            .to_dict()
        ),
        "age_min": int(prepared["age"].min()),
        "age_max": int(prepared["age"].max()),
        "date_min": str(prepared["employment_date"].min().date()),
        "date_max": str(prepared["employment_date"].max().date()),
    }
    return prepared, summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare JobBridge LightGBM dataset.")
    parser.add_argument(
        "--source",
        default=(
            "Data/01_training_employment_success/"
            "disabled_employment_success_20251231.csv"
        ),
        help="Source employment-success CSV.",
    )
    parser.add_argument(
        "--out-dir",
        default="Data/processed",
        help="Directory for processed dataset and summary.",
    )
    args = parser.parse_args()

    source = Path(args.source)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    prepared, summary = build_training_dataset(source)

    dataset_path = out_dir / "training_dataset.csv"
    summary_path = out_dir / "training_dataset_summary.json"
    prepared.to_csv(dataset_path, index=False, encoding="utf-8-sig")
    summary["output_file"] = str(dataset_path)
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
