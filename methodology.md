# 方法说明

## 1. 当前分析范围

当前可运行成果是重庆轨道交通站点步行可达性 MVP，研究沙坪坝—高庙村走廊中的4个已运营站点：沙坪坝、小龙坎、马家岩和高庙村。

运营状态以重庆市交通运输委员会发布的2024年12月运营线网图核验；坐标、地铁出入口和道路来自 OpenStreetMap 重庆2026-10-09快照。两个来源时间不完全一致，属于已知限制。

## 2. 输入与坐标系统

- 原始 OSM PBF：WGS84（EPSG:4326）。
- 分析坐标：WGS84 / UTM zone 48N（EPSG:32648），距离单位为米。
- 测试包围框：`(106.4420, 29.5280, 106.4785, 29.5685)`。
- 站点位置：选择 OSM 中精确的 `railway=station`、`station=subway` 对象，并使用固定 OSM ID 检查名称和状态标签。

## 3. 步行道路筛选

纳入的 `highway` 类型包括人行道、路径、步行街、台阶、生活街道、住宅道路、服务道路和普通城市道路。高速公路、快速路、施工道路及未列入白名单的类型不进入图。

访问限制规则：

- `foot=no` 或 `foot=private`：排除；
- `access=no/private` 且没有 `foot=yes/designated/permissive`：排除；
- 其余白名单道路保留。

该规则是保守的 MVP 规则，不等同于完整的 OSMnx 步行过滤器。

## 4. 道路网络构建

1. 使用 Pyogrio/GDAL 从本地 PBF 的 `lines` 图层按包围框读取道路。
2. 将每条道路几何按相邻顶点拆分成边。
3. 以投影坐标（保留毫米级舍入）作为 NetworkX 无向图节点。
4. 以线段长度（米）作为边权重。
5. 同一对节点存在重复边时保留较短边。

最终公开 GeoPackage 中的 `walk_network_edges` 图层包含7,456条图边。图中可能存在由数据缺失或立体道路表达造成的断裂。

## 5. 站点出入口与多源最短路

地下车站中心点不是乘客进入街道网络的位置。模型从 OSM 中提取 `railway=subway_entrance` 点，将300米以内的出入口分配给最近的测试站。本次共有12个出入口：

- 沙坪坝：5个；
- 小龙坎：3个；
- 马家岩：2个；
- 高庙村：2个。

每个出入口吸附到最近的步行图节点；最大允许吸附距离为100米。每站所有出入口节点共同作为 NetworkX 多源 Dijkstra 的源点。

对节点 \(v\) 的最短路距离定义为：

\[
d(v)=\min_{s\in S}\operatorname{shortest\_path\_length}(s,v),
\]

其中 \(S\) 是该站全部出入口对应的道路节点集合，边权重为道路长度。

## 6. 时间阈值与步速假设

步速是研究假设，不是本地实测值：

\[
v=4.5\ \text{km/h}=75\ \text{m/min}.
\]

因此：

- 10分钟：\(75\times10=750\) 米；
- 15分钟：\(75\times15=1,125\) 米。

模型保留两个端点均能在阈值内到达的道路边。这个离散规则会略微低估阈值边界处可走到的部分边段。

## 7. 地图与指标

`reachable_network_km` 是每站在给定阈值下可达道路边长度的总和。它反映已建图网络的长度，不等于覆盖面积、人口覆盖或服务质量。

地图将可达道路缓冲25米，以便在静态图中可见。该缓冲仅用于制图；缓冲内部的建筑、封闭地块或空间不能因此被视为可达。

## 8. 已得到的结果

| 站点 | 出入口 | 10分钟可达道路 | 15分钟可达道路 |
| --- | ---: | ---: | ---: |
| 沙坪坝 | 5 | 22.75 km | 43.88 km |
| 小龙坎 | 3 | 12.16 km | 29.88 km |
| 马家岩 | 2 | 5.30 km | 10.94 km |
| 高庙村 | 2 | 16.11 km | 29.64 km |

这些差异同时受到道路密度、出入口数量、OSM 完整性和网络拓扑影响，不能直接解释成站点优劣排名。

## 9. 可复现输出

- `data/processed/shapingba_walk_accessibility_mvp.gpkg`
  - `stations`
  - `station_entrances`
  - `walk_network_edges`
  - `service_areas`
- `data/processed/shapingba_accessibility_summary.csv`
- `data/processed/shapingba_walk_accessibility_metadata.json`
- `reports/figures/shapingba_walk_accessibility_mvp.png`

源代码入口为 `src/chongqing_rail_transit/walk_accessibility_mvp.py`。
