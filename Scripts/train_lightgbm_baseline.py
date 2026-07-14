from __future__ import annotations

import argparse
import json
from pathlib import Path

import joblib
import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.metrics import (
    accuracy_score,
    balanced_accuracy_score,
    classification_report,
    f1_score,
    log_loss,
)
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import LabelEncoder


DEFAULT_FEATURE_COLUMNS = [
    "sido",
    "sigungu",
    "age",
    "age_group",
    "disability_type",
    "severity",
]

DEFAULT_CATEGORICAL_COLUMNS = [
    "sido",
    "sigungu",
    "age_group",
    "disability_type",
    "severity",
]


def read_training_dataset(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, encoding="utf-8-sig", parse_dates=["employment_date"])


def top_k_accuracy(y_true: np.ndarray, proba: np.ndarray, k: int) -> float:
    top_k = np.argsort(proba, axis=1)[:, -k:]
    return float(np.mean([actual in pred for actual, pred in zip(y_true, top_k)]))


def prepare_features(
    df: pd.DataFrame,
    feature_columns: list[str],
    categorical_columns: list[str],
) -> pd.DataFrame:
    X = df[feature_columns].copy()

    for column in categorical_columns:
        X[column] = X[column].astype("string").fillna("unknown").astype("category")

    for column in feature_columns:
        if column not in categorical_columns:
            X[column] = pd.to_numeric(X[column], errors="coerce")

    return X


