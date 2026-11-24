"""Build the 5-category fire-history class from times burned + time since last burn.

    python make_risk_class_5cat.py            (reads and writes ../tifs)

Per burned 1 km cell:
    frequency points  times burned   1 -> 1, 2 -> 2, 3-4 -> 3, 5-7 -> 4, 8+ -> 5
    recency points    time since     40+ yr -> 1, 20-39 -> 2, 10-19 -> 3, 5-9 -> 4, 0-4 -> 5
    total = frequency + recency      (2 to 10)
    class 1 Very low  (2-3)
          2 Low       (4-5)
          3 Moderate  (6-7)
          4 High      (8)
          5 Very high (9-10)

Codes in the output raster:
    1-5 = class, 0 = on land but no fire in the record, 255 = off land.
Never-burned land is its own code, not class 1: an unburned cell is unknown, not low.
"""
import sys
from pathlib import Path

import numpy as np
import rasterio

sys.stdout.reconfigure(encoding="utf-8")

TIFS = Path(__file__).resolve().parent.parent / "tifs"

# region -> (times burned, time since last burn, burn-rate raster that defines land)
REGIONS = {
    "canada": ("nburns_canada_1km.tif", "tslb_canada_1km.tif", "risk_rate_1972_2025_1km.tif"),
    "california": ("nburns_california_1km.tif", "tslb_california_1km.tif",
                   "ca_risk_rate_1950_2025_1km.tif"),
    "conus": ("nburns_conus_1km.tif", "tslb_conus_1km.tif", "risk_rate_conus_1984_2026_1km.tif"),
}
NAMES = ["no fire", "Very low", "Low", "Moderate", "High", "Very high"]


def frequency_points(nburns):
    pts = np.zeros(nburns.shape, dtype=np.int32)
    pts[nburns >= 1] = 1
    pts[nburns >= 2] = 2
    pts[nburns >= 3] = 3
    pts[nburns >= 5] = 4
    pts[nburns >= 8] = 5
    return pts


def recency_points(tslb):
    pts = np.zeros(tslb.shape, dtype=np.int32)
    burned = tslb >= 0
    pts[burned] = 1
    pts[burned & (tslb < 40)] = 2
    pts[burned & (tslb < 20)] = 3
    pts[burned & (tslb < 10)] = 4
    pts[burned & (tslb < 5)] = 5
    return pts


for region, (nb_file, tslb_file, land_file) in REGIONS.items():
    with rasterio.open(TIFS / nb_file) as src:
        nburns = src.read(1).astype(np.int32)
        profile = src.profile
    with rasterio.open(TIFS / tslb_file) as src:
        tslb = src.read(1).astype(np.int32)
    with rasterio.open(TIFS / land_file) as src:
        land = src.read(1) >= 0

    burned = land & (nburns >= 1) & (tslb >= 0)
    total = frequency_points(nburns) + recency_points(tslb)

    cls = np.full(nburns.shape, 255, dtype=np.uint8)
    cls[land] = 0
    cls[burned & (total <= 3)] = 1
    cls[burned & (total >= 4) & (total <= 5)] = 2
    cls[burned & (total >= 6) & (total <= 7)] = 3
    cls[burned & (total == 8)] = 4
    cls[burned & (total >= 9)] = 5

    profile.update(dtype="uint8", nodata=255, compress="lzw")
    out = TIFS / f"risk_class_5cat_{region}_1km.tif"
    with rasterio.open(out, "w", **profile) as dst:
        dst.write(cls, 1)

    n_land = int(land.sum())
    parts = []
    for code in range(6):
        share = (cls == code).sum() / n_land * 100
        parts.append(f"{NAMES[code]} {share:.2f}%")
    print(f"{out.name}: " + " | ".join(parts))
