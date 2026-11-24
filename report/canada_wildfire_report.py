# =====================================================================
# CANADA WILDFIRE-HISTORY REPORT FOR A SET OF LOCATIONS
# https://github.com/drdww/wildfire-geotiffs   (report/canada_wildfire_report.py)
#
# One cell. Edit the four paths below, then run it.
#
# Produces, in OUT_DIR:
#   canada_wildfire_report.pdf   16:9 pages, one per metric:
#                                A = the raster as a map, B = your locations on top,
#                                C = distribution (your locations vs all Canadian land),
#                                plus the formula behind the metric
#   pages/page_XX.png            the same pages as PNGs, ready for slides
#   locations_wildfire.xlsx      your rows + the sampled wf_* columns
#
# Needs: numpy, pandas, rasterio, pyproj, scipy, matplotlib, openpyxl
#        (geopandas optional: only used for province outlines)
# =====================================================================

# ---------------------------------------------------------------------
# EDIT THESE
# ---------------------------------------------------------------------
BASE_DIR = "/Workspace/Users/<you>/CanadaWildfireExposure"
POINTS_FILE = BASE_DIR + "/my_locations.xlsx"          # .xlsx or .csv
TIF_DIR = BASE_DIR + "/tifs"                           # the 6 Canada rasters
OUT_DIR = BASE_DIR + "/wildfire_report"

# Column names. None = detect automatically (Latitude / Longitude / CountryCode /
# LOCID / LOCNAME / CityName / Admin1Name and common variants).
LAT_COL = None
LON_COL = None
COUNTRY_COL = None        # rows whose country is CA / CAN / Canada are kept
ID_COL = None
NAME_COL = None
CITY_COL = None
PROVINCE_COL = None
VALUE_COL = None          # optional insured-value column, e.g. "TIV"

SNAP_KM = 3               # move points that fall "in the water" to land within this distance
WATCH_SCORE = 10          # watchlist: no fire in the 1 km cell, smoothed score >= this
REPORT_TITLE = "Wildfire history at Canadian locations"

# ---------------------------------------------------------------------
# IMPORTS
# ---------------------------------------------------------------------
import datetime
import math
import os
import tempfile
import urllib.request

import numpy as np
import pandas as pd
import rasterio
from pyproj import Transformer
from scipy import ndimage

import matplotlib.pyplot as plt
from matplotlib import colors as mcolors
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.patches import Patch
from matplotlib.patches import FancyBboxPatch
from matplotlib.patches import Rectangle

plt.rcParams["font.family"] = "DejaVu Sans"
plt.rcParams["axes.spines.top"] = False
plt.rcParams["axes.spines.right"] = False

RASTERS = {
    "rate_full": "risk_rate_1972_2025_1km.tif",
    "rate_recent": "risk_rate_2021_2025_1km.tif",
    "nburns": "nburns_canada_1km.tif",
    "tslb": "tslb_canada_1km.tif",
    "score": "wildfire_risk_score_canada_1km.tif",
    "cls": "risk_class_5cat_canada_1km.tif",
}
BOUNDARY_URL = ("https://raw.githubusercontent.com/drdww/wildfire-geotiffs/"
                "main/boundaries/na_admin1.geojson")

N_YEARS = 54              # 1972-2025
OCEAN = "#dbe6ee"
NEIGHBOUR = "#eeeeee"
NEUTRAL = "#e2ddd2"       # "no fire" / "no signal" category
INK = "#1f2a33"
MUTED = "#5b6770"
SOURCE_LINE = ("Source: NBAC National Burned Area Composite 1972-2025 (Natural Resources Canada)"
               " | 1 km grid | EPSG:3978 NAD83 / Canada Atlas Lambert"
               " | github.com/drdww/wildfire-geotiffs")


# ---------------------------------------------------------------------
# SMALL HELPERS
# ---------------------------------------------------------------------
def find_column(df, chosen, candidates):
    """Return the column the user named, or the first candidate present (any case)."""
    if chosen is not None:
        if chosen not in df.columns:
            raise KeyError(f"column {chosen!r} not in the file; columns are {list(df.columns)}")
        return chosen
    lookup = {}
    for col in df.columns:
        lookup[str(col).strip().lower()] = col
    for cand in candidates:
        if cand.lower() in lookup:
            return lookup[cand.lower()]
    return None


def ramp(n):
    """n colours from pale yellow to dark brown (ordered, colour-blind safe)."""
    cmap = plt.get_cmap("YlOrBr")
    out = []
    for t in np.linspace(0.3, 0.95, n):
        out.append(mcolors.to_hex(cmap(t)))
    return out


def pct(part, whole):
    if whole == 0:
        return 0.0
    return 100.0 * part / whole


def fmt_int(n):
    return f"{int(n):,}"


# ---------------------------------------------------------------------
# 1. LOAD THE POINTS
# ---------------------------------------------------------------------
os.makedirs(OUT_DIR, exist_ok=True)
os.makedirs(os.path.join(OUT_DIR, "pages"), exist_ok=True)

if POINTS_FILE.lower().endswith((".xlsx", ".xlsm", ".xls")):
    raw = pd.read_excel(POINTS_FILE)
else:
    raw = pd.read_csv(POINTS_FILE)
print(f"rows in file            : {len(raw):,}")

lat_col = find_column(raw, LAT_COL, ["Latitude", "lat", "lat_dd", "y"])
lon_col = find_column(raw, LON_COL, ["Longitude", "lon", "long", "lng", "lon_dd", "x"])
if lat_col is None or lon_col is None:
    raise KeyError(f"could not find latitude/longitude columns in {list(raw.columns)}; "
                   "set LAT_COL and LON_COL")
country_col = find_column(raw, COUNTRY_COL, ["CountryCode", "country_code", "country", "cntry"])
id_col = find_column(raw, ID_COL, ["LOCID", "location_id", "loc_id", "id"])
name_col = find_column(raw, NAME_COL, ["LOCNAME", "location_name", "name"])
city_col = find_column(raw, CITY_COL, ["CityName", "city"])
prov_col = find_column(raw, PROVINCE_COL, ["Admin1Name", "province", "prov", "state", "admin1"])
value_col = find_column(raw, VALUE_COL, [])
print(f"lat / lon columns       : {lat_col} / {lon_col}")

pts = raw.copy()
if country_col is not None:
    codes = pts[country_col].astype(str).str.strip().str.upper()
    pts = pts[codes.isin(["CA", "CAN", "CANADA"])].copy()
    print(f"Canadian rows ({country_col}) : {len(pts):,}")
pts[lat_col] = pd.to_numeric(pts[lat_col], errors="coerce")
pts[lon_col] = pd.to_numeric(pts[lon_col], errors="coerce")
bad = pts[lat_col].isna() | pts[lon_col].isna()
n_bad_coords = int(bad.sum())
pts = pts[~bad].reset_index(drop=True)
print(f"rows with coordinates   : {len(pts):,}  (dropped {n_bad_coords:,} without lat/lon)")
n_pts = len(pts)

