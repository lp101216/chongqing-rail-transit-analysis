"""Compare local walking-connector scenarios with official policy evidence.

Official travel-time fields remain empty when public documents do not identify
a site-level project geometry. OSM Line 27 geometry is only spatial context,
not an official engineering alignment or a modeled official benefit.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import geopandas as gpd
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
import pandas as pd
import pyogrio

from .walk_accessibility_mvp import ANALYSIS_CRS, SOURCE_CRS, TEST_BBOX, parse_other_tags


OFFICIAL_PLAN_URL = (
    "https://jtysw.cq.gov.cn/zwgk_240/zfxxgkml/zcwj/qtwj/"
    "202608/t20260820_15956773.html"
)
LINE27_STATUS_URL = "https://www.cq.gov.cn/ywdt/jrcq/202608/t20260818_15945156.html"


def official_evidence_table() -> pd.DataFrame:
    """Return audited facts from public government documents."""
    return pd.DataFrame([
        {
            "evidence_id": "PLAN-2026-2030",
            "document_title": "重庆市“十五五”现代综合交通运输体系和物流发展规划（2026—2030年）",
            "publisher": "重庆市人民政府",
            "document_date": "2026-07-29",
            "publication_date": "2026-08-20",
            "status_supported": "official_plan",
            "case_relevant_fact": (
                "加强轨道与公交、非机动车、步行接驳，推动未开通出入口应开尽开，"
                "加强车站与市政道路、住宅小区、商业楼宇直连直通。"
            ),
            "quantitative_fact": "公交轨道50米接驳率：规划基期84%，2030年目标87%",
            "local_project_named": False,
            "public_cost_found": False,
            "source_url": OFFICIAL_PLAN_URL,
            "source_role": "primary_government_plan",
            "reuse_note": "引用事实和短摘录；官方PDF不在Git仓库再分发。",
        },
        {
            "evidence_id": "LINE27-STATUS-2026-08",
            "document_title": "轨道交通27号线璧山—西永区段“电通”进展",
            "publisher": "重庆市人民政府网",
            "document_date": "2026-08-18",
            "publication_date": "2026-08-18",
            "status_supported": "under_construction_system_commissioning",
            "case_relevant_fact": (
                "27号线定位为跨三大槽谷、衔接沙坪坝站等枢纽的东西向快线；"
                "证据日仍处建设和系统调试阶段，不属于已运营基线。"
            ),
            "quantitative_fact": "未给出高庙村案例区的站点级步行时间或受益人口",
            "local_project_named": False,
            "public_cost_found": False,
            "source_url": LINE27_STATUS_URL,
            "source_role": "government_portal_status_release",
            "reuse_note": "仅用于状态和目标核验，不用于推算本案例出行时间。",
        },
    ])


def build_comparison_table(simulation: pd.DataFrame) -> pd.DataFrame:
    """Build a no-score comparison with explicit non-evaluable fields."""
    required = {
        "case_id", "residential_name", "baseline_time_min_4_5",
        "scenario_time_min_4_5", "time_saved_min_4_5", "time_saved_pct",
        "connector_length_m", "station_reachable_network_km_15_change",
    }
    missing = required - set(simulation.columns)
    if missing:
        raise ValueError(f"Simulation table is missing columns: {sorted(missing)}")

    rows: list[dict[str, object]] = []
    for row in simulation.itertuples():
        rows.append({
            "scheme_id": f"INDEPENDENT-{row.case_id}",
            "scheme_name": f"{row.residential_name}单一假设步行连接",
            "scheme_owner": "independent_research_scenario",
            "scope": "site_level",
            "status": "hypothetical_unverified",
            "evidence_date": "2026-10-14",
            "target_area": row.residential_name,
            "intervention_type": "pedestrian_connection",
            "baseline_time_min_4_5": row.baseline_time_min_4_5,
            "scenario_time_min_4_5": row.scenario_time_min_4_5,
            "time_saved_min_4_5": row.time_saved_min_4_5,
            "time_saved_pct": row.time_saved_pct,
            "local_15min_reachable_network_change_km": row.station_reachable_network_km_15_change,
            "connector_length_m": row.connector_length_m,
            "public_transport_coverage": "not_evaluated",
            "potential_beneficiaries": "not_evaluable_no_population_data",
            "engineering_difficulty": "not_evaluable_requires_site_survey",
            "capital_cost": "not_publicly_available",
            "maintenance_cost": "not_publicly_available",
            "equity": "not_evaluable_no_population_or_group_data",
            "local_effect_evidence": "model_estimate",
            "policy_or_status_evidence": "not_applicable",
            "direct_comparison_allowed": True,
            "source_url": "reports/improvement_case_study.md",
            "comparison_note": "可与相同模型下的现状比较；不可视为实测或可建工程。",
        })

    rows.extend([
        {
            "scheme_id": "OFFICIAL-INTEGRATED-ACCESS",
            "scheme_name": "十五五轨道站综合接驳与直连直通政策",
            "scheme_owner": "Chongqing_Municipal_Government",
            "scope": "municipal_policy",
            "status": "official_plan_2026_2030",
            "evidence_date": "2026-07-29",
            "target_area": "Chongqing_citywide_not_site_specified",
            "intervention_type": "walk_bus_bicycle_exit_integration",
            "baseline_time_min_4_5": pd.NA,
            "scenario_time_min_4_5": pd.NA,
            "time_saved_min_4_5": pd.NA,
            "time_saved_pct": pd.NA,
            "local_15min_reachable_network_change_km": pd.NA,
            "connector_length_m": pd.NA,
            "public_transport_coverage": "bus_rail_50m_rate_84pct_to_87pct_by_2030",
            "potential_beneficiaries": "not_evaluable_no_site_list_or_population",
            "engineering_difficulty": "not_evaluable_no_case_geometry",
            "capital_cost": "not_publicly_available_at_case_level",
            "maintenance_cost": "not_publicly_available_at_case_level",
            "equity": "not_evaluable_no_site_allocation_or_group_data",
            "local_effect_evidence": "not_evaluable",
            "policy_or_status_evidence": "primary_government_plan",
            "direct_comparison_allowed": False,
            "source_url": OFFICIAL_PLAN_URL,
            "comparison_note": "方向与独立方案同类，但未点名三个住宅区，不能计算本地节时。",
        },
        {
            "scheme_id": "OFFICIAL-LINE27",
            "scheme_name": "轨道交通27号线",
            "scheme_owner": "Chongqing_official_project",
            "scope": "regional_rail_network",
            "status": "under_construction_system_commissioning",
            "evidence_date": "2026-08-18",
            "target_area": "regional_corridor_including_Shapingba_hub",
            "intervention_type": "regional_express_rail",
            "baseline_time_min_4_5": pd.NA,
            "scenario_time_min_4_5": pd.NA,
            "time_saved_min_4_5": pd.NA,
            "time_saved_pct": pd.NA,
            "local_15min_reachable_network_change_km": pd.NA,
            "connector_length_m": pd.NA,
            "public_transport_coverage": "regional_east_west_connectivity",
            "potential_beneficiaries": "not_evaluable_no_comparable_public_demand_data",
            "engineering_difficulty": "major_rail_project_not_comparable_to_short_connector",
            "capital_cost": "not_verified_in_sources_used_for_this_case",
            "maintenance_cost": "not_publicly_available_in_comparable_scope",
            "equity": "not_evaluable_in_case_scope",
            "local_effect_evidence": "not_evaluable",
            "policy_or_status_evidence": "government_status_release",
            "direct_comparison_allowed": False,
            "source_url": LINE27_STATUS_URL,
            "comparison_note": "解决区域快线问题；无案例区站点级情景，不能作节时排名。",
        },
    ])
    return pd.DataFrame(rows)


def load_line27_context(pbf_path: Path) -> gpd.GeoDataFrame:
    """Load OSM Line 27 construction ways inside the study bbox."""
    lines = pyogrio.read_dataframe(
        pbf_path, layer="lines", bbox=TEST_BBOX,
        columns=["osm_id", "name", "railway", "other_tags"],
    )
    lines.set_crs(SOURCE_CRS, inplace=True, allow_override=True)
    keep = []
    for row in lines.itertuples():
        tags = parse_other_tags(row.other_tags)
        keep.append(
            tags.get("ref") == "27" or tags.get("name:en") == "Line 27"
            or str(row.name).strip() == "27号线"
        )
    result = lines[keep].copy().to_crs(ANALYSIS_CRS)
    if result.empty:
        raise RuntimeError("No OSM Line 27 context geometry found in the study bbox")
    result["geometry_role"] = "osm_construction_context_not_official_alignment"
    result["osm_snapshot"] = "2026-10-09"
    return result


def plot_comparison(
    output_path: Path, simulation: pd.DataFrame,
    spatial: dict[str, gpd.GeoDataFrame], stations: gpd.GeoDataFrame,
    network: gpd.GeoDataFrame, line27: gpd.GeoDataFrame,
) -> None:
    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
    fig, (ax_map, ax_bar) = plt.subplots(1, 2, figsize=(14, 7), dpi=180)
    focus = spatial["baseline_routes"].total_bounds
    pad = 450
    local_network = network.cx[focus[0]-pad:focus[2]+pad, focus[1]-pad:focus[3]+pad]
    local_stations = stations.cx[focus[0]-pad:focus[2]+pad, focus[1]-pad:focus[3]+pad]
    local_network.plot(ax=ax_map, color="#dedede", linewidth=0.45, zorder=1)
    line27.plot(ax=ax_map, color="#e67e22", linewidth=2.4, linestyle="--", zorder=2)
    spatial["baseline_routes"].plot(ax=ax_map, color="#777777", linewidth=1.4, zorder=3)
    spatial["scenario_routes"].plot(ax=ax_map, color="#1677b8", linewidth=2.0, zorder=4)
    spatial["connectors"].plot(ax=ax_map, color="#d01c8b", linewidth=4.0, zorder=5)
    spatial["origins"].plot(ax=ax_map, color="#d7301f", edgecolor="white", markersize=75, zorder=6)
    local_stations.plot(ax=ax_map, color="#2166ac", edgecolor="white", marker="s", markersize=60, zorder=6)
    for row in spatial["origins"].itertuples():
        ax_map.annotate(row.residential_name, (row.geometry.x, row.geometry.y),
                        xytext=(5, 5), textcoords="offset points", fontsize=8)
    ax_map.set_xlim(focus[0]-pad, focus[2]+pad)
    ax_map.set_ylim(focus[1]-pad, focus[3]+pad)
    ax_map.set_axis_off()
    ax_map.set_title("案例区空间关系（27号线仅作OSM施工线位背景）", fontsize=12)
    ax_map.legend(handles=[
        Line2D([0], [0], color="#777777", lw=1.6, label="现状最短路"),
        Line2D([0], [0], color="#1677b8", lw=2.2, label="独立情景路线"),
        Line2D([0], [0], color="#d01c8b", lw=4, label="假设步行连接"),
        Line2D([0], [0], color="#e67e22", lw=2.4, ls="--", label="OSM 27号线施工线位"),
    ], loc="lower left", fontsize=8, frameon=True)

    y = list(range(len(simulation)))
    ax_bar.barh(y, simulation["baseline_time_min_4_5"], color="#bdbdbd", height=0.66, label="现状模型")
    ax_bar.barh(y, simulation["scenario_time_min_4_5"], color="#1677b8", height=0.42, label="单连接情景")
    ax_bar.axvline(15, color="#b2182b", linestyle="--", linewidth=1.5, label="15分钟")
    ax_bar.set_yticks(y, simulation["residential_name"])
    ax_bar.invert_yaxis()
    ax_bar.set_xlabel("模型步行时间（分钟，4.5 km/h）")
    ax_bar.set_title("仅独立方案具有站点级可计算效果", fontsize=12)
    ax_bar.legend(loc="lower right", fontsize=8)
    ax_bar.grid(axis="x", color="#eeeeee")
    for index, row in enumerate(simulation.itertuples()):
        ax_bar.text(row.scenario_time_min_4_5 + 0.2, index,
                    f"-{row.time_saved_min_4_5:.1f}分", va="center", fontsize=8, color="#084081")
    ax_bar.text(
        0.02, -0.20,
        "官方规划要求轨道与步行/公交/非机动车接驳、住宅小区直连；\n"
        "未公开本案例点位与几何，故不填节时值、不参与数值排名。",
        transform=ax_bar.transAxes, fontsize=8, va="top",
    )
    fig.suptitle("高庙村案例：官方规划方向与独立步行连接情景比较", fontsize=16)
    fig.tight_layout(rect=(0, 0.06, 1, 0.95))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, bbox_inches="tight", facecolor="white")
    plt.close(fig)


def run(project_root: Path, pbf_path: Path) -> dict[str, Path]:
    processed = project_root / "data" / "processed"
    simulation = pd.read_csv(processed / "shapingba_improvement_simulation.csv")
    comparison = build_comparison_table(simulation)
    evidence = official_evidence_table()
    simulation_gpkg = processed / "shapingba_improvement_simulation.gpkg"
    spatial = {layer: gpd.read_file(simulation_gpkg, layer=layer)
               for layer in ("origins", "baseline_routes", "scenario_routes", "connectors")}
    access_gpkg = processed / "shapingba_walk_accessibility_mvp.gpkg"
    stations = gpd.read_file(access_gpkg, layer="stations")
    network = gpd.read_file(access_gpkg, layer="walk_network_edges")
    line27 = load_line27_context(pbf_path)

    comparison_path = processed / "shapingba_official_independent_comparison.csv"
    evidence_path = processed / "shapingba_official_plan_evidence.csv"
    gpkg_path = processed / "shapingba_official_independent_comparison.gpkg"
    figure_path = project_root / "reports" / "figures" / "shapingba_official_independent_comparison.png"
    comparison.to_csv(comparison_path, index=False, encoding="utf-8-sig")
    evidence.to_csv(evidence_path, index=False, encoding="utf-8-sig")
    if gpkg_path.exists():
        gpkg_path.unlink()
    for index, (layer, frame) in enumerate([
        ("case_origins", spatial["origins"]),
        ("independent_connectors", spatial["connectors"]),
        ("line27_osm_context", line27),
    ]):
        frame.to_file(gpkg_path, layer=layer, driver="GPKG", mode="w" if index == 0 else "a")
    plot_comparison(figure_path, simulation, spatial, stations, network, line27)
    return {"comparison": comparison_path, "evidence": evidence_path,
            "geopackage": gpkg_path, "figure": figure_path}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--pbf", type=Path,
                        default=Path("work/data_source_survey/chongqing_osm_2026-10-10.osm.pbf"))
    args = parser.parse_args()
    for name, path in run(args.project_root.resolve(), args.pbf.resolve()).items():
        print(f"{name}: {path}")


if __name__ == "__main__":
    main()
