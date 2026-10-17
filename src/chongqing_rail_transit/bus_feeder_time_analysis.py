"""Audit directional bus stops and compute feeder break-even time budgets.

This module deliberately does not estimate bus running time.  It answers the
more limited, reproducible question: after the two walking legs, how much
waiting plus in-vehicle time can a feeder trip consume before it becomes
slower than walking directly to the nearest rail entrance?
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

import geopandas as gpd
import matplotlib.pyplot as plt
import networkx as nx
import pandas as pd

from .bus_feeder_accessibility import load_bus_platforms, load_bus_routes
from .slope_accessibility import HgtTile, build_slope_graph
from .walk_accessibility_mvp import WALK_SPEED_KMH, build_walking_graph, load_walkable_lines, nearest_graph_node


HEADWAY_SCENARIOS_MIN = (5, 10, 15, 20, 30)


def break_even_budget(direct_walk_min: float, access_walk_min: float, egress_walk_min: float) -> float:
    """Maximum combined waiting and riding time under one consistent model basis."""
    return float(direct_walk_min) - float(access_walk_min) - float(egress_walk_min)


@dataclass(frozen=True)
class Candidate:
    residence: str
    route_ref: str
    direction_relation_id: str
    direction_from: str
    direction_to: str
    boarding_platform_id: str
    boarding_platform_name: str
    alighting_platform_id: str
    alighting_platform_name: str
    rail_station: str
    boarding_stop_sequence: int
    alighting_stop_sequence: int
    stop_hops: int
    audit_result: str
    audit_note: str


CANDIDATES = (
    Candidate("凤天花园", "1429", "21469227", "万科金色悦城三期", "香榭街路口",
              "12865714996", "凤天大道", "13121349537", "高庙村西", "高庙村", 5, 8, 3,
              "directional_stop_match", "初筛站台属于所需方向关系。"),
    Candidate("卓越·美丽山水", "210", "21404246", "菜园坝火车站", "白公馆",
              "14188353589", "羊角堡", "14188353569", "小龙坎立交", "小龙坎", 10, 12, 2,
              "directional_stop_match", "初筛站台属于所需方向关系；菜园坝端施工可能影响现行运营。"),
    Candidate("凯莱阳光", "209", "21428493", "兰美路立交南", "井口工业园",
              "14102490529", "美茵河谷", "8493314656", "陈家湾", "沙坪坝", 1, 14, 13,
              "corrected_platform", "初筛站台不在关系成员中，改用同名、属于所需方向的站台。"),
    Candidate("升伟·新时空", "209", "21428493", "兰美路立交南", "井口工业园",
              "11516814993", "西南医院", "8493314656", "陈家湾", "沙坪坝", 8, 14, 6,
              "screen_false_positive_alternative_stop", "凤天大道北段不是209路关系成员；改用最近的方向有效站西南医院。"),
    Candidate("周李小区", "209", "21428493", "兰美路立交南", "井口工业园",
              "11516814993", "西南医院", "8493314656", "陈家湾", "沙坪坝", 8, 14, 6,
              "screen_false_positive_alternative_stop", "凤天大道北段不是209路关系成员；改用最近的方向有效站西南医院。"),
)


def network_distance_between_points(graph: nx.Graph, origin, destination) -> float:
    origin_node, origin_snap_m = nearest_graph_node(graph, origin)
    destination_node, destination_snap_m = nearest_graph_node(graph, destination)
    return float(
        origin_snap_m
        + nx.shortest_path_length(graph, origin_node, destination_node, weight="length_m")
        + destination_snap_m
    )


def directional_slope_route(
    graph: nx.DiGraph,
    origin,
    destinations: gpd.GeoDataFrame,
) -> dict[str, object]:
    """Return the fastest directional terrain-model route to any destination.

    Network-edge time uses the slope model. Point-to-network snap legs retain
    the declared level-ground speed because the DEM cannot resolve their route.
    """
    origin_node, origin_snap_m = nearest_graph_node(graph, origin)
    times, paths = nx.single_source_dijkstra(graph, origin_node, weight="time_min")
    level_m_min = WALK_SPEED_KMH * 1000.0 / 60.0
    candidates: list[tuple[float, str, float, tuple[float, float]]] = []
    for destination in destinations.itertuples():
        node, snap_m = nearest_graph_node(graph, destination.geometry)
        if node in times:
            total_time = float(times[node]) + (origin_snap_m + snap_m) / level_m_min
            candidates.append((total_time, str(destination.osm_id), snap_m, node))
    if not candidates:
        raise nx.NetworkXNoPath("No destination can be reached from origin")
    total_time, osm_id, destination_snap_m, destination_node = min(candidates)
    path = paths[destination_node]
    distance_m = origin_snap_m + destination_snap_m
    for u, v in zip(path[:-1], path[1:]):
        distance_m += float(graph[u][v]["length_m"])
    return {
        "time_min": total_time,
        "distance_m": float(distance_m),
        "destination_osm_id": osm_id,
        "origin_snap_m": float(origin_snap_m),
        "destination_snap_m": float(destination_snap_m),
    }


def build_break_even_table(
    graph: nx.Graph,
    slope_graph: nx.DiGraph,
    residences: gpd.GeoDataFrame,
    platforms: gpd.GeoDataFrame,
    rail_entrances: gpd.GeoDataFrame,
    residential_results: pd.DataFrame,
    slope_results: pd.DataFrame,
    platform_results: pd.DataFrame,
) -> pd.DataFrame:
    flat = residential_results.set_index("residential_name")
    slope = slope_results[slope_results["grade_scale"].eq(1.0)].set_index("residential_name")
    platform_metrics = platform_results.set_index(platform_results["osm_id"].astype(str))
    residence_points = residences.set_index("residential_name")
    platform_points = platforms.set_index(platforms["osm_id"].astype(str))
    rows: list[dict[str, object]] = []

    for candidate in CANDIDATES:
        origin = residence_points.loc[candidate.residence, "geometry"]
        boarding = platform_points.loc[candidate.boarding_platform_id, "geometry"]
        access_m = network_distance_between_points(graph, origin, boarding)
        slope_access = directional_slope_route(
            slope_graph,
            origin,
            platforms[platforms["osm_id"].astype(str).eq(candidate.boarding_platform_id)],
        )
        egress = platform_metrics.loc[candidate.alighting_platform_id]
        if str(egress["nearest_rail_station"]) != candidate.rail_station:
            raise ValueError(
                f"{candidate.alighting_platform_name} maps to {egress['nearest_rail_station']}, "
                f"not declared station {candidate.rail_station}"
            )
        alighting = platform_points.loc[candidate.alighting_platform_id, "geometry"]
        station_entrances = rail_entrances[
            rail_entrances["station_name"].eq(candidate.rail_station)
        ]
        if station_entrances.empty:
            raise ValueError(f"No rail entrances found for {candidate.rail_station}")
        slope_egress = directional_slope_route(slope_graph, alighting, station_entrances)
        access_min = access_m / (WALK_SPEED_KMH * 1000 / 60)
        egress_min = float(egress["walk_to_rail_min_4_5"])
        direct_flat = float(flat.loc[candidate.residence, "walk_time_min_4_5"])
        direct_slope = float(slope.loc[candidate.residence, "time_min"])
        fixed_walk_legs = access_min + egress_min
        slope_walk_legs = float(slope_access["time_min"]) + float(slope_egress["time_min"])
        rows.append({
            "residential_name": candidate.residence,
            "route_ref": candidate.route_ref,
            "direction_relation_id": candidate.direction_relation_id,
            "direction_from": candidate.direction_from,
            "direction_to": candidate.direction_to,
            "boarding_platform_osm_id": candidate.boarding_platform_id,
            "boarding_platform_name": candidate.boarding_platform_name,
            "alighting_platform_osm_id": candidate.alighting_platform_id,
            "alighting_platform_name": candidate.alighting_platform_name,
            "rail_station": candidate.rail_station,
            "boarding_stop_sequence": candidate.boarding_stop_sequence,
            "alighting_stop_sequence": candidate.alighting_stop_sequence,
            "stop_hops": candidate.stop_hops,
            "audit_result": candidate.audit_result,
            "audit_note": candidate.audit_note,
            "walk_to_boarding_m": access_m,
            "walk_to_boarding_min_4_5": access_min,
            "walk_to_boarding_m_slope_route": slope_access["distance_m"],
            "walk_to_boarding_min_slope_model": slope_access["time_min"],
            "walk_alighting_to_rail_m": float(egress["network_walk_to_rail_m"]),
            "walk_alighting_to_rail_min_4_5": egress_min,
            "walk_alighting_to_rail_m_slope_route": slope_egress["distance_m"],
            "walk_alighting_to_rail_min_slope_model": slope_egress["time_min"],
            "slope_model_rail_entrance_osm_id": slope_egress["destination_osm_id"],
            "two_walk_legs_min_4_5": fixed_walk_legs,
            "two_walk_legs_min_slope_model": slope_walk_legs,
            "direct_walk_min_flat_4_5": direct_flat,
            "direct_walk_min_slope_model": direct_slope,
            "max_wait_plus_ride_min_vs_flat": break_even_budget(direct_flat, access_min, egress_min),
            "max_wait_plus_ride_min_vs_slope": break_even_budget(
                direct_slope, slope_access["time_min"], slope_egress["time_min"]
            ),
            "actual_wait_min": pd.NA,
            "actual_in_vehicle_min": pd.NA,
            "actual_total_feeder_min": pd.NA,
            "actual_time_comparison_status": "not_computable_without_observed_or_operator_time_data",
        })
    return pd.DataFrame(rows)


def build_survey_priority(break_even: pd.DataFrame) -> pd.DataFrame:
    """Create a field-investigation order, not a ranking of service quality."""
    details = {
        "凤天花园": (1, "高", "班距、分方向候车、分站车内时间、运营时段", "方向站序明确且时间余量较大，先采集可最快检验公交是否可能节时。"),
        "卓越·美丽山水": (2, "高", "210路现行线路状态、施工期站点、班距、车内时间、运营时段", "理论余量大，但菜园坝施工期运营状态是决定性前置条件。"),
        "凯莱阳光": (3, "中", "班距、13站车内时间、运营时段、换乘端步行", "方向站台已纠正，但站数多，需先核实车内时间能否低于上限。"),
        "升伟·新时空": (4, "中", "到西南医院站的现场步行、班距、6站车内时间、运营时段", "原空间初筛站点为误报，替代上车站接驳步行较长。"),
        "周李小区": (5, "低", "到西南医院站的现场步行、班距、6站车内时间、运营时段", "模型盈亏平衡余量最小，先验上最难节时，但仍需现场数据反证。"),
    }
    rows: list[dict[str, object]] = []
    indexed = break_even.set_index("residential_name")
    for residence, (rank, label, unknowns, reason) in details.items():
        row = indexed.loc[residence]
        rows.append({
            "survey_priority_rank": rank,
            "survey_priority": label,
            "residential_name": residence,
            "route_ref": row["route_ref"],
            "boarding_platform_name": row["boarding_platform_name"],
            "alighting_platform_name": row["alighting_platform_name"],
            "rail_station": row["rail_station"],
            "walk_to_boarding_min_4_5": row["walk_to_boarding_min_4_5"],
            "walk_to_boarding_min_slope_model": row["walk_to_boarding_min_slope_model"],
            "max_wait_plus_ride_min_vs_flat": row["max_wait_plus_ride_min_vs_flat"],
            "max_wait_plus_ride_min_vs_slope": row["max_wait_plus_ride_min_vs_slope"],
            "key_unknown_data": unknowns,
            "priority_reason": reason,
            "interpretation": "调查顺序，不代表公交已经更快或服务质量排名",
        })
    return pd.DataFrame(rows).sort_values("survey_priority_rank")


def build_wait_sensitivity(break_even: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for row in break_even.itertuples():
        for headway in HEADWAY_SCENARIOS_MIN:
            expected_wait = headway / 2
            for benchmark, budget in (
                ("flat_4_5_kmh", row.max_wait_plus_ride_min_vs_flat),
                ("slope_model", row.max_wait_plus_ride_min_vs_slope),
            ):
                maximum_ride = float(budget) - expected_wait
                rows.append({
                    "residential_name": row.residential_name,
                    "route_ref": row.route_ref,
                    "direct_walk_benchmark": benchmark,
                    "assumed_headway_min": headway,
                    "assumed_expected_wait_min": expected_wait,
                    "maximum_in_vehicle_min_to_save_time": maximum_ride,
                    "feasible_if_ride_time_nonnegative": maximum_ride >= 0,
                    "assumption": "parameter_only_random_arrival_expected_wait_equals_half_headway",
                })
    return pd.DataFrame(rows)


def route_stop_audit_table() -> pd.DataFrame:
    return pd.DataFrame([{
        "residential_name": c.residence,
        "route_ref": c.route_ref,
        "direction_relation_id": c.direction_relation_id,
        "direction": f"{c.direction_from} → {c.direction_to}",
        "boarding_platform_osm_id": c.boarding_platform_id,
        "boarding_platform_name": c.boarding_platform_name,
        "alighting_platform_osm_id": c.alighting_platform_id,
        "alighting_platform_name": c.alighting_platform_name,
        "rail_station": c.rail_station,
        "boarding_stop_sequence": c.boarding_stop_sequence,
        "alighting_stop_sequence": c.alighting_stop_sequence,
        "stop_hops": c.stop_hops,
        "audit_result": c.audit_result,
        "audit_note": c.audit_note,
        "verification_method": "manual_osm_relation_member_and_sequence_audit_recorded_as_constants",
        "automatic_relation_member_validation": False,
        "operation_verification": "route_geometry_and_direction_only_not_operator_confirmation",
    } for c in CANDIDATES])


def plot_results(path: Path, routes: gpd.GeoDataFrame, residences: gpd.GeoDataFrame,
                 platforms: gpd.GeoDataFrame, break_even: pd.DataFrame) -> None:
    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
    fig, axes = plt.subplots(1, 2, figsize=(14, 7), dpi=180)
    route_ids = {c.direction_relation_id for c in CANDIDATES}
    selected_routes = routes[routes["osm_id"].astype(str).isin(route_ids)]
    colours = {"209": "#3182bd", "210": "#756bb1", "1429": "#e6550d"}
    for ref, group in selected_routes.groupby("route_ref"):
        group.plot(ax=axes[0], color=colours.get(str(ref), "#636363"), linewidth=2.2, label=f"{ref}路")
    names = [c.residence for c in CANDIDATES]
    residences[residences["residential_name"].isin(names)].plot(
        ax=axes[0], color="#cb181d", edgecolor="white", markersize=65, zorder=4, label="住宅代表点"
    )
    platform_ids = {c.boarding_platform_id for c in CANDIDATES}
    alight_ids = {c.alighting_platform_id for c in CANDIDATES}
    platforms[platforms["osm_id"].astype(str).isin(platform_ids)].plot(
        ax=axes[0], color="#31a354", marker="^", markersize=70, zorder=5, label="方向有效上车站"
    )
    platforms[platforms["osm_id"].astype(str).isin(alight_ids)].plot(
        ax=axes[0], color="#08519c", marker="s", markersize=55, zorder=5, label="轨道端下车站"
    )
    focus = pd.concat([
        residences[residences["residential_name"].isin(names)].geometry,
        platforms[platforms["osm_id"].astype(str).isin(platform_ids | alight_ids)].geometry,
    ])
    xmin, ymin, xmax, ymax = gpd.GeoSeries(focus, crs=residences.crs).total_bounds
    pad = 700
    axes[0].set_xlim(xmin - pad, xmax + pad)
    axes[0].set_ylim(ymin - pad, ymax + pad)
    axes[0].set_title("候选线路、方向有效站台与住宅代表点")
    axes[0].set_axis_off()
    axes[0].set_aspect("equal")
    axes[0].legend(loc="lower left", fontsize=8)

    chart = break_even.sort_values("max_wait_plus_ride_min_vs_slope")
    y = range(len(chart))
    axes[1].barh(y, chart["max_wait_plus_ride_min_vs_slope"], color="#2ca25f", label="相对坡度步行")
    axes[1].scatter(chart["max_wait_plus_ride_min_vs_flat"], y, color="#de2d26", zorder=3, label="相对固定步速")
    axes[1].set_yticks(list(y), chart["residential_name"])
    axes[1].set_xlabel("候车 + 车内时间的盈亏平衡上限（分钟）")
    axes[1].set_title("公交要节时，候车与车内时间总和必须低于此值")
    axes[1].axvline(0, color="#555555", linewidth=0.8)
    axes[1].grid(axis="x", alpha=0.25)
    axes[1].legend(fontsize=8)
    fig.suptitle("沙坪坝公交接驳时间：方向核验与盈亏平衡分析", fontsize=16)
    fig.text(0.5, 0.015,
             "线路与站台来自OSM 2026-10-09快照；时间上限不是实际公交用时。固定与坡度基准分别使用同口径接驳步行腿；未加入候车或车内观测值。© OpenStreetMap contributors。",
             ha="center", fontsize=8)
    fig.tight_layout(rect=(0, 0.055, 1, 0.94))
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, bbox_inches="tight", facecolor="white")
    plt.close(fig)


def run(project_root: Path, pbf_path: Path, hgt_path: Path) -> dict[str, object]:
    walk_lines = load_walkable_lines(pbf_path)
    graph = build_walking_graph(walk_lines)
    slope_graph = build_slope_graph(graph, HgtTile.from_gzip(hgt_path))
    platforms = load_bus_platforms(pbf_path)
    routes = load_bus_routes(pbf_path)
    residences = gpd.read_file(
        project_root / "data/processed/shapingba_bus_rail_feeder_screen.gpkg",
        layer="residential_screen",
    )
    rail_entrances = gpd.read_file(
        project_root / "data/processed/shapingba_bus_rail_feeder_screen.gpkg",
        layer="rail_entrances",
    )
    residential_results = pd.read_csv(project_root / "data/processed/shapingba_residential_accessibility.csv")
    slope_results = pd.read_csv(project_root / "data/processed/shapingba_slope_adjusted_residential.csv")
    platform_results = pd.read_csv(project_root / "data/processed/shapingba_bus_platform_rail_access.csv")
    break_even = build_break_even_table(
        graph, slope_graph, residences, platforms, rail_entrances,
        residential_results, slope_results, platform_results
    )
    sensitivity = build_wait_sensitivity(break_even)
    audit = route_stop_audit_table()
    priority = build_survey_priority(break_even)

    processed = project_root / "data/processed"
    break_even.to_csv(processed / "shapingba_bus_feeder_break_even.csv", index=False, encoding="utf-8-sig")
    sensitivity.to_csv(processed / "shapingba_bus_feeder_wait_sensitivity.csv", index=False, encoding="utf-8-sig")
    audit.to_csv(processed / "shapingba_bus_feeder_route_verification.csv", index=False, encoding="utf-8-sig")
    priority.to_csv(processed / "shapingba_bus_feeder_survey_priority.csv", index=False, encoding="utf-8-sig")

    selected_names = [c.residence for c in CANDIDATES]
    selected_platform_ids = {c.boarding_platform_id for c in CANDIDATES} | {c.alighting_platform_id for c in CANDIDATES}
    selected_route_ids = {c.direction_relation_id for c in CANDIDATES}
    gpkg = processed / "shapingba_bus_feeder_time_candidates.gpkg"
    if gpkg.exists():
        gpkg.unlink()
    residences[residences["residential_name"].isin(selected_names)].to_file(gpkg, layer="residences", driver="GPKG")
    platforms[platforms["osm_id"].astype(str).isin(selected_platform_ids)].to_file(gpkg, layer="bus_platforms", driver="GPKG")
    routes[routes["osm_id"].astype(str).isin(selected_route_ids)].to_file(gpkg, layer="directional_routes", driver="GPKG")
    rail_entrances[rail_entrances["station_name"].isin({c.rail_station for c in CANDIDATES})].to_file(
        gpkg, layer="rail_entrances", driver="GPKG"
    )
    plot_results(
        project_root / "reports/figures/shapingba_bus_feeder_time_candidates.png",
        routes, residences, platforms, break_even,
    )
    return {
        "candidates": len(break_even),
        "directional_exact_matches": int(break_even["audit_result"].eq("directional_stop_match").sum()),
        "corrected_or_false_positive_candidates": int((~break_even["audit_result"].eq("directional_stop_match")).sum()),
        "actual_total_times_computed": int(break_even["actual_total_feeder_min"].notna().sum()),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path("."))
    parser.add_argument("--pbf", type=Path,
                        default=Path("work/data_source_survey/chongqing_osm_2026-10-10.osm.pbf"))
    parser.add_argument("--hgt", type=Path,
                        default=Path("work/data_source_survey/N29E106.hgt.gz"))
    args = parser.parse_args()
    print(run(args.project_root.resolve(), args.pbf.resolve(), args.hgt.resolve()))


if __name__ == "__main__":
    main()
