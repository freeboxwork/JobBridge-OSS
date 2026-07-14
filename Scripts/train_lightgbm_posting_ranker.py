from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import joblib
import lightgbm as lgb
import numpy as np
import pandas as pd

try:
    from prepare_job_ranking_dataset import CATEGORICAL_COLUMNS, FEATURE_COLUMNS
except ModuleNotFoundError:
    from Scripts.prepare_job_ranking_dataset import CATEGORICAL_COLUMNS, FEATURE_COLUMNS


MODEL_VERSION = "lightgbm_posting_ranker_v1"


def read_dataset(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, encoding="utf-8-sig")


def build_category_levels(df: pd.DataFrame, categorical_columns: list[str]) -> dict[str, list[str]]:
    levels: dict[str, list[str]] = {}
    for column in categorical_columns:
        values = df[column].astype("string").fillna("unknown")
        unique_values = sorted(str(value) for value in values.unique() if str(value))
        if "unknown" not in unique_values:
            unique_values.insert(0, "unknown")
        levels[column] = unique_values
    return levels


def prepare_features(
    df: pd.DataFrame,
    feature_columns: list[str],
    categorical_columns: list[str],
    category_levels: dict[str, list[str]] | None = None,
) -> pd.DataFrame:
    X = df[feature_columns].copy()
    for column in categorical_columns:
        values = X[column].astype("string").fillna("unknown")
        levels = (category_levels or {}).get(column)
        if levels:
            allowed = set(levels)
            values = values.where(values.isin(allowed), "unknown")
            X[column] = pd.Categorical(values, categories=levels)
        else:
            X[column] = values.astype("category")

    for column in feature_columns:
        if column not in categorical_columns:
            X[column] = pd.to_numeric(X[column], errors="coerce").fillna(0)
    return X


