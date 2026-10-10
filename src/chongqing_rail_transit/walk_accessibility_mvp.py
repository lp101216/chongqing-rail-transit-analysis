"""Small-area walking accessibility MVP for operational CRT stations.

Reads a local OSM PBF, builds a walking graph, and calculates 10/15-minute
reachable road networks. It deliberately does not calculate population coverage.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import geopandas as gpd
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.patches import Patch
import networkx as nx
import numpy as np
import pandas as pd
import pyogrio
from shapely.geometry import LineString, Point


ANALYSIS_CRS = "EPSG:32648"
SOURCE_CRS = "EPSG:4326"
OSM_SNAPSHOT_DATE = "2026-10-09"
OFFICIAL_NETWORK_DATE = "2024-12"
WALK_SPEED_KMH = 4.5
TIME_THRESHOLDS_MIN = (10, 15)
CORRIDOR_BUFFER_M = 25.0

# minx, miny, maxx, maxy in WGS84. The box extends beyond every 15-minute cutoff.
TEST_BBOX = (106.4420, 29.5280, 106.4785, 29.5685)

# Exact OSM railway=station objects. Names and operation are cross-checked
# against the official December 2024 operating network map.
TEST_STATIONS = {
    "6494407274": {"name": "沙坪坝", "name_en": "Shapingba", "line": "1"},
    "9889652819": {"name": "小龙坎", "name_en": "Xiaolongkan", "line": "1"},
    "13049544548": {"name": "马家岩", "name_en": "Majiayan", "line": "1"},
    "9954696072": {"name": "高庙村", "name_en": "Gaomiaocun", "line": "1"},
}

WALKABLE_HIGHWAYS = {
    "footway", "path", "pedestrian", "steps", "living_street",
    "residential", "service", "unclassified", "tertiary", "tertiary_link",
    "secondary", "secondary_link", "primary", "primary_link",
}


@dataclass(frozen=True)
class ModelConfig:
    walk_speed_kmh: float = WALK_SPEED_KMH
    time_thresholds_min: tuple[int, ...] = TIME_THRESHOLDS_MIN
    corridor_buffer_m: float = CORRIDOR_BUFFER_M
    analysis_crs: str = ANALYSIS_CRS


def parse_other_tags(value: object) -> dict[str, str]:
    """Parse GDAL's OSM `other_tags` hstore-like string."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return {}
    return dict(re.findall(r'"([^"\\]+)"=>"([^"\\]*)"', str(value)))


def distance_for_minutes(minutes: float, speed_kmh: float) -> float:
    if minutes <= 0 or speed_kmh <= 0:
        raise ValueError("minutes and speed_kmh must be positive")
    return speed_kmh * 1000.0 / 60.0 * minutes


def is_walkable_row(highway: object, other_tags: object) -> bool:
    """Apply a conservative, documented walking filter to an OSM way."""
    if highway not in WALKABLE_HIGHWAYS:
        return False
    tags = parse_other_tags(other_tags)
    if tags.get("foot") in {"no", "private"}:
        return False
    if tags.get("access") in {"no", "private"} and tags.get("foot") not in {
        "yes", "designated", "permissive",
    }:
        return False
    return True


def load_walkable_lines(pbf_path: Path) -> gpd.GeoDataFrame:
    lines = pyogrio.read_dataframe(
        pbf_path,
        layer="lines",
        bbox=TEST_BBOX,
        columns=["osm_id", "name", "highway", "other_tags"],
    )
    mask = [
        is_walkable_row(highway, tags)
        for highway, tags in zip(lines["highway"], lines["other_tags"])
    ]
    lines = lines[mask].copy()
    if lines.empty:
        raise RuntimeError("No walkable OSM ways found inside the test area")
    lines.set_crs(SOURCE_CRS, inplace=True, allow_override=True)
    lines = lines.to_crs(ANALYSIS_CRS)
    return lines[~lines.geometry.is_empty & lines.geometry.notna()].copy()


