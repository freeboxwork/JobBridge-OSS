from __future__ import annotations

import argparse

import joblib
import pandas as pd


SAMPLES = [
    {
        "name": "P1 청각장애 경증 20대 경기 수원",
        "sido": "경기",
        "sigungu": "수원시",
        "age": 29,
        "age_group": "20s",
        "disability_type": "청각장애",
        "severity": "경증",
    },
    {
        "name": "P2 지체장애 중증 40대 전북 전주",
        "sido": "전북",
        "sigungu": "전주시",
        "age": 42,
        "age_group": "40s",
        "disability_type": "지체장애",
        "severity": "중증",
    },
]


def main() -> None:
    parser = argparse.ArgumentParser(description="Run sample predictions.")
    parser.add_argument(
        "--model",
        default="Models/lightgbm_baseline/lightgbm_job_class_baseline.joblib",
    )
    parser.add_argument("--top-k", type=int, default=5)
    args = parser.parse_args()

    artifact = joblib.load(args.model)
    model = artifact["model"]
    label_encoder = artifact["label_encoder"]
    feature_columns = artifact["feature_columns"]
    categorical_columns = artifact["categorical_columns"]

    df = pd.DataFrame(SAMPLES)
    names = df.pop("name")

    for column in categorical_columns:
        df[column] = df[column].astype("category")

    proba = model.predict_proba(df[feature_columns])

    for row_idx, name in enumerate(names):
        print(f"[{name}]")
        top_indices = proba[row_idx].argsort()[-args.top_k :][::-1]
        for rank, class_idx in enumerate(top_indices, start=1):
            label = label_encoder.inverse_transform([class_idx])[0]
            print(f"{rank}. {label}: {proba[row_idx][class_idx]:.4f}")
        print()


if __name__ == "__main__":
    main()
