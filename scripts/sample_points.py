"""Sample every wildfire GeoTIFF at a set of lat/lon points.

Usage
    python sample_points.py points.csv out.csv
    python sample_points.py points.csv out.csv --lat latitude --lon longitude
    python sample_points.py points.csv out.csv --final-only
    python sample_points.py points.csv out.csv --tifs D:/wildfire/tifs --snap-km 3

The answer is in the final_* columns: one set of fields for every point, read from the
rasters of the region the point falls in (Canada = NBAC, California = CAL FIRE FRAP,
rest of the lower 48 = MTBS). final_class_5cat / final_class_label is the final score.
--final-only drops the per-raster columns and keeps just your columns + region + final_*.

Needs: rasterio, pyproj, pandas, numpy.

What it handles for you
1. CRS. The rasters use four projections (EPSG:3978 Canada, EPSG:3310 California,
   EPSG:5070 CONUS, ESRI:102008 North America). Each point is reprojected from WGS84
   lat/lon into each raster's own CRS before sampling.
2. Nodata. On some layers the nodata value is also a real value:
     tslb_*, firstburn_*  -1 = outside land OR no fire in the record
     nburns_*, burn_count, fire_occurrence_*   0 = outside land OR never burned
   A point is judged on or off land from the burn-rate raster of the same region and
   cell size, where -1 only ever means "off the land mask". On land, -1 / 0 are kept
   as real values; off land, the cell is left blank.
3. Coastlines. The land masks use a 1:50M coastline, so points within a km or two of a
   coast or a large lake can land "in the water". A point that falls off the land mask
   is moved to the nearest land cell within --snap-km (default 3 km), and the distance
   moved is written to snap_km_<CRS> (0 = not moved, blank = no land within reach).
"""
import argparse
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import rasterio
from pyproj import Transformer
from rasterio.windows import Window

sys.stdout.reconfigure(encoding="utf-8")

# files whose nodata value doubles as a real "never burned" value
AMBIGUOUS_PREFIXES = ("tslb_", "firstburn_", "nburns_", "burn_count", "fire_occurrence_")

# the finest burn-rate raster in each CRS - used to decide on/off land and to snap
REFERENCE = {
    "EPSG:3978": "risk_rate_1972_2025_1km.tif",
    "EPSG:3310": "ca_risk_rate_1950_2025_1km.tif",
    "EPSG:5070": "risk_rate_conus_1984_2026_1km.tif",
    "ESRI:102008": "na_fire_5km_burn_rate_common.tif",
}

# final_* columns: the same field for every point, read from the raster of the
# region the point falls in (California uses CAL FIRE FRAP, not MTBS)
FINAL_FIELDS = ["final_source", "final_burn_rate_full", "final_burn_rate_recent",
                "final_tslb_yr", "final_times_burned", "final_score_0_100",
                "final_class_5cat", "final_class_label", "final_snap_km"]

REGION_FILES = {
    "Canada": {
        "source": "NBAC 1972-2025",
        "snap": "snap_km_canada",
        "final_burn_rate_full": "risk_rate_1972_2025_1km",
        "final_burn_rate_recent": "risk_rate_2021_2025_1km",
        "final_tslb_yr": "tslb_canada_1km",
        "final_times_burned": "nburns_canada_1km",
        "final_score_0_100": "wildfire_risk_score_canada_1km",
        "final_class_5cat": "risk_class_5cat_canada_1km",
    },
    "California": {
        "source": "CAL FIRE FRAP 1878-2025",
        "snap": "snap_km_california",
        "final_burn_rate_full": "ca_risk_rate_1950_2025_1km",
        "final_burn_rate_recent": "ca_risk_rate_2021_2025_1km",
        "final_tslb_yr": "tslb_california_1km",
        "final_times_burned": "nburns_california_1km",
        "final_score_0_100": "wildfire_risk_score_california_1km",
        "final_class_5cat": "risk_class_5cat_california_1km",
    },
    "CONUS": {
        "source": "MTBS 1984-2026",
        "snap": "snap_km_conus",
        "final_burn_rate_full": "risk_rate_conus_1984_2026_1km",
        "final_burn_rate_recent": "risk_rate_conus_2020_2024_1km",
        "final_tslb_yr": "tslb_conus_1km",
        "final_times_burned": "nburns_conus_1km",
        "final_score_0_100": "wildfire_risk_score_conus_1km",
        "final_class_5cat": "risk_class_5cat_conus_1km",
    },
}

