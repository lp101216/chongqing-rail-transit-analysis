"""Slope-sensitive walking-time analysis for the Shapingba corridor.

The model samples a 1 arc-second SRTM/Mapzen HGT tile at every walking-network
node and applies a level-speed-normalized Tobler hiking function to each edge in
both directions.  Results are model estimates, not observed walking times.
"""

from __future__ import annotations

import argparse
import gzip
import math
import re
from dataclasses import dataclass
from pathlib import Path

import geopandas as gpd
import matplotlib.pyplot as plt
import networkx as nx
import numpy as np
import pandas as pd
from pyproj import Transformer
from shapely.geometry import LineString, Point

from .improvement_simulation import CASE_AREAS, _add_connector, select_path_shortcut
from .residential_accessibility import _entrance_sources, load_named_residential_areas
from .walk_accessibility_mvp import (
    ANALYSIS_CRS,
    WALK_SPEED_KMH,
    build_walking_graph,
    load_station_entrances,
    load_verified_stations,
    load_walkable_lines,
    nearest_graph_node,
    parse_other_tags,
)


GRADE_SCALES = (0.0, 0.5, 1.0, 1.5)
GRADE_CAPS = (0.20, 0.35, 0.50, 1.00)
MAX_ABS_GRADE = 0.35
MAXIMUM_ORIGIN_SNAP_M = 150.0
SRTM_VOID = -32768


@dataclass(frozen=True)
class HgtTile:
    """A single north-up HGT tile with bilinear point sampling."""

    elevations: np.ndarray
    south: float
    west: float
    north: float
    east: float

    @classmethod
    def from_gzip(cls, path: Path) -> "HgtTile":
        match = re.search(r"([NS])(\d{2})([EW])(\d{3})\.hgt(?:\.gz)?$", path.name)
        if not match:
            raise ValueError(f"Cannot derive HGT bounds from filename: {path.name}")
        lat = int(match.group(2)) * (1 if match.group(1) == "N" else -1)
        lon = int(match.group(4)) * (1 if match.group(3) == "E" else -1)
        with gzip.open(path, "rb") as handle:
            values = np.frombuffer(handle.read(), dtype=">i2").astype(float)
        side = int(math.isqrt(values.size))
        if side * side != values.size:
            raise ValueError(f"HGT cell count is not square: {values.size}")
        elevations = values.reshape((side, side))
        return cls(elevations, lat, lon, lat + 1.0, lon + 1.0)

    @property
    def shape(self) -> tuple[int, int]:
        return self.elevations.shape

    def sample(self, lon: float, lat: float) -> float:
        """Return bilinearly interpolated elevation in metres."""
        if not (self.west <= lon <= self.east and self.south <= lat <= self.north):
            raise ValueError(f"Point ({lon}, {lat}) lies outside HGT tile")
        rows, cols = self.elevations.shape
        row = (self.north - lat) / (self.north - self.south) * (rows - 1)
        col = (lon - self.west) / (self.east - self.west) * (cols - 1)
        r0, c0 = int(math.floor(row)), int(math.floor(col))
        r1, c1 = min(r0 + 1, rows - 1), min(c0 + 1, cols - 1)
        dr, dc = row - r0, col - c0
        corners = np.asarray([
            self.elevations[r0, c0], self.elevations[r0, c1],
            self.elevations[r1, c0], self.elevations[r1, c1],
        ])
        if np.any(corners <= SRTM_VOID):
            valid = corners[corners > SRTM_VOID]
            if not len(valid):
                raise ValueError(f"All surrounding HGT cells are void at ({lon}, {lat})")
            return float(valid.mean())
        top = corners[0] * (1 - dc) + corners[1] * dc
        bottom = corners[2] * (1 - dc) + corners[3] * dc
        return float(top * (1 - dr) + bottom * dr)


