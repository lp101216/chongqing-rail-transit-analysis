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
from chongqing_rail_transit.bus_feeder_time_analysis import (
    break_even_budget,
    build_survey_priority,
    build_wait_sensitivity,
    directional_slope_route,
    route_stop_audit_table,
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


def test_wait_sensitivity_uses_half_headway_but_not_actual_time() -> None:
    break_even = pd.DataFrame({
        "residential_name": ["测试住宅"],
        "route_ref": ["100"],
        "max_wait_plus_ride_min_vs_flat": [12.0],
        "max_wait_plus_ride_min_vs_slope": [15.0],
    })
    result = build_wait_sensitivity(break_even)
    row = result.query(
        "direct_walk_benchmark == 'flat_4_5_kmh' and assumed_headway_min == 10"
    ).iloc[0]
    assert row["assumed_expected_wait_min"] == pytest.approx(5.0)
    assert row["maximum_in_vehicle_min_to_save_time"] == pytest.approx(7.0)
    assert result["assumption"].str.startswith("parameter_only").all()


def test_break_even_budget_deducts_both_walking_legs_on_each_basis() -> None:
    assert break_even_budget(20.0, 2.0, 3.0) == pytest.approx(15.0)
    assert break_even_budget(25.0, 4.0, 6.0) == pytest.approx(15.0)


def test_directional_slope_route_respects_direction_and_snap_time() -> None:
    graph = nx.DiGraph()
    graph.add_edge((0.0, 0.0), (100.0, 0.0), length_m=100.0, time_min=4.0)
    graph.add_edge((100.0, 0.0), (0.0, 0.0), length_m=100.0, time_min=1.0)
    destinations = gpd.GeoDataFrame(
        {"osm_id": ["stop"]}, geometry=[Point(105, 0)], crs="EPSG:32648"
    )
    outward = directional_slope_route(graph, Point(0, 0), destinations)
    assert outward["time_min"] == pytest.approx(4.0 + 5.0 / 75.0)
    assert outward["distance_m"] == pytest.approx(105.0)
    reverse_destination = gpd.GeoDataFrame(
        {"osm_id": ["home"]}, geometry=[Point(0, 0)], crs="EPSG:32648"
    )
    inward = directional_slope_route(graph, Point(100, 0), reverse_destination)
    assert inward["time_min"] == pytest.approx(1.0)


def test_survey_priority_is_explicitly_an_investigation_order() -> None:
    rows = []
    for residence, route in [
        ("凤天花园", "1429"), ("卓越·美丽山水", "210"), ("凯莱阳光", "209"),
        ("升伟·新时空", "209"), ("周李小区", "209"),
    ]:
        rows.append({
            "residential_name": residence, "route_ref": route,
            "boarding_platform_name": "上车站", "alighting_platform_name": "下车站",
            "rail_station": "轨道站", "walk_to_boarding_min_4_5": 1.0,
            "walk_to_boarding_min_slope_model": 1.2,
            "max_wait_plus_ride_min_vs_flat": 5.0,
            "max_wait_plus_ride_min_vs_slope": 6.0,
        })
    priority = build_survey_priority(pd.DataFrame(rows))
    assert priority["survey_priority_rank"].tolist() == [1, 2, 3, 4, 5]
    assert priority["interpretation"].str.contains("不代表公交已经更快").all()


def test_route_stop_audit_preserves_false_positive_correction() -> None:
    audit = route_stop_audit_table()
    assert len(audit) == 5
    corrected = audit[audit["residential_name"].isin(["升伟·新时空", "周李小区"])]
    assert corrected["audit_result"].eq("screen_false_positive_alternative_stop").all()
    assert corrected["boarding_platform_name"].eq("西南医院").all()
    assert (audit["alighting_stop_sequence"] - audit["boarding_stop_sequence"]).eq(
        audit["stop_hops"]
    ).all()
    assert audit["operation_verification"].str.contains("not_operator_confirmation").all()