def split_by_group(df: pd.DataFrame, test_size: float, random_state: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    group_ids = df["group_id"].drop_duplicates().to_numpy()
    rng = np.random.default_rng(random_state)
    rng.shuffle(group_ids)
    test_count = max(1, int(round(len(group_ids) * test_size)))
    test_groups = set(group_ids[:test_count])
    test_df = df[df["group_id"].isin(test_groups)].copy()
    train_df = df[~df["group_id"].isin(test_groups)].copy()
    return (
        train_df.sort_values(["group_id", "relevance"], ascending=[True, False]).reset_index(drop=True),
        test_df.sort_values(["group_id", "relevance"], ascending=[True, False]).reset_index(drop=True),
    )


def group_sizes(df: pd.DataFrame) -> np.ndarray:
    return df.groupby("group_id", sort=False).size().to_numpy()


def dcg_at_k(relevance: np.ndarray, k: int) -> float:
    values = relevance[:k]
    if len(values) == 0:
        return 0.0
    gains = np.power(2.0, values) - 1.0
    discounts = np.log2(np.arange(2, len(values) + 2))
    return float(np.sum(gains / discounts))


def ndcg_at_k(relevance: np.ndarray, scores: np.ndarray, k: int) -> float:
    order = np.argsort(scores)[::-1]
    ideal = np.argsort(relevance)[::-1]
    actual = dcg_at_k(relevance[order], k)
    best = dcg_at_k(relevance[ideal], k)
    if best <= 0:
        return 0.0
    return actual / best


def ranking_metrics(df: pd.DataFrame, score_column: str = "_pred_score") -> dict[str, float]:
    ndcg_1: list[float] = []
    ndcg_3: list[float] = []
    ndcg_5: list[float] = []
    top1_relevance: list[float] = []
    top1_positive = 0
    top1_exact = 0
    top3_positive = 0
    group_count = 0

    for _, group in df.groupby("group_id", sort=False):
        relevance = group["relevance"].to_numpy(dtype=float)
        scores = group[score_column].to_numpy(dtype=float)
        if len(relevance) == 0:
            continue
        order = np.argsort(scores)[::-1]
        group_count += 1
        ndcg_1.append(ndcg_at_k(relevance, scores, 1))
        ndcg_3.append(ndcg_at_k(relevance, scores, 3))
        ndcg_5.append(ndcg_at_k(relevance, scores, 5))
        top_relevance = float(relevance[order[0]])
        top1_relevance.append(top_relevance)
        top1_positive += int(top_relevance > 0)
        top1_exact += int(top_relevance >= 3)
        top3_positive += int(np.any(relevance[order[:3]] > 0))

    if group_count == 0:
        return {}
    return {
        "ndcg_at_1": float(np.mean(ndcg_1)),
        "ndcg_at_3": float(np.mean(ndcg_3)),
        "ndcg_at_5": float(np.mean(ndcg_5)),
        "top1_relevance_mean": float(np.mean(top1_relevance)),
        "top1_positive_rate": float(top1_positive / group_count),
        "top1_exact_rate": float(top1_exact / group_count),
        "top3_positive_rate": float(top3_positive / group_count),
    }


def write_sample_rankings(df: pd.DataFrame, out_path: Path, sample_groups: int) -> None:
    groups = df["group_id"].drop_duplicates().head(sample_groups)
    sample = df[df["group_id"].isin(groups)].copy()
    sample = sample.sort_values(["group_id", "_pred_score"], ascending=[True, False])
    columns = [
        "group_id",
        "seeker_id",
        "posting_id",
        "relevance",
        "label_source",
        "_pred_score",
        "disability_type",
        "severity",
        "profile_sido",
        "profile_sigungu",
        "posting_sido",
        "posting_sigungu",
        "posting_job_class",
        "job_title",
        "company_name",
    ]
    sample[columns].to_csv(out_path, index=False, encoding="utf-8-sig")


def train(args: argparse.Namespace) -> dict[str, Any]:
    dataset_path = Path(args.dataset)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    df = read_dataset(dataset_path)
    raw_rows = len(df)
    feature_columns = list(FEATURE_COLUMNS) + list(CATEGORICAL_COLUMNS)
    required_columns = {"group_id", "relevance", *feature_columns}
    missing_columns = sorted(required_columns - set(df.columns))
    if missing_columns:
        raise ValueError(f"Missing required columns: {missing_columns}")

    df = df.dropna(subset=["group_id", "relevance"]).copy()
    df["group_id"] = df["group_id"].astype(int)
    df["relevance"] = df["relevance"].astype(int)
    train_df, test_df = split_by_group(df, args.test_size, args.random_state)
    category_levels = build_category_levels(train_df, CATEGORICAL_COLUMNS)

    X_train = prepare_features(train_df, feature_columns, CATEGORICAL_COLUMNS, category_levels)
    X_test = prepare_features(test_df, feature_columns, CATEGORICAL_COLUMNS, category_levels)
    y_train = train_df["relevance"].to_numpy()
    y_test = test_df["relevance"].to_numpy()
    train_groups = group_sizes(train_df)
    test_groups = group_sizes(test_df)

    model = lgb.LGBMRanker(
        objective="lambdarank",
        metric="ndcg",
        n_estimators=args.n_estimators,
        learning_rate=args.learning_rate,
        num_leaves=args.num_leaves,
        max_depth=args.max_depth,
        min_child_samples=args.min_child_samples,
        subsample=args.subsample,
        colsample_bytree=args.colsample_bytree,
        random_state=args.random_state,
        n_jobs=-1,
        verbosity=-1,
    )

    model.fit(
        X_train,
        y_train,
        group=train_groups,
        eval_set=[(X_test, y_test)],
        eval_group=[test_groups],
        eval_at=[1, 3, 5],
        categorical_feature=list(CATEGORICAL_COLUMNS),
        callbacks=[
            lgb.early_stopping(args.early_stopping_rounds, verbose=False),
            lgb.log_evaluation(period=50),
        ],
    )

    test_df["_pred_score"] = model.predict(X_test, num_iteration=model.best_iteration_)
    train_df["_pred_score"] = model.predict(X_train, num_iteration=model.best_iteration_)
    test_metrics = ranking_metrics(test_df)
    train_metrics = ranking_metrics(train_df)

    feature_importance = pd.DataFrame(
        {
            "feature": feature_columns,
            "importance_split": model.feature_importances_,
            "importance_gain": model.booster_.feature_importance(importance_type="gain"),
        }
    ).sort_values(["importance_gain", "importance_split"], ascending=False)
    feature_importance.to_csv(out_dir / "feature_importance.csv", index=False, encoding="utf-8-sig")
    write_sample_rankings(test_df, out_dir / "sample_rankings.csv", args.sample_group_count)

    artifact = {
        "model": model,
        "model_version": MODEL_VERSION,
        "feature_columns": feature_columns,
        "numeric_columns": list(FEATURE_COLUMNS),
        "categorical_columns": list(CATEGORICAL_COLUMNS),
        "category_levels": category_levels,
        "target_column": "relevance",
        "group_column": "group_id",
        "weak_label_notice": (
            "This ranker is trained on weak profile-posting labels derived from desired job title, "
            "broad job class, geography, and public posting metadata. It is not a real application/outcome label model."
        ),
    }
    model_path = out_dir / "lightgbm_posting_ranker.joblib"
    joblib.dump(artifact, model_path)

    metrics: dict[str, Any] = {
        "model_version": MODEL_VERSION,
        "dataset": str(dataset_path),
        "raw_rows": int(raw_rows),
        "used_rows": int(len(df)),
        "groups": int(df["group_id"].nunique()),
        "train_rows": int(len(train_df)),
        "test_rows": int(len(test_df)),
        "train_groups": int(train_df["group_id"].nunique()),
        "test_groups": int(test_df["group_id"].nunique()),
        "test_size": args.test_size,
        "random_state": args.random_state,
        "best_iteration": int(model.best_iteration_ or args.n_estimators),
        "feature_columns": feature_columns,
        "numeric_columns": list(FEATURE_COLUMNS),
        "categorical_columns": list(CATEGORICAL_COLUMNS),
        "label_distribution": df["relevance"].value_counts().sort_index().to_dict(),
        "label_sources": df["label_source"].value_counts().to_dict() if "label_source" in df else {},
        "train_metrics": train_metrics,
        "test_metrics": test_metrics,
        "model_file": str(model_path),
        "feature_importance_file": str(out_dir / "feature_importance.csv"),
        "sample_rankings_file": str(out_dir / "sample_rankings.csv"),
        "weak_label_notice": artifact["weak_label_notice"],
    }
    (out_dir / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    return metrics


def main() -> None:
    parser = argparse.ArgumentParser(description="Train JobBridge LightGBM posting ranker.")
    parser.add_argument("--dataset", default="Data/processed/profile_contract_v1/job_ranking_dataset.csv")
    parser.add_argument("--out-dir", default="Models/lightgbm_posting_ranker_v1")
    parser.add_argument("--test-size", type=float, default=0.2)
    parser.add_argument("--random-state", type=int, default=42)
    parser.add_argument("--n-estimators", type=int, default=600)
    parser.add_argument("--learning-rate", type=float, default=0.04)
    parser.add_argument("--num-leaves", type=int, default=63)
    parser.add_argument("--max-depth", type=int, default=-1)
    parser.add_argument("--min-child-samples", type=int, default=40)
    parser.add_argument("--subsample", type=float, default=0.9)
    parser.add_argument("--colsample-bytree", type=float, default=0.9)
    parser.add_argument("--early-stopping-rounds", type=int, default=50)
    parser.add_argument("--sample-group-count", type=int, default=80)
    args = parser.parse_args()

    metrics = train(args)
    print(json.dumps(metrics, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