# ---------------------------------------------------------------------
# 2. LOAD THE RASTERS (+ distance to the nearest burned cell)
# ---------------------------------------------------------------------
grids = {}
for key, fname in RASTERS.items():
    path = os.path.join(TIF_DIR, fname)
    with rasterio.open(path) as src:
        grids[key] = {
            "data": src.read(1),
            "transform": src.transform,
            "bounds": src.bounds,
            "crs": src.crs,
        }
    print(f"read {fname:40s} {grids[key]['data'].shape}")

main = grids["rate_full"]
# Hand pyproj / geopandas a plain "EPSG:3978" string, not rasterio's CRS object: on some
# platforms (Databricks) pyproj cannot build a transformer from the rasterio object.
MAP_CRS = "EPSG:3978"
if main["crs"].to_epsg() not in (None, 3978):
    MAP_CRS = main["crs"].to_string()
# EPSG:3978 written out in full, used if PROJ cannot look the EPSG code up
MAP_CRS_PROJ4 = ("+proj=lcc +lat_0=49 +lon_0=-95 +lat_1=49 +lat_2=77 +x_0=0 +y_0=0 "
                 "+ellps=GRS80 +towgs84=0,0,0,0,0,0,0 +units=m +no_defs")


def lonlat_to_map():
    """WGS84 lon/lat -> map CRS, with a fallback that needs no PROJ database lookup."""
    try:
        return Transformer.from_crs("EPSG:4326", MAP_CRS, always_xy=True)
    except Exception as err:
        print(f"note: EPSG lookup failed ({err}); using the PROJ string for EPSG:3978")
        return Transformer.from_crs("+proj=longlat +datum=WGS84 +no_defs", MAP_CRS_PROJ4, always_xy=True)
res = main["transform"].a
x0 = main["transform"].c
y0 = main["transform"].f
land = main["data"] >= 0
n_rows, n_cols = land.shape
for key in ["rate_recent", "nburns", "tslb", "cls"]:
    if grids[key]["data"].shape != land.shape:
        raise ValueError(f"{RASTERS[key]} is not on the same grid as {RASTERS['rate_full']}")

burned_cell = land & (grids["nburns"]["data"] > 0)
print("computing distance to the nearest burned cell ...")
dist_km = ndimage.distance_transform_edt(~burned_cell).astype(np.float32) * (res / 1000.0)
# the score raster stops short of the high Arctic: paste it onto the main grid
sg = grids["score"]
col_off = (sg["transform"].c - x0) / res
row_off = (y0 - sg["transform"].f) / res
if abs(col_off - round(col_off)) > 1e-6 or abs(row_off - round(row_off)) > 1e-6:
    raise ValueError("score raster is not aligned with the 1 km grid")
col_off = int(round(col_off))
row_off = int(round(row_off))
score_main = np.full(land.shape, 255, dtype=np.uint8)
src_r0 = max(0, -row_off)
src_c0 = max(0, -col_off)
dst_r0 = max(0, row_off)
dst_c0 = max(0, col_off)
h = min(sg["data"].shape[0] - src_r0, n_rows - dst_r0)
w = min(sg["data"].shape[1] - src_c0, n_cols - dst_c0)
score_main[dst_r0:dst_r0 + h, dst_c0:dst_c0 + w] = sg["data"][src_r0:src_r0 + h, src_c0:src_c0 + w]
score_land = land & (score_main != 255)


def grid_extent(g):
    b = g["bounds"]
    return (b.left, b.right, b.bottom, b.top)


MAIN_EXTENT = grid_extent(main)

# ---------------------------------------------------------------------
# 3. SAMPLE: reproject, snap waterfront points to land, read every layer
# ---------------------------------------------------------------------
to_lcc = lonlat_to_map()
px, py = to_lcc.transform(pts[lon_col].to_numpy(), pts[lat_col].to_numpy())
px = np.asarray(px, dtype=float)
py = np.asarray(py, dtype=float)
cols = np.floor((px - x0) / res).astype(int)
rows = np.floor((y0 - py) / res).astype(int)

reach = int(math.ceil(SNAP_KM * 1000 / res))
off_r, off_c = np.mgrid[-reach:reach + 1, -reach:reach + 1]
off_d = np.hypot(off_r, off_c).ravel()
off_r = off_r.ravel()
off_c = off_c.ravel()
inside_circle = off_d <= reach
order = np.argsort(off_d[inside_circle], kind="stable")
off_r = off_r[inside_circle][order]
off_c = off_c[inside_circle][order]

matched = np.zeros(n_pts, dtype=bool)
snap_km = np.full(n_pts, np.nan)
cell_r = rows.copy()
cell_c = cols.copy()
for i in range(n_pts):
    for k in range(len(off_r)):
        r = rows[i] + off_r[k]
        c = cols[i] + off_c[k]
        if r < 0 or c < 0 or r >= n_rows or c >= n_cols:
            continue
        if land[r, c]:
            matched[i] = True
            cell_r[i] = r
            cell_c[i] = c
            if k == 0:
                snap_km[i] = 0.0
            else:
                cx = x0 + (c + 0.5) * res
                cy = y0 - (r + 0.5) * res
                snap_km[i] = round(math.hypot(cx - px[i], cy - py[i]) / 1000.0, 2)
            break

cell_x = x0 + (cell_c + 0.5) * res
cell_y = y0 - (cell_r + 0.5) * res


def sample_main(arr):
    out = np.full(n_pts, np.nan)
    out[matched] = arr[cell_r[matched], cell_c[matched]]
    return out


score_vals = sample_main(score_main)
score_vals[score_vals == 255] = np.nan

vals = {
    "cls": sample_main(grids["cls"]["data"]),
    "score": score_vals,
    "dist": sample_main(dist_km),
    "nburns": sample_main(grids["nburns"]["data"]),
    "tslb": sample_main(grids["tslb"]["data"]),
    "rate_full": sample_main(grids["rate_full"]["data"]),
    "rate_recent": sample_main(grids["rate_recent"]["data"]),
}
vals["cls"][vals["cls"] == 255] = np.nan

CLASS_LABELS = {0: "No fire in record", 1: "Very low", 2: "Low", 3: "Moderate",
                4: "High", 5: "Very high"}
watch = matched & (vals["cls"] == 0) & (vals["score"] >= WATCH_SCORE)

pts["wf_matched"] = matched
pts["wf_snap_km"] = snap_km
pts["wf_class_5cat"] = vals["cls"]
labels = []
for v in vals["cls"]:
    if np.isnan(v):
        labels.append(None)
    else:
        labels.append(CLASS_LABELS[int(v)])
pts["wf_class_label"] = labels
pts["wf_score_0_100"] = vals["score"]
pts["wf_km_to_fire"] = np.round(vals["dist"], 1)
pts["wf_times_burned"] = vals["nburns"]
pts["wf_burn_rate_1972_2025"] = np.round(vals["rate_full"], 4)
pts["wf_burn_rate_2021_2025"] = vals["rate_recent"]
pts["wf_tslb_yr"] = vals["tslb"]
last_year = np.where(vals["tslb"] >= 0, 2025 - vals["tslb"], np.nan)
pts["wf_last_burn_year"] = last_year
pts["wf_watchlist"] = watch

n_matched = int(matched.sum())
n_snapped = int((snap_km > 0).sum())
print(f"matched to land         : {n_matched:,}  ({n_snapped:,} moved onto land, "
      f"{n_pts - n_matched:,} with no land within {SNAP_KM:g} km)")

