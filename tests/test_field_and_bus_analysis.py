from pathlib import Path

import geopandas as gpd
import networkx as nx
import pandas as pd
import pytest
from shapely.geometry import LineString, Point

from chongqing_rail_transit.bus_feeder_accessibility import (
    analyse_residential_bus_access,
    analyse_station_transfers,
    associate_routes,
    build_threshold_sensitivity,
)
from chongqing_rail_transit.field_validation import (
    compare_models,
    completed_observations,
    parse_gpx,
)


def test_parse_gpx_distance_time_and_elevation(tmp_path: Path) -> None:
    path = tmp_path / "walk.gpx"
    path.write_text(
        """<?xml version="1.0"?>
        <gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1">
          <trk><trkseg>
            <trkpt lat="29.5500" lon="106.4500"><ele>250</ele><time>2026-10-17T00:00:00Z</time></trkpt>
            <trkpt lat="29.5509" lon="106.4500"><ele>260</ele><time>2026-10-17T00:02:00Z</time></trkpt>
          </trkseg></trk>
        </gpx>""",
        encoding="utf-8",
    )
    result = parse_gpx(path)
    assert result["gpx_point_count"] == 2
    assert result["gpx_elapsed_min"] == pytest.approx(2.0)
    assert result["gpx_distance_m"] == pytest.approx(100.1, abs=1.0)
    assert result["gpx_elevation_gain_m"] == pytest.approx(10.0)
    assert result["gpx_elevation_loss_m"] == pytest.approx(0.0)


def test_no_field_time_means_no_validation_result() -> None:
    empty = pd.DataFrame({
        "walk_id": ["V01-W1"],
        "route_id": ["V01"],
        "fixed_model_time_min": [5.8],
        "slope_model_time_min": [5.9],
        "observed_total_time_min": [""],
        "observed_moving_time_min": [""],
    })
    assert completed_observations(empty).empty
    per_walk, metrics = compare_models(empty)
    assert per_walk.empty
    assert metrics.empty


def test_model_error_metrics_have_declared_sign() -> None:
    observations = pd.DataFrame({
        "walk_id": ["V01-W1", "V02-W1"],
        "route_id": ["V01", "V02"],
        "fixed_model_time_min": [8.0, 12.0],
        "slope_model_time_min": [9.0, 14.0],
        "observed_total_time_min": [10.0, 10.0],
        "observed_moving_time_min": [9.0, 9.0],
    })
    _, metrics = compare_models(observations)
    fixed_total = metrics.query("observed_time_basis == 'total' and model == 'fixed'").iloc[0]
    slope_total = metrics.query("observed_time_basis == 'total' and model == 'slope'").iloc[0]
    assert fixed_total["mae_min"] == pytest.approx(2.0)
    assert fixed_total["median_absolute_error_min"] == pytest.approx(2.0)
    assert fixed_total["mean_signed_error_min"] == pytest.approx(0.0)
    assert slope_total["mae_min"] == pytest.approx(2.5)
    assert slope_total["mean_signed_error_min"] == pytest.approx(1.5)
    assert fixed_total["lower_absolute_error_walks"] == 1
    assert slope_total["lower_absolute_error_walks"] == 1


def test_bus_route_geometry_match_is_explicitly_distance_limited() -> None:
    platforms = gpd.GeoDataFrame(
        {"osm_id": ["1", "2"], "platform_name": ["近", "远"]},
        geometry=[Point(0, 5), Point(0, 50)], crs="EPSG:32648",
    )
    routes = gpd.GeoDataFrame(
        {"route_ref": ["100"]},
        geometry=[LineString([(-10, 0), (10, 0)])], crs="EPSG:32648",
    )
    result = associate_routes(platforms, routes)
    assert result.loc[0, "mapped_route_refs_30m"] == "100"
    assert result.loc[1, "mapped_route_refs_30m"] == ""
    assert result.loc[0, "nearest_route_geometry_m"] == pytest.approx(5.0)


def test_bus_and_rail_network_distances_drive_candidate_classification() -> None:
    graph = nx.Graph()
    for start, end in [((0.0, 0.0), (100.0, 0.0)), ((100.0, 0.0), (200.0, 0.0))]:
        graph.add_edge(start, end, length_m=100.0)
    entrances = gpd.GeoDataFrame(
        {"osm_id": ["entrance"], "station_name": ["测试站"]},
        geometry=[Point(0, 0)], crs="EPSG:32648",
    )
    platforms = gpd.GeoDataFrame(
        {
            "osm_id": ["platform"],
            "platform_name": ["测试公交站"],
            "mapped_route_refs_30m": ["100"],
            "mapped_route_ref_count_30m": [1],
            "nearest_route_geometry_m": [5.0],
        },
        geometry=[Point(105, 0)], crs="EPSG:32648",
    )
    platforms, _ = analyse_station_transfers(graph, platforms, entrances)
    assert platforms.loc[0, "network_walk_to_rail_m"] == pytest.approx(105.0)
    assert bool(platforms.loc[0, "within_300m_rail_walk"])

    residences = gpd.GeoDataFrame(
        {
            "osm_ref": ["way/1"],
            "residential_name": ["测试住宅"],
            "station_name": ["测试站"],
            "slope_time_to_rail_min": [20.0],
        },
        geometry=[Point(200, 0)], crs="EPSG:32648",
    )
    screened, routes = analyse_residential_bus_access(graph, residences, platforms)
    assert screened.loc[0, "walk_to_bus_platform_m"] == pytest.approx(105.0)
    assert screened.loc[0, "candidate_direct_feeder_route_refs"] == "100"
    assert screened.loc[0, "screening_result"] == "spatial_direct_feeder_candidate"
    assert len(routes) == 1
    sensitivity = build_threshold_sensitivity(platforms, screened)
    assert len(sensitivity) == 9
    assert sensitivity["spatial_direct_feeder_candidate_count"].eq(1).all()


def test_bus_threshold_sensitivity_recomputes_candidate_counts() -> None:
    platforms = gpd.GeoDataFrame(
        {
            "network_walk_to_rail_m": [100.0],
            "mapped_route_refs_30m": ["100"],
        },
        geometry=[Point(0, 0)], crs="EPSG:32648",
    )
    residences = gpd.GeoDataFrame(
        {
            "slope_time_to_rail_min": [20.0, 20.0, 20.0],
            "walk_to_bus_platform_m": [250.0, 350.0, 450.0],
            "nearest_platform_mapped_route_refs": ["100", "100", "100"],
        },
        geometry=[Point(0, 0), Point(1, 0), Point(2, 0)], crs="EPSG:32648",
    )
    result = build_threshold_sensitivity(platforms, residences)
    base_rail = result[result["rail_transfer_walk_threshold_m"].eq(300)]
    assert base_rail["spatial_direct_feeder_candidate_count"].tolist() == [1, 2, 3]
    assert base_rail["bus_platform_over_threshold_count"].tolist() == [2, 1, 0]