CLASS_LABELS = {0: "No fire in record", 1: "Very low", 2: "Low", 3: "Moderate",
                4: "High", 5: "Very high"}

SNAP_LABEL = {
    "EPSG:3978": "snap_km_canada",
    "EPSG:3310": "snap_km_california",
    "EPSG:5070": "snap_km_conus",
    "ESRI:102008": "snap_km_na",
}


def is_burn_rate(name):
    if name.startswith("na_fire_"):
        return "burn_rate" in name
    if name.startswith("risk_rate_"):
        return True
    if name.startswith("ca_risk_rate_"):
        return True
    return False


def snap_points(ref_path, lons, lats, snap_km):
    """Projected x, y per point, moved onto the nearest land cell when needed."""
    with rasterio.open(ref_path) as src:
        transformer = Transformer.from_crs("EPSG:4326", src.crs, always_xy=True)
        xs, ys = transformer.transform(lons, lats)
        reach = int(math.ceil(snap_km * 1000 / src.res[0]))
        out_x = []
        out_y = []
        moved = []
        for x, y in zip(xs, ys):
            row, col = src.index(x, y)
            win = Window(col - reach, row - reach, 2 * reach + 1, 2 * reach + 1)
            block = src.read(1, window=win, boundless=True, fill_value=-1)
            centre = block[reach, reach]
            if centre >= 0:
                out_x.append(x)
                out_y.append(y)
                moved.append(0.0)
                continue
            land_r, land_c = np.nonzero(block >= 0)
            if land_r.size == 0:
                out_x.append(x)
                out_y.append(y)
                moved.append(None)
                continue
            dist = np.hypot(land_r - reach, land_c - reach)
            k = int(np.argmin(dist))
            new_row = row - reach + int(land_r[k])
            new_col = col - reach + int(land_c[k])
            nx, ny = src.xy(new_row, new_col)
            out_x.append(nx)
            out_y.append(ny)
            moved.append(round(math.hypot(nx - x, ny - y) / 1000, 2))
    return out_x, out_y, moved