# ---------------------------------------------------------------------
# 4. PROVINCE OUTLINES (local file -> GitHub -> land-mask outline)
# ---------------------------------------------------------------------
admin = None
try:
    import geopandas as gpd
    candidates = [
        os.path.join(TIF_DIR, "na_admin1.geojson"),
        os.path.join(os.path.dirname(TIF_DIR.rstrip("/\\")), "boundaries", "na_admin1.geojson"),
        os.path.join(BASE_DIR, "na_admin1.geojson"),
    ]
    path = None
    for cand in candidates:
        if os.path.exists(cand):
            path = cand
            break
    if path is None:
        path = os.path.join(tempfile.gettempdir(), "na_admin1.geojson")
        urllib.request.urlretrieve(BOUNDARY_URL, path)
    admin = gpd.read_file(path).to_crs(MAP_CRS)
    print(f"province outlines       : {path}")
except Exception as err:
    print(f"province outlines unavailable ({err}); drawing the land-mask outline instead")
    admin = None

if prov_col is None and admin is not None:
    pt_gdf = gpd.GeoDataFrame(geometry=gpd.points_from_xy(cell_x, cell_y), crs=MAP_CRS)
    joined = gpd.sjoin(pt_gdf, admin[admin["country"] == "CAN"][["name", "geometry"]],
                       how="left", predicate="within")
    joined = joined[~joined.index.duplicated(keep="first")]
    pts["wf_province"] = joined["name"].to_numpy()
    prov_col = "wf_province"

# graticule lines in the map CRS
to_lcc_lines = lonlat_to_map()
GRATICULE = []
for lat in [45, 50, 55, 60, 65, 70, 75, 80]:
    lons = np.linspace(-145, -45, 200)
    gx, gy = to_lcc_lines.transform(lons, np.full(lons.shape, lat))
    GRATICULE.append((np.asarray(gx), np.asarray(gy)))
for lon in range(-140, -49, 10):
    lats = np.linspace(40, 85, 200)
    gx, gy = to_lcc_lines.transform(np.full(lats.shape, lon), lats)
    GRATICULE.append((np.asarray(gx), np.asarray(gy)))

# Canada's land extent, a little padded
land_rows = np.nonzero(land.any(axis=1))[0]
land_cols = np.nonzero(land.any(axis=0))[0]
CANADA_VIEW = (
    x0 + land_cols[0] * res - 60000,
    x0 + (land_cols[-1] + 1) * res + 60000,
    y0 - (land_rows[-1] + 1) * res - 60000,
    y0 - land_rows[0] * res + 60000,
)
LAND_SMALL = land[::8, ::8]
CANADA_ASPECT = (CANADA_VIEW[1] - CANADA_VIEW[0]) / (CANADA_VIEW[3] - CANADA_VIEW[2])


# ---------------------------------------------------------------------
# 5. METRICS: how each one is binned for colour, and its formula
# ---------------------------------------------------------------------
def bin_class(v, ok):
    codes = np.full(v.shape, -1, dtype=np.int8)
    codes[ok] = np.clip(np.nan_to_num(v[ok]), 0, 5).astype(np.int8)
    return codes


def bin_score(v, ok):
    codes = np.full(v.shape, -1, dtype=np.int8)
    codes[ok] = 0
    codes[ok & (v >= 1)] = 1
    codes[ok & (v >= 10)] = 2
    codes[ok & (v >= 25)] = 3
    codes[ok & (v >= 50)] = 4
    return codes


def bin_dist(v, ok):
    codes = np.full(v.shape, -1, dtype=np.int8)
    codes[ok] = 0
    codes[ok & (v <= 25)] = 1
    codes[ok & (v <= 10)] = 2
    codes[ok & (v <= 5)] = 3
    codes[ok & (v <= 2)] = 4
    codes[ok & (v == 0)] = 5
    return codes


def bin_nburns(v, ok):
    codes = np.full(v.shape, -1, dtype=np.int8)
    codes[ok] = 0
    codes[ok & (v >= 1)] = 1
    codes[ok & (v >= 2)] = 2
    codes[ok & (v >= 3)] = 3
    codes[ok & (v >= 5)] = 4
    codes[ok & (v >= 8)] = 5
    return codes


def bin_tslb(v, ok):
    codes = np.full(v.shape, -1, dtype=np.int8)
    codes[ok] = 0
    hit = ok & (v >= 0)
    codes[hit] = 1
    codes[hit & (v < 40)] = 2
    codes[hit & (v < 20)] = 3
    codes[hit & (v < 10)] = 4
    codes[hit & (v < 5)] = 5
    return codes


def bin_recent(v, ok):
    codes = np.full(v.shape, -1, dtype=np.int8)
    years = np.round(np.nan_to_num(v) * 5)
    codes[ok] = 0
    codes[ok & (years >= 1)] = 1
    codes[ok & (years >= 2)] = 2
    codes[ok & (years >= 3)] = 3
    return codes


