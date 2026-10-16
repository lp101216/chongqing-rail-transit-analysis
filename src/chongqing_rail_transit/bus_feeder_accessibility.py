"""Screen bus-to-rail spatial connections in the Shapingba study area.

The output measures mapped platform locations and walking-network distances.
OSM route geometries are used only to flag possible direct feeder relations;
they do not provide timetables, frequencies, fares, reliability or ridership.
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
    parse_other_tags,
)


BUS_ROUTE_MATCH_M = 30.0
RAIL_TRANSFER_WALK_M = 300.0
RESIDENTIAL_BUS_WALK_M = 375.0  # five minutes at the fixed 4.5 km/h comparison speed
RAIL_TRANSFER_SENSITIVITY_M = (200.0, 300.0, 500.0)
RESIDENTIAL_BUS_SENSITIVITY_M = (300.0, 375.0, 500.0)


def load_bus_platforms(pbf_path: Path) -> gpd.GeoDataFrame:
    points = pyogrio.read_dataframe(
        pbf_path,
        layer="points",
        bbox=TEST_BBOX,
        columns=["osm_id", "name", "highway", "other_tags"],
    )
    tags = points["other_tags"].map(parse_other_tags)
    mask = (
        points["highway"].eq("bus_stop")
        & tags.map(lambda value: value.get("bus") == "yes")
        & tags.map(lambda value: value.get("public_transport") == "platform")
    )
    platforms = points[mask].copy()
    if platforms.empty:
        raise RuntimeError("No OSM bus platforms were found in the study area")
    platforms.set_crs(SOURCE_CRS, inplace=True, allow_override=True)
    platforms = platforms.to_crs(ANALYSIS_CRS)
    platforms["osm_id"] = platforms["osm_id"].astype(str)
    platforms["platform_name"] = platforms["name"].fillna("未命名站台")
    platforms["source_snapshot"] = f"OpenStreetMap {OSM_SNAPSHOT_DATE}"
    return platforms[["osm_id", "platform_name", "source_snapshot", "geometry"]].reset_index(drop=True)


def load_bus_routes(pbf_path: Path) -> gpd.GeoDataFrame:
    relations = pyogrio.read_dataframe(
        pbf_path,
        layer="multilinestrings",
        bbox=TEST_BBOX,
        columns=["osm_id", "name", "type", "other_tags"],
    )
    tags = relations["other_tags"].map(parse_other_tags)
    mask = tags.map(lambda value: value.get("route") == "bus")
    routes = relations[mask].copy()
    routes["route_ref"] = tags.loc[routes.index].map(lambda value: value.get("ref"))
    routes["route_from"] = tags.loc[routes.index].map(lambda value: value.get("from"))
    routes["route_to"] = tags.loc[routes.index].map(lambda value: value.get("to"))
    routes = routes[routes["route_ref"].notna()].copy()
    routes.set_crs(SOURCE_CRS, inplace=True, allow_override=True)
    return routes.to_crs(ANALYSIS_CRS)[
        ["osm_id", "route_ref", "route_from", "route_to", "geometry"]
    ].reset_index(drop=True)


def _point_assignments(
    graph: nx.Graph, points: gpd.GeoDataFrame, maximum_snap_m: float = 150.0
) -> list[tuple[int, tuple[float, float], float]]:
    assignments = []
    for index, geometry in zip(points.index, points.geometry):
        node, snap_m = nearest_graph_node(graph, geometry)
        if snap_m <= maximum_snap_m:
            assignments.append((index, node, snap_m))
    return assignments


def _augmented_distances(
    graph: nx.Graph,
    assignments: list[tuple[int, tuple[float, float], float]],
) -> tuple[dict[object, float], dict[object, list[object]], dict[tuple[float, float], int]]:
    augmented = graph.copy()
    source = "__analysis_super_source__"
    source_for_node: dict[tuple[float, float], int] = {}
    source_snap: dict[tuple[float, float], float] = {}
    for index, node, snap_m in assignments:
        if node not in source_snap or snap_m < source_snap[node]:
            source_snap[node] = snap_m
            source_for_node[node] = index
    for node, snap_m in source_snap.items():
        augmented.add_edge(source, node, length_m=snap_m)
    distances, paths = nx.single_source_dijkstra(augmented, source, weight="length_m")
    return distances, paths, source_for_node


def associate_routes(platforms: gpd.GeoDataFrame, routes: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    result = platforms.copy()
    route_refs: list[str] = []
    route_counts: list[int] = []
    minimum_distances: list[float] = []
    for geometry in result.geometry:
        distances = routes.geometry.distance(geometry)
        matched = routes.loc[distances.le(BUS_ROUTE_MATCH_M), "route_ref"].astype(str)
        refs = sorted(set(matched))
        route_refs.append(";".join(refs))
        route_counts.append(len(refs))
        minimum_distances.append(float(distances.min()) if len(distances) else np.nan)
    result["mapped_route_refs_30m"] = route_refs
    result["mapped_route_ref_count_30m"] = route_counts
    result["nearest_route_geometry_m"] = minimum_distances
    return result


def analyse_station_transfers(
    graph: nx.Graph,
    platforms: gpd.GeoDataFrame,
    entrances: gpd.GeoDataFrame,
) -> tuple[gpd.GeoDataFrame, pd.DataFrame]:
    entrance_assignments = _point_assignments(graph, entrances, maximum_snap_m=100.0)
    distances, paths, source_for_node = _augmented_distances(graph, entrance_assignments)
    records: list[dict[str, object]] = []
    for index, node, platform_snap_m in _point_assignments(graph, platforms):
        path = paths.get(node)
        if not path or len(path) < 2:
            continue
        entrance_node = path[1]
        entrance_index = source_for_node[entrance_node]
        entrance = entrances.loc[entrance_index]
        platform = platforms.loc[index]
        walk_distance_m = float(distances[node] + platform_snap_m)
        record = platform.drop(labels="geometry").to_dict()
        record.update({
            "nearest_rail_station": entrance["station_name"],
            "nearest_rail_entrance_osm_id": str(entrance["osm_id"]),
            "platform_snap_m": platform_snap_m,
            "network_walk_to_rail_m": walk_distance_m,
            "walk_to_rail_min_4_5": walk_distance_m / (WALK_SPEED_KMH * 1000 / 60),
            "within_200m_rail_walk": walk_distance_m <= 200,
            "within_300m_rail_walk": walk_distance_m <= 300,
            "within_500m_rail_walk": walk_distance_m <= 500,
            "geometry": platform.geometry,
        })
        records.append(record)
    analysed = gpd.GeoDataFrame(records, geometry="geometry", crs=ANALYSIS_CRS)

    summary_rows: list[dict[str, object]] = []
    for station_name, group in analysed.groupby("nearest_rail_station"):
        near = group[group["within_300m_rail_walk"]]
        refs = sorted({ref for value in near["mapped_route_refs_30m"] for ref in str(value).split(";") if ref})
        summary_rows.append({
            "rail_station": station_name,
            "nearest_assigned_bus_platforms": len(group),
            "platforms_within_200m_walk": int(group["within_200m_rail_walk"].sum()),
            "platforms_within_300m_walk": int(group["within_300m_rail_walk"].sum()),
            "platforms_within_500m_walk": int(group["within_500m_rail_walk"].sum()),
            "unique_stop_names_within_300m": near["platform_name"].nunique(),
            "mapped_route_refs_within_300m": ";".join(refs),
            "mapped_route_ref_count_within_300m": len(refs),
            "median_platform_walk_to_rail_m": group["network_walk_to_rail_m"].median(),
        })
    return analysed, pd.DataFrame(summary_rows).sort_values("rail_station").reset_index(drop=True)


def _split_refs(value: object) -> set[str]:
    return {part for part in str(value).split(";") if part and part != "nan"}


def analyse_residential_bus_access(
    graph: nx.Graph,
    residences: gpd.GeoDataFrame,
    platforms: gpd.GeoDataFrame,
) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
    platform_assignments = _point_assignments(graph, platforms)
    distances, paths, source_for_node = _augmented_distances(graph, platform_assignments)
    transfer_refs: dict[str, set[str]] = {}
    for row in platforms[platforms["within_300m_rail_walk"]].itertuples():
        for ref in _split_refs(row.mapped_route_refs_30m):
            transfer_refs.setdefault(ref, set()).add(row.nearest_rail_station)

    records: list[dict[str, object]] = []
    route_records: list[dict[str, object]] = []
    for residence in residences.itertuples():
        origin_node, origin_snap_m = nearest_graph_node(graph, residence.geometry)
        path = paths.get(origin_node)
        if not path or len(path) < 2 or origin_snap_m > 150:
            continue
        platform_node = path[1]
        platform_index = source_for_node[platform_node]
        platform = platforms.loc[platform_index]
        bus_walk_m = float(distances[origin_node] + origin_snap_m)
        origin_refs = _split_refs(platform["mapped_route_refs_30m"])
        candidate_refs = sorted(origin_refs & set(transfer_refs))
        candidate_stations = sorted({station for ref in candidate_refs for station in transfer_refs[ref]})
        slope_time = float(residence.slope_time_to_rail_min)
        if slope_time <= 15:
            screening = "rail_walk_within_15_min"
        elif bus_walk_m > RESIDENTIAL_BUS_WALK_M:
            screening = "bus_platform_over_5_min_walk"
        elif candidate_refs:
            screening = "spatial_direct_feeder_candidate"
        else:
            screening = "nearby_platform_no_mapped_direct_route"
        records.append({
            "osm_ref": residence.osm_ref,
            "residential_name": residence.residential_name,
            "slope_time_to_rail_min": slope_time,
            "nearest_model_rail_station": residence.station_name,
            "nearest_bus_platform_osm_id": platform["osm_id"],
            "nearest_bus_platform_name": platform["platform_name"],
            "walk_to_bus_platform_m": bus_walk_m,
            "walk_to_bus_platform_min_4_5": bus_walk_m / (WALK_SPEED_KMH * 1000 / 60),
            "nearest_platform_mapped_route_refs": platform["mapped_route_refs_30m"],
            "candidate_direct_feeder_route_refs": ";".join(candidate_refs),
            "candidate_feeder_rail_stations": ";".join(candidate_stations),
            "screening_result": screening,
            "interpretation_limit": "spatial_screen_only_no_schedule_or_ride_time",
            "geometry": residence.geometry,
        })
        coordinate_path = [
            (platform.geometry.x, platform.geometry.y),
            *[node for node in path[1:] if isinstance(node, tuple)],
            (residence.geometry.x, residence.geometry.y),
        ]
        coordinate_path = [
            point for position, point in enumerate(coordinate_path)
            if position == 0 or point != coordinate_path[position - 1]
        ]
        if len(coordinate_path) >= 2:
            route_records.append({
                "osm_ref": residence.osm_ref,
                "residential_name": residence.residential_name,
                "nearest_bus_platform_name": platform["platform_name"],
                "walk_to_bus_platform_m": bus_walk_m,
                "geometry": LineString(coordinate_path),
            })
    return (
        gpd.GeoDataFrame(records, geometry="geometry", crs=ANALYSIS_CRS),
        gpd.GeoDataFrame(route_records, geometry="geometry", crs=ANALYSIS_CRS),
    )


def load_residential_inputs(project_root: Path) -> gpd.GeoDataFrame:
    origins = gpd.read_file(
        project_root / "data/processed/shapingba_residential_accessibility.gpkg",
        layer="residential_origins",
    )
    slope = pd.read_csv(project_root / "data/processed/shapingba_slope_adjusted_residential.csv")
    slope = slope[slope["grade_scale"].eq(1.0)][
        ["osm_ref", "residential_name", "station_name", "time_min"]
    ].rename(columns={"time_min": "slope_time_to_rail_min"})
    valid = origins[origins["network_reachable"]].copy()
    return valid[["osm_ref", "geometry"]].merge(slope, on="osm_ref", how="inner")


def build_threshold_sensitivity(
    platforms: gpd.GeoDataFrame,
    residences: gpd.GeoDataFrame,
) -> pd.DataFrame:
    poor = residences[residences["slope_time_to_rail_min"] > 15]
    rows: list[dict[str, object]] = []
    for rail_threshold_m in RAIL_TRANSFER_SENSITIVITY_M:
        transfer_refs = {
            ref
            for value in platforms.loc[
                platforms["network_walk_to_rail_m"] <= rail_threshold_m,
                "mapped_route_refs_30m",
            ]
            for ref in _split_refs(value)
        }
        for bus_threshold_m in RESIDENTIAL_BUS_SENSITIVITY_M:
            bus_near = poor["walk_to_bus_platform_m"] <= bus_threshold_m
            direct = poor["nearest_platform_mapped_route_refs"].map(
                lambda value: bool(_split_refs(value) & transfer_refs)
            ) & bus_near
            rows.append({
                "rail_transfer_walk_threshold_m": rail_threshold_m,
                "residential_bus_walk_threshold_m": bus_threshold_m,
                "poor_residential_count": len(poor),
                "bus_platform_over_threshold_count": int((~bus_near).sum()),
                "spatial_direct_feeder_candidate_count": int(direct.sum()),
                "nearby_platform_no_mapped_direct_route_count": int((bus_near & ~direct).sum()),
            })
    return pd.DataFrame(rows)


def plot_results(
    output_path: Path,
    walk_lines: gpd.GeoDataFrame,
    stations: gpd.GeoDataFrame,
    entrances: gpd.GeoDataFrame,
    platforms: gpd.GeoDataFrame,
    residences: gpd.GeoDataFrame,
    access_routes: gpd.GeoDataFrame,
) -> None:
    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
    fig, axes = plt.subplots(1, 2, figsize=(14, 7), dpi=180)
    for ax in axes:
        walk_lines.plot(ax=ax, color="#dedede", linewidth=0.35, zorder=1)
        stations.plot(ax=ax, color="#253494", marker="s", edgecolor="white", markersize=60, zorder=5)
        entrances.plot(ax=ax, color="#2c7fb8", marker="^", markersize=28, zorder=5)
        ax.set_axis_off()
        ax.set_aspect("equal")

    far = platforms[~platforms["within_300m_rail_walk"]]
    near = platforms[platforms["within_300m_rail_walk"]]
    far.plot(ax=axes[0], color="#bdbdbd", markersize=10, zorder=2, label="其他公交站台")
    near.plot(ax=axes[0], color="#f03b20", markersize=25, zorder=4, label="步行网络≤300米")
    axes[0].set_title("公交站台到最近轨道站出入口\n（空间距离，不代表换乘时间）")
    axes[0].legend(loc="lower left", fontsize=8)

    poor = residences[residences["slope_time_to_rail_min"] > 15].copy()
    access_routes[access_routes["osm_ref"].isin(poor["osm_ref"])].plot(
        ax=axes[1], color="#969696", linewidth=1.0, alpha=0.8, zorder=2
    )
    colours = {
        "spatial_direct_feeder_candidate": "#238b45",
        "nearby_platform_no_mapped_direct_route": "#fe9929",
        "bus_platform_over_5_min_walk": "#cb181d",
    }
    labels = {
        "spatial_direct_feeder_candidate": "空间直达候选",
        "nearby_platform_no_mapped_direct_route": "近站台但未匹配直达线路",
        "bus_platform_over_5_min_walk": "最近站台也超过5分钟",
    }
    for category, colour in colours.items():
        subset = poor[poor["screening_result"].eq(category)]
        if not subset.empty:
            subset.plot(ax=axes[1], color=colour, edgecolor="white", markersize=48, zorder=4, label=labels[category])
    platforms.plot(ax=axes[1], color="#3182bd", markersize=7, alpha=0.55, zorder=3, label="公交站台")
    axes[1].set_title("坡度模型>15分钟住宅区到最近公交站台\n（候选线路不含班次与车内时间）")
    axes[1].legend(loc="lower left", fontsize=7)
    fig.suptitle("沙坪坝—高庙村：公交—轨道空间接驳筛查", fontsize=16)
    fig.text(
        0.5, 0.02,
        "OSM快照2026-10-09；4.5 km/h仅换算到公交站台的步行时间。候选直达关系由站台与路线几何30米匹配得到，不是运营验证。© OpenStreetMap contributors。",
        ha="center", fontsize=8,
    )
    fig.tight_layout(rect=(0, 0.055, 1, 0.94))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, bbox_inches="tight", facecolor="white")
    plt.close(fig)


def save_outputs(
    project_root: Path,
    walk_lines: gpd.GeoDataFrame,
    stations: gpd.GeoDataFrame,
    entrances: gpd.GeoDataFrame,
    platforms: gpd.GeoDataFrame,
    station_summary: pd.DataFrame,
    residences: gpd.GeoDataFrame,
    access_routes: gpd.GeoDataFrame,
    threshold_sensitivity: pd.DataFrame,
) -> None:
    processed = project_root / "data/processed"
    figure = project_root / "reports/figures/shapingba_bus_rail_feeder_screen.png"
    platforms.drop(columns="geometry").to_csv(
        processed / "shapingba_bus_platform_rail_access.csv", index=False, encoding="utf-8-sig"
    )
    station_summary.to_csv(
        processed / "shapingba_bus_rail_station_summary.csv", index=False, encoding="utf-8-sig"
    )
    residences.drop(columns="geometry").to_csv(
        processed / "shapingba_residential_bus_feeder_screen.csv", index=False, encoding="utf-8-sig"
    )
    threshold_sensitivity.to_csv(
        processed / "shapingba_bus_feeder_threshold_sensitivity.csv", index=False, encoding="utf-8-sig"
    )
    gpkg = processed / "shapingba_bus_rail_feeder_screen.gpkg"
    if gpkg.exists():
        gpkg.unlink()
    platforms.to_file(gpkg, layer="bus_platforms", driver="GPKG")
    stations.to_file(gpkg, layer="rail_stations", driver="GPKG")
    entrances.to_file(gpkg, layer="rail_entrances", driver="GPKG")
    residences.to_file(gpkg, layer="residential_screen", driver="GPKG")
    access_routes.to_file(gpkg, layer="walk_routes_to_bus", driver="GPKG")
    plot_results(figure, walk_lines, stations, entrances, platforms, residences, access_routes)


def run(project_root: Path, pbf_path: Path) -> dict[str, object]:
    walk_lines = load_walkable_lines(pbf_path)
    graph = build_walking_graph(walk_lines)
    stations = load_verified_stations(pbf_path)
    entrances = load_station_entrances(pbf_path, stations)
    bus_routes = load_bus_routes(pbf_path)
    platforms = associate_routes(load_bus_platforms(pbf_path), bus_routes)
    platforms, station_summary = analyse_station_transfers(graph, platforms, entrances)
    residential_inputs = load_residential_inputs(project_root)
    residences, access_routes = analyse_residential_bus_access(graph, residential_inputs, platforms)
    threshold_sensitivity = build_threshold_sensitivity(platforms, residences)
    save_outputs(
        project_root, walk_lines, stations, entrances, platforms,
        station_summary, residences, access_routes, threshold_sensitivity,
    )
    return {
        "bus_platforms": len(platforms),
        "unique_bus_stop_names": platforms["platform_name"].nunique(),
        "directional_bus_route_relations": len(bus_routes),
        "unique_bus_route_refs": bus_routes["route_ref"].nunique(),
        "platforms_within_300m_rail_walk": int(platforms["within_300m_rail_walk"].sum()),
        "poor_residences": int((residences["slope_time_to_rail_min"] > 15).sum()),
        "screening_counts": residences.loc[
            residences["slope_time_to_rail_min"] > 15, "screening_result"
        ].value_counts().to_dict(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path("."))
    parser.add_argument(
        "--pbf", type=Path,
        default=Path("work/data_source_survey/chongqing_osm_2026-10-10.osm.pbf"),
    )
    args = parser.parse_args()
    print(run(args.project_root.resolve(), args.pbf.resolve()))


if __name__ == "__main__":
    main()
