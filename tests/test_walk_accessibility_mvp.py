from __future__ import annotations

import geopandas as gpd
import networkx as nx
from shapely.geometry import LineString

from chongqing_rail_transit.walk_accessibility_mvp import (
    ANALYSIS_CRS,
    build_walking_graph,
    distance_for_minutes,
    is_walkable_row,
    parse_other_tags,
    reachable_edge_frame,
    reachable_edge_frame_multi,
)


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