def load_verified_stations(pbf_path: Path) -> gpd.GeoDataFrame:
    points = pyogrio.read_dataframe(
        pbf_path,
        layer="points",
        bbox=TEST_BBOX,
        columns=["osm_id", "name", "other_tags"],
    )
    points.set_crs(SOURCE_CRS, inplace=True, allow_override=True)
    points["osm_id"] = points["osm_id"].astype(str)
    stations = points[points["osm_id"].isin(TEST_STATIONS)].copy()
    missing = sorted(set(TEST_STATIONS) - set(stations["osm_id"]))
    if missing:
        raise RuntimeError(f"Required OSM station objects are missing: {missing}")

    for row in stations.itertuples():
        expected = TEST_STATIONS[row.osm_id]
        tags = parse_other_tags(row.other_tags)
        if row.name != expected["name"]:
            raise RuntimeError(f"Station {row.osm_id} name changed: {row.name!r}")
        if tags.get("railway") != "station" or tags.get("station") != "subway":
            raise RuntimeError(f"Station {row.osm_id} is not an operating subway station")
        if "construction" in tags:
            raise RuntimeError(f"Construction station was included: {row.osm_id}")

    stations["name_en"] = stations["osm_id"].map(
        {key: value["name_en"] for key, value in TEST_STATIONS.items()}
    )
    stations["line"] = "1"
    stations["status"] = "operational_verified_2024-12"
    stations["coordinate_source"] = f"OpenStreetMap snapshot {OSM_SNAPSHOT_DATE}"
    stations["status_source"] = "Chongqing official operating map 2024-12"
    return stations.to_crs(ANALYSIS_CRS).sort_values("osm_id").reset_index(drop=True)


def load_station_entrances(
    pbf_path: Path, stations: gpd.GeoDataFrame, maximum_distance_m: float = 300.0
) -> gpd.GeoDataFrame:
    """Assign mapped subway entrances to the nearest test station."""
    points = pyogrio.read_dataframe(
        pbf_path,
        layer="points",
        bbox=TEST_BBOX,
        columns=["osm_id", "name", "other_tags"],
    )
    points.set_crs(SOURCE_CRS, inplace=True, allow_override=True)
    entrances = points[
        points["other_tags"].fillna("").str.contains('"railway"=>"subway_entrance"')
    ].to_crs(ANALYSIS_CRS).copy()
    assignments = []
    for entrance in entrances.itertuples():
        distances = stations.geometry.distance(entrance.geometry)
        nearest_index = distances.idxmin()
        nearest_distance = float(distances.loc[nearest_index])
        if nearest_distance <= maximum_distance_m:
            station = stations.loc[nearest_index]
            assignments.append({
                "osm_id": str(entrance.osm_id),
                "station_osm_id": station["osm_id"],
                "station_name": station["name"],
                "distance_to_station_m": nearest_distance,
                "geometry": entrance.geometry,
            })
    assigned = gpd.GeoDataFrame(assignments, geometry="geometry", crs=ANALYSIS_CRS)
    missing = sorted(set(stations["osm_id"]) - set(assigned["station_osm_id"]))
    if missing:
        raise RuntimeError(f"No mapped subway entrance within {maximum_distance_m} m: {missing}")
    return assigned.sort_values(["station_name", "osm_id"]).reset_index(drop=True)


def _iter_lines(geometry: object) -> Iterable[LineString]:
    if geometry is None or geometry.is_empty:
        return
    if geometry.geom_type == "LineString":
        yield geometry
    elif geometry.geom_type == "MultiLineString":
        yield from geometry.geoms


def _node_key(x: float, y: float) -> tuple[float, float]:
    return (round(float(x), 3), round(float(y), 3))


def build_walking_graph(lines: gpd.GeoDataFrame) -> nx.Graph:
    """Split OSM way geometry at vertices and create a metric undirected graph."""
    graph = nx.Graph(crs=str(lines.crs))
    for row in lines.itertuples():
        for line in _iter_lines(row.geometry):
            coords = list(line.coords)
            for index, (start, end) in enumerate(zip(coords[:-1], coords[1:])):
                u, v = _node_key(*start), _node_key(*end)
                segment = LineString([start, end])
                length_m = float(segment.length)
                if length_m <= 0:
                    continue
                graph.add_node(u, x=u[0], y=u[1])
                graph.add_node(v, x=v[0], y=v[1])
                attrs = {
                    "length_m": length_m,
                    "geometry": segment,
                    "osm_id": str(row.osm_id),
                    "highway": row.highway,
                    "segment_index": index,
                }
                if not graph.has_edge(u, v) or length_m < graph[u][v]["length_m"]:
                    graph.add_edge(u, v, **attrs)
    if graph.number_of_nodes() == 0:
        raise RuntimeError("Walking graph contains no nodes")
    return graph