METRICS = [
    {
        "key": "cls",
        "title": "Fire-history class (the final score)",
        "grid": grids["cls"]["data"], "grid_ok": land, "extent": MAIN_EXTENT,
        "binner": bin_class,
        "labels": ["No fire in record", "Very low", "Low", "Moderate", "High", "Very high"],
        "colors": [NEUTRAL] + ramp(5),
        "formula": [
            "Frequency points from times burned:   1 → 1    2 → 2    3–4 → 3    5–7 → 4    8+ → 5",
            "Recency points from years since last burn:   40+ → 1    20–39 → 2    10–19 → 3    5–9 → 4    0–4 → 5",
            "Total = frequency + recency (2–10):   2–3 Very low   ·   4–5 Low   ·   6–7 Moderate   ·   8 High   ·   9–10 Very high",
        ],
        "read": [
            "Built only from the 1 km cell the location sits in.",
            "'No fire in record' is its own category: unknown, not safe.",
            "Fire history, not a hazard forecast.",
        ],
    },
    {
        "key": "score",
        "title": "Smoothed risk score (neighbourhood fire intensity, 0–100)",
        "grid": score_main, "grid_ok": score_land, "extent": MAIN_EXTENT,
        "binner": bin_score,
        "labels": ["0", "1–9", "10–24", "25–49", "50–100"],
        "colors": [NEUTRAL] + ramp(4),
        "formula": [
            r"$w_y = \mathrm{rank}(y)\,/\,54$  (1972 = 1/54 … 2025 = 1)        "
            r"$\mathrm{freq} = \sum_y B_y\,/\,54$        "
            r"$\mathrm{recency} = \sum_y w_y B_y\,/\,\sum_y w_y$",
            r"$\mathrm{raw} = 0.5\cdot\mathrm{freq} + 0.5\cdot\mathrm{recency}$        "
            r"$\mathrm{score} = \mathrm{round}(100\cdot G_{\sigma = 15\,\mathrm{km}}(\mathrm{raw})\,/\,\mathrm{max})$",
            r"$B_y = 1$ if any NBAC perimeter from year $y$ touches the 1 km cell (all-touched), else 0;"
            r"  $G$ = Gaussian blur, so burn history spreads 15–45 km into unburned neighbours.",
        ],
        "read": [
            "Picks up locations next to fire country even when",
            "their own cell never burned (see the watchlist page).",
            "Relative: 100 = Canada's most fire-intense neighbourhood.",
        ],
    },
    {
        "key": "dist",
        "title": "Distance to the nearest mapped fire, 1972–2025",
        "grid": dist_km, "grid_ok": land, "extent": MAIN_EXTENT,
        "binner": bin_dist,
        "labels": ["> 25 km", "10–25 km", "5–10 km", "2–5 km", "≤ 2 km", "Cell burned"],
        "colors": [NEUTRAL] + ramp(5),
        "formula": [
            r"$d$ = distance from the location's 1 km cell to the nearest cell with $\sum_y B_y \geq 1$",
            "Exact Euclidean distance transform on the 1 km grid (cell centre to cell centre), "
            "computed in this script from nburns_canada_1km.",
            "0 = the cell itself burned at least once.",
        ],
        "read": [
            "The plainest neighbourhood measure:",
            "no weighting, no smoothing, just kilometres.",
            "Most towns are a few km from the nearest perimeter.",
        ],
    },
    {
        "key": "nburns",
        "title": "Times burned, 1972–2025 (how often)",
        "grid": grids["nburns"]["data"], "grid_ok": land, "extent": MAIN_EXTENT,
        "binner": bin_nburns,
        "labels": ["Never", "1 (rate 0.02)", "2 (0.04)", "3–4 (0.06–0.07)", "5–7 (0.09–0.13)", "8+ (≥ 0.15)"],
        "colors": [NEUTRAL] + ramp(5),
        "formula": [
            r"$\mathrm{times\ burned} = \sum_{y=1972}^{2025} B_y$        "
            r"$\mathrm{burn\ rate} = \mathrm{times\ burned}\,/\,54$",
            "The bins are the frequency points of the fire-history class (1, 2, 3–4, 5–7, 8+).",
            "Counts years with fire, not fires: two fires in one year count once.",
        ],
        "read": [
            "Burn rate = share of years with fire;",
            "0.02 ≈ once in 54 years.",
            "Repeat burns mark fire-prone landscapes.",
        ],
    },
    {
        "key": "tslb",
        "title": "Time since last burn (how recently)",
        "grid": grids["tslb"]["data"], "grid_ok": land, "extent": MAIN_EXTENT,
        "binner": bin_tslb,
        "labels": ["No fire in record", "40+ yr (≤ 1985)", "20–39 yr (1986–2005)",
                   "10–19 yr (2006–2015)", "5–9 yr (2016–2020)", "0–4 yr (2021–2025)"],
        "colors": [NEUTRAL] + ramp(5),
        "formula": [
            r"$\mathrm{TSLB} = 2025 - \mathrm{max}\{\,y : B_y = 1\,\}$",
            "The bins are the recency points of the fire-history class (40+, 20–39, 10–19, 5–9, 0–4).",
            "No fire in record = censored: at least 54 years, true value unknown.",
        ],
        "read": [
            "Recent burns raise the class.",
            "Freshly burned ground can carry less fuel",
            "for a few years: history is not hazard.",
        ],
    },
    {
        "key": "rate_recent",
        "title": "Recent fire, 2021–2025",
        "grid": grids["rate_recent"]["data"], "grid_ok": land, "extent": MAIN_EXTENT,
        "binner": bin_recent,
        "labels": ["None", "1 of 5 years", "2 of 5 years", "3+ of 5 years"],
        "colors": [NEUTRAL] + ramp(3),
        "formula": [
            r"$\mathrm{recent\ rate} = \sum_{y=2021}^{2025} B_y\,/\,5$     → 0, 0.2, 0.4, … 1.0",
            "2023 was Canada's largest fire year on record (~15 Mha burned), so this window is unusually active.",
        ],
        "read": [
            "Shows where the last five seasons burned,",
            "including the 2023 and 2024 megafire years.",
            "",
        ],
    },
]


def point_codes(m):
    v = vals[m["key"]]
    ok = matched & ~np.isnan(v)
    return m["binner"](np.nan_to_num(v, nan=-9999.0), ok), ok


def land_shares(m):
    print(f"  land shares: {m['key']}")
    codes = m["binner"](m["grid"], m["grid_ok"])
    counts = np.bincount(codes[codes >= 0].astype(np.int64), minlength=len(m["labels"]))
    return codes, 100.0 * counts / counts.sum()


# ---------------------------------------------------------------------
# 6. DRAWING HELPERS
# ---------------------------------------------------------------------
def nice_length(span_m):
    target = span_m / 5000.0
    best = 50
    for cand in [10, 25, 50, 100, 200, 250, 500, 1000]:
        if cand <= target:
            best = cand
    return best


def draw_scalebar(ax, view):
    left, right, bottom, top = view
    km = nice_length(right - left)
    seg = km * 1000 / 2
    bx = right - 0.05 * (right - left) - 2 * seg
    by = bottom + 0.05 * (top - bottom)
    h = 0.012 * (top - bottom)
    ax.add_patch(Rectangle((bx, by), seg, h, facecolor=INK, edgecolor=INK, lw=0.6, zorder=9))
    ax.add_patch(Rectangle((bx + seg, by), seg, h, facecolor="white", edgecolor=INK, lw=0.6, zorder=9))
    ax.text(bx, by + 2.2 * h, "0", fontsize=7, ha="center", color=INK, zorder=9)
    ax.text(bx + 2 * seg, by + 2.2 * h, f"{km:g} km", fontsize=7, ha="center", color=INK, zorder=9)


def draw_base(ax, view):
    ax.set_facecolor(OCEAN)
    if admin is not None:
        admin[admin["country"] != "CAN"].plot(ax=ax, facecolor=NEIGHBOUR, edgecolor="#c9c9c9",
                                              linewidth=0.3, zorder=1)
    for gx, gy in GRATICULE:
        ax.plot(gx, gy, color="#8f9ba4", lw=0.35, ls=(0, (2, 3)), zorder=4)


def draw_outlines(ax):
    if admin is not None:
        admin[admin["country"] == "CAN"].boundary.plot(ax=ax, color="#4a4a4a", linewidth=0.45, zorder=5)
    else:
        left, right, bottom, top = MAIN_EXTENT
        ax.contour(LAND_SMALL.astype(float), levels=[0.5], colors="#4a4a4a", linewidths=0.45,
                   extent=(left, right, top, bottom), origin="upper", zorder=5)


def finish_map(ax, view):
    ax.set_xlim(view[0], view[1])
    ax.set_ylim(view[2], view[3])
    ax.set_aspect("equal")
    ax.set_xticks([])
    ax.set_yticks([])
    for side in ["top", "right", "bottom", "left"]:
        ax.spines[side].set_visible(True)
        ax.spines[side].set_color("#9aa3a9")
        ax.spines[side].set_linewidth(0.6)


