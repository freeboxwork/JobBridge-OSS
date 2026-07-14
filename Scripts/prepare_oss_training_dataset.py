from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import pandas as pd

from prepare_profile_contract_dataset import normalize_job_codes, normalize_job_seekers


FEATURE_COLUMNS = [
    "sido",
    "sigungu",
    "age",
    "age_group",
    "disability_type",
    "severity",
]
TARGET_COLUMN = "target_job_class"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_normalized(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, encoding="utf-8-sig")


def prepare(args: argparse.Namespace) -> dict[str, object]:
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.normalized_seekers:
        source_path = Path(args.normalized_seekers)
        seekers = read_normalized(source_path)
        source_mode = "pre_normalized"
    else:
        source_path = Path(args.job_seekers)
        job_codes, lookup = normalize_job_codes(Path(args.job_codes))
        seekers, _ = normalize_job_seekers(source_path, lookup)
        source_mode = "official_raw_files"

    required = set(FEATURE_COLUMNS + ["is_official_disability_type", "target_job_class_candidate"])
    missing = sorted(required - set(seekers.columns))
    if missing:
        raise ValueError(f"Missing required seeker columns: {missing}")

    training = seekers.loc[
        seekers["is_official_disability_type"].astype(bool)
        & seekers["target_job_class_candidate"].notna(),
        FEATURE_COLUMNS + ["target_job_class_candidate"],
    ].copy()
    training = training.rename(columns={"target_job_class_candidate": TARGET_COLUMN})
    training["sigungu"] = training["sigungu"].fillna("unknown")
    training = training.dropna(subset=["sido", "age", "age_group", "disability_type", "severity"])

    class_counts = training[TARGET_COLUMN].value_counts()
    retained_classes = class_counts[class_counts >= args.min_class_rows].index
    training = training[training[TARGET_COLUMN].isin(retained_classes)].reset_index(drop=True)

    dataset_path = out_dir / "training_dataset.csv"
    training.to_csv(dataset_path, index=False, encoding="utf-8-sig")

    metadata = {
        "dataset_version": "jobbridge_jobseeker_preference_v1",
        "source_mode": source_mode,
        "source_file_sha256": sha256(source_path),
        "source_dataset": "한국장애인고용공단_장애인 구직자 현황",
        "source_url": "https://www.data.go.kr/data/15014774/fileData.do",
        "source_license": "공공누리 제1유형 (출처표시)",
        "label_semantics": "구직자가 등록한 희망직종을 직업 대분류로 매핑한 약한 선호 라벨",
        "not_an_outcome_label": True,
        "feature_columns": FEATURE_COLUMNS,
        "target_column": TARGET_COLUMN,
        "min_class_rows": args.min_class_rows,
        "rows": int(len(training)),
        "classes": int(training[TARGET_COLUMN].nunique()),
        "class_distribution": training[TARGET_COLUMN].value_counts().to_dict(),
        "privacy": "연번, 희망직종 원문, 희망임금, 기관분류 등 직접·준식별 가능 열은 학습 산출물에서 제외",
    }
    metadata_path = out_dir / "dataset_metadata.json"
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    return {**metadata, "dataset": str(dataset_path), "metadata": str(metadata_path)}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Prepare the contest-safe JobBridge preference training dataset."
    )
    parser.add_argument(
        "--job-seekers",
        default="Data/raw/disabled_job_seekers_20251231.csv",
        help="Official KOGL Type 1 job-seeker CSV downloaded from data.go.kr.",
    )
    parser.add_argument(
        "--job-codes",
        default="Data/raw/job_codes_20230825.csv",
        help="Official job-code reference CSV downloaded from data.go.kr.",
    )
    parser.add_argument(
        "--normalized-seekers",
        help="Optional pre-normalized seeker CSV for maintainers; raw source files are the public path.",
    )
    parser.add_argument("--out-dir", default="Data/processed/oss_preference_v1")
    parser.add_argument("--min-class-rows", type=int, default=30)
    args = parser.parse_args()
    print(json.dumps(prepare(args), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
