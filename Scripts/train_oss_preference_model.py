from __future__ import annotations

import argparse
import json
from pathlib import Path

import joblib
import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.metrics import accuracy_score, balanced_accuracy_score, f1_score, log_loss
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import LabelEncoder


FEATURE_COLUMNS = ["sido", "sigungu", "age", "age_group", "disability_type", "severity"]
CATEGORICAL_COLUMNS = ["sido", "sigungu", "age_group", "disability_type", "severity"]
TARGET_COLUMN = "target_job_class"


def top_k_accuracy(y_true: np.ndarray, probabilities: np.ndarray, k: int) -> float:
    width = min(k, probabilities.shape[1])
    top_k = np.argsort(probabilities, axis=1)[:, -width:]
    return float(np.mean([actual in predicted for actual, predicted in zip(y_true, top_k)]))


def prepare_features(frame: pd.DataFrame) -> pd.DataFrame:
    features = frame[FEATURE_COLUMNS].copy()
    for column in CATEGORICAL_COLUMNS:
        features[column] = features[column].astype("string").fillna("unknown").astype("category")
    features["age"] = pd.to_numeric(features["age"], errors="coerce")
    return features


def train(args: argparse.Namespace) -> dict[str, object]:
    dataset_path = Path(args.dataset)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    frame = pd.read_csv(dataset_path, encoding="utf-8-sig")

    missing = sorted(set(FEATURE_COLUMNS + [TARGET_COLUMN]) - set(frame.columns))
    if missing:
        raise ValueError(f"Missing required columns: {missing}")

    labels = LabelEncoder()
    y = labels.fit_transform(frame[TARGET_COLUMN].astype(str))
    X = prepare_features(frame)
    X_train, X_test, y_train, y_test = train_test_split(
        X,
        y,
        test_size=args.test_size,
        random_state=args.random_state,
        stratify=y,
    )

    model = lgb.LGBMClassifier(
        objective="multiclass",
        num_class=len(labels.classes_),
        n_estimators=args.n_estimators,
        learning_rate=args.learning_rate,
        num_leaves=args.num_leaves,
        min_child_samples=args.min_child_samples,
        class_weight="balanced",
        random_state=args.random_state,
        n_jobs=-1,
        verbosity=-1,
    )
    model.fit(
        X_train,
        y_train,
        categorical_feature=CATEGORICAL_COLUMNS,
        eval_set=[(X_test, y_test)],
        eval_metric="multi_logloss",
        callbacks=[lgb.early_stopping(args.early_stopping_rounds, verbose=False)],
    )

    probabilities = model.predict_proba(X_test)
    predicted = np.argmax(probabilities, axis=1)
    artifact = {
        "model": model,
        "label_encoder": labels,
        "feature_columns": FEATURE_COLUMNS,
        "categorical_columns": CATEGORICAL_COLUMNS,
        "target_column": TARGET_COLUMN,
        "model_version": "lightgbm_jobseeker_preference_v1",
        "label_semantics": "registered desired job class; preference prior, not employment success",
        "license": "Apache-2.0",
    }
    model_path = out_dir / "jobbridge_preference_model.joblib"
    joblib.dump(artifact, model_path, compress=3)

    metrics = {
        "model_version": artifact["model_version"],
        "dataset": str(dataset_path),
        "rows": int(len(frame)),
        "train_rows": int(len(X_train)),
        "test_rows": int(len(X_test)),
        "classes": int(len(labels.classes_)),
        "random_state": args.random_state,
        "best_iteration": int(model.best_iteration_ or args.n_estimators),
        "accuracy_top1": float(accuracy_score(y_test, predicted)),
        "accuracy_top3": top_k_accuracy(y_test, probabilities, 3),
        "accuracy_top5": top_k_accuracy(y_test, probabilities, 5),
        "balanced_accuracy": float(balanced_accuracy_score(y_test, predicted)),
        "macro_f1": float(f1_score(y_test, predicted, average="macro")),
        "weighted_f1": float(f1_score(y_test, predicted, average="weighted")),
        "log_loss": float(log_loss(y_test, probabilities, labels=np.arange(len(labels.classes_)))),
        "model_file": str(model_path),
        "label_semantics": artifact["label_semantics"],
        "limitations": [
            "Demographic and regional variables do not determine an individual's aptitude.",
            "Predictions reflect historical registered preferences, not employment outcomes.",
            "Use only as an optional prior; user choices and accessibility needs must override it.",
        ],
    }
    (out_dir / "metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return metrics


def main() -> None:
    parser = argparse.ArgumentParser(description="Train the open JobBridge preference-prior model.")
    parser.add_argument("--dataset", default="Data/processed/oss_preference_v1/training_dataset.csv")
    parser.add_argument("--out-dir", default="Models/lightgbm_jobseeker_preference_v1")
    parser.add_argument("--test-size", type=float, default=0.2)
    parser.add_argument("--random-state", type=int, default=42)
    parser.add_argument("--n-estimators", type=int, default=500)
    parser.add_argument("--learning-rate", type=float, default=0.05)
    parser.add_argument("--num-leaves", type=int, default=31)
    parser.add_argument("--min-child-samples", type=int, default=30)
    parser.add_argument("--early-stopping-rounds", type=int, default=40)
    args = parser.parse_args()
    print(json.dumps(train(args), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