def tobler_speed_kmh(
    grade: float,
    grade_scale: float = 1.0,
    level_speed_kmh: float = WALK_SPEED_KMH,
    maximum_abs_grade: float = MAX_ABS_GRADE,
) -> float:
    """Return Tobler speed normalized to the chosen speed on level ground."""
    scaled_grade = float(np.clip(grade * grade_scale, -maximum_abs_grade, maximum_abs_grade))
    raw_speed = 6.0 * math.exp(-3.5 * abs(scaled_grade + 0.05))
    raw_level_speed = 6.0 * math.exp(-3.5 * 0.05)
    return level_speed_kmh * raw_speed / raw_level_speed


def _node_elevations(graph: nx.Graph, tile: HgtTile) -> dict[tuple[float, float], float]:
    transformer = Transformer.from_crs(ANALYSIS_CRS, "EPSG:4326", always_xy=True)
    elevations: dict[tuple[float, float], float] = {}
    for node in graph.nodes:
        lon, lat = transformer.transform(node[0], node[1])
        elevations[node] = tile.sample(lon, lat)
    return elevations


def build_slope_graph(
    graph: nx.Graph,
    tile: HgtTile,
    grade_scale: float = 1.0,
    maximum_abs_grade: float = MAX_ABS_GRADE,
) -> nx.DiGraph:
    """Create a two-direction graph with time weights and elevation attributes."""
    elevations = _node_elevations(graph, tile)
    directed = nx.DiGraph()
    for node, data in graph.nodes(data=True):
        directed.add_node(node, **data, elevation_m=elevations[node])
    for u, v, data in graph.edges(data=True):
        length_m = float(data["length_m"])
        for start, end in ((u, v), (v, u)):
            delta = elevations[end] - elevations[start]
            raw_grade = delta / length_m
            used_grade = float(np.clip(raw_grade, -maximum_abs_grade, maximum_abs_grade))
            speed = tobler_speed_kmh(
                raw_grade,
                grade_scale=grade_scale,
                maximum_abs_grade=maximum_abs_grade,
            )
            attrs = dict(data)
            attrs.update({
                "elevation_start_m": elevations[start],
                "elevation_end_m": elevations[end],
                "elevation_delta_m": delta,
                "raw_grade": raw_grade,
                "used_grade": used_grade,
                "grade_was_capped": abs(raw_grade) > maximum_abs_grade,
                "maximum_abs_grade": maximum_abs_grade,
                "grade_scale": grade_scale,
                "slope_speed_kmh": speed,
                "time_min": length_m / (speed * 1000.0 / 60.0),
            })
            directed.add_edge(start, end, **attrs)
    return directed


def _route_to_station(
    graph: nx.DiGraph,
    origin: tuple[float, float],
    origin_snap_m: float,
    source_metadata: dict[tuple[float, float], tuple[str, float]],
) -> dict[str, object]:
    times, paths = nx.single_source_dijkstra(graph, origin, weight="time_min")
    candidates = []
    level_m_min = WALK_SPEED_KMH * 1000.0 / 60.0
    for source, (station, entrance_snap_m) in source_metadata.items():
        if source in times:
            total_time = times[source] + (origin_snap_m + entrance_snap_m) / level_m_min
            candidates.append((total_time, source, station, entrance_snap_m))
    if not candidates:
        raise nx.NetworkXNoPath("No station entrance can be reached from origin")
    total_time, source, station, entrance_snap_m = min(candidates, key=lambda row: row[0])
    path = paths[source]
    length_m = origin_snap_m + entrance_snap_m
    gain_m = 0.0
    loss_m = 0.0
    capped_edges = 0
    maximum_abs_grade = 0.0
    for u, v in zip(path[:-1], path[1:]):
        edge = graph[u][v]
        length_m += float(edge["length_m"])
        delta = float(edge["elevation_delta_m"])
        gain_m += max(0.0, delta)
        loss_m += max(0.0, -delta)
        capped_edges += int(edge["grade_was_capped"])
        maximum_abs_grade = max(maximum_abs_grade, abs(float(edge["raw_grade"])))
    return {
        "station_name": station,
        "time_min": float(total_time),
        "distance_m": float(length_m),
        "elevation_gain_m": gain_m,
        "elevation_loss_m": loss_m,
        "maximum_abs_edge_grade": maximum_abs_grade,
        "capped_edge_count": capped_edges,
        "path": path,
    }


