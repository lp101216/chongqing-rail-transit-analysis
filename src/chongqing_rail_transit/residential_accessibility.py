"""Residential-area screening for the Shapingba--Gaomiaocun corridor.

The analysis uses named OpenStreetMap ``landuse=residential`` polygons as
places, not as population observations.  Each polygon is represented by an
interior point and routed over the same walking graph as the station MVP.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import geopandas as gpd
import matplotlib.pyplot as plt
import networkx as nx
import numpy as np
import pandas as pd
import pyogrio
from shapely.geometry import LineString

from .walk_accessibility_mvp import (
    ANALYSIS_CRS,
    OSM_SNAPSHOT_DATE,
    SOURCE_CRS,
    TEST_BBOX,
    WALK_SPEED_KMH,
    build_walking_graph,
    load_station_entrances,
    load_verified_stations,
    load_walkable_lines,
    nearest_graph_node,
)


SENSITIVITY_SPEEDS_KMH = (3.6, 4.5, 5.4)
ACCESS_THRESHOLDS_MIN = (10, 15, 20)
DIRECTNESS_CAPS = (1.15, 1.25, 1.40)


def load_named_residential_areas(pbf_path: Path) -> gpd.GeoDataFrame:
    """Load named residential land-use polygons and create interior origins."""
    areas = pyogrio.read_dataframe(
        pbf_path,
        layer="multipolygons",
        bbox=TEST_BBOX,
        columns=["osm_id", "osm_way_id", "name", "landuse", "building", "other_tags"],
    )
    areas.set_crs(SOURCE_CRS, inplace=True, allow_override=True)
    areas = areas[(areas["landuse"] == "residential") & areas["name"].notna()].copy()
    areas = areas[areas["name"].str.strip().ne("")].to_crs(ANALYSIS_CRS)
    areas["osm_ref"] = np.where(
        areas["osm_id"].notna(),
        "relation/" + areas["osm_id"].fillna(0).astype("int64").astype(str),
        "way/" + areas["osm_way_id"].fillna(0).astype("int64").astype(str),
    )
    areas["area_ha"] = areas.geometry.area / 10_000.0
    areas["polygon_geometry"] = areas.geometry
    areas["geometry"] = areas.geometry.representative_point()
    return areas[["osm_ref", "name", "area_ha", "polygon_geometry", "geometry"]].reset_index(drop=True)


def _entrance_sources(
    graph: nx.Graph, entrances: gpd.GeoDataFrame
) -> tuple[list[tuple[float, float]], dict[tuple[float, float], tuple[str, float]]]:
    sources: list[tuple[float, float]] = []
    source_metadata: dict[tuple[float, float], tuple[str, float]] = {}
    for row in entrances.itertuples():
        node, snap_m = nearest_graph_node(graph, row.geometry)
        if snap_m > 100:
            raise RuntimeError(f"Entrance {row.osm_id} is {snap_m:.1f} m from the graph")
        sources.append(node)
        current = source_metadata.get(node)
        if current is None or snap_m < current[1]:
            source_metadata[node] = (row.station_name, snap_m)
    return list(dict.fromkeys(sources)), source_metadata


def _path_step_length(graph: nx.Graph, path: list[tuple[float, float]]) -> float:
    return float(sum(
        edge["length_m"]
        for u, v in zip(path[:-1], path[1:])
        if (edge := graph[u][v]).get("highway") == "steps"
    ))


def analyse_residential_areas(
    graph: nx.Graph,
    areas: gpd.GeoDataFrame,
    entrances: gpd.GeoDataFrame,
    maximum_origin_snap_m: float = 150.0,
) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
    """Route area representative points to the nearest station entrance."""
    sources, source_metadata = _entrance_sources(graph, entrances)
    distances, paths = nx.multi_source_dijkstra(graph, sources, weight="length_m")
    area_records: list[dict[str, object]] = []
    route_records: list[dict[str, object]] = []

    for row in areas.itertuples():
        origin, origin_snap_m = nearest_graph_node(graph, row.geometry)
        record: dict[str, object] = {
            "osm_ref": row.osm_ref,
            "residential_name": row.name,
            "area_ha": row.area_ha,
            "origin_snap_m": origin_snap_m,
            "network_reachable": origin in distances and origin_snap_m <= maximum_origin_snap_m,
            "geometry": row.geometry,
        }
        if not record["network_reachable"]:
            record["data_quality_flag"] = (
                "origin_snap_over_150m" if origin_snap_m > maximum_origin_snap_m
                else "disconnected_from_station_component"
            )
            area_records.append(record)
            continue

        path = paths[origin]
        station_name, entrance_snap_m = source_metadata[path[0]]
        station_entrances = entrances[entrances["station_name"] == station_name]
        straight_m = float(station_entrances.geometry.distance(row.geometry).min())
        graph_distance_m = float(distances[origin])
        network_distance_m = graph_distance_m + origin_snap_m + entrance_snap_m
        step_length_m = _path_step_length(graph, path)
        detour_ratio = network_distance_m / straight_m if straight_m > 0 else np.nan
        record.update({
            "nearest_station": station_name,
            "entrance_snap_m": entrance_snap_m,
            "straight_line_distance_m": straight_m,
            "network_walk_distance_m": network_distance_m,
            "walk_time_min_4_5": network_distance_m / (WALK_SPEED_KMH * 1000 / 60),
            "detour_ratio": detour_ratio,
            "steps_length_m": step_length_m,
            "steps_share_pct": 100 * step_length_m / graph_distance_m if graph_distance_m else 0.0,
            "connection_candidate_1_25": (
                network_distance_m > 1125 and straight_m * 1.25 <= 1125
            ),
            "feeder_candidate_1_25": straight_m * 1.25 > 1125,
            "data_quality_flag": "ok",
        })
        for speed in SENSITIVITY_SPEEDS_KMH:
            speed_suffix = str(speed).replace('.', '_')
            walk_time = (
                network_distance_m / (speed * 1000 / 60)
            )
            record[f"walk_time_min_{speed_suffix}"] = walk_time
            for threshold in ACCESS_THRESHOLDS_MIN:
                record[f"over_{threshold}_min_{speed_suffix}"] = walk_time > threshold
        base_time = float(record["walk_time_min_4_5"])
        idealized_distance = min(network_distance_m, straight_m * 1.25)
        idealized_time = idealized_distance / (WALK_SPEED_KMH * 1000 / 60)
        record["idealized_distance_m_1_25"] = idealized_distance
        record["idealized_walk_time_min_1_25"] = idealized_time
        record["modeled_time_reduction_min_1_25"] = max(0.0, base_time - idealized_time)
        record["improvement_candidate_type"] = (
            "walking_connection_screen" if base_time > 15 and idealized_time <= 15 else
            "feeder_transit_screen" if base_time > 15 else
            "not_over_15_min"
        )
        record["accessibility_band_4_5"] = (
            "le_10_min" if base_time <= 10 else
            "10_15_min" if base_time <= 15 else
            "15_20_min" if base_time <= 20 else
            "over_20_min"
        )
        area_records.append(record)
        route_records.append({
            "osm_ref": row.osm_ref,
            "residential_name": row.name,
            "nearest_station": station_name,
            "network_walk_distance_m": network_distance_m,
            "geometry": LineString(path),
        })

    analysed = gpd.GeoDataFrame(area_records, geometry="geometry", crs=ANALYSIS_CRS)
    routes = gpd.GeoDataFrame(route_records, geometry="geometry", crs=ANALYSIS_CRS)
    return analysed, routes


def build_summary(analysed: gpd.GeoDataFrame) -> pd.DataFrame:
    valid = analysed[analysed["network_reachable"]].copy()
    rows: list[dict[str, object]] = []
    for speed in SENSITIVITY_SPEEDS_KMH:
        suffix = str(speed).replace(".", "_")
        for threshold in ACCESS_THRESHOLDS_MIN:
            rows.append({
                "scenario": f"walk_speed_{speed:.1f}_kmh",
                "assumption_value": speed,
                "threshold_min": threshold,
                "named_residential_areas": len(valid),
                "over_threshold_count": int(valid[f"over_{threshold}_min_{suffix}"].sum()),
                "over_threshold_share_pct": 100 * valid[f"over_{threshold}_min_{suffix}"].mean(),
            })
    gaps = valid[valid["over_15_min_4_5"]]
    for cap in DIRECTNESS_CAPS:
        improved = gaps["straight_line_distance_m"] * cap <= 1125
        rows.append({
            "scenario": f"directness_cap_{cap:.2f}",
            "assumption_value": cap,
            "threshold_min": 15,
            "named_residential_areas": len(valid),
            "over_threshold_count": int(len(gaps) - improved.sum()),
            "over_threshold_share_pct": 100 * (len(gaps) - improved.sum()) / len(valid),
            "potentially_shifted_under_15_min": int(improved.sum()),
        })
    return pd.DataFrame(rows)


def build_connectivity_summary(
    graph: nx.Graph,
    entrances: gpd.GeoDataFrame,
    analysed: gpd.GeoDataFrame,
) -> pd.DataFrame:
    components = list(nx.connected_components(graph))
    component_by_node = {
        node: component_id
        for component_id, nodes in enumerate(components)
        for node in nodes
    }
    entrance_nodes, _ = _entrance_sources(graph, entrances)
    entrance_components = {component_by_node[node] for node in entrance_nodes}
    largest = max((len(nodes) for nodes in components), default=0)
    return pd.DataFrame([{
        "network_nodes": graph.number_of_nodes(),
        "network_edges": graph.number_of_edges(),
        "connected_components": len(components),
        "largest_component_nodes": largest,
        "largest_component_node_share_pct": 100 * largest / graph.number_of_nodes(),
        "station_entrance_components": len(entrance_components),
        "residential_origins_total": len(analysed),
        "residential_origins_routed": int(analysed["network_reachable"].sum()),
        "origins_disconnected_from_station_component": int(
            (analysed["data_quality_flag"] == "disconnected_from_station_component").sum()
        ),
        "origins_over_150m_snap": int(
            (analysed["data_quality_flag"] == "origin_snap_over_150m").sum()
        ),
    }])


def save_outputs(
    project_root: Path,
    analysed: gpd.GeoDataFrame,
    routes: gpd.GeoDataFrame,
    summary: pd.DataFrame,
    connectivity: pd.DataFrame,
    stations: gpd.GeoDataFrame,
    network_edges: gpd.GeoDataFrame,
) -> dict[str, Path]:
    processed = project_root / "data" / "processed"
    figures = project_root / "reports" / "figures"
    processed.mkdir(parents=True, exist_ok=True)
    figures.mkdir(parents=True, exist_ok=True)
    csv_path = processed / "shapingba_residential_accessibility.csv"
    summary_path = processed / "shapingba_residential_accessibility_sensitivity.csv"
    poor_path = processed / "shapingba_poor_accessibility_areas.csv"
    candidates_path = processed / "shapingba_improvement_candidates.csv"
    connectivity_path = processed / "shapingba_walk_network_connectivity.csv"
    gpkg_path = processed / "shapingba_residential_accessibility.gpkg"
    figure_path = figures / "shapingba_residential_accessibility.png"

    csv_frame = analysed.drop(columns="geometry").copy()
    csv_frame.to_csv(csv_path, index=False, encoding="utf-8-sig")
    summary.to_csv(summary_path, index=False, encoding="utf-8-sig")
    poor_columns = [
        "osm_ref", "residential_name", "nearest_station", "walk_time_min_4_5",
        "network_walk_distance_m", "straight_line_distance_m", "detour_ratio",
        "accessibility_band_4_5", "connection_candidate_1_25",
        "feeder_candidate_1_25", "origin_snap_m", "entrance_snap_m",
        "idealized_walk_time_min_1_25", "modeled_time_reduction_min_1_25",
        "improvement_candidate_type",
    ]
    analysed[
        analysed["network_reachable"] & analysed["over_15_min_4_5"].fillna(False)
    ].sort_values("walk_time_min_4_5", ascending=False)[poor_columns].to_csv(
        poor_path, index=False, encoding="utf-8-sig"
    )
    connectivity.to_csv(connectivity_path, index=False, encoding="utf-8-sig")
    candidate_columns = [
        "residential_name", "nearest_station", "walk_time_min_4_5",
        "straight_line_distance_m", "detour_ratio", "idealized_distance_m_1_25",
        "idealized_walk_time_min_1_25", "modeled_time_reduction_min_1_25",
        "improvement_candidate_type",
    ]
    analysed[
        analysed["network_reachable"] & analysed["over_15_min_4_5"].fillna(False)
    ].sort_values(
        ["improvement_candidate_type", "modeled_time_reduction_min_1_25"],
        ascending=[True, False],
    )[candidate_columns].to_csv(candidates_path, index=False, encoding="utf-8-sig")
    if gpkg_path.exists():
        gpkg_path.unlink()
    analysed.drop(columns="polygon_geometry", errors="ignore").to_file(
        gpkg_path, layer="residential_origins", driver="GPKG"
    )
    routes.to_file(gpkg_path, layer="residential_routes", driver="GPKG", mode="a")

    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
    fig, ax = plt.subplots(figsize=(14, 10), dpi=180)
    network_edges.plot(ax=ax, color="#d7d7d7", linewidth=0.35, zorder=1)
    routes.plot(ax=ax, color="#a6a6a6", linewidth=0.5, alpha=0.65, zorder=2)
    band_styles = {
        "le_10_min": ("#238b45", "≤10分钟"),
        "10_15_min": ("#78c679", "10—15分钟"),
        "15_20_min": ("#f39c12", "15—20分钟"),
        "over_20_min": ("#de2d26", ">20分钟"),
    }
    excluded = analysed[~analysed["network_reachable"]]
    for band, (color, label) in band_styles.items():
        subset = analysed[analysed["network_reachable"] & (analysed["accessibility_band_4_5"] == band)]
        subset.plot(ax=ax, color=color, edgecolor="white", markersize=48, zorder=5, label=label)
    excluded.plot(ax=ax, color="#777777", marker="x", markersize=45, zorder=5,
                  label="未通过吸附质量门槛")
    stations.plot(ax=ax, color="#2166ac", edgecolor="white", markersize=80, marker="s", zorder=6, label="轨道站")
    ranked = analysed[analysed["network_reachable"]].nlargest(10, "walk_time_min_4_5").reset_index(drop=True)
    for number, row in enumerate(ranked.itertuples(), start=1):
        ax.annotate(str(number), (row.geometry.x, row.geometry.y), ha="center", va="center",
                    color="white", fontsize=6.5, weight="bold", zorder=7)
    ax.set_axis_off()
    ax.set_aspect("equal")
    ax.set_title("沙坪坝—高庙村：具名住宅区到最近轨道站出入口的步行可达性")
    ax.legend(loc="upper left", frameon=True, fontsize=8)
    ranking_text = "最长10条模型路线（分钟 / 最近站）\n" + "\n".join(
        f"{i:>2}. {row.residential_name}  {row.walk_time_min_4_5:.1f} / {row.nearest_station}"
        for i, row in enumerate(ranked.itertuples(), start=1)
    )
    fig.text(0.73, 0.72, ranking_text, ha="left", va="top", fontsize=9, linespacing=1.5)
    fig.text(
        0.5, 0.025,
        f"OSM具名 residential 土地利用面；内部代表点；步速4.5 km/h；快照 {OSM_SNAPSHOT_DATE}。"
        " 每个点等权，不代表人口；路线未计坡度与站内时间。",
        ha="center", fontsize=7.5,
    )
    fig.tight_layout(rect=(0, 0.06, 0.72, 1))
    fig.savefig(figure_path, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return {
        "areas": csv_path, "poor_areas": poor_path, "candidates": candidates_path,
        "sensitivity": summary_path,
        "connectivity": connectivity_path, "geopackage": gpkg_path, "figure": figure_path,
    }


def run(pbf_path: Path, project_root: Path) -> dict[str, Path]:
    lines = load_walkable_lines(pbf_path)
    graph = build_walking_graph(lines)
    stations = load_verified_stations(pbf_path)
    entrances = load_station_entrances(pbf_path, stations)
    areas = load_named_residential_areas(pbf_path)
    analysed, routes = analyse_residential_areas(graph, areas, entrances)
    summary = build_summary(analysed)
    connectivity = build_connectivity_summary(graph, entrances, analysed)
    network_records = [
        {"geometry": data["geometry"]} for _, _, data in graph.edges(data=True)
    ]
    network_edges = gpd.GeoDataFrame(network_records, geometry="geometry", crs=ANALYSIS_CRS)
    return save_outputs(
        project_root, analysed, routes, summary, connectivity, stations, network_edges
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pbf", type=Path, default=Path("work/data_source_survey/chongqing_osm_2026-10-10.osm.pbf"))
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    for name, path in run(args.pbf.resolve(), args.project_root.resolve()).items():
        print(f"{name}: {path}")


if __name__ == "__main__":
    main()
