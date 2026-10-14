"""Simulate explicitly hypothetical pedestrian shortcuts for three case areas.

The connector geometry is a straight line between two existing nodes on the
baseline shortest path.  It is a screening scenario, not evidence that a path
exists or can be built across the intervening land and terrain.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

import geopandas as gpd
import matplotlib.pyplot as plt
import networkx as nx
import pandas as pd
from shapely.geometry import LineString, Point

from .residential_accessibility import (
    SENSITIVITY_SPEEDS_KMH,
    _entrance_sources,
    load_named_residential_areas,
)
from .walk_accessibility_mvp import (
    ANALYSIS_CRS,
    WALK_SPEED_KMH,
    build_walking_graph,
    load_station_entrances,
    load_verified_stations,
    load_walkable_lines,
    nearest_graph_node,
    reachable_edge_frame_multi,
)


CASE_AREAS = ("成都城投锦樾峰境", "奇峰自由湾", "大城小爱")
CONNECTOR_MAX_M = 250.0
CONNECTOR_MIN_M = 20.0
CONNECTOR_MIN_DETOUR_RATIO = 1.5
CONNECTOR_COST_MULTIPLIERS = (1.0, 1.25, 1.5)
TIME_THRESHOLDS_MIN = (10, 15, 20)


@dataclass(frozen=True)
class Shortcut:
    u: tuple[float, float]
    v: tuple[float, float]
    connector_length_m: float
    replaced_path_length_m: float

    @property
    def local_detour_ratio(self) -> float:
        return self.replaced_path_length_m / self.connector_length_m

    @property
    def theoretical_saving_m(self) -> float:
        return self.replaced_path_length_m - self.connector_length_m


def select_path_shortcut(
    graph: nx.Graph,
    path: list[tuple[float, float]],
    minimum_length_m: float = CONNECTOR_MIN_M,
    maximum_length_m: float = CONNECTOR_MAX_M,
    minimum_detour_ratio: float = CONNECTOR_MIN_DETOUR_RATIO,
) -> Shortcut:
    """Find the largest detour removable by one short chord of the base path."""
    cumulative = [0.0]
    for u, v in zip(path[:-1], path[1:]):
        cumulative.append(cumulative[-1] + float(graph[u][v]["length_m"]))

    best: Shortcut | None = None
    for i, u in enumerate(path[:-2]):
        for j in range(i + 2, len(path)):
            v = path[j]
            if graph.has_edge(u, v):
                continue
            connector_length = Point(u).distance(Point(v))
            if not minimum_length_m <= connector_length <= maximum_length_m:
                continue
            replaced = cumulative[j] - cumulative[i]
            if replaced / connector_length < minimum_detour_ratio:
                continue
            candidate = Shortcut(u, v, connector_length, replaced)
            if best is None or candidate.theoretical_saving_m > best.theoretical_saving_m:
                best = candidate
    if best is None:
        raise RuntimeError("No shortcut satisfies the scenario constraints")
    return best


def _route_result(
    graph: nx.Graph,
    origin: tuple[float, float],
    origin_snap_m: float,
    sources: list[tuple[float, float]],
    source_metadata: dict[tuple[float, float], tuple[str, float]],
) -> tuple[float, str, list[tuple[float, float]]]:
    distance, paths = nx.multi_source_dijkstra(graph, sources, weight="length_m")
    path = paths[origin]
    station_name, entrance_snap_m = source_metadata[path[0]]
    return float(distance[origin] + origin_snap_m + entrance_snap_m), station_name, path


def _add_connector(
    graph: nx.Graph,
    shortcut: Shortcut,
    case_id: str,
    cost_multiplier: float = 1.0,
) -> nx.Graph:
    scenario = graph.copy()
    scenario.add_edge(
        shortcut.u,
        shortcut.v,
        length_m=shortcut.connector_length_m * cost_multiplier,
        geometry=LineString([shortcut.u, shortcut.v]),
        osm_id=f"scenario:{case_id}",
        highway="proposed_footway",
        segment_index=0,
        scenario_only=True,
        cost_multiplier=cost_multiplier,
    )
    return scenario


def _station_sources(
    graph: nx.Graph, entrances: gpd.GeoDataFrame, station_name: str
) -> list[tuple[float, float]]:
    nodes = []
    for row in entrances[entrances["station_name"] == station_name].itertuples():
        node, _ = nearest_graph_node(graph, row.geometry)
        nodes.append(node)
    return list(dict.fromkeys(nodes))


def simulate_cases(
    graph: nx.Graph,
    areas: gpd.GeoDataFrame,
    entrances: gpd.GeoDataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, gpd.GeoDataFrame]]:
    sources, source_metadata = _entrance_sources(graph, entrances)
    results: list[dict[str, object]] = []
    sensitivity: list[dict[str, object]] = []
    spatial = {"origins": [], "baseline_routes": [], "scenario_routes": [], "connectors": []}

    for case_number, area_name in enumerate(CASE_AREAS, start=1):
        area = areas[areas["name"] == area_name]
        if len(area) != 1:
            raise RuntimeError(f"Expected exactly one residential area named {area_name!r}")
        area_row = area.iloc[0]
        origin, origin_snap_m = nearest_graph_node(graph, area_row.geometry)
        baseline_m, baseline_station, baseline_path = _route_result(
            graph, origin, origin_snap_m, sources, source_metadata
        )
        shortcut = select_path_shortcut(graph, baseline_path)
        scenario_graph = _add_connector(graph, shortcut, str(case_number))
        scenario_m, scenario_station, scenario_path = _route_result(
            scenario_graph, origin, origin_snap_m, sources, source_metadata
        )
        saving_m = baseline_m - scenario_m

        result: dict[str, object] = {
            "case_id": case_number,
            "residential_name": area_name,
            "baseline_station": baseline_station,
            "scenario_station": scenario_station,
            "baseline_distance_m": baseline_m,
            "scenario_distance_m": scenario_m,
            "distance_saved_m": saving_m,
            "distance_saved_pct": 100 * saving_m / baseline_m,
            "baseline_time_min_4_5": baseline_m / 75.0,
            "scenario_time_min_4_5": scenario_m / 75.0,
            "time_saved_min_4_5": saving_m / 75.0,
            "time_saved_pct": 100 * saving_m / baseline_m,
            "connector_length_m": shortcut.connector_length_m,
            "replaced_path_length_m": shortcut.replaced_path_length_m,
            "connector_local_detour_ratio": shortcut.local_detour_ratio,
            "scenario_status": "hypothetical_unverified_pedestrian_connector",
            "data_confidence": "medium_for_network_effect_low_for_buildability",
        }

        station_sources = _station_sources(graph, entrances, scenario_station)
        for threshold in TIME_THRESHOLDS_MIN:
            cutoff_m = threshold * 75.0
            base_edges, _ = reachable_edge_frame_multi(graph, station_sources, cutoff_m)
            scenario_edges, _ = reachable_edge_frame_multi(
                scenario_graph, station_sources, cutoff_m
            )
            base_km = float(base_edges["length_m"].sum() / 1000)
            scenario_km = float(scenario_edges["length_m"].sum() / 1000)
            result[f"station_reachable_network_km_{threshold}_baseline"] = base_km
            result[f"station_reachable_network_km_{threshold}_scenario"] = scenario_km
            result[f"station_reachable_network_km_{threshold}_change"] = scenario_km - base_km

        origin_wgs84 = gpd.GeoSeries([area_row.geometry], crs=ANALYSIS_CRS).to_crs(4326).iloc[0]
        endpoints_wgs84 = gpd.GeoSeries(
            [Point(shortcut.u), Point(shortcut.v)], crs=ANALYSIS_CRS
        ).to_crs(4326)
        result.update({
            "origin_lon": origin_wgs84.x,
            "origin_lat": origin_wgs84.y,
            "connector_u_lon": endpoints_wgs84.iloc[0].x,
            "connector_u_lat": endpoints_wgs84.iloc[0].y,
            "connector_v_lon": endpoints_wgs84.iloc[1].x,
            "connector_v_lat": endpoints_wgs84.iloc[1].y,
        })
        results.append(result)

        for speed in SENSITIVITY_SPEEDS_KMH:
            for multiplier in CONNECTOR_COST_MULTIPLIERS:
                sensitivity_graph = _add_connector(
                    graph, shortcut, str(case_number), multiplier
                )
                sensitive_m, sensitive_station, _ = _route_result(
                    sensitivity_graph, origin, origin_snap_m, sources, source_metadata
                )
                speed_m_min = speed * 1000 / 60
                sensitivity.append({
                    "case_id": case_number,
                    "residential_name": area_name,
                    "walk_speed_kmh": speed,
                    "connector_cost_multiplier": multiplier,
                    "baseline_time_min": baseline_m / speed_m_min,
                    "scenario_time_min": sensitive_m / speed_m_min,
                    "time_saved_min": (baseline_m - sensitive_m) / speed_m_min,
                    "time_saved_pct": 100 * (baseline_m - sensitive_m) / baseline_m,
                    "scenario_station": sensitive_station,
                    "under_15_min_after": sensitive_m / speed_m_min <= 15,
                })

        spatial["origins"].append({
            "case_id": case_number, "residential_name": area_name,
            "geometry": area_row.geometry,
        })
        spatial["baseline_routes"].append({
            "case_id": case_number, "residential_name": area_name,
            "geometry": LineString(baseline_path),
        })
        spatial["scenario_routes"].append({
            "case_id": case_number, "residential_name": area_name,
            "geometry": LineString(scenario_path),
        })
        spatial["connectors"].append({
            "case_id": case_number, "residential_name": area_name,
            "connector_length_m": shortcut.connector_length_m,
            "scenario_status": "hypothetical_unverified",
            "geometry": LineString([shortcut.u, shortcut.v]),
        })

    frames = {
        key: gpd.GeoDataFrame(records, geometry="geometry", crs=ANALYSIS_CRS)
        for key, records in spatial.items()
    }
    return pd.DataFrame(results), pd.DataFrame(sensitivity), frames


def plot_scenarios(
    output_path: Path,
    graph: nx.Graph,
    results: pd.DataFrame,
    spatial: dict[str, gpd.GeoDataFrame],
    stations: gpd.GeoDataFrame,
) -> None:
    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
    network = gpd.GeoDataFrame(
        [{"geometry": data["geometry"]} for _, _, data in graph.edges(data=True)],
        geometry="geometry", crs=ANALYSIS_CRS,
    )
    fig, axes = plt.subplots(1, 3, figsize=(16, 6), dpi=180)
    for ax, result in zip(axes, results.itertuples()):
        case_id = result.case_id
        base_route = spatial["baseline_routes"].query("case_id == @case_id")
        scenario_route = spatial["scenario_routes"].query("case_id == @case_id")
        connector = spatial["connectors"].query("case_id == @case_id")
        origin = spatial["origins"].query("case_id == @case_id")
        bounds = base_route.total_bounds
        pad = 180
        local_network = network.cx[bounds[0]-pad:bounds[2]+pad, bounds[1]-pad:bounds[3]+pad]
        local_stations = stations.cx[bounds[0]-pad:bounds[2]+pad, bounds[1]-pad:bounds[3]+pad]
        local_network.plot(ax=ax, color="#d6d6d6", linewidth=0.5, zorder=1)
        base_route.plot(ax=ax, color="#666666", linewidth=2.2, zorder=2, label="改善前路线")
        scenario_route.plot(ax=ax, color="#1677b8", linewidth=2.6, zorder=3, label="模拟后路线")
        connector.plot(ax=ax, color="#d01c8b", linewidth=4.0, zorder=4, label="假设步行连接")
        origin.plot(ax=ax, color="#d7301f", edgecolor="white", markersize=80, zorder=5, label="住宅区代表点")
        local_stations.plot(ax=ax, color="#2166ac", edgecolor="white", marker="s", markersize=75, zorder=5, label="轨道站")
        ax.set_xlim(bounds[0]-pad, bounds[2]+pad)
        ax.set_ylim(bounds[1]-pad, bounds[3]+pad)
        ax.set_aspect("equal")
        ax.set_axis_off()
        ax.set_title(
            f"{result.residential_name}\n{result.baseline_time_min_4_5:.1f} → "
            f"{result.scenario_time_min_4_5:.1f} 分钟（模拟）",
            fontsize=11,
        )
    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(
        handles, labels, loc="upper center", bbox_to_anchor=(0.5, 0.91),
        ncol=5, frameon=True, fontsize=8,
    )
    fig.suptitle("高庙村站周边假设步行连接：改善前后最短路对比", fontsize=16, y=0.99)
    fig.text(
        0.5, 0.025,
        "连接线仅为网络筛查情景，不表示现实中存在道路，也未验证地形、产权、建筑、施工与无障碍条件。",
        ha="center", fontsize=8,
    )
    fig.tight_layout(rect=(0, 0.07, 1, 0.84))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, bbox_inches="tight", facecolor="white")
    plt.close(fig)


def save_outputs(
    project_root: Path,
    graph: nx.Graph,
    stations: gpd.GeoDataFrame,
    results: pd.DataFrame,
    sensitivity: pd.DataFrame,
    spatial: dict[str, gpd.GeoDataFrame],
) -> dict[str, Path]:
    processed = project_root / "data" / "processed"
    figures = project_root / "reports" / "figures"
    comparison_path = processed / "shapingba_improvement_simulation.csv"
    sensitivity_path = processed / "shapingba_improvement_simulation_sensitivity.csv"
    gpkg_path = processed / "shapingba_improvement_simulation.gpkg"
    figure_path = figures / "shapingba_improvement_scenarios.png"
    processed.mkdir(parents=True, exist_ok=True)
    results.to_csv(comparison_path, index=False, encoding="utf-8-sig")
    sensitivity.to_csv(sensitivity_path, index=False, encoding="utf-8-sig")
    if gpkg_path.exists():
        gpkg_path.unlink()
    for index, (layer, frame) in enumerate(spatial.items()):
        frame.to_file(gpkg_path, layer=layer, driver="GPKG", mode="w" if index == 0 else "a")
    plot_scenarios(figure_path, graph, results, spatial, stations)
    return {
        "comparison": comparison_path,
        "sensitivity": sensitivity_path,
        "geopackage": gpkg_path,
        "figure": figure_path,
    }


def run(pbf_path: Path, project_root: Path) -> dict[str, Path]:
    graph = build_walking_graph(load_walkable_lines(pbf_path))
    stations = load_verified_stations(pbf_path)
    entrances = load_station_entrances(pbf_path, stations)
    areas = load_named_residential_areas(pbf_path)
    results, sensitivity, spatial = simulate_cases(graph, areas, entrances)
    return save_outputs(project_root, graph, stations, results, sensitivity, spatial)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pbf", type=Path, default=Path("work/data_source_survey/chongqing_osm_2026-10-10.osm.pbf"))
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    for name, path in run(args.pbf.resolve(), args.project_root.resolve()).items():
        print(f"{name}: {path}")


if __name__ == "__main__":
    main()
