from __future__ import annotations

import geopandas as gpd
import networkx as nx
import pandas as pd
from shapely.geometry import LineString, Point

from chongqing_rail_transit.walk_accessibility_mvp import (
    ANALYSIS_CRS,
    build_walking_graph,
    distance_for_minutes,
    is_walkable_row,
    parse_other_tags,
    reachable_edge_frame,
    reachable_edge_frame_multi,
)
from chongqing_rail_transit.residential_accessibility import (
    analyse_residential_areas,
    build_summary,
)
from chongqing_rail_transit.improvement_simulation import (
    _add_connector,
    select_path_shortcut,
)
from chongqing_rail_transit.official_plan_comparison import build_comparison_table


def test_distance_cutoffs_match_mvp_assumption() -> None:
    assert distance_for_minutes(10, 4.5) == 750
    assert distance_for_minutes(15, 4.5) == 1125


def test_osm_tag_parser_and_access_filter() -> None:
    assert parse_other_tags('"foot"=>"yes","surface"=>"paved"') == {
        "foot": "yes", "surface": "paved"
    }
    assert is_walkable_row("footway", '"foot"=>"yes"')
    assert not is_walkable_row("footway", '"foot"=>"no"')
    assert not is_walkable_row("motorway", None)
    assert not is_walkable_row("service", '"access"=>"private"')
    assert is_walkable_row("service", '"access"=>"private","foot"=>"permissive"')


def test_graph_and_cutoff_use_metric_edge_lengths() -> None:
    lines = gpd.GeoDataFrame(
        {
            "osm_id": ["1"], "name": ["test"], "highway": ["footway"],
            "other_tags": [None],
            "geometry": [LineString([(0, 0), (100, 0), (200, 0)])],
        },
        crs=ANALYSIS_CRS,
    )
    graph = build_walking_graph(lines)
    assert isinstance(graph, nx.Graph)
    assert graph.number_of_nodes() == 3
    assert graph.number_of_edges() == 2
    edges, distances = reachable_edge_frame(graph, (0.0, 0.0), cutoff_m=100)
    assert len(distances) == 2
    assert len(edges) == 1
    assert edges.iloc[0]["length_m"] == 100

    multi_edges, multi_distances = reachable_edge_frame_multi(
        graph, [(0.0, 0.0), (200.0, 0.0)], cutoff_m=100
    )
    assert len(multi_distances) == 3
    assert len(multi_edges) == 2


def test_cutoff_clips_partial_edge_instead_of_dropping_it() -> None:
    lines = gpd.GeoDataFrame(
        {
            "osm_id": ["2"], "name": ["boundary"], "highway": ["footway"],
            "other_tags": [None],
            "geometry": [LineString([(0, 0), (200, 0)])],
        },
        crs=ANALYSIS_CRS,
    )
    graph = build_walking_graph(lines)
    edges, distances = reachable_edge_frame(graph, (0.0, 0.0), cutoff_m=75)
    assert len(distances) == 1
    assert len(edges) == 1
    assert edges.iloc[0]["length_m"] == 75
    assert edges.iloc[0].geometry.length == 75


def test_residential_routing_and_speed_sensitivity() -> None:
    lines = gpd.GeoDataFrame(
        {
            "osm_id": ["1"], "name": ["test"], "highway": ["footway"],
            "other_tags": [None],
            "geometry": [LineString([(0, 0), (100, 0), (200, 0), (1200, 0)])],
        },
        crs=ANALYSIS_CRS,
    )
    graph = build_walking_graph(lines)
    areas = gpd.GeoDataFrame(
        [{"osm_ref": "way/1", "name": "测试住宅区", "area_ha": 1.0,
          "geometry": Point(1200, 0)}],
        crs=ANALYSIS_CRS,
    )
    entrances = gpd.GeoDataFrame(
        [{"osm_id": "e1", "station_name": "测试站",
          "geometry": Point(0, 0)}],
        crs=ANALYSIS_CRS,
    )
    analysed, routes = analyse_residential_areas(graph, areas, entrances)
    row = analysed.iloc[0]
    assert row["nearest_station"] == "测试站"
    assert row["network_walk_distance_m"] == 1200
    assert row["walk_time_min_4_5"] == 16
    assert bool(row["over_10_min_4_5"])
    assert bool(row["over_15_min_3_6"])
    assert bool(row["over_15_min_4_5"])
    assert not bool(row["over_15_min_5_4"])
    assert not bool(row["over_20_min_4_5"])
    assert row["accessibility_band_4_5"] == "15_20_min"
    assert len(routes) == 1
    summary = build_summary(analysed)
    row_15 = summary[
        (summary["scenario"] == "walk_speed_4.5_kmh")
        & (summary["threshold_min"] == 15)
    ].iloc[0]
    assert row_15["over_threshold_count"] == 1


def test_hypothetical_shortcut_reduces_a_known_detour() -> None:
    lines = gpd.GeoDataFrame(
        {
            "osm_id": ["3"], "name": ["u-shape"], "highway": ["footway"],
            "other_tags": [None],
            "geometry": [LineString([(0, 0), (0, 100), (100, 100), (100, 0)])],
        },
        crs=ANALYSIS_CRS,
    )
    graph = build_walking_graph(lines)
    path = nx.shortest_path(graph, (0.0, 0.0), (100.0, 0.0), weight="length_m")
    shortcut = select_path_shortcut(
        graph, path, minimum_length_m=20, maximum_length_m=150,
        minimum_detour_ratio=1.5,
    )
    assert shortcut.connector_length_m == 100
    assert shortcut.replaced_path_length_m == 300
    scenario = _add_connector(graph, shortcut, "test")
    assert nx.shortest_path_length(
        scenario, (0.0, 0.0), (100.0, 0.0), weight="length_m"
    ) == 100


def test_official_comparison_does_not_invent_local_effects() -> None:
    simulation = pd.DataFrame([{
        "case_id": 1,
        "residential_name": "测试小区",
        "baseline_time_min_4_5": 20.0,
        "scenario_time_min_4_5": 16.0,
        "time_saved_min_4_5": 4.0,
        "time_saved_pct": 20.0,
        "connector_length_m": 100.0,
        "station_reachable_network_km_15_change": 0.2,
    }])
    result = build_comparison_table(simulation)
    independent = result[result["scheme_id"] == "INDEPENDENT-1"].iloc[0]
    official = result[result["scheme_owner"] == "Chongqing_Municipal_Government"].iloc[0]

    assert independent["time_saved_min_4_5"] == 4.0
    assert pd.isna(official["time_saved_min_4_5"])
    assert not bool(official["direct_comparison_allowed"])