def analyse_residential_slope(
    graph: nx.Graph,
    tile: HgtTile,
    areas: gpd.GeoDataFrame,
    entrances: gpd.GeoDataFrame,
    grade_scales: tuple[float, ...] = GRADE_SCALES,
    maximum_abs_grade: float = MAX_ABS_GRADE,
) -> pd.DataFrame:
    """Calculate slope-sensitive routes for all valid named residential areas."""
    _, source_metadata = _entrance_sources(graph, entrances)
    slope_graphs = {
        scale: build_slope_graph(graph, tile, scale, maximum_abs_grade)
        for scale in grade_scales
    }
    records: list[dict[str, object]] = []
    for row in areas.itertuples():
        origin, origin_snap_m = nearest_graph_node(graph, row.geometry)
        if origin_snap_m > MAXIMUM_ORIGIN_SNAP_M:
            continue
        flat_path: tuple[tuple[float, float], ...] | None = None
        for scale, slope_graph in slope_graphs.items():
            route = _route_to_station(slope_graph, origin, origin_snap_m, source_metadata)
            path_tuple = tuple(route.pop("path"))
            if scale == 0.0:
                flat_path = path_tuple
            records.append({
                "osm_ref": row.osm_ref,
                "residential_name": row.name,
                "grade_scale": scale,
                "maximum_abs_grade": maximum_abs_grade,
                "model_name": "flat_4_5_kmh" if scale == 0 else "normalized_tobler",
                "origin_snap_m": origin_snap_m,
                **route,
                "route_changed_from_flat": path_tuple != flat_path if flat_path is not None else False,
            })
    return pd.DataFrame(records)