def draw_raster(ax, codes, extent, colours, alpha=1.0):
    if alpha < 1.0:
        white = mcolors.ListedColormap(["white"])
        ax.imshow(np.ma.masked_less(codes, 0), cmap=white, extent=extent, origin="upper",
                  interpolation="antialiased", interpolation_stage="rgba", zorder=2)
    n = len(colours)
    cmap = mcolors.ListedColormap(colours)
    norm = mcolors.BoundaryNorm(np.arange(-0.5, n + 0.5, 1.0), n)
    masked = np.ma.masked_less(codes, 0)
    ax.imshow(masked, cmap=cmap, norm=norm, extent=extent, origin="upper",
              interpolation="antialiased", interpolation_stage="rgba", alpha=alpha, zorder=2)


def points_view(sel, aspect):
    """Box around the selected points, padded, with width / height = aspect."""
    if sel.sum() == 0:
        return CANADA_VIEW
    xs = cell_x[sel]
    ys = cell_y[sel]
    left = np.percentile(xs, 2)
    right = np.percentile(xs, 98)
    bottom = np.percentile(ys, 2)
    top = np.percentile(ys, 98)
    span = max(right - left, top - bottom, 300000.0)
    cx = (left + right) / 2
    cy = (bottom + top) / 2
    half_w = max(right - left, 300000.0) / 2 + 0.08 * span
    half_h = max(top - bottom, 300000.0) / 2 + 0.08 * span
    if half_w < aspect * half_h:
        half_w = aspect * half_h
    else:
        half_h = half_w / aspect
    return (cx - half_w, cx + half_w, cy - half_h, cy + half_h)


def scatter_points(ax, codes, colours, size=16):
    for code in range(len(colours)):
        sel = codes == code
        if sel.sum() == 0:
            continue
        ax.scatter(cell_x[sel], cell_y[sel], s=size, c=colours[code], edgecolors=INK,
                   linewidths=0.35, zorder=10 + code)
    lost = ~matched
    if lost.sum() > 0:
        ax.scatter(px[lost], py[lost], s=size, marker="x", c="#555555", linewidths=0.8, zorder=30)


def map_legend(ax, labels, colours, title):
    handles = []
    for lab, col in zip(labels, colours):
        handles.append(Patch(facecolor=col, edgecolor="#777777", linewidth=0.4, label=lab))
    leg = ax.legend(handles=handles, title=title, loc="lower left", fontsize=6.5, title_fontsize=7,
                    frameon=True, framealpha=0.92, edgecolor="#bbbbbb", borderpad=0.5,
                    handlelength=1.2, handleheight=0.9)
    leg.set_zorder(40)


def draw_distribution(ax, labels, colours, loc_share, loc_counts, land_share, n_loc):
    n = len(labels)
    y = np.arange(n)
    h = 0.38
    ax.barh(y + h / 2, loc_share, height=h, color=colours, edgecolor=INK, linewidth=0.5,
            label=f"Your locations (n = {fmt_int(n_loc)})")
    ax.barh(y - h / 2, land_share, height=h, color=colours, alpha=0.38, edgecolor="none",
            label="All Canadian land (area)")
    top = max(np.max(loc_share), np.max(land_share), 1.0)
    for i in range(n):
        ax.text(loc_share[i] + top * 0.02, y[i] + h / 2, f"{loc_share[i]:.1f}% ({fmt_int(loc_counts[i])})",
                va="center", fontsize=7, color=INK)
        ax.text(land_share[i] + top * 0.02, y[i] - h / 2, f"{land_share[i]:.1f}%",
                va="center", fontsize=7, color=MUTED)
    ax.set_yticks(y)
    ax.set_yticklabels(labels, fontsize=8)
    ax.set_xlim(0, top * 1.75)
    ticks = []
    for t in ax.get_xticks():
        if 0 <= t <= min(100, top * 1.1):
            ticks.append(t)
    ax.set_xticks(ticks)
    ax.set_xlabel("Share (%)", fontsize=8, color=MUTED)
    ax.tick_params(axis="x", labelsize=7, colors=MUTED)
    ax.grid(axis="x", color="#e6e6e6", lw=0.6)
    ax.set_axisbelow(True)
    ax.legend(loc="upper left", bbox_to_anchor=(-0.55, -0.14), fontsize=7.5, frameon=False)


def page_header(fig, title, subtitle):
    fig.text(0.02, 0.955, title, fontsize=20, fontweight="bold", color=INK, va="center")
    fig.text(0.02, 0.905, subtitle, fontsize=11.5, color=MUTED, va="center")


def page_footer(fig, page_no):
    fig.text(0.02, 0.015, SOURCE_LINE, fontsize=7, color="#8a949b")
    fig.text(0.98, 0.015, f"{page_no}", fontsize=8, color="#8a949b", ha="right")


def formula_band(fig, formula_lines, read_lines):
    box = fig.add_axes([0.02, 0.045, 0.96, 0.175])
    box.axis("off")
    box.add_patch(FancyBboxPatch((0, 0), 1, 1, boxstyle="round,pad=0,rounding_size=0.02",
                                 transform=box.transAxes, facecolor="#f4f2ed", edgecolor="#dad5ca",
                                 linewidth=0.8))
    box.text(0.012, 0.86, "HOW IT IS COMPUTED", fontsize=8, fontweight="bold", color=MUTED,
             transform=box.transAxes)
    y = 0.66
    for line in formula_lines:
        box.text(0.012, y, line, fontsize=10, color=INK, transform=box.transAxes, va="center")
        y = y - 0.22
    box.text(0.775, 0.86, "HOW TO READ IT", fontsize=8, fontweight="bold", color=MUTED,
             transform=box.transAxes)
    y = 0.66
    for line in read_lines:
        box.text(0.775, y, line, fontsize=9, color=INK, transform=box.transAxes, va="center")
        y = y - 0.2


def panel_label(ax, letter, text):
    ax.text(0.0, 1.015, letter, transform=ax.transAxes, fontsize=11, fontweight="bold",
            color=INK, va="bottom")
    ax.text(0.045, 1.015, text, transform=ax.transAxes, fontsize=9.5, color=MUTED, va="bottom")


PAGES = []


def save_page(fig):
    PAGES.append(fig)


def draw_table(ax, df, col_widths, fontsize=8):
    ax.axis("off")
    cells = []
    for _, row in df.iterrows():
        line = []
        for col in df.columns:
            line.append(row[col])
        cells.append(line)
    if len(cells) == 0:
        ax.text(0.5, 0.5, "No locations", ha="center", va="center", fontsize=12, color=MUTED)
        return None
    total = float(sum(col_widths))
    scaled = []
    for w in col_widths:
        scaled.append(w / total)
    table = ax.table(cellText=cells, colLabels=list(df.columns), colWidths=scaled,
                     loc="upper center", cellLoc="left")
    table.auto_set_font_size(False)
    table.set_fontsize(fontsize)
    table.scale(1, 1.25)
    for (r, c), cell in table.get_celld().items():
        cell.set_edgecolor("#e3e3e3")
        cell.set_linewidth(0.5)
        if r == 0:
            cell.set_facecolor("#2f3b45")
            cell.get_text().set_color("white")
            cell.get_text().set_fontweight("bold")
        elif r % 2 == 0:
            cell.set_facecolor("#f6f6f4")
        else:
            cell.set_facecolor("white")
    return table


# ---------------------------------------------------------------------
# 7. PAGES
# ---------------------------------------------------------------------
today = datetime.date.today().strftime("%B %d, %Y")
cls_codes, cls_ok = point_codes(METRICS[0])
n_fire = int((cls_codes >= 1).sum())
n_mod = int((cls_codes >= 3).sum())
n_high = int((cls_codes >= 4).sum())
n_watch = int(watch.sum())

