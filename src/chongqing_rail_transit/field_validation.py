"""Prepare and analyse field walks for the published accessibility models.

The module deliberately creates no validation result when no completed field
observation is available. GPX files remain local unless the researcher has
trimmed private portions and explicitly decides to publish them.
"""

from __future__ import annotations

import argparse
import math
from pathlib import Path
import xml.etree.ElementTree as ET

import numpy as np
import pandas as pd


ROUTES = (
    ("V01", "荣鼎新苑", "高庙村", "近似平地对照"),
    ("V02", "富洲新城二期", "高庙村", "总体下行"),
    ("V03", "电力小区", "沙坪坝", "含楼梯"),
    ("V04", "卓越·美丽山水", "小龙坎", "高累计爬升"),
    ("V05", "升伟·新时空", "马家岩", "高累计爬升"),
    ("V06", "成都城投锦樾峰境", "高庙村", "改善候选现场核验"),
    ("V07", "奇峰自由湾", "高庙村", "陡坡与阻挡核验"),
    ("V08", "大城小爱", "高庙村", "长连接与绕行核验"),
)

OBSERVATION_COLUMNS = (
    "collection_date", "start_time", "end_time", "start_lon", "start_lat",
    "end_lon", "end_lat", "gpx_file", "observed_total_time_min",
    "observed_moving_time_min", "crossing_wait_min", "stairs_present",
    "weather", "route_blocked", "actual_distance_m", "notes",
)


def _route_predictions(slope_results_path: Path) -> dict[str, tuple[float, float]]:
    results = pd.read_csv(slope_results_path)
    selected = results[results["maximum_abs_grade"].eq(0.35)].copy()
    predictions: dict[str, tuple[float, float]] = {}
    for _, residential_name, _, _ in ROUTES:
        rows = selected[selected["residential_name"].eq(residential_name)]
        values = rows.set_index("grade_scale")["time_min"].to_dict()
        if 0.0 not in values or 1.0 not in values:
            raise RuntimeError(f"Missing fixed/slope prediction for {residential_name}")
        predictions[residential_name] = (float(values[0.0]), float(values[1.0]))
    return predictions


def build_collection_template(slope_results_path: Path, replicates: int = 2) -> pd.DataFrame:
    """Return a two-repeat field sheet with observations intentionally blank."""
    if replicates < 1:
        raise ValueError("replicates must be positive")
    predictions = _route_predictions(slope_results_path)
    rows: list[dict[str, object]] = []
    for route_id, residential_name, station_name, route_type in ROUTES:
        fixed_time, slope_time = predictions[residential_name]
        for replicate in range(1, replicates + 1):
            row: dict[str, object] = {
                "route_id": route_id,
                "walk_id": f"{route_id}-W{replicate}",
                "residential_name": residential_name,
                "station_name": station_name,
                "direction": "residential_to_station",
                "route_type": route_type,
                "replicate": replicate,
                "fixed_model_time_min": fixed_time,
                "slope_model_time_min": slope_time,
            }
            row.update({column: "" for column in OBSERVATION_COLUMNS})
            rows.append(row)
    return pd.DataFrame(rows)


def _haversine_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    radius_m = 6_371_008.8
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * radius_m * math.asin(math.sqrt(a))


def parse_gpx(path: Path) -> dict[str, float | int | str]:
    """Summarise all GPX track segments using timestamps when present."""
    root = ET.parse(path).getroot()
    segments: list[list[tuple[float, float, float | None, pd.Timestamp | None]]] = []
    for segment_element in (element for element in root.iter() if element.tag.endswith("trkseg")):
        segment = []
        for element in segment_element:
            if not element.tag.endswith("trkpt"):
                continue
            lat, lon = float(element.attrib["lat"]), float(element.attrib["lon"])
            elevation = None
            timestamp = None
            for child in element:
                if child.tag.endswith("ele") and child.text:
                    elevation = float(child.text)
                elif child.tag.endswith("time") and child.text:
                    timestamp = pd.to_datetime(child.text, utc=True)
            segment.append((lon, lat, elevation, timestamp))
        if segment:
            segments.append(segment)
    points = [point for segment in segments for point in segment]
    if len(points) < 2:
        raise ValueError(f"GPX needs at least two track points: {path}")

    distance_m = sum(
        _haversine_m(a[0], a[1], b[0], b[1])
        for segment in segments for a, b in zip(segment[:-1], segment[1:])
    )
    elevation_pairs = [
        (a[2], b[2]) for segment in segments for a, b in zip(segment[:-1], segment[1:])
        if a[2] is not None and b[2] is not None
    ]
    gain_m = sum(max(0.0, b - a) for a, b in elevation_pairs)
    loss_m = sum(max(0.0, a - b) for a, b in elevation_pairs)
    timestamps = [point[3] for point in points]
    elapsed_min = np.nan
    if all(timestamp is not None for timestamp in timestamps):
        elapsed_min = (timestamps[-1] - timestamps[0]).total_seconds() / 60.0
    return {
        "gpx_point_count": len(points),
        "gpx_distance_m": distance_m,
        "gpx_elapsed_min": elapsed_min,
        "gpx_elevation_gain_m": gain_m,
        "gpx_elevation_loss_m": loss_m,
        "gpx_start_lon": points[0][0],
        "gpx_start_lat": points[0][1],
        "gpx_end_lon": points[-1][0],
        "gpx_end_lat": points[-1][1],
    }


