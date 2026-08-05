"""Reusable statistical viral-prediction baseline logic shared by
05_viral_prediction.ipynb and 00_youtube_automation_master.ipynb.

Notebook 05 additionally offers an optional LLM-vs-baseline comparison (kept
notebook-local since it is an experimental add-on, not the production path);
the actual baseline methods and holdout evaluation that decide the exported
viral_prediction.json live here so both call sites share one implementation.

Never uploads or publishes anything, never makes a network call.
"""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd

STATUS_VALUES = {
    "experimental", "development_placeholder", "review_required",
    "approved", "insufficient_data", "failed", "production_ready",
}

DEFAULT_MIN_HISTORY_ROWS = 20
DEFAULT_HOLDOUT_FRACTION = 0.2
DEFAULT_MIN_HOLDOUT_ROWS = 5
DEFAULT_ROLLING_WINDOW = 10


def baseline_channel_median(train_df: pd.DataFrame, channel_id) -> float | None:
    channel_rows = train_df[train_df["channel_id"] == channel_id]
    if channel_rows.empty:
        return float(train_df["view_count"].median()) if not train_df.empty else None
    return float(channel_rows["view_count"].median())


def baseline_rolling_recent(train_df: pd.DataFrame, channel_id, window: int = DEFAULT_ROLLING_WINDOW) -> float | None:
    channel_rows = train_df[train_df["channel_id"] == channel_id].sort_values("published_at")
    if channel_rows.empty:
        return float(train_df["view_count"].median()) if not train_df.empty else None
    recent = channel_rows.tail(window)
    return float(recent["view_count"].mean())


def baseline_engagement_adjusted(train_df: pd.DataFrame, channel_id) -> float | None:
    channel_rows = train_df[train_df["channel_id"] == channel_id]
    if channel_rows.empty:
        return float(train_df["view_count"].median()) if not train_df.empty else None
    median_views = float(channel_rows["view_count"].median())
    likes = channel_rows["like_count"].fillna(0)
    views = channel_rows["view_count"].replace(0, np.nan)
    engagement_rate = float((likes / views).mean()) if views.notna().any() else 0.0
    reference_engagement = 0.03
    factor = 1.0 + max(min((engagement_rate - reference_engagement) * 5, 0.5), -0.5)
    return median_views * factor


BASELINE_METHODS = {
    "heuristic-channel-median": baseline_channel_median,
    "heuristic-rolling-recent": baseline_rolling_recent,
    "heuristic-engagement-adjusted": baseline_engagement_adjusted,
}


def zero_safe_smape(actuals, preds) -> float | None:
    actuals = np.asarray(actuals, dtype=float)
    preds = np.asarray(preds, dtype=float)
    denom = np.abs(actuals) + np.abs(preds)
    mask = denom > 1e-9
    if not mask.any():
        return None
    return float(np.mean(2 * np.abs(preds[mask] - actuals[mask]) / denom[mask]) * 100)


def evaluate_baselines_on_holdout(history_df: pd.DataFrame, holdout_fraction: float = DEFAULT_HOLDOUT_FRACTION,
                                   min_holdout_rows: int = DEFAULT_MIN_HOLDOUT_ROWS) -> dict:
    """Splits history_df (already sorted by published_at) into train/holdout
    and evaluates every BASELINE_METHODS entry via MAE/median-AE/sMAPE.
    Returns {'train_df', 'holdout_df', 'metrics': {method_name: {...}}}.
    Never raises -- a method with no valid predictions is simply omitted from
    'metrics'."""
    split_idx = min(int(len(history_df) * (1 - holdout_fraction)), len(history_df) - 1)
    train_df = history_df.iloc[:split_idx]
    holdout_df = history_df.iloc[split_idx:]

    metrics = {}
    for method_name, method_fn in BASELINE_METHODS.items():
        preds, actuals = [], []
        for _, row in holdout_df.iterrows():
            pred = method_fn(train_df, row["channel_id"])
            if pred is None:
                continue
            preds.append(pred)
            actuals.append(row["view_count"])
        if not preds:
            continue
        mae = float(np.mean(np.abs(np.array(preds) - np.array(actuals))))
        median_ae = float(np.median(np.abs(np.array(preds) - np.array(actuals))))
        smape = zero_safe_smape(actuals, preds)
        metrics[method_name] = {
            "mae": round(mae, 1), "median_ae": round(median_ae, 1),
            "smape_pct": round(smape, 2) if smape is not None else None,
            "sample_count": len(preds),
        }
    return {"train_df": train_df, "holdout_df": holdout_df, "metrics": metrics}