# ---- page 1: summary ---------------------------------------------------
fig = plt.figure(figsize=(16, 9))
page_header(fig, REPORT_TITLE,
            f"{fmt_int(n_pts)} locations sampled against 1 km NBAC fire history, 1972–2025  |  {today}")
tiles = [
    (fmt_int(n_pts), "locations sampled", f"dropped without lat/lon: {n_bad_coords:,}"),
    (f"{pct(n_fire, n_matched):.1f}%", "fire in the record", f"{fmt_int(n_fire)} locations, class 1–5"),
    (f"{pct(n_mod, n_matched):.1f}%", "Moderate or higher", f"{fmt_int(n_mod)} locations"),
    (f"{pct(n_high, n_matched):.2f}%", "High or Very high", f"{fmt_int(n_high)} locations"),
    (fmt_int(n_watch), "watchlist", f"no fire in cell, score ≥ {WATCH_SCORE}"),
    (fmt_int(n_pts - n_matched), "not matched", f"no land within {SNAP_KM:g} km  ({n_snapped:,} snapped)"),
]
tile_w = 0.148
for i, (big, small, note) in enumerate(tiles):
    ax = fig.add_axes([0.02 + i * (tile_w + 0.012), 0.66, tile_w, 0.19])
    ax.axis("off")
    ax.add_patch(FancyBboxPatch((0, 0), 1, 1, boxstyle="round,pad=0,rounding_size=0.04",
                                transform=ax.transAxes, facecolor="#f4f2ed", edgecolor="#dad5ca"))
    ax.text(0.08, 0.62, big, fontsize=26, fontweight="bold", color=INK, transform=ax.transAxes)
    ax.text(0.08, 0.36, small, fontsize=11, color=INK, transform=ax.transAxes)
    ax.text(0.08, 0.14, note, fontsize=8, color=MUTED, transform=ax.transAxes)

# class mix: locations vs land, as two stacked bars
_, cls_land_share = land_shares(METRICS[0])
cls_counts = np.bincount(cls_codes[cls_codes >= 0].astype(np.int64), minlength=6)
cls_share = 100.0 * cls_counts / max(cls_counts.sum(), 1)
ax = fig.add_axes([0.12, 0.25, 0.46, 0.30])
colours = METRICS[0]["colors"]
rows_to_draw = [("Your locations", cls_share), ("All Canadian land", cls_land_share)]
for j, (name, share) in enumerate(rows_to_draw):
    left = 0.0
    for code in range(6):
        ax.barh(j, share[code], left=left, color=colours[code], edgecolor="white", linewidth=0.8, height=0.55)
        if share[code] >= 4:
            ax.text(left + share[code] / 2, j, f"{share[code]:.0f}%", ha="center", va="center",
                    fontsize=8.5, color=INK if code < 4 else "white")
        left = left + share[code]
ax.set_yticks([0, 1])
ax.set_yticklabels(["Your locations", "All Canadian land"], fontsize=10)
ax.invert_yaxis()
ax.set_xlim(0, 100)
ax.set_xlabel("Share (%)", fontsize=9, color=MUTED)
ax.set_title("Fire-history class mix", fontsize=12, loc="left", color=INK, fontweight="bold")
handles = []
for lab, col in zip(METRICS[0]["labels"], colours):
    handles.append(Patch(facecolor=col, label=lab))
ax.legend(handles=handles, ncol=6, loc="upper center", bbox_to_anchor=(0.5, -0.28), fontsize=8, frameon=False)

notes = [
    "What is in this report",
    "",
    "•  One page per metric: A = the raster as a map, B = your",
    "    locations on top, C = how your locations compare with",
    "    all Canadian land.",
    "•  The final score is the fire-history class (next page).",
    "•  The smoothed score and the distance page show who sits",
    "    NEXT to fire country without burning themselves.",
    "•  'No fire in record' means unknown, not safe: burn",
    "    perimeters stop at town edges, so most urban",
    "    locations land here.",
    "•  Classes describe history, not a forecast, and burned",
    "    area is not loss.",
]
y = 0.55
for k, line in enumerate(notes):
    weight = "bold" if k == 0 else "normal"
    size = 12 if k == 0 else 10
    fig.text(0.63, y, line, fontsize=size, fontweight=weight, color=INK)
    y = y - 0.03
page_footer(fig, 1)
save_page(fig)

# ---- page 2: methods ---------------------------------------------------
fig = plt.figure(figsize=(16, 9))
page_header(fig, "Data and method", "Every layer comes from one annual burned-area record on one 1 km grid")
left_lines = [
    ("DATA", True),
    ("NBAC National Burned Area Composite, Natural Resources Canada: every mapped fire", False),
    ("perimeter in Canada, 1972–2025, small fires included.", False),
    ("Gridded to 1 km cells in EPSG:3978 (NAD83 / Canada Atlas Lambert).", False),
    (r"A cell burned in year $y$ ($B_y = 1$) if any perimeter from that year touches it", False),
    ("(all-touched), so a 1 km cell counts even when a fire clips its edge.", False),
    ("Land mask: Natural Earth 1:50M Canada.", False),
    ("", False),
    ("SAMPLING THESE LOCATIONS", True),
    ("Lat/lon (WGS84) reprojected to EPSG:3978; the value of the 1 km cell is read.", False),
    (f"Locations off the coarse coastline are moved to the nearest land cell within {SNAP_KM:g} km", False),
    (f"({n_snapped:,} moved, median {np.nanmedian(snap_km[snap_km > 0]) if n_snapped else 0:.1f} km); "
     f"{n_pts - n_matched:,} had no land within reach and are left blank.", False),
    ("Off-land cells are told apart from 'never burned' using the burn-rate raster", False),
    ("(-1 there only ever means off land).", False),
    ("", False),
    ("CAVEATS", True),
    ("•  History, not hazard: recently burned ground can carry less fuel for a few years.", False),
    ("•  Burn perimeters rarely enter towns, so urban locations mostly read 'No fire in record'.", False),
    ("•  The smoothed score is relative to Canada's own maximum; not comparable with the US layers.", False),
    ("•  1 km cells: a location's own parcel may differ from its cell.", False),
]
right_lines = [
    ("FORMULAS", True),
    (r"$\mathrm{times\ burned} = \sum_y B_y$          $\mathrm{burn\ rate} = \sum_y B_y\,/\,54$", False),
    (r"$\mathrm{TSLB} = 2025 - \mathrm{max}\{\,y : B_y = 1\,\}$", False),
    (r"$\mathrm{recent\ rate} = \sum_{y=2021}^{2025} B_y\,/\,5$", False),
    ("", False),
    ("Smoothed score", True),
    (r"$w_y = \mathrm{rank}(y)/54$,   $\mathrm{freq} = \sum B_y/54$,   $\mathrm{recency} = \sum w_y B_y / \sum w_y$", False),
    (r"$\mathrm{score} = \mathrm{round}(100\cdot G_{15\,\mathrm{km}}(0.5\,\mathrm{freq} + 0.5\,\mathrm{recency})\,/\,\mathrm{max})$", False),
    ("", False),
    ("Fire-history class (final score)", True),
    ("frequency points:  1→1   2→2   3–4→3   5–7→4   8+→5", False),
    ("recency points:  40+→1   20–39→2   10–19→3   5–9→4   0–4→5", False),
    ("total 2–3 Very low · 4–5 Low · 6–7 Moderate · 8 High · 9–10 Very high", False),
    ("no fire in the record = its own class (0)", False),
    ("", False),
    ("Distance to fire", True),
    ("Euclidean distance from the cell to the nearest cell that ever burned", False),
    ("", False),
    ("Watchlist", True),
    (f"class = No fire in record  AND  smoothed score ≥ {WATCH_SCORE}", False),
]
for x_pos, lines in [(0.03, left_lines), (0.53, right_lines)]:
    y = 0.84
    for text, is_head in lines:
        if is_head:
            fig.text(x_pos, y, text, fontsize=10, fontweight="bold", color=MUTED)
        else:
            fig.text(x_pos, y, text, fontsize=10.5, color=INK)
        y = y - 0.037