def nearest_graph_node(graph: nx.Graph, point: Point) -> tuple[tuple[float, float], float]:
    nodes = np.asarray(list(graph.nodes), dtype=float)
    distances_sq = (nodes[:, 0] - point.x) ** 2 + (nodes[:, 1] - point.y) ** 2
    index = int(np.argmin(distances_sq))
    return tuple(nodes[index]), float(math.sqrt(distances_sq[index]))


def reachable_edge_frame(
    graph: nx.Graph,
    source: tuple[float, float],
    cutoff_m: float,
    crs: str = ANALYSIS_CRS,
) -> tuple[gpd.GeoDataFrame, dict[tuple[float, float], float]]:
    """Return edges whose two endpoints are reachable within the cutoff."""
    distances = nx.single_source_dijkstra_path_length(
        graph, source, cutoff=cutoff_m, weight="length_m"
    )
    reachable_nodes = set(distances)
    records = [
        {
            "u": str(u), "v": str(v), "osm_id": data["osm_id"],
            "highway": data["highway"], "length_m": data["length_m"],
            "geometry": data["geometry"],
        }
        for u, v, data in graph.edges(data=True)
        if u in reachable_nodes and v in reachable_nodes
    ]
    return gpd.GeoDataFrame(records, geometry="geometry", crs=crs), distances


def reachable_edge_frame_multi(
    graph: nx.Graph,
    sources: list[tuple[float, float]],
    cutoff_m: float,
    crs: str = ANALYSIS_CRS,
) -> tuple[gpd.GeoDataFrame, dict[tuple[float, float], float]]:
    """Return reachable edges using all mapped entrances for one station."""
    distances = nx.multi_source_dijkstra_path_length(
        graph, sources, cutoff=cutoff_m, weight="length_m"
    )
    reachable_nodes = set(distances)
    records = [
        {
            "u": str(u), "v": str(v), "osm_id": data["osm_id"],
            "highway": data["highway"], "length_m": data["length_m"],
            "geometry": data["geometry"],
        }
        for u, v, data in graph.edges(data=True)
        if u in reachable_nodes and v in reachable_nodes
    ]
    return gpd.GeoDataFrame(records, geometry="geometry", crs=crs), distances


def graph_edges_frame(graph: nx.Graph) -> gpd.GeoDataFrame:
    records = [
        {
            "u": str(u), "v": str(v), "osm_id": data["osm_id"],
            "highway": data["highway"], "length_m": data["length_m"],
            "geometry": data["geometry"],
        }
        for u, v, data in graph.edges(data=True)
    ]
    return gpd.GeoDataFrame(records, geometry="geometry", crs=ANALYSIS_CRS)


def calculate_accessibility(
    graph: nx.Graph,
    stations: gpd.GeoDataFrame,
    entrances: gpd.GeoDataFrame,
    config: ModelConfig,
) -> tuple[gpd.GeoDataFrame, pd.DataFrame, gpd.GeoDataFrame, gpd.GeoDataFrame]:
    service_records, summary_records = [], []
    stations = stations.copy()
    snapped_nodes, snap_distances = [], []

    for row in stations.itertuples():
        station_entrances = entrances[entrances["station_osm_id"] == row.osm_id].copy()
        sources, entrance_snap_distances, entrance_nodes = [], [], []
        for entrance in station_entrances.itertuples():
            source, snap_distance = nearest_graph_node(graph, entrance.geometry)
            if snap_distance > 100:
                raise RuntimeError(f"Entrance {entrance.osm_id} is too far from the walking graph")
            sources.append(source)
            entrance_nodes.append(str(source))
            entrance_snap_distances.append(snap_distance)
        sources = list(dict.fromkeys(sources))
        snapped_nodes.append(" | ".join(str(source) for source in sources))
        snap_distances.append(max(entrance_snap_distances))
        entrances.loc[station_entrances.index, "snapped_node"] = entrance_nodes
        entrances.loc[station_entrances.index, "snap_distance_m"] = entrance_snap_distances
        for minutes in config.time_thresholds_min:
            cutoff_m = distance_for_minutes(minutes, config.walk_speed_kmh)
            edges, distances = reachable_edge_frame_multi(graph, sources, cutoff_m)
            if edges.empty:
                raise RuntimeError(f"No reachable edges for station {row.name}")
            corridor = edges.geometry.buffer(config.corridor_buffer_m).union_all()
            service_records.append({
                "station_osm_id": row.osm_id,
                "station_name": row.name,
                "minutes": minutes,
                "walk_speed_kmh": config.walk_speed_kmh,
                "cutoff_m": cutoff_m,
                "corridor_buffer_m": config.corridor_buffer_m,
                "geometry": corridor,
            })
            summary_records.append({
                "station_osm_id": row.osm_id,
                "station_name": row.name,
                "minutes": minutes,
                "walk_speed_kmh": config.walk_speed_kmh,
                "cutoff_m": cutoff_m,
                "entrance_count": len(station_entrances),
                "maximum_entrance_snap_m": max(entrance_snap_distances),
                "reachable_nodes": len(distances),
                "reachable_edges": len(edges),
                "reachable_network_km": edges["length_m"].sum() / 1000.0,
            })

    stations["snapped_node"] = snapped_nodes
    stations["snap_distance_m"] = snap_distances
    service_areas = gpd.GeoDataFrame(
        service_records, geometry="geometry", crs=config.analysis_crs
    )
    summary = pd.DataFrame(summary_records).sort_values(["station_name", "minutes"])
    return service_areas, summary, stations, entrances