def analyse_case_scenarios(
    graph: nx.Graph,
    tile: HgtTile,
    areas: gpd.GeoDataFrame,
    entrances: gpd.GeoDataFrame,
    grade_scales: tuple[float, ...] = GRADE_SCALES,
    maximum_abs_grade: float = MAX_ABS_GRADE,
) -> tuple[pd.DataFrame, dict[str, gpd.GeoDataFrame]]:
    """Re-evaluate the three connector scenarios under slope-sensitive costs."""
    sources, source_metadata = _entrance_sources(graph, entrances)
    baseline_slope_graphs = {
        scale: build_slope_graph(graph, tile, scale, maximum_abs_grade)
        for scale in grade_scales
    }
    records: list[dict[str, object]] = []
    spatial: dict[str, list[dict[str, object]]] = {
        "origins": [], "baseline_routes": [], "scenario_routes": [], "connectors": [],
    }

    for case_id, area_name in enumerate(CASE_AREAS, start=1):
        area = areas[areas["name"] == area_name]
        if len(area) != 1:
            raise RuntimeError(f"Expected exactly one area named {area_name!r}")
        area_row = area.iloc[0]
        origin, origin_snap_m = nearest_graph_node(graph, area_row.geometry)
        _, length_paths = nx.multi_source_dijkstra(graph, sources, weight="length_m")
        baseline_length_path = list(reversed(length_paths[origin]))
        shortcut = select_path_shortcut(graph, baseline_length_path)
        scenario_graph = _add_connector(graph, shortcut, str(case_id))

        for scale in grade_scales:
            baseline = _route_to_station(
                baseline_slope_graphs[scale], origin, origin_snap_m, source_metadata
            )
            scenario_slope_graph = build_slope_graph(
                scenario_graph, tile, scale, maximum_abs_grade
            )
            scenario = _route_to_station(
                scenario_slope_graph, origin, origin_snap_m, source_metadata
            )
            saving = float(baseline["time_min"]) - float(scenario["time_min"])
            records.append({
                "case_id": case_id,
                "residential_name": area_name,
                "grade_scale": scale,
                "maximum_abs_grade": maximum_abs_grade,
                "model_name": "flat_4_5_kmh" if scale == 0 else "normalized_tobler",
                "baseline_station": baseline["station_name"],
                "scenario_station": scenario["station_name"],
                "baseline_time_min": baseline["time_min"],
                "scenario_time_min": scenario["time_min"],
                "time_saved_min": saving,
                "time_saved_pct": 100.0 * saving / float(baseline["time_min"]),
                "baseline_distance_m": baseline["distance_m"],
                "scenario_distance_m": scenario["distance_m"],
                "baseline_elevation_gain_m": baseline["elevation_gain_m"],
                "scenario_elevation_gain_m": scenario["elevation_gain_m"],
                "baseline_capped_edge_count": baseline["capped_edge_count"],
                "scenario_capped_edge_count": scenario["capped_edge_count"],
                "under_15_min_after": float(scenario["time_min"]) <= 15.0,
                "connector_length_m": shortcut.connector_length_m,
                "connector_elevation_delta_m": scenario_slope_graph[shortcut.u][shortcut.v]["elevation_delta_m"],
                "connector_raw_grade": scenario_slope_graph[shortcut.u][shortcut.v]["raw_grade"],
                "connector_used_grade": scenario_slope_graph[shortcut.u][shortcut.v]["used_grade"],
                "connector_grade_was_capped": scenario_slope_graph[shortcut.u][shortcut.v]["grade_was_capped"],
                "connector_direction_speed_kmh": scenario_slope_graph[shortcut.u][shortcut.v]["slope_speed_kmh"],
                "scenario_status": "hypothetical_unverified_pedestrian_connector",
            })
            if scale == 1.0:
                spatial["origins"].append({
                    "case_id": case_id, "residential_name": area_name,
                    "geometry": area_row.geometry,
                })
                spatial["baseline_routes"].append({
                    "case_id": case_id, "residential_name": area_name,
                    "time_min": baseline["time_min"],
                    "geometry": LineString(baseline.pop("path")),
                })
                spatial["scenario_routes"].append({
                    "case_id": case_id, "residential_name": area_name,
                    "time_min": scenario["time_min"],
                    "geometry": LineString(scenario.pop("path")),
                })
                spatial["connectors"].append({
                    "case_id": case_id, "residential_name": area_name,
                    "connector_length_m": shortcut.connector_length_m,
                    "scenario_status": "hypothetical_unverified",
                    "geometry": LineString([shortcut.u, shortcut.v]),
                })

    frames = {
        name: gpd.GeoDataFrame(rows, geometry="geometry", crs=ANALYSIS_CRS)
        for name, rows in spatial.items()
    }
    return pd.DataFrame(records), frames


def build_edge_quality_table(
    graph: nx.Graph,
    tile: HgtTile,
    lines: gpd.GeoDataFrame,
) -> pd.DataFrame:
    """Audit DEM-derived grades against OSM bridge, tunnel and steps context."""
    slope_graph = build_slope_graph(graph, tile, 1.0, MAX_ABS_GRADE)
    tag_lookup = {
        str(row.osm_id): parse_other_tags(row.other_tags)
        for row in lines.itertuples()
    }
    rows: list[dict[str, object]] = []
    for u, v, data in graph.edges(data=True):
        edge = slope_graph[u][v]
        tags = tag_lookup.get(str(data["osm_id"]), {})
        bridge = tags.get("bridge") not in (None, "no")
        tunnel = tags.get("tunnel") not in (None, "no")
        steps = data["highway"] == "steps"
        abs_grade = abs(float(edge["raw_grade"]))
        rows.append({
            "osm_id": data["osm_id"],
            "highway": data["highway"],
            "length_m": data["length_m"],
            "elevation_low_m": min(edge["elevation_start_m"], edge["elevation_end_m"]),
            "elevation_high_m": max(edge["elevation_start_m"], edge["elevation_end_m"]),
            "absolute_elevation_change_m": abs(edge["elevation_delta_m"]),
            "absolute_raw_grade": abs_grade,
            "absolute_used_grade": abs(edge["used_grade"]),
            "grade_was_capped": edge["grade_was_capped"],
            "is_bridge": bridge,
            "is_tunnel": tunnel,
            "is_steps": steps,
            "review_priority": (
                "high_grade_over_35pct" if abs_grade > MAX_ABS_GRADE else
                "structure_context" if bridge or tunnel or steps else
                "routine"
            ),
        })
    return pd.DataFrame(rows).sort_values(
        ["grade_was_capped", "absolute_raw_grade"], ascending=[False, False]
    )


