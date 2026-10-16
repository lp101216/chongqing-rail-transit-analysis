# 数据许可与署名

## OpenStreetMap 派生数据

本仓库中的以下成果由 OpenStreetMap 数据生成：

- `data/processed/shapingba_walk_accessibility_mvp.gpkg`
- `data/processed/shapingba_accessibility_summary.csv`
- `data/processed/shapingba_walk_accessibility_metadata.json`
- `reports/figures/shapingba_walk_accessibility_mvp.png`

数据署名：**© OpenStreetMap contributors**。

OpenStreetMap 数据依据 [Open Data Commons Open Database License（ODbL）](https://opendatacommons.org/licenses/odbl/) 提供。数据和许可说明见 [OpenStreetMap Copyright](https://www.openstreetmap.org/copyright)。本项目使用的重庆提取包由 [Geofabrik](https://download.geofabrik.de/asia/china/chongqing.html) 提供。

GeoPackage 是由 OSM 数据生成的公开派生数据库。再分发或修改该数据库时，应检查并遵守 ODbL 的署名、通知和相同方式共享要求。地图属于 Produced Work，保留上述署名并在仓库中提供本通知。

## 政府资料

政府网站发布的 PDF、统计年鉴和规划原件没有提交到仓库。仓库仅保存来源链接、校验值、少量人工核验事实和研究者生成的结构化证据。公开可访问不等于允许完整再分发；使用者应从原发布页面获取文件并遵守其使用条件。

## SRTM高程数据

坡度分析使用Mapzen Terrain Tiles公开的SRTM HGT瓦片。原始瓦片不提交仓库，派生结果保留以下署名：

`SRTM data courtesy of the U.S. Geological Survey. Terrain Tiles accessed from Mapzen on AWS.`

Mapzen许可说明将SRTM标为公共领域数据，同时要求在发布或展示中给予USGS适当署名。下载地址、日期、校验值和数据限制见`data_sources.md`。

## 项目代码和文档

截至当前版本，项目负责人尚未选择代码和原创文档的开源许可证。除 OSM 派生数据适用的 ODbL 条款外，不应推定仓库其他内容已获得额外开源授权。此项标记为**待项目负责人确认**。