def train(args: argparse.Namespace) -> dict:
    dataset_path = Path(args.dataset)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    metadata = None
    metadata_path = Path(args.metadata) if args.metadata else None
    if metadata_path is not None:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))

    df = read_training_dataset(dataset_path)
    raw_rows = len(df)

    if not args.include_non_official:
        df = df[df["is_official_disability_type"].astype(bool)].copy()
    official_filtered_rows = len(df)

    required_columns = set(DEFAULT_FEATURE_COLUMNS + ["target_job_class"])
    missing_columns = sorted(required_columns - set(df.columns))
    if missing_columns:
        raise ValueError(f"Missing required columns: {missing_columns}")

    class_counts = df["target_job_class"].value_counts()
    too_small_classes = class_counts[class_counts < 2].index.tolist()
    if too_small_classes:
        df = df[~df["target_job_class"].isin(too_small_classes)].copy()

    feature_columns = DEFAULT_FEATURE_COLUMNS
    categorical_columns = DEFAULT_CATEGORICAL_COLUMNS

    X = prepare_features(df, feature_columns, categorical_columns)
    label_encoder = LabelEncoder()
    y = label_encoder.fit_transform(df["target_job_class"])

    X_train, X_test, y_train, y_test = train_test_split(
        X,
        y,
        test_size=args.test_size,
        random_state=args.random_state,
        stratify=y,
    )

    model = lgb.LGBMClassifier(
        objective="multiclass",
        num_class=len(label_encoder.classes_),
        n_estimators=args.n_estimators,
        learning_rate=args.learning_rate,
        num_leaves=args.num_leaves,
        max_depth=args.max_depth,
        min_child_samples=args.min_child_samples,
        subsample=args.subsample,
        colsample_bytree=args.colsample_bytree,
        class_weight="balanced" if args.class_weight_balanced else None,
        random_state=args.random_state,
        n_jobs=-1,
        verbosity=-1,
    )

    model.fit(
        X_train,
        y_train,
        categorical_feature=categorical_columns,
        eval_set=[(X_test, y_test)],
        eval_metric="multi_logloss",
        callbacks=[
            lgb.early_stopping(args.early_stopping_rounds, verbose=False),
            lgb.log_evaluation(period=50),
        ],
    )

    proba = model.predict_proba(X_test)
    y_pred = np.argmax(proba, axis=1)

    report_dict = classification_report(
        y_test,
        y_pred,
        target_names=label_encoder.classes_,
        output_dict=True,
        zero_division=0,
    )
    report_df = pd.DataFrame(report_dict).transpose()
    report_df.to_csv(out_dir / "classification_report.csv", encoding="utf-8-sig")

    feature_importance = pd.DataFrame(
        {
            "feature": feature_columns,
            "importance": model.feature_importances_,
        }
    ).sort_values("importance", ascending=False)
    feature_importance.to_csv(out_dir / "feature_importance.csv", index=False, encoding="utf-8-sig")

    sample_predictions = pd.DataFrame(
        {
            "actual": label_encoder.inverse_transform(y_test[: args.prediction_sample_size]),
            "predicted": label_encoder.inverse_transform(y_pred[: args.prediction_sample_size]),
        }
    )
    top3_indices = np.argsort(proba[: args.prediction_sample_size], axis=1)[:, -3:][:, ::-1]
    for idx in range(3):
        sample_predictions[f"top{idx + 1}_class"] = label_encoder.inverse_transform(
            top3_indices[:, idx]
        )
        sample_predictions[f"top{idx + 1}_prob"] = [
            float(proba[row_idx, class_idx])
            for row_idx, class_idx in enumerate(top3_indices[:, idx])
        ]
    sample_predictions.to_csv(
        out_dir / "sample_predictions.csv", index=False, encoding="utf-8-sig"
    )

    artifact = {
        "model": model,
        "label_encoder": label_encoder,
        "feature_columns": feature_columns,
        "categorical_columns": categorical_columns,
        "target_column": "target_job_class",
        "include_non_official": args.include_non_official,
        "profile_contract_metadata": metadata,
    }
    model_path = out_dir / "lightgbm_job_class_baseline.joblib"
    joblib.dump(artifact, model_path)

    metrics = {
        "dataset": str(dataset_path),
        "raw_rows": int(raw_rows),
        "used_rows": int(len(df)),
        "excluded_non_official_rows": int(raw_rows - official_filtered_rows)
        if not args.include_non_official
        else 0,
        "dropped_too_small_class_rows": int(class_counts.loc[too_small_classes].sum())
        if too_small_classes
        else 0,
        "too_small_classes": too_small_classes,
        "feature_columns": feature_columns,
        "categorical_columns": categorical_columns,
        "target_column": "target_job_class",
        "target_class_count": int(len(label_encoder.classes_)),
        "train_rows": int(len(X_train)),
        "test_rows": int(len(X_test)),
        "test_size": args.test_size,
        "random_state": args.random_state,
        "best_iteration": int(model.best_iteration_ or args.n_estimators),
        "accuracy_top1": float(accuracy_score(y_test, y_pred)),
        "accuracy_top3": top_k_accuracy(y_test, proba, 3),
        "accuracy_top5": top_k_accuracy(y_test, proba, 5),
        "balanced_accuracy": float(balanced_accuracy_score(y_test, y_pred)),
        "macro_f1": float(f1_score(y_test, y_pred, average="macro")),
        "weighted_f1": float(f1_score(y_test, y_pred, average="weighted")),
        "log_loss": float(log_loss(y_test, proba, labels=np.arange(len(label_encoder.classes_)))),
        "model_file": str(model_path),
        "classification_report_file": str(out_dir / "classification_report.csv"),
        "feature_importance_file": str(out_dir / "feature_importance.csv"),
        "sample_predictions_file": str(out_dir / "sample_predictions.csv"),
        "class_distribution": df["target_job_class"].value_counts().to_dict(),
    }
    if metadata_path is not None:
        metrics["profile_contract_metadata_file"] = str(metadata_path)
        metrics["profile_contract_version"] = metadata.get("profile_contract_version")
        metrics["postprocessing_resources"] = metadata.get("postprocessing_resources")
    (out_dir / "metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    return metrics


def main() -> None:
    parser = argparse.ArgumentParser(description="Train JobBridge LightGBM baseline.")
    parser.add_argument("--dataset", default="Data/processed/training_dataset.csv")
    parser.add_argument("--out-dir", default="Models/lightgbm_baseline")
    parser.add_argument("--test-size", type=float, default=0.2)
    parser.add_argument("--random-state", type=int, default=42)
    parser.add_argument("--n-estimators", type=int, default=500)
    parser.add_argument("--learning-rate", type=float, default=0.05)
    parser.add_argument("--num-leaves", type=int, default=31)
    parser.add_argument("--max-depth", type=int, default=-1)
    parser.add_argument("--min-child-samples", type=int, default=30)
    parser.add_argument("--subsample", type=float, default=0.9)
    parser.add_argument("--colsample-bytree", type=float, default=0.9)
    parser.add_argument("--early-stopping-rounds", type=int, default=40)
    parser.add_argument("--prediction-sample-size", type=int, default=200)
    parser.add_argument(
        "--metadata",
        help=(
            "Optional profile-contract metadata JSON to embed in the model artifact "
            "and metrics."
        ),
    )
    parser.add_argument(
        "--include-non-official",
        action="store_true",
        help="Include non-official disability values such as 국가유공.",
    )
    parser.add_argument(
        "--no-balanced-class-weight",
        dest="class_weight_balanced",
        action="store_false",
        help="Disable class_weight='balanced'.",
    )
    parser.set_defaults(class_weight_balanced=True)
    args = parser.parse_args()

    metrics = train(args)
    print(json.dumps(metrics, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