page_footer(fig, 2)
save_page(fig)

# ---- metric pages ----------------------------------------------------
TAKEAWAY = {}
page_no = 3
for m in METRICS:
    print(f"page {page_no}: {m['title']}")
    codes_pts, ok_pts = point_codes(m)
    grid_codes, land_share = land_shares(m)
    n_cat = len(m["labels"])
    counts = np.bincount(codes_pts[codes_pts >= 0].astype(np.int64), minlength=n_cat)
    n_ok = int(counts.sum())
    share = 100.0 * counts / max(n_ok, 1)

    if m["key"] == "cls":
        sub = (f"{pct(n_fire, n_ok):.1f}% of locations sit on a cell with fire in the record "
               f"(vs {100 - land_share[0]:.1f}% of Canadian land); {pct(n_mod, n_ok):.1f}% are Moderate or higher.")
    elif m["key"] == "score":
        sub = (f"{pct((codes_pts >= 2).sum(), n_ok):.1f}% of locations score 10 or more; "
               f"{fmt_int(n_watch)} of them have no fire in their own cell (watchlist).")
    elif m["key"] == "dist":
        sub = (f"Median distance to the nearest mapped fire: {np.nanmedian(vals['dist'][ok_pts]):.0f} km; "
               f"{pct((codes_pts >= 3).sum(), n_ok):.1f}% of locations are within 5 km.")
    elif m["key"] == "nburns":
        sub = (f"{pct((codes_pts >= 1).sum(), n_ok):.1f}% of locations burned at least once since 1972; "
               f"{pct((codes_pts >= 3).sum(), n_ok):.1f}% three times or more.")
    elif m["key"] == "tslb":
        n_b = int((codes_pts >= 1).sum())
        sub = (f"Of the {fmt_int(n_b)} locations with fire in the record, "
               f"{pct((codes_pts >= 4).sum(), n_b):.0f}% last burned in 2016–2025.")
    else:
        sub = (f"{pct((codes_pts >= 1).sum(), n_ok):.1f}% of locations burned at least once in 2021–2025 "
               f"(vs {100 - land_share[0]:.1f}% of Canadian land).")

    fig = plt.figure(figsize=(16, 9))
    page_header(fig, m["title"], sub)

    ax_a = fig.add_axes([0.02, 0.25, 0.36, 0.60])
    draw_base(ax_a, CANADA_VIEW)
    draw_raster(ax_a, grid_codes, m["extent"], m["colors"])
    draw_outlines(ax_a)
    draw_scalebar(ax_a, CANADA_VIEW)
    map_legend(ax_a, m["labels"], m["colors"], None)
    finish_map(ax_a, CANADA_VIEW)
    panel_label(ax_a, "A", "The raster: all of Canada, 1 km")

    view = points_view(matched, CANADA_ASPECT)
    if (view[1] - view[0]) > 0.6 * (CANADA_VIEW[1] - CANADA_VIEW[0]):
        view = CANADA_VIEW
    ax_b = fig.add_axes([0.39, 0.25, 0.36, 0.60])
    draw_base(ax_b, view)
    draw_raster(ax_b, grid_codes, m["extent"], m["colors"], alpha=0.55)
    draw_outlines(ax_b)
    scatter_points(ax_b, codes_pts, m["colors"])
    draw_scalebar(ax_b, view)
    finish_map(ax_b, view)
    outside = matched & ((cell_x < view[0]) | (cell_x > view[1]) | (cell_y < view[2]) | (cell_y > view[3]))
    note = f"Your {fmt_int(n_ok)} locations on the raster"
    if outside.sum() > 0:
        note = note + f"  ({fmt_int(outside.sum())} outside this view)"
    panel_label(ax_b, "B", note)

    ax_c = fig.add_axes([0.845, 0.36, 0.145, 0.47])
    draw_distribution(ax_c, m["labels"], m["colors"], share, counts, land_share, n_ok)
    fig.text(0.765, 0.862, "C", fontsize=11, fontweight="bold", color=INK, va="bottom")
    fig.text(0.78, 0.862, "Locations vs Canadian land", fontsize=9.5, color=MUTED, va="bottom")

    formula_band(fig, m["formula"], m["read"])
    page_footer(fig, page_no)
    save_page(fig)
    del grid_codes
    page_no = page_no + 1

# ---- watchlist page ----------------------------------------------------
print(f"page {page_no}: watchlist")
fire_free = score_land & (grids["cls"]["data"] == 0)
watch_land_share = pct(int((fire_free & (score_main >= WATCH_SCORE)).sum()), int(fire_free.sum()))
fig = plt.figure(figsize=(16, 9))
page_header(fig, "Watchlist: next to fire country, no fire in their own cell",
            f"{fmt_int(n_watch)} locations read 'No fire in record' but have a smoothed score ≥ {WATCH_SCORE} "
            f"(the top {watch_land_share:.0f}% of fire-free Canadian land). Sorted by score.")
m_score = METRICS[1]
score_codes_grid, _ = land_shares(m_score)
codes_pts, _ = point_codes(m_score)
ax = fig.add_axes([0.02, 0.06, 0.44, 0.78])
if n_watch > 0:
    view = points_view(watch, 1.0)
else:
    view = points_view(matched, 1.0)
draw_base(ax, view)
draw_raster(ax, score_codes_grid, m_score["extent"], m_score["colors"], alpha=0.7)
draw_outlines(ax)
others = matched & ~watch
ax.scatter(cell_x[others], cell_y[others], s=6, c="#9aa3a9", edgecolors="none", zorder=10)
ax.scatter(cell_x[watch], cell_y[watch], s=34, c="#6a1b9a", edgecolors="white", linewidths=0.6, zorder=12)
draw_scalebar(ax, view)
map_legend(ax, m_score["labels"] + ["Watchlist location", "Other location"],
           m_score["colors"] + ["#6a1b9a", "#9aa3a9"], "Smoothed score")
ax.get_legend().set_loc("upper left")
finish_map(ax, view)
panel_label(ax, "", "Watchlist locations over the smoothed score")
del score_codes_grid