def sample_at(path, xs, ys):
    """Raw values at projected points; points outside the raster get nodata."""
    with rasterio.open(path) as src:
        coords = list(zip(xs, ys))
        values = []
        for v in src.sample(coords, masked=False):
            values.append(v[0].item())
        return values, src.nodata


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("points")
    ap.add_argument("out")
    ap.add_argument("--lat", default="lat")
    ap.add_argument("--lon", default="lon")
    default_tifs = Path(__file__).resolve().parent.parent / "tifs"
    ap.add_argument("--tifs", default=str(default_tifs))
    ap.add_argument("--snap-km", type=float, default=3.0)
    ap.add_argument("--final-only", action="store_true",
                    help="write only your columns + region + the final_* columns")
    args = ap.parse_args()

    pts = pd.read_csv(args.points)
    lons = pts[args.lon].astype(float).tolist()
    lats = pts[args.lat].astype(float).tolist()
    tif_dir = Path(args.tifs)
    tifs = sorted(tif_dir.glob("*.tif"))
    print(f"{len(pts):,} points x {len(tifs)} rasters (snap within {args.snap_km:g} km)")

    # group rasters by CRS, and snap the points once per CRS
    by_crs = {}
    for p in tifs:
        with rasterio.open(p) as src:
            crs = src.crs.to_string()
            res = round(src.res[0])
        if crs not in by_crs:
            by_crs[crs] = []
        by_crs[crs].append((p, res))

    out = pts.copy()
    snap_cols = {}
    for crs, layers in by_crs.items():
        ref = tif_dir / REFERENCE[crs]
        if not ref.exists():
            print(f"  skipping {crs}: its land-mask raster {ref.name} is not in {tif_dir}")
            continue
        xs, ys, moved = snap_points(ref, lons, lats, args.snap_km)
        snap_cols[SNAP_LABEL[crs]] = moved

        # land masks from this CRS's burn-rate rasters, keyed by cell size
        raw = {}
        land = {}
        for p, res in layers:
            values, nodata = sample_at(p, xs, ys)
            raw[p.stem] = (values, nodata, res)
            if is_burn_rate(p.name):
                mask = []
                for v in values:
                    mask.append(v >= 0)
                land[res] = mask

        for p, res in layers:
            values, nodata, res = raw[p.stem]
            cleaned = []
            if p.name.startswith(AMBIGUOUS_PREFIXES):
                mask = land.get(res)
                if mask is None:
                    mask = land[min(land)]
                for v, on_land in zip(values, mask):
                    if on_land:
                        cleaned.append(v)
                    else:
                        cleaned.append(None)
            else:
                for v in values:
                    if nodata is not None and v == nodata:
                        cleaned.append(None)
                    else:
                        cleaned.append(v)
            out[p.stem] = cleaned

    # which regional set each point falls in (California has its own FRAP set)
    # (works with any subset of the files, e.g. only the Canada bundle unzipped)
    region = []
    ca = out.get("ca_risk_rate_1950_2025_1km")
    cn = out.get("risk_rate_1972_2025_1km")
    us = out.get("risk_rate_conus_1984_2026_1km")
    for i in range(len(out)):
        if ca is not None and pd.notna(ca.iloc[i]):
            region.append("California")
        elif cn is not None and pd.notna(cn.iloc[i]):
            region.append("Canada")
        elif us is not None and pd.notna(us.iloc[i]):
            region.append("CONUS")
        else:
            region.append("outside")

    # final_* columns: the same fields for every point, taken from its own region's rasters
    final = {}
    for name in FINAL_FIELDS:
        final[name] = []
    for i in range(len(out)):
        files = REGION_FILES.get(region[i])
        for name in FINAL_FIELDS:
            if files is None:
                final[name].append(None)
            elif name == "final_source":
                final[name].append(files["source"])
            elif name == "final_snap_km":
                moved = snap_cols.get(files["snap"])
                if moved is None:
                    final[name].append(None)
                else:
                    final[name].append(moved[i])
            elif name == "final_class_label":
                code = final["final_class_5cat"][i]
                if code is None or pd.isna(code):
                    final[name].append(None)
                else:
                    final[name].append(CLASS_LABELS[int(code)])
            else:
                col = out.get(files[name])
                if col is None:
                    final[name].append(None)
                else:
                    final[name].append(col.iloc[i])

    pos = len(pts.columns)
    out.insert(pos, "region", region)
    for name in FINAL_FIELDS:
        pos = pos + 1
        out.insert(pos, name, final[name])
    # one snap column per region whose rasters are present
    for name in ["snap_km_canada", "snap_km_california", "snap_km_conus", "snap_km_na"]:
        if name in snap_cols:
            pos = pos + 1
            out.insert(pos, name, snap_cols[name])

    if args.final_only:
        keep = list(pts.columns) + ["region"] + FINAL_FIELDS
        out = out[keep]

    out.to_csv(args.out, index=False)
    print(f"wrote {args.out} ({len(out.columns)} columns)")
    print(out["region"].value_counts().to_string())
    print(out["final_class_label"].value_counts(dropna=False).to_string())


if __name__ == "__main__":
    main()