def save_geopackage(
    path: Path,
    stations: gpd.GeoDataFrame,
    entrances: gpd.GeoDataFrame,
    network_edges: gpd.GeoDataFrame,
    service_areas: gpd.GeoDataFrame,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    stations.to_file(path, layer="stations", driver="GPKG")
    entrances.to_file(path, layer="station_entrances", driver="GPKG", mode="a")
    network_edges.to_file(path, layer="walk_network_edges", driver="GPKG", mode="a")
    service_areas.to_file(path, layer="service_areas", driver="GPKG", mode="a")


def plot_map(
    output_path: Path,
    network_edges: gpd.GeoDataFrame,
    service_areas: gpd.GeoDataFrame,
    stations: gpd.GeoDataFrame,
    config: ModelConfig,
) -> None:
    plt.rcParams["font.sans-serif"] = [
        "Microsoft YaHei", "SimHei", "Noto Sans CJK SC", "DejaVu Sans"
    ]
    plt.rcParams["axes.unicode_minus"] = False
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig, ax = plt.subplots(figsize=(10, 10), dpi=180)
    network_edges.plot(ax=ax, color="#c8c8c8", linewidth=0.35, zorder=1)
    for minutes, color, alpha in [(15, "#76b7e5", 0.45), (10, "#1768ac", 0.65)]:
        geometry = service_areas.loc[service_areas["minutes"] == minutes, "geometry"].union_all()
        gpd.GeoSeries([geometry], crs=service_areas.crs).plot(
            ax=ax, color=color, alpha=alpha, edgecolor="none", zorder=2
        )
    stations.plot(
        ax=ax, color="white", edgecolor="#111111", linewidth=1.2,
        markersize=65, zorder=4,
    )
    offsets = {"沙坪坝": (35, 25), "小龙坎": (35, -35), "马家岩": (35, 20), "高庙村": (35, -35)}
    for row in stations.itertuples():
        ax.annotate(
            row.name,
            xy=(row.geometry.x, row.geometry.y),
            xytext=offsets[row.name],
            textcoords="offset points",
            fontsize=9,
            weight="bold",
            arrowprops={"arrowstyle": "-", "color": "#444444", "lw": 0.6},
            zorder=5,
        )

    minx, miny, maxx, maxy = network_edges.total_bounds
    ax.set(xlim=(minx, maxx), ylim=(miny, maxy))
    ax.set_aspect("equal")
    ax.set_axis_off()
    ax.set_title("重庆轨道交通站点步行可达性 MVP\n沙坪坝—高庙村（10 / 15 分钟）", fontsize=16, pad=14)
    ax.legend(handles=[
        Patch(facecolor="#1768ac", alpha=0.65, label="10分钟可达道路范围"),
        Patch(facecolor="#76b7e5", alpha=0.45, label="15分钟可达道路范围"),
        Line2D([0], [0], marker="o", color="none", markerfacecolor="white",
               markeredgecolor="#111111", markersize=8, label="已运营站点"),
        Line2D([0], [0], color="#c8c8c8", lw=1.2, label="OSM步行道路"),
    ], loc="upper left", frameon=True, fontsize=9)

    scalebar_x = minx + (maxx - minx) * 0.06
    scalebar_y = miny + (maxy - miny) * 0.05
    ax.plot([scalebar_x, scalebar_x + 500], [scalebar_y, scalebar_y], color="black", lw=3)
    ax.text(scalebar_x + 250, scalebar_y + 35, "500 m", ha="center", fontsize=8)
    ax.annotate("N", xy=(maxx - 120, maxy - 80), xytext=(maxx - 120, maxy - 340),
                ha="center", fontsize=10, weight="bold",
                arrowprops={"arrowstyle": "-|>", "color": "black", "lw": 1.2})
    note = (
        f"假设：{config.walk_speed_kmh:.1f} km/h；10分钟=750 m，15分钟=1,125 m；"
        f"道路色带为{config.corridor_buffer_m:.0f} m制图缓冲。\n"
        f"道路与坐标：© OpenStreetMap contributors，ODbL，快照 {OSM_SNAPSHOT_DATE}；"
        f"运营状态：重庆市交通运输委员会线网图 {OFFICIAL_NETWORK_DATE}。\n"
        "未计坡度、站内步行、等候时间和人口；色带不是人口覆盖范围。"
    )
    fig.text(0.5, 0.025, note, ha="center", va="bottom", fontsize=7.5)
    fig.tight_layout(rect=(0.01, 0.08, 0.99, 0.98))
    fig.savefig(output_path, bbox_inches="tight", facecolor="white")
    plt.close(fig)


def write_metadata(
    path: Path, pbf_path: Path, project_root: Path, config: ModelConfig
) -> None:
    try:
        public_input_path = pbf_path.relative_to(project_root).as_posix()
    except ValueError:
        public_input_path = pbf_path.name
    metadata = {
        "analysis_name": "Shapingba-Gaomiaocun walking accessibility MVP",
        "generated_at": pd.Timestamp.now(tz="Asia/Shanghai").isoformat(),
        "input_pbf": public_input_path,
        "osm_snapshot_date": OSM_SNAPSHOT_DATE,
        "official_operating_network_date": OFFICIAL_NETWORK_DATE,
        "analysis_crs": config.analysis_crs,
        "bbox_wgs84": TEST_BBOX,
        "walk_speed_kmh": config.walk_speed_kmh,
        "time_thresholds_min": config.time_thresholds_min,
        "distance_cutoffs_m": {
            str(m): distance_for_minutes(m, config.walk_speed_kmh)
            for m in config.time_thresholds_min
        },
        "corridor_buffer_m": config.corridor_buffer_m,
        "network_origins": "Mapped OSM subway entrances assigned to nearest test station within 300 m",
        "population_analysis": False,
        "limitations": [
            "No slope or elevation penalty",
            "No station interior, gate, platform, or waiting time",
            "Mapped station entrances may be incomplete",
            "Reachable edges require both endpoints within cutoff",
            "OSM completeness and access tags may be imperfect",
            "Corridor polygon is a cartographic road buffer, not population coverage",
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")


def run(pbf_path: Path, project_root: Path) -> dict[str, Path]:
    if not pbf_path.exists():
        raise FileNotFoundError(pbf_path)
    config = ModelConfig()
    lines = load_walkable_lines(pbf_path)
    stations = load_verified_stations(pbf_path)
    entrances = load_station_entrances(pbf_path, stations)
    graph = build_walking_graph(lines)
    network_edges = graph_edges_frame(graph)
    service_areas, summary, stations, entrances = calculate_accessibility(
        graph, stations, entrances, config
    )

    processed = project_root / "data" / "processed"
    figure_dir = project_root / "reports" / "figures"
    output_dir = project_root / "outputs"
    paths = {
        "geopackage": processed / "shapingba_walk_accessibility_mvp.gpkg",
        "summary": processed / "shapingba_accessibility_summary.csv",
        "metadata": processed / "shapingba_walk_accessibility_metadata.json",
        "figure": figure_dir / "shapingba_walk_accessibility_mvp.png",
    }
    save_geopackage(paths["geopackage"], stations, entrances, network_edges, service_areas)
    summary.to_csv(paths["summary"], index=False, encoding="utf-8-sig")
    write_metadata(paths["metadata"], pbf_path, project_root, config)
    plot_map(paths["figure"], network_edges, service_areas, stations, config)

    output_dir.mkdir(parents=True, exist_ok=True)
    for key in ("geopackage", "summary", "metadata", "figure"):
        output_path = output_dir / paths[key].name
        shutil.copy2(paths[key], output_path)
        paths[f"output_{key}"] = output_path
    return paths


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pbf", type=Path,
        default=Path("work/data_source_survey/chongqing_osm_2026-10-10.osm.pbf"),
    )
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    outputs = run(args.pbf.resolve(), args.project_root.resolve())
    for name, path in outputs.items():
        print(f"{name}: {path}")


if __name__ == "__main__":
    main()