wl = pts[watch].copy()
wl = wl.sort_values(["wf_score_0_100", "wf_km_to_fire"], ascending=[False, True])
if id_col is not None:
    wl = wl.drop_duplicates(id_col)
table_cols = {}
if id_col is not None:
    table_cols["ID"] = wl[id_col].astype(str).str.slice(0, 14)
if name_col is not None:
    table_cols["Name"] = wl[name_col].astype(str).str.slice(0, 26)
if city_col is not None:
    table_cols["City"] = wl[city_col].astype(str).str.slice(0, 18)
if prov_col is not None:
    table_cols["Province"] = wl[prov_col].astype(str).str.slice(0, 18)
table_cols["Score"] = wl["wf_score_0_100"].map("{:.0f}".format)
table_cols["Fire km"] = wl["wf_km_to_fire"].map("{:.0f}".format)
if value_col is not None:
    table_cols["Value"] = pd.to_numeric(wl[value_col], errors="coerce").map("{:,.0f}".format)
wl_table = pd.DataFrame(table_cols).head(24)
ax_t = fig.add_axes([0.49, 0.06, 0.49, 0.76])
widths = []
for col in wl_table.columns:
    if col in ["Score", "Fire km"]:
        widths.append(0.08)
    elif col == "Name":
        widths.append(0.27)
    else:
        widths.append(0.17)
draw_table(ax_t, wl_table, widths)
ax_t.set_title(f"Top {len(wl_table)} by smoothed score (unique locations)", fontsize=10, color=MUTED, loc="left")
page_footer(fig, page_no)
save_page(fig)
page_no = page_no + 1

# ---- top locations page ------------------------------------------------
print(f"page {page_no}: top locations")
fig = plt.figure(figsize=(16, 9))
page_header(fig, "Highest fire-history class",
            "Locations sorted by class, then smoothed score. Each value is the location's own 1 km cell.")
top = pts[matched & (pts["wf_class_5cat"] >= 1)].copy()
top = top.sort_values(["wf_class_5cat", "wf_score_0_100", "wf_times_burned"], ascending=[False, False, False])
if id_col is not None:
    top = top.drop_duplicates(id_col)
table_cols = {}
if id_col is not None:
    table_cols["ID"] = top[id_col].astype(str).str.slice(0, 14)
if name_col is not None:
    table_cols["Name"] = top[name_col].astype(str).str.slice(0, 28)
if city_col is not None:
    table_cols["City"] = top[city_col].astype(str).str.slice(0, 18)
if prov_col is not None:
    table_cols["Province"] = top[prov_col].astype(str).str.slice(0, 18)
table_cols["Class"] = top["wf_class_label"]
table_cols["Times burned"] = top["wf_times_burned"].map("{:.0f}".format)
table_cols["Last burn"] = top["wf_last_burn_year"].map("{:.0f}".format)
table_cols["Score"] = top["wf_score_0_100"].map("{:.0f}".format)
if value_col is not None:
    table_cols["Value"] = pd.to_numeric(top[value_col], errors="coerce").map("{:,.0f}".format)
top_table = pd.DataFrame(table_cols).head(26)
ax_t = fig.add_axes([0.02, 0.06, 0.96, 0.80])
widths = []
for col in top_table.columns:
    if col in ["Times burned", "Last burn", "Score"]:
        widths.append(0.08)
    elif col == "Name":
        widths.append(0.22)
    elif col == "Class":
        widths.append(0.10)
    else:
        widths.append(0.13)
draw_table(ax_t, top_table, widths, fontsize=8.5)
page_footer(fig, page_no)
save_page(fig)
page_no = page_no + 1

# ---- province page -----------------------------------------------------
if prov_col is not None:
    print(f"page {page_no}: provinces")
    fig = plt.figure(figsize=(16, 9))
    page_header(fig, "By province", "Locations per fire-history class; shading = that class's share of the province's locations")
    sub = pts[matched].copy()
    sub["_prov"] = sub[prov_col].fillna("(blank)").astype(str)
    sub["_cls"] = sub["wf_class_label"]
    tab = pd.crosstab(sub["_prov"], sub["_cls"])
    ordered = []
    for lab in METRICS[0]["labels"]:
        if lab not in tab.columns:
            tab[lab] = 0
        ordered.append(lab)
    tab = tab[ordered]
    tab["Total"] = tab.sum(axis=1)
    tab["% fire in record"] = (100.0 * (tab["Total"] - tab["No fire in record"]) / tab["Total"]).round(1)
    tab["Watchlist"] = sub[sub["wf_watchlist"]].groupby("_prov").size().reindex(tab.index).fillna(0).astype(int)
    if value_col is not None:
        sub["_val"] = pd.to_numeric(sub[value_col], errors="coerce")
        tab["Value, Moderate+"] = sub[sub["wf_class_5cat"] >= 3].groupby("_prov")["_val"].sum().reindex(tab.index).fillna(0)
    tab = tab.sort_values("Total", ascending=False).head(16)

    show = tab.copy()
    shade = np.zeros(show.shape)
    for j, col in enumerate(show.columns):
        if col in ordered and col != "No fire in record":
            shade[:, j] = show[col].to_numpy() / show["Total"].to_numpy()
    if shade.max() > 0:
        shade = shade / shade.max()
    show = show.reset_index().rename(columns={"_prov": "Province"})
    for col in show.columns:
        if col == "Value, Moderate+":
            show[col] = show[col].map("{:,.0f}".format)
        elif col == "% fire in record":
            show[col] = show[col].map("{:.1f}".format)
        elif col != "Province":
            show[col] = show[col].map("{:,.0f}".format)
    ax_t = fig.add_axes([0.02, 0.08, 0.96, 0.76])
    widths = [0.16]
    for col in show.columns[1:]:
        widths.append(0.84 / (len(show.columns) - 1))
    table = draw_table(ax_t, show, widths, fontsize=9)
    if table is not None:
        cmap = plt.get_cmap("YlOrBr")
        for i in range(shade.shape[0]):
            for j in range(shade.shape[1]):
                if shade[i, j] > 0:
                    table[(i + 1, j + 1)].set_facecolor(mcolors.to_hex(cmap(0.1 + 0.55 * shade[i, j])))
    page_footer(fig, page_no)
    save_page(fig)
    page_no = page_no + 1

# ---------------------------------------------------------------------
# 8. WRITE
# ---------------------------------------------------------------------
pdf_path = os.path.join(OUT_DIR, "canada_wildfire_report.pdf")
with PdfPages(pdf_path) as pdf:
    for i, fig in enumerate(PAGES):
        pdf.savefig(fig, dpi=200)
        fig.savefig(os.path.join(OUT_DIR, "pages", f"page_{i + 1:02d}.png"), dpi=150)
        plt.close(fig)

xlsx_path = os.path.join(OUT_DIR, "locations_wildfire.xlsx")
pts.to_excel(xlsx_path, index=False)

print("")
print("=" * 60)
print(f"PDF   : {pdf_path}  ({len(PAGES)} pages)")
print(f"PNGs  : {os.path.join(OUT_DIR, 'pages')}")
print(f"XLSX  : {xlsx_path}  ({len(pts):,} rows)")
print("=" * 60)
print(pts["wf_class_label"].value_counts(dropna=False).to_string())
print(f"watchlist: {n_watch:,}")