def build_viral_prediction_artifact(history_df: pd.DataFrame, history_load_status: str, history_load_reason: str,
                                     collection_status, min_history_rows: int = DEFAULT_MIN_HISTORY_ROWS,
                                     holdout_fraction: float = DEFAULT_HOLDOUT_FRACTION,
                                     min_holdout_rows: int = DEFAULT_MIN_HOLDOUT_ROWS,
                                     resolved_channel_count=None, collection_timestamp=None) -> dict:
    """Builds the viral_prediction.json contract dict end-to-end: gates on
    row-count + collection status, runs the holdout evaluation, and picks the
    baseline method with the lowest MAE. Never returns 'approved' or
    'production_ready' -- at most 'review_required'."""
    history_row_count = len(history_df)
    collection_status_ok = collection_status not in ("failed", "insufficient_data")
    data_sufficient = (history_row_count >= min_history_rows) and collection_status_ok

    date_range = {"start": None, "end": None}
    if history_row_count and "published_at" in history_df.columns and history_df["published_at"].notna().any():
        date_range["start"] = history_df["published_at"].min().isoformat()
        date_range["end"] = history_df["published_at"].max().isoformat()

    limitations = []
    if history_load_status != "ok":
        limitations.append(f"gecmis veri yuklenemedi/yetersiz: {history_load_reason}")
    if not data_sufficient:
        limitations.append(f"gercek gecmis veri yetersiz ({history_row_count} < {min_history_rows} satir)")

    baseline_metrics, chosen_method, predicted_range, confidence_level = {}, None, None, "none"
    holdout_df = pd.DataFrame()

    if not collection_status_ok:
        prediction_status = "insufficient_data"
        status_reason = f"collection_manifest.json status='{collection_status}' -- toplama basarisiz/yetersizdi"
    elif not data_sufficient:
        prediction_status = "insufficient_data"
        status_reason = (
            f"gercek gecmis veri {min_history_rows} satir esiginin altinda ({history_row_count} satir) - "
            f"{history_load_reason or 'yeterli veri toplanamadi'}"
        )
    else:
        evaluation = evaluate_baselines_on_holdout(history_df, holdout_fraction, min_holdout_rows)
        holdout_df = evaluation["holdout_df"]
        baseline_metrics = evaluation["metrics"]
        if len(holdout_df) < min_holdout_rows:
            limitations.append(f"holdout seti kucuk ({len(holdout_df)} satir); metrikler gurultulu olabilir")
        if baseline_metrics:
            chosen_method = min(baseline_metrics, key=lambda name: baseline_metrics[name]["mae"])
            chosen = baseline_metrics[chosen_method]
            center = float(evaluation["train_df"]["view_count"].median())
            predicted_range = {"low": round(max(center - chosen["mae"], 0), 0), "high": round(center + chosen["mae"], 0)}
            confidence_level = "medium" if history_row_count >= 200 else ("low" if history_row_count >= 50 else "very_low")
            prediction_status = "review_required"
            status_reason = f"istatistiksel baseline ({chosen_method}) holdout uzerinde degerlendirildi; insan onayi bekleniyor"
        else:
            prediction_status = "failed"
            status_reason = "veri yeterliydi ama hicbir baseline holdout tahmini uretemedi"

    limitations.append(
        "YouTube Shorts algoritmasindaki degisiklikler, mevsimsellik ve konu turu farkliliklari modele dahil edilmemistir"
    )
    limitations.append(
        "burada kullanilan view_count/like_count herkese acik YouTube Data API istatistikleridir; "
        "YouTube Analytics'in ozel CTR/retention/gelir metrikleriyle AYNI SEY DEGILDIR"
    )

    return {
        "status": prediction_status,
        "status_reason": status_reason,
        "dataset_size": history_row_count,
        "date_range": date_range,
        "min_history_rows_threshold": min_history_rows,
        "history_load_status": history_load_status,
        "history_load_reason": history_load_reason or None,
        "collection_status": collection_status,
        "collection_timestamp": collection_timestamp,
        "resolved_channel_count": resolved_channel_count,
        "method": chosen_method,
        "llm_provider_model": None,
        "metrics": {"baseline": baseline_metrics, "llm": None},
        "predicted_range": predicted_range,
        "confidence_level": confidence_level,
        "limitations": limitations,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