def analyse_grade_cap_sensitivity(
    graph: nx.Graph,
    tile: HgtTile,
    areas: gpd.GeoDataFrame,
    entrances: gpd.GeoDataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Measure how alternative grade caps affect portfolio and case results."""
    summary_rows: list[dict[str, object]] = []
    case_frames: list[pd.DataFrame] = []
    for cap in GRADE_CAPS:
        residential = analyse_residential_slope(
            graph, tile, areas, entrances,
            grade_scales=(1.0,), maximum_abs_grade=cap,
        )
        cases, _ = analyse_case_scenarios(
            graph, tile, areas, entrances,
            grade_scales=(1.0,), maximum_abs_grade=cap,
        )
        summary_rows.append({
            "maximum_abs_grade": cap,
            "residential_count": len(residential),
            "over_10_min_count": int((residential["time_min"] > 10).sum()),
            "over_15_min_count": int((residential["time_min"] > 15).sum()),
            "over_20_min_count": int((residential["time_min"] > 20).sum()),
            "mean_time_min": residential["time_min"].mean(),
            "median_time_min": residential["time_min"].median(),
            "routes_with_capped_edges": int((residential["capped_edge_count"] > 0).sum()),
            "capped_edges_across_routes": int(residential["capped_edge_count"].sum()),
        })
        case_frames.append(cases)
    return pd.DataFrame(summary_rows), pd.concat(case_frames, ignore_index=True)


def plot_slope_cases(
    output_path: Path,
    graph: nx.Graph,
    cases: pd.DataFrame,
    spatial: dict[str, gpd.GeoDataFrame],
    stations: gpd.GeoDataFrame,
) -> None:
    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
    network = gpd.GeoDataFrame(
        [{"geometry": data["geometry"]} for _, _, data in graph.edges(data=True)],
        geometry="geometry", crs=ANALYSIS_CRS,
    )
    focus = cases[cases["grade_scale"] == 1.0].sort_values("case_id")
    fig, axes = plt.subplots(1, 3, figsize=(16, 6), dpi=180)
    for ax, result in zip(axes, focus.itertuples()):
        case_id = result.case_id
        baseline = spatial["baseline_routes"].query("case_id == @case_id")
        scenario = spatial["scenario_routes"].query("case_id == @case_id")
        connector = spatial["connectors"].query("case_id == @case_id")
        origin = spatial["origins"].query("case_id == @case_id")
        bounds = baseline.total_bounds
        pad = 180
        network.cx[bounds[0]-pad:bounds[2]+pad, bounds[1]-pad:bounds[3]+pad].plot(
            ax=ax, color="#d6d6d6", linewidth=0.5, zorder=1
        )
        baseline.plot(ax=ax, color="#666666", linewidth=2.2, zorder=2, label="坡度校正基线")
        scenario.plot(ax=ax, color="#1677b8", linewidth=2.6, zorder=3, label="坡度校正情景")
        connector.plot(ax=ax, color="#d01c8b", linewidth=4.0, zorder=4, label="假设连接")
        origin.plot(ax=ax, color="#d7301f", edgecolor="white", markersize=80, zorder=5, label="住宅区代表点")
        stations.cx[bounds[0]-pad:bounds[2]+pad, bounds[1]-pad:bounds[3]+pad].plot(
            ax=ax, color="#2166ac", edgecolor="white", marker="s", markersize=75,
            zorder=5, label="轨道站",
        )
        ax.set_xlim(bounds[0]-pad, bounds[2]+pad)
        ax.set_ylim(bounds[1]-pad, bounds[3]+pad)
        ax.set_aspect("equal")
        ax.set_axis_off()
        ax.set_title(
            f"{result.residential_name}\n{result.baseline_time_min:.1f} → "
            f"{result.scenario_time_min:.1f} 分钟（坡度模型）",
            fontsize=11,
        )
    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper center", bbox_to_anchor=(0.5, 0.91),
               ncol=5, frameon=True, fontsize=8)
    fig.suptitle("高庙村站周边：SRTM坡度校正后的假设连接情景", fontsize=16, y=0.99)
    fig.text(
        0.5, 0.025,
        "Tobler函数归一化至平地4.5 km/h；连接为未核验假设。© OpenStreetMap contributors；SRTM data courtesy of USGS / Mapzen。",
        ha="center", fontsize=8,
    )
    fig.tight_layout(rect=(0, 0.07, 1, 0.84))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, bbox_inches="tight", facecolor="white")
    plt.close(fig)


def plot_model_comparison(
    output_path: Path,
    residential: pd.DataFrame,
    cases: pd.DataFrame,
) -> None:
    """Explain threshold and case-level changes in a reader-facing chart."""
    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
    flat = residential[residential["grade_scale"] == 0.0]
    slope = residential[residential["grade_scale"] == 1.0]
    thresholds = [10, 15, 20]
    flat_counts = [int((flat["time_min"] > value).sum()) for value in thresholds]
    slope_counts = [int((slope["time_min"] > value).sum()) for value in thresholds]

    focus = cases[cases["grade_scale"].isin([0.0, 1.0])].copy()
    flat_cases = focus[focus["grade_scale"] == 0.0].sort_values("case_id")
    slope_cases = focus[focus["grade_scale"] == 1.0].sort_values("case_id")
    labels = ["锦樾峰境", "奇峰自由湾", "大城小爱"]

    fig, axes = plt.subplots(1, 2, figsize=(13, 5.5), dpi=180)
    x = np.arange(len(thresholds))
    width = 0.34
    axes[0].bar(x - width / 2, flat_counts, width, label="固定4.5 km/h", color="#9ecae1")
    axes[0].bar(x + width / 2, slope_counts, width, label="SRTM坡度模型", color="#2171b5")
    for xpos, value in zip(x - width / 2, flat_counts):
        axes[0].text(xpos, value + 0.4, str(value), ha="center", fontsize=9)
    for xpos, value in zip(x + width / 2, slope_counts):
        axes[0].text(xpos, value + 0.4, str(value), ha="center", fontsize=9)
    axes[0].set_xticks(x, [f">{value}分钟" for value in thresholds])
    axes[0].set_ylim(0, 32)
    axes[0].set_ylabel("住宅区数量（31个有效等权样本）")
    axes[0].set_title("坡度主要改变严重超时识别")
    axes[0].legend(frameon=False)
    axes[0].grid(axis="y", alpha=0.2)

    x2 = np.arange(3)
    width2 = 0.25
    axes[1].bar(
        x2 - width2, flat_cases["baseline_time_min"], width2,
        label="固定速度基线", color="#bdbdbd",
    )
    axes[1].bar(
        x2, slope_cases["baseline_time_min"], width2,
        label="坡度基线", color="#636363",
    )
    axes[1].bar(
        x2 + width2, slope_cases["scenario_time_min"], width2,
        label="坡度连接情景", color="#2b8cbe",
    )
    axes[1].axhline(15, color="#d7301f", linestyle="--", linewidth=1.2, label="15分钟")
    axes[1].set_xticks(x2, labels)
    axes[1].set_ylabel("模型步行时间（分钟）")
    axes[1].set_title("坡度复算未改变三个方案的优先顺序")
    axes[1].legend(frameon=False, fontsize=8)
    axes[1].grid(axis="y", alpha=0.2)

    fig.suptitle("固定速度模型与坡度模型：结果改变在哪里？", fontsize=16)
    fig.text(
        0.5, 0.015,
        "时间均为模型估计，不是实测；住宅区样本不代表人口。SRTM data courtesy of USGS / Mapzen。",
        ha="center", fontsize=8,
    )
    fig.tight_layout(rect=(0, 0.05, 1, 0.92))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, bbox_inches="tight", facecolor="white")
    plt.close(fig)


def save_outputs(
    project_root: Path,
    graph: nx.Graph,
    stations: gpd.GeoDataFrame,
    residential: pd.DataFrame,
    cases: pd.DataFrame,
    spatial: dict[str, gpd.GeoDataFrame],
    edge_quality: pd.DataFrame,
    cap_summary: pd.DataFrame,
    cap_cases: pd.DataFrame,
) -> dict[str, Path]:
    processed = project_root / "data" / "processed"
    figures = project_root / "reports" / "figures"
    residential_path = processed / "shapingba_slope_adjusted_residential.csv"
    cases_path = processed / "shapingba_slope_adjusted_improvement.csv"
    edge_quality_path = processed / "shapingba_slope_edge_quality.csv"
    cap_summary_path = processed / "shapingba_slope_cap_sensitivity_summary.csv"
    cap_cases_path = processed / "shapingba_slope_cap_sensitivity_cases.csv"
    gpkg_path = processed / "shapingba_slope_adjusted_improvement.gpkg"
    figure_path = figures / "shapingba_slope_adjusted_improvement.png"
    comparison_figure_path = figures / "shapingba_slope_model_comparison.png"
    residential.to_csv(residential_path, index=False, encoding="utf-8-sig")
    cases.to_csv(cases_path, index=False, encoding="utf-8-sig")
    edge_quality.to_csv(edge_quality_path, index=False, encoding="utf-8-sig")
    cap_summary.to_csv(cap_summary_path, index=False, encoding="utf-8-sig")
    cap_cases.to_csv(cap_cases_path, index=False, encoding="utf-8-sig")
    if gpkg_path.exists():
        gpkg_path.unlink()
    for index, (name, frame) in enumerate(spatial.items()):
        frame.to_file(gpkg_path, layer=name, driver="GPKG", mode="w" if index == 0 else "a")
    plot_slope_cases(figure_path, graph, cases, spatial, stations)
    plot_model_comparison(comparison_figure_path, residential, cases)
    return {
        "residential": residential_path,
        "cases": cases_path,
        "edge_quality": edge_quality_path,
        "cap_summary": cap_summary_path,
        "cap_cases": cap_cases_path,
        "geopackage": gpkg_path,
        "figure": figure_path,
        "comparison_figure": comparison_figure_path,
    }


def run(pbf_path: Path, hgt_path: Path, project_root: Path) -> dict[str, Path]:
    lines = load_walkable_lines(pbf_path)
    graph = build_walking_graph(lines)
    stations = load_verified_stations(pbf_path)
    entrances = load_station_entrances(pbf_path, stations)
    areas = load_named_residential_areas(pbf_path)
    tile = HgtTile.from_gzip(hgt_path)
    residential = analyse_residential_slope(graph, tile, areas, entrances)
    cases, spatial = analyse_case_scenarios(graph, tile, areas, entrances)
    edge_quality = build_edge_quality_table(graph, tile, lines)
    cap_summary, cap_cases = analyse_grade_cap_sensitivity(
        graph, tile, areas, entrances
    )
    return save_outputs(
        project_root, graph, stations, residential, cases, spatial,
        edge_quality, cap_summary, cap_cases,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pbf", type=Path,
        default=Path("work/data_source_survey/chongqing_osm_2026-10-10.osm.pbf"),
    )
    parser.add_argument(
        "--hgt", type=Path,
        default=Path("work/data_source_survey/N29E106.hgt.gz"),
    )
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    for name, path in run(
        args.pbf.resolve(), args.hgt.resolve(), args.project_root.resolve()
    ).items():
        print(f"{name}: {path}")


if __name__ == "__main__":
    main()