def completed_observations(observations: pd.DataFrame) -> pd.DataFrame:
    """Keep only rows with a positive measured total time."""
    required = {
        "walk_id", "route_id", "fixed_model_time_min", "slope_model_time_min",
        "observed_total_time_min",
    }
    missing = sorted(required - set(observations.columns))
    if missing:
        raise ValueError(f"Observation table is missing columns: {missing}")
    result = observations.copy()
    numeric = ("fixed_model_time_min", "slope_model_time_min", "observed_total_time_min", "observed_moving_time_min")
    for column in numeric:
        if column in result:
            result[column] = pd.to_numeric(result[column], errors="coerce")
    result = result[result["observed_total_time_min"].gt(0)].copy()
    if result["walk_id"].duplicated().any():
        raise ValueError("walk_id must be unique")
    return result


def attach_gpx_summaries(observations: pd.DataFrame, gpx_dir: Path) -> pd.DataFrame:
    records: list[dict[str, object]] = []
    for row in observations.to_dict("records"):
        gpx_file = str(row.get("gpx_file", "")).strip()
        if gpx_file:
            path = gpx_dir / gpx_file
            if not path.is_file():
                raise FileNotFoundError(path)
            row.update(parse_gpx(path))
        records.append(row)
    return pd.DataFrame(records)


def compare_models(observations: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Calculate per-walk errors and aggregate MAE, median AE and signed error."""
    completed = completed_observations(observations)
    if completed.empty:
        return completed, pd.DataFrame()

    for model in ("fixed", "slope"):
        prediction = completed[f"{model}_model_time_min"]
        for basis in ("total", "moving"):
            observed_column = f"observed_{basis}_time_min"
            if observed_column not in completed:
                continue
            completed[f"{model}_error_{basis}_min"] = prediction - completed[observed_column]
            completed[f"{model}_abs_error_{basis}_min"] = completed[f"{model}_error_{basis}_min"].abs()

    metrics: list[dict[str, object]] = []
    for basis in ("total", "moving"):
        observed_column = f"observed_{basis}_time_min"
        if observed_column not in completed:
            continue
        valid = completed[completed[observed_column].gt(0)]
        for model in ("fixed", "slope"):
            errors = valid[f"{model}_model_time_min"] - valid[observed_column]
            if errors.empty:
                continue
            other_model = "slope" if model == "fixed" else "fixed"
            own_absolute = errors.abs()
            other_absolute = (
                valid[f"{other_model}_model_time_min"] - valid[observed_column]
            ).abs()
            metrics.append({
                "observed_time_basis": basis,
                "model": model,
                "walk_count": len(errors),
                "route_count": valid["route_id"].nunique(),
                "mae_min": errors.abs().mean(),
                "median_absolute_error_min": errors.abs().median(),
                "mean_signed_error_min": errors.mean(),
                "lower_absolute_error_walks": int((own_absolute < other_absolute).sum()),
                "tied_absolute_error_walks": int(np.isclose(own_absolute, other_absolute).sum()),
            })
    return completed, pd.DataFrame(metrics)


def run_validation(observations_path: Path, gpx_dir: Path, output_dir: Path) -> bool:
    observations = pd.read_csv(observations_path)
    completed = completed_observations(observations)
    if completed.empty:
        print("No completed field observations; no validation result was generated.")
        return False
    completed = attach_gpx_summaries(completed, gpx_dir)
    per_walk, metrics = compare_models(completed)
    output_dir.mkdir(parents=True, exist_ok=True)
    per_walk.to_csv(output_dir / "field_walk_validation_per_walk.csv", index=False, encoding="utf-8-sig")
    metrics.to_csv(output_dir / "field_walk_validation_metrics.csv", index=False, encoding="utf-8-sig")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--create-template", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--template", type=Path, default=Path("data/field/field_walk_observations_template.csv"))
    parser.add_argument("--observations", type=Path, default=Path("data/field/field_walk_observations.csv"))
    parser.add_argument("--gpx-dir", type=Path, default=Path("data/field/gpx"))
    parser.add_argument("--output-dir", type=Path, default=Path("outputs/field_validation"))
    parser.add_argument("--slope-results", type=Path, default=Path("data/processed/shapingba_slope_adjusted_residential.csv"))
    args = parser.parse_args()
    if args.create_template:
        if args.template.exists() and not args.force:
            raise FileExistsError(f"Refusing to overwrite existing template: {args.template}")
        args.template.parent.mkdir(parents=True, exist_ok=True)
        build_collection_template(args.slope_results).to_csv(args.template, index=False, encoding="utf-8-sig")
        print(f"Created empty field template: {args.template}")
        return
    if not args.observations.is_file():
        raise FileNotFoundError(
            f"No field observations found: {args.observations}. Copy and fill {args.template} first."
        )
    run_validation(args.observations, args.gpx_dir, args.output_dir)


if __name__ == "__main__":
    main()
