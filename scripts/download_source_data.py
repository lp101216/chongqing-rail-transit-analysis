"""Download the exact public source files used or inspected in this project.

Large/raw files are written under work/data_source_survey and intentionally
ignored by Git. Every download is verified against the recorded SHA-256 hash.
"""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
from urllib.request import Request, urlopen


DATASETS = {
    "osm": {
        "filename": "chongqing_osm_2026-10-10.osm.pbf",
        "url": "https://download.geofabrik.de/asia/china/chongqing-261009.osm.pbf",
        "sha256": "01016D53B156CCA02C5E837574648BAB358D40E766514BB90697B31E087BC354",
    },
    "network_map": {
        "filename": "crt_network_map_2024-12.pdf",
        "url": "https://jtj.cq.gov.cn/zwgk_240/zfxxgkml/gggs/tzgg/202503/W020250305556609678968.pdf",
        "sha256": "9B57DAF1CD28E22A77D1C95B40F391E2FFBD53C451465F619C9BCA921430F700",
    },
    "yearbook": {
        "filename": "chongqing_statistical_yearbook_2025.pdf",
        "url": "https://tjj.cq.gov.cn/cslm/tjsjzl/202603/P020260330529346889373.pdf",
        "sha256": "6C7219649073ED7106F654F8B26AFC6205AD94C6DCD06276A3075B5F8EBDA2BD",
    },
    "spatial_plan": {
        "filename": "chongqing_territorial_spatial_plan_2021-2035.pdf",
        "url": "https://ghzrzyj.cq.gov.cn/ztlm_186/ghgb/ztgh/202406/P020250617415109349125.pdf",
        "sha256": "C538F40EB78DAF0AD1096F410A42728EF03309461968A67B81A0A3DA1E75405F",
    },
    "transport_plan_2026_2030": {
        "filename": "chongqing_transport_plan_2026-2030.pdf",
        "url": (
            "https://jtysw.cq.gov.cn/zwgk_240/zfxxgkml/zcwj/qtwj/"
            "202608/W020260820540097135901.pdf"
        ),
        "sha256": "963B83B6FEF1BD552822C95A92B8B23BBE6555326997919AF1DB1144870CB4B4",
    },
    "srtm_n29e106": {
        "filename": "N29E106.hgt.gz",
        "url": "https://s3.amazonaws.com/elevation-tiles-prod/skadi/N29/N29E106.hgt.gz",
        "sha256": "968AB4EBD001029116FB3BA2326CD6D41A456B3556A41E2E6ACE744EAC408439",
    },
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def download(name: str, destination: Path, force: bool = False) -> Path:
    dataset = DATASETS[name]
    path = destination / dataset["filename"]
    destination.mkdir(parents=True, exist_ok=True)
    if path.exists() and not force:
        actual = sha256(path)
        if actual != dataset["sha256"]:
            raise RuntimeError(f"Existing file has unexpected SHA-256: {path}")
        print(f"verified existing {name}: {path}")
        return path

    request = Request(dataset["url"], headers={"User-Agent": "crt-analysis-reproducibility/1.0"})
    temporary = path.with_suffix(path.suffix + ".part")
    with urlopen(request, timeout=120) as response, temporary.open("wb") as output:
        while chunk := response.read(1024 * 1024):
            output.write(chunk)
    actual = sha256(temporary)
    if actual != dataset["sha256"]:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(f"SHA-256 mismatch for {name}: {actual}")
    temporary.replace(path)
    print(f"downloaded {name}: {path}")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "datasets",
        nargs="*",
        choices=DATASETS,
        default=list(DATASETS),
        help="Datasets to download; omit to download all recorded source files",
    )
    parser.add_argument(
        "--destination",
        type=Path,
        default=Path("work/data_source_survey"),
    )
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    for name in args.datasets:
        download(name, args.destination, force=args.force)


if __name__ == "__main__":
    main()
