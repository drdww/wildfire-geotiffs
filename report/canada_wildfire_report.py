# =====================================================================
# CANADA WILDFIRE-HISTORY REPORT FOR A SET OF LOCATIONS  (v2)
# https://github.com/drdww/wildfire-geotiffs   (report/canada_wildfire_report.py)
#
# One cell. Edit the four paths below, then run it.
#
# Produces, in OUT_DIR:
#   canada_wildfire_report.pdf   10 pages, 16:9:
#                                summary, where the locations are, then one page each for
#                                the smoothed score, times burned, years since last burn and
#                                the fire-history class (A = raster, B = our locations on a
#                                basemap, C = distribution, plus key numbers and the formula),
#                                then top-25 table, by province, summary statistics, formulas
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
# 5. BINS, COLOURS AND BASEMAP PIECES
# ---------------------------------------------------------------------
from matplotlib import patheffects

LAND_FILL = "#f3eee3"


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


CLASS_NAMES = ["No fire in record", "Very low", "Low", "Moderate", "High", "Very high"]
NBURN_NAMES = ["Never burned", "1 year", "2 years", "3–4 years", "5–7 years", "8+ years"]
TSLB_NAMES = ["No fire in record", "40+ yr (1985 or earlier)", "20–39 yr (1986–2005)",
              "10–19 yr (2006–2015)", "5–9 yr (2016–2020)", "0–4 yr (2021–2025)"]
SCORE_NAMES = ["0", "1–9", "10–24", "25–49", "50–100"]
SIX_COLOURS = [NEUTRAL] + ramp(5)
FIVE_COLOURS = [NEUTRAL] + ramp(4)

SCORE_CMAP = mcolors.LinearSegmentedColormap.from_list("score", [NEUTRAL] + ramp(6))
SCORE_NORM = mcolors.PowerNorm(gamma=0.5, vmin=0, vmax=100)

CITIES = [
    ("Vancouver", -123.12, 49.28), ("Victoria", -123.37, 48.43), ("Kelowna", -119.50, 49.89),
    ("Kamloops", -120.33, 50.67), ("Prince George", -122.75, 53.92), ("Calgary", -114.07, 51.05),
    ("Edmonton", -113.49, 53.54), ("Fort McMurray", -111.38, 56.73), ("Saskatoon", -106.67, 52.13),
    ("Regina", -104.62, 50.45), ("Winnipeg", -97.14, 49.90), ("Thunder Bay", -89.25, 48.38),
    ("Sudbury", -80.99, 46.49), ("Toronto", -79.38, 43.65), ("Ottawa", -75.70, 45.42),
    ("Montréal", -73.57, 45.50), ("Québec", -71.21, 46.81), ("Halifax", -63.57, 44.65),
    ("St. John's", -52.71, 47.56), ("Yellowknife", -114.37, 62.45), ("Whitehorse", -135.06, 60.72),
]
city_lon = []
city_lat = []
for _, lon, lat in CITIES:
    city_lon.append(lon)
    city_lat.append(lat)
city_x, city_y = to_lcc_lines.transform(city_lon, city_lat)

# page layout (fractions of a 16 x 9 inch page)
A_BOX = [0.02, 0.22, 0.36, 0.64]
B_BOX = [0.40, 0.50, 0.58, 0.36]
C_BOX = [0.45, 0.25, 0.29, 0.185]
S_BOX = [0.775, 0.235, 0.205, 0.215]
B_ASPECT = (B_BOX[2] * 16) / (B_BOX[3] * 9)


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
    bx = right - 0.04 * (right - left) - 2 * seg
    by = bottom + 0.06 * (top - bottom)
    h = 0.014 * (top - bottom)
    ax.add_patch(Rectangle((bx, by), seg, h, facecolor=INK, edgecolor=INK, lw=0.6, zorder=40))
    ax.add_patch(Rectangle((bx + seg, by), seg, h, facecolor="white", edgecolor=INK, lw=0.6, zorder=40))
    ax.text(bx, by + 2.2 * h, "0", fontsize=7, ha="center", color=INK, zorder=40)
    ax.text(bx + 2 * seg, by + 2.2 * h, f"{km:g} km", fontsize=7, ha="center", color=INK, zorder=40)


def draw_base(ax):
    """Ocean, neighbouring US states and the graticule."""
    ax.set_facecolor(OCEAN)
    if admin is not None:
        admin[admin["country"] != "CAN"].plot(ax=ax, facecolor=NEIGHBOUR, edgecolor="#c9c9c9",
                                              linewidth=0.3, zorder=1)
    for gx, gy in GRATICULE:
        ax.plot(gx, gy, color="#8f9ba4", lw=0.35, ls=(0, (2, 3)), zorder=4)


def draw_land(ax):
    """Plain Canadian land, for maps where the points carry the colour."""
    cmap = mcolors.ListedColormap([LAND_FILL])
    ax.imshow(np.ma.masked_where(~land, np.zeros(land.shape, dtype=np.uint8)), cmap=cmap,
              extent=MAIN_EXTENT, origin="upper", interpolation="antialiased",
              interpolation_stage="rgba", zorder=2)


def draw_outlines(ax):
    if admin is not None:
        admin[admin["country"] == "CAN"].boundary.plot(ax=ax, color="#4a4a4a", linewidth=0.45, zorder=5)
    else:
        left, right, bottom, top = MAIN_EXTENT
        ax.contour(LAND_SMALL.astype(float), levels=[0.5], colors="#4a4a4a", linewidths=0.45,
                   extent=(left, right, top, bottom), origin="upper", zorder=5)


def draw_cities(ax, view):
    halo = [patheffects.withStroke(linewidth=2.2, foreground="white")]
    for k in range(len(CITIES)):
        x = city_x[k]
        y = city_y[k]
        if x < view[0] or x > view[1] or y < view[2] or y > view[3]:
            continue
        ax.plot(x, y, marker="s", ms=2.4, mfc="white", mec=INK, mew=0.6, zorder=15)
        ax.text(x + 0.008 * (view[1] - view[0]), y + 0.008 * (view[3] - view[2]), CITIES[k][0],
                fontsize=7, color=INK, zorder=36, path_effects=halo)


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


def draw_binned(ax, codes, colours):
    n = len(colours)
    cmap = mcolors.ListedColormap(colours)
    norm = mcolors.BoundaryNorm(np.arange(-0.5, n + 0.5, 1.0), n)
    ax.imshow(np.ma.masked_less(codes, 0), cmap=cmap, norm=norm, extent=MAIN_EXTENT, origin="upper",
              interpolation="antialiased", interpolation_stage="rgba", zorder=2)


def map_legend(ax, labels, colours, title=None, loc="lower left"):
    handles = []
    for lab, col in zip(labels, colours):
        handles.append(Patch(facecolor=col, edgecolor="#777777", linewidth=0.4, label=lab))
    leg = ax.legend(handles=handles, title=title, loc=loc, fontsize=6.5, title_fontsize=7,
                    frameon=True, framealpha=0.92, edgecolor="#bbbbbb", borderpad=0.5,
                    handlelength=1.2, handleheight=0.9)
    leg.set_zorder(50)


def score_colorbar(ax, label):
    cax = ax.inset_axes([0.05, 0.08, 0.36, 0.035])
    sm = plt.cm.ScalarMappable(cmap=SCORE_CMAP, norm=SCORE_NORM)
    cb = plt.colorbar(sm, cax=cax, orientation="horizontal")
    cb.set_ticks([0, 5, 10, 25, 50, 100])
    cb.ax.tick_params(labelsize=6.5, length=2)
    cb.outline.set_linewidth(0.4)
    cax.set_title(label, fontsize=7, color=INK, pad=3)


def points_view(sel, aspect):
    """Box around most of the selected points (1st-99th percentile), padded, width/height = aspect."""
    if sel.sum() == 0:
        return CANADA_VIEW
    xs = cell_x[sel]
    ys = cell_y[sel]
    left = np.percentile(xs, 1)
    right = np.percentile(xs, 99)
    bottom = np.percentile(ys, 1)
    top = np.percentile(ys, 99)
    span = max(right - left, top - bottom, 300000.0)
    cx = (left + right) / 2
    cy = (bottom + top) / 2
    half_w = max(right - left, 300000.0) / 2 + 0.06 * span
    half_h = max(top - bottom, 300000.0) / 2 + 0.06 * span
    if half_w < aspect * half_h:
        half_w = aspect * half_h
    else:
        half_h = half_w / aspect
    return (cx - half_w, cx + half_w, cy - half_h, cy + half_h)


def outside_note(view):
    out = matched & ((cell_x < view[0]) | (cell_x > view[1]) | (cell_y < view[2]) | (cell_y > view[3]))
    if out.sum() == 0:
        return ""
    return f"  ({fmt_int(out.sum())} outside this view)"


def page_header(fig, title, subtitle):
    fig.text(0.02, 0.955, title, fontsize=18, fontweight="bold", color=INK, va="center")
    fig.text(0.02, 0.908, subtitle, fontsize=11, color=MUTED, va="center")


def page_footer(fig, page_no):
    fig.text(0.02, 0.012, SOURCE_LINE, fontsize=7, color="#8a949b")
    fig.text(0.98, 0.012, f"{page_no}", fontsize=8, color="#8a949b", ha="right")


def panel_label(fig, box, letter, text):
    fig.text(box[0], box[1] + box[3] + 0.008, letter, fontsize=11, fontweight="bold", color=INK, va="bottom")
    fig.text(box[0] + 0.017, box[1] + box[3] + 0.008, text, fontsize=9.5, color=MUTED, va="bottom")


def soft_box(fig, box):
    ax = fig.add_axes(box)
    ax.axis("off")
    ax.add_patch(Rectangle((0, 0), 1, 1, transform=ax.transAxes, facecolor="white",
                           edgecolor="#bdbdbd", linewidth=0.8))
    return ax


def stats_box(fig, lines):
    ax = soft_box(fig, S_BOX)
    y = 0.86
    for label, value in lines:
        ax.text(0.06, y, label, fontsize=9, color=MUTED, transform=ax.transAxes, va="center")
        ax.text(0.94, y, value, fontsize=10, color=INK, fontweight="bold", transform=ax.transAxes,
                va="center", ha="right")
        y = y - 0.145


def formula_band(fig, lines):
    ax = soft_box(fig, [0.02, 0.035, 0.96, 0.145])
    ax.text(0.012, 0.83, "FORMULA", fontsize=8, fontweight="bold", color=MUTED, transform=ax.transAxes)
    y = 0.6
    for line in lines:
        ax.text(0.012, y, line, fontsize=10, color=INK, transform=ax.transAxes, va="center")
        y = y - 0.24


def style_axis(ax):
    ax.tick_params(labelsize=7.5, colors=MUTED)
    ax.grid(axis="y", color="#e6e6e6", lw=0.6)
    ax.set_axisbelow(True)


def bar_counts(ax, names, colours, counts, log=True):
    x = np.arange(len(names))
    ax.bar(x, np.maximum(counts, 0.0), color=colours, edgecolor=INK, linewidth=0.5)
    total = max(counts.sum(), 1)
    for i in range(len(names)):
        if counts[i] > 0:
            ax.text(x[i], counts[i] * (1.15 if log else 1.0) + (0 if log else counts.max() * 0.02),
                    f"{fmt_int(counts[i])}\n{pct(counts[i], total):.1f}%", ha="center", va="bottom",
                    fontsize=6.5, color=INK)
    ax.set_xticks(x)
    ax.set_xticklabels(names, fontsize=7, rotation=0)
    if log:
        ax.set_yscale("log")
        ax.set_ylim(0.8, max(counts.max(), 1) * 12)
        ax.set_ylabel("Locations (log scale)", fontsize=7.5, color=MUTED)
    else:
        ax.set_ylim(0, max(counts.max(), 1) * 1.3)
        ax.set_ylabel("Locations", fontsize=7.5, color=MUTED)
    style_axis(ax)


PAGES = []


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
    table.scale(1, 1.3)
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
# 6. NUMBERS USED ON SEVERAL PAGES
# ---------------------------------------------------------------------
today = datetime.date.today().strftime("%B %d, %Y")
ok = matched
sc = vals["score"]
sc_ok = ok & ~np.isnan(sc)
nb = vals["nburns"]
ts = vals["tslb"]
cl = vals["cls"]
burned_pt = ok & (nb >= 1)
n_burned = int(burned_pt.sum())
n_score10 = int((sc_ok & (sc >= 10)).sum())
n_high = int((ok & (cl >= 4)).sum())
n_mod = int((ok & (cl >= 3)).sum())
n_watch = int(watch.sum())
n_outside = n_pts - n_matched

print("binning the rasters ...")
score_codes_grid = bin_score(score_main, score_land)
counts = np.bincount(score_codes_grid[score_codes_grid >= 0].astype(np.int64), minlength=5)
score_land_share = 100.0 * counts / counts.sum()
cls_grid = bin_class(grids["cls"]["data"], land)
counts = np.bincount(cls_grid[cls_grid >= 0].astype(np.int64), minlength=6)
cls_land_share = 100.0 * counts / counts.sum()


def codes_at_points(binner, v):
    good = ok & ~np.isnan(v)
    return binner(np.nan_to_num(v, nan=-9999.0), good)


score_pt_codes = codes_at_points(bin_score, sc)
cls_pt_codes = codes_at_points(bin_class, cl)
nb_pt_codes = codes_at_points(bin_nburns, nb)
ts_pt_codes = codes_at_points(bin_tslb, ts)


def shares(codes, n):
    c = np.bincount(codes[codes >= 0].astype(np.int64), minlength=n)
    return c, 100.0 * c / max(c.sum(), 1)


# ---------------------------------------------------------------------
# 7. PAGES
# ---------------------------------------------------------------------
page_no = 1

# ---- 1. summary --------------------------------------------------------
print(f"page {page_no}: summary")
fig = plt.figure(figsize=(16, 9))
page_header(fig, REPORT_TITLE,
            f"{fmt_int(n_pts)} Canadian locations sampled against 1 km NBAC fire history, 1972–2025  |  {today}")
tiles = [
    (fmt_int(n_matched), "locations on land",
     f"{fmt_int(n_snapped)} snapped to coast · {fmt_int(n_outside)} not matched"),
    (f"{np.nanmean(sc[sc_ok]):.1f}", "mean smoothed score",
     f"median {np.nanmedian(sc[sc_ok]):.0f} · max {np.nanmax(sc[sc_ok]):.0f} (scale 0–100)"),
    (f"{pct(n_score10, int(sc_ok.sum())):.1f}%", "score 10 or more", f"{fmt_int(n_score10)} locations"),
    (f"{pct(n_burned, n_matched):.1f}%", "fire in their own cell", f"{fmt_int(n_burned)} locations, 1972–2025"),
    (fmt_int(n_high), "High or Very high", f"fire-history class; {fmt_int(n_mod)} Moderate or above"),
    (fmt_int(n_watch), "near-fire watchlist", f"no fire in own cell, score ≥ {WATCH_SCORE}"),
]
tile_w = 0.148
for i, (big, small, note) in enumerate(tiles):
    ax = soft_box(fig, [0.02 + i * (tile_w + 0.012), 0.66, tile_w, 0.19])
    ax.text(0.08, 0.62, big, fontsize=22, color=INK, transform=ax.transAxes)
    ax.text(0.08, 0.38, small, fontsize=11, color=INK, transform=ax.transAxes)
    ax.text(0.08, 0.15, note, fontsize=7.2, color=MUTED, transform=ax.transAxes, wrap=True)

# two mixes: smoothed score and fire-history class, locations vs land
mixes = [
    ("Smoothed score", SCORE_NAMES, FIVE_COLOURS, shares(score_pt_codes, 5)[1], score_land_share, 0.40),
    ("Fire-history class", CLASS_NAMES, SIX_COLOURS, shares(cls_pt_codes, 6)[1], cls_land_share, 0.12),
]
for name, labs, cols, loc_share, land_share, ybox in mixes:
    ax = fig.add_axes([0.13, ybox, 0.45, 0.16])
    rows_to_draw = [("Our sampled locations", loc_share), ("All Canadian land", land_share)]
    for j, (_, share) in enumerate(rows_to_draw):
        left = 0.0
        for code in range(len(labs)):
            ax.barh(j, share[code], left=left, color=cols[code], edgecolor="white", linewidth=0.8, height=0.6)
            if share[code] >= 5:
                ax.text(left + share[code] / 2, j, f"{share[code]:.0f}%", ha="center", va="center",
                        fontsize=8, color=INK if code < len(labs) - 2 else "white")
            left = left + share[code]
    ax.set_yticks([0, 1])
    ax.set_yticklabels(["Our sampled locations", "All Canadian land"], fontsize=9)
    ax.invert_yaxis()
    ax.set_xlim(0, 100)
    ax.tick_params(axis="x", labelsize=7, colors=MUTED)
    ax.set_title(name, fontsize=11, loc="left", color=INK, fontweight="bold")
    handles = []
    for lab, col in zip(labs, cols):
        handles.append(Patch(facecolor=col, label=lab))
    ax.legend(handles=handles, ncol=6, loc="upper left", bbox_to_anchor=(0.0, -0.18), fontsize=7.5,
              frameon=False, handlelength=1.2)

notes = [
    ("Notes", True),
    ("Times burned, years since last burn and the class", False),
    ("use the location's own 1 km cell.", False),
    ("", False),
    ("The smoothed score blurs fire history over about", False),
    ("15 km, so every location has a value.", False),
    ("", False),
    ("Variables and formulas: page 2.", False),
]
y = 0.55
for text, head in notes:
    fig.text(0.64, y, text, fontsize=11 if head else 10, fontweight="bold" if head else "normal", color=INK)
    y = y - 0.032
page_footer(fig, page_no)
PAGES.append(fig)
page_no = page_no + 1

# ---- 2. data and formulas ----------------------------------------------
print(f"page {page_no}: data and formulas")
fig = plt.figure(figsize=(16, 9))
page_header(fig, "Data and formulas", "Every measure comes from one annual record of burned area on one 1 km grid")


def section(x, y, title):
    fig.text(x, y, title, fontsize=10.5, fontweight="bold", color=MUTED)
    return y - 0.042


def bullet(x, y, text, size=10.5):
    fig.text(x, y, "•", fontsize=size, color=INK)
    fig.text(x + 0.012, y, text, fontsize=size, color=INK)
    return y - 0.036


def term(x, y, symbol, meaning, gap=0.16):
    fig.text(x, y, symbol, fontsize=10.5, color=INK)
    fig.text(x + gap, y, meaning, fontsize=10, color=MUTED)
    return y - 0.036


x = 0.03
y = 0.84
y = section(x, y, "DATA")
y = bullet(x, y, "Source: NBAC National Burned Area Composite, Natural Resources Canada.")
y = bullet(x, y, "Every mapped fire perimeter in Canada, 1972–2025, small fires included.")
y = bullet(x, y, "Perimeters gridded to 1 km cells in EPSG:3978 (NAD83 / Canada Atlas Lambert).")
y = bullet(x, y, "A cell burned in a year if any fire perimeter from that year touches it.")
y = bullet(x, y, "Land mask: Natural Earth 1:50M coastline.")
y = y - 0.02
y = section(x, y, "SAMPLING OUR LOCATIONS")
y = bullet(x, y, "Each location's latitude/longitude (WGS84) is projected to EPSG:3978.")
y = bullet(x, y, "Each measure is read from the 1 km cell the location falls in.")
y = bullet(x, y, f"Locations just off the coarse coastline move to the nearest land cell within {SNAP_KM:g} km "
                 f"({fmt_int(n_snapped)} moved).")
y = bullet(x, y, f"{fmt_int(n_outside)} locations had no land within {SNAP_KM:g} km and are left blank.")
y = y - 0.02
y = section(x, y, "NOTES")
y = bullet(x, y, "Fire history only: no fuel, weather or building information.")
y = bullet(x, y, "The smoothed score is scaled to Canada's own maximum (100).")

x = 0.53
y = 0.84
y = section(x, y, "VARIABLES")
y = term(x, y, r"$y$", "a year: 1972, 1973, … 2025")
y = term(x, y, r"$n$", "number of years = 54")
y = term(x, y, r"$B_y$", "1 if any fire perimeter from year y touches the cell, else 0")
y = term(x, y, r"$w_y = (y - 1971)\,/\,n$", "year weight: 1972 → 1/54, 1973 → 2/54, … 2025 → 1")
y = term(x, y, r"$G$", "Gaussian blur with σ = 15 km")
y = y - 0.015
y = section(x, y, "MEASURES OF THE LOCATION'S OWN CELL")
y = term(x, y, r"$\mathrm{times\ burned} = \sum_y B_y$", "number of years with fire", gap=0.2)
y = term(x, y, r"$\mathrm{years\ since\ last\ burn} = 2025 - \mathrm{last}\ y\ \mathrm{with}\ B_y = 1$",
         "", gap=0.17)
y = term(x, y, "", "blank (no value) if the cell never burned", gap=0.0)
y = term(x, y, "km to nearest fire", "distance to the closest cell that burned in 1972–2025 (0 = own cell burned)",
         gap=0.11)
y = y - 0.015
y = section(x, y, "SMOOTHED SCORE (0–100)")
y = term(x, y, r"$\mathrm{freq} = \sum_y B_y\,/\,n$", "share of years with fire", gap=0.2)
y = term(x, y, r"$\mathrm{recency} = \sum_y w_y B_y\,/\,\sum_y w_y$", "same share, recent years count more",
         gap=0.2)
y = term(x, y, r"$\mathrm{score} = \mathrm{round}(100 \times G(0.5\,\mathrm{freq} + 0.5\,\mathrm{recency})\,/\,\mathrm{max})$",
         "", gap=0.17)
y = term(x, y, "", "max = the largest blurred value anywhere in Canada", gap=0.0)
y = y - 0.015
y = section(x, y, "FIRE-HISTORY CLASS")
y = term(x, y, "frequency points", "times burned  1→1   2→2   3–4→3   5–7→4   8+→5", gap=0.11)
y = term(x, y, "recency points", "years since last burn  40+→1   20–39→2   10–19→3   5–9→4   0–4→5", gap=0.11)
y = term(x, y, "class", "points total 2–3 Very low · 4–5 Low · 6–7 Moderate · 8 High · 9–10 Very high", gap=0.11)
y = term(x, y, "watchlist", f"no fire in the location's own cell, and smoothed score ≥ {WATCH_SCORE}", gap=0.11)
page_footer(fig, page_no)
PAGES.append(fig)
page_no = page_no + 1

# ---- 3. where the locations are --------------------------------------
print(f"page {page_no}: where the locations are")
fig = plt.figure(figsize=(16, 9))
page_header(fig, "Where the locations are",
            f"{fmt_int(n_matched)} locations on land, counted in 40 km hexagons")
box = [0.02, 0.05, 0.55, 0.80]
ax = fig.add_axes(box)
draw_base(ax)
draw_land(ax)
draw_outlines(ax)
width = CANADA_VIEW[1] - CANADA_VIEW[0]
hb = ax.hexbin(cell_x[ok], cell_y[ok], gridsize=int(width / 40000), extent=CANADA_VIEW, bins="log",
               mincnt=1, cmap="PuBu", linewidths=0.2, edgecolors="#4a6fa5", zorder=20)
draw_cities(ax, CANADA_VIEW)
draw_scalebar(ax, CANADA_VIEW)
finish_map(ax, CANADA_VIEW)
cax = ax.inset_axes([0.05, 0.08, 0.3, 0.03])
cb = plt.colorbar(hb, cax=cax, orientation="horizontal")
cb.ax.tick_params(labelsize=6.5)
cax.set_title("Locations per hexagon (log scale)", fontsize=7, color=INK, pad=3)

if prov_col is not None:
    by_prov = pts[ok].groupby(pts[ok][prov_col].fillna("(blank)").astype(str)).size()
    by_prov = by_prov.sort_values(ascending=True).tail(13)
    ax = fig.add_axes([0.70, 0.50, 0.27, 0.35])
    ax.barh(by_prov.index, by_prov.to_numpy(), color="#6b8fbf")
    for i, v in enumerate(by_prov.to_numpy()):
        ax.text(v, i, f"  {fmt_int(v)}", va="center", fontsize=7, color=INK)
    ax.set_xlim(0, by_prov.max() * 1.25)
    ax.set_title("Locations by province", fontsize=10.5, loc="left", color=INK, fontweight="bold")
    ax.tick_params(labelsize=7.5, colors=INK)
    ax.tick_params(axis="x", colors=MUTED)
if city_col is not None:
    by_city = pts[ok].groupby(pts[ok][city_col].fillna("(blank)").astype(str)).size()
    by_city = by_city.sort_values(ascending=True).tail(12)
    ax = fig.add_axes([0.70, 0.07, 0.27, 0.33])
    ax.barh(by_city.index, by_city.to_numpy(), color="#9fb6d6")
    for i, v in enumerate(by_city.to_numpy()):
        ax.text(v, i, f"  {fmt_int(v)}", va="center", fontsize=7, color=INK)
    ax.set_xlim(0, by_city.max() * 1.25)
    ax.set_title("Top cities", fontsize=10.5, loc="left", color=INK, fontweight="bold")
    ax.tick_params(labelsize=7.5, colors=INK)
    ax.tick_params(axis="x", colors=MUTED)
page_footer(fig, page_no)
PAGES.append(fig)
page_no = page_no + 1

view_b = points_view(ok, B_ASPECT)

# ---- 4. smoothed score -------------------------------------------------
print(f"page {page_no}: smoothed score")
fig = plt.figure(figsize=(16, 9))
page_header(fig, "Smoothed wildfire score (0–100)",
            f"Mean {np.nanmean(sc[sc_ok]):.1f}, median {np.nanmedian(sc[sc_ok]):.0f}, max {np.nanmax(sc[sc_ok]):.0f}; "
            f"{fmt_int(n_score10)} locations ({pct(n_score10, int(sc_ok.sum())):.1f}%) score 10 or more.")
ax = fig.add_axes(A_BOX)
draw_base(ax)
masked = np.ma.masked_where(~score_land, score_main.astype(np.float32))
ax.imshow(masked, cmap=SCORE_CMAP, norm=SCORE_NORM, extent=MAIN_EXTENT, origin="upper",
          interpolation="antialiased", zorder=2)
draw_outlines(ax)
draw_scalebar(ax, CANADA_VIEW)
finish_map(ax, CANADA_VIEW)
score_colorbar(ax, "Smoothed score")
panel_label(fig, A_BOX, "A", "The raster: all of Canada, 1 km")

ax = fig.add_axes(B_BOX)
draw_base(ax)
draw_land(ax)
draw_outlines(ax)
order = np.argsort(np.nan_to_num(sc, nan=-1))
sel = order[sc_ok[order]]
ax.scatter(cell_x[sel], cell_y[sel], c=sc[sel], cmap=SCORE_CMAP, norm=SCORE_NORM, s=14,
           edgecolors=INK, linewidths=0.25, zorder=20)
draw_cities(ax, view_b)
draw_scalebar(ax, view_b)
finish_map(ax, view_b)
panel_label(fig, B_BOX, "B", f"Our locations, coloured by their score{outside_note(view_b)}")

ax = fig.add_axes(C_BOX)
edges = np.arange(0, 102, 2)
hist, _ = np.histogram(sc[sc_ok], bins=edges)
centres = (edges[:-1] + edges[1:]) / 2
ax.bar(centres, hist, width=2, color=SCORE_CMAP(SCORE_NORM(centres)), edgecolor=INK, linewidth=0.3)
ax.set_yscale("log")
ax.set_xlim(0, 100)
ax.set_xlabel("Smoothed score", fontsize=7.5, color=MUTED)
ax.set_ylabel("Locations (log scale)", fontsize=7.5, color=MUTED)
style_axis(ax)
panel_label(fig, C_BOX, "C", "Distribution of our locations")

stats_box(fig, [
    ("Locations", fmt_int(sc_ok.sum())),
    ("Mean / median", f"{np.nanmean(sc[sc_ok]):.1f} / {np.nanmedian(sc[sc_ok]):.0f}"),
    ("90th percentile", f"{np.nanpercentile(sc[sc_ok], 90):.0f}"),
    ("Maximum", f"{np.nanmax(sc[sc_ok]):.0f}"),
    ("Score 0", f"{pct((sc_ok & (sc == 0)).sum(), sc_ok.sum()):.1f}%"),
    ("Score 10+", f"{pct(n_score10, sc_ok.sum()):.1f}%"),
])
formula_band(fig, [
    r"$y$ = year, 1972 … 2025 ($n$ = 54 years);   $B_y = 1$ if any NBAC fire perimeter from year $y$ touches "
    r"the 1 km cell, else 0;   $w_y = (y - 1971)\,/\,54$ = year weight (1972 → 1/54, 2025 → 1)",
    r"$\mathrm{freq} = \sum_y B_y\,/\,54$ = share of years with fire;     "
    r"$\mathrm{recency} = \sum_y w_y B_y\,/\,\sum_y w_y$ = the same share with recent years counting more",
    r"$\mathrm{score} = \mathrm{round}(100 \times G(0.5\,\mathrm{freq} + 0.5\,\mathrm{recency})\,/\,\mathrm{max})$"
    "     G = Gaussian blur, σ = 15 km;  max = the largest blurred value anywhere in Canada (scores 100)",
])
page_footer(fig, page_no)
PAGES.append(fig)
page_no = page_no + 1


# ---- 5-7. own-cell metrics: times burned, years since last burn, class
def own_cell_page(title, subtitle, grid_codes, names, colours, pt_codes, legend_title,
                  draw_c, stats_lines, formula_lines):
    fig = plt.figure(figsize=(16, 9))
    page_header(fig, title, subtitle)
    ax = fig.add_axes(A_BOX)
    draw_base(ax)
    draw_binned(ax, grid_codes, colours)
    draw_outlines(ax)
    draw_scalebar(ax, CANADA_VIEW)
    map_legend(ax, names, colours, legend_title)
    finish_map(ax, CANADA_VIEW)
    panel_label(fig, A_BOX, "A", "The raster: all of Canada, 1 km")

    ax = fig.add_axes(B_BOX)
    draw_base(ax)
    draw_land(ax)
    draw_outlines(ax)
    none = pt_codes == 0
    ax.scatter(cell_x[none], cell_y[none], s=4, c="#9aa3a9", edgecolors="none", zorder=18)
    for code in range(1, len(colours)):
        hit = pt_codes == code
        if hit.sum() > 0:
            ax.scatter(cell_x[hit], cell_y[hit], s=24, c=colours[code], edgecolors=INK,
                       linewidths=0.4, zorder=20 + code)
    draw_cities(ax, view_b)
    draw_scalebar(ax, view_b)
    finish_map(ax, view_b)
    n_hit = int((pt_codes >= 1).sum())
    panel_label(fig, B_BOX, "B", f"Our {fmt_int(n_hit)} locations with fire in their cell; "
                                 f"grey = no fire in record{outside_note(view_b)}")

    ax = fig.add_axes(C_BOX)
    draw_c(ax)
    stats_box(fig, stats_lines)
    formula_band(fig, formula_lines)
    return fig


print(f"page {page_no}: times burned")
nb_grid = bin_nburns(grids["nburns"]["data"], land)
nb_counts, _ = shares(nb_pt_codes, 6)


nb_ok = ok & ~np.isnan(nb)
nb_stats = [
    ("Locations", fmt_int(nb_ok.sum())),
    ("Burned at least once", f"{fmt_int(n_burned)} ({pct(n_burned, nb_ok.sum()):.1f}%)"),
    ("Burned 2+ times", fmt_int((ok & (nb >= 2)).sum())),
    ("Most times burned", f"{np.nanmax(nb[nb_ok]):.0f}" if nb_ok.sum() else "–"),
    ("Mean, burned only", f"{np.nanmean(nb[burned_pt]):.1f}" if n_burned else "–"),
    ("Canadian land burned", f"{100 - cls_land_share[0]:.1f}%"),
]
nb_formula = [
    r"$\mathrm{times\ burned} = \sum_{y=1972}^{2025} B_y$      "
    r"$B_y = 1$ if any NBAC fire perimeter from year $y$ touches the 1 km cell",
    "Counts years with fire, not individual fires.",
    "Most of our locations are 0 because their own cell never burned.",
]
def draw_nothing(ax):
    ax.axis("off")


def make_nb_page():
    page = own_cell_page("Times burned, 1972–2025",
                         f"{fmt_int(n_burned)} locations ({pct(n_burned, nb_ok.sum()):.1f}%) sit on a cell that "
                         f"burned at least once.",
                         nb_grid, NBURN_NAMES, SIX_COLOURS, nb_pt_codes, "Times burned",
                         draw_nothing, nb_stats, nb_formula)
    ax = page.add_axes(C_BOX)
    bar_counts(ax, ["Never", "1", "2", "3–4", "5–7", "8+"], SIX_COLOURS, nb_counts.astype(float))
    ax.set_xlabel("Years with fire in the location's cell", fontsize=7.5, color=MUTED)
    panel_label(page, C_BOX, "C", "Our locations by times burned")
    return page


PAGES.append(make_nb_page())
del nb_grid
page_no = page_no + 1

print(f"page {page_no}: years since last burn")
ts_grid = bin_tslb(grids["tslb"]["data"], land)
ts_burned = ok & (ts >= 0)
n_ts = int(ts_burned.sum())
ts_stats = [
    ("Fire in record", f"{fmt_int(n_ts)} ({pct(n_ts, ok.sum()):.1f}%)"),
    ("No fire in record", fmt_int((ok & (ts < 0)).sum())),
    ("Median years since", f"{np.nanmedian(ts[ts_burned]):.0f}" if n_ts else "–"),
    ("Burned 2021–2025", fmt_int((ts_burned & (ts <= 4)).sum())),
    ("Burned 2016–2025", fmt_int((ts_burned & (ts <= 9)).sum())),
    ("Most recent year", f"{2025 - np.nanmin(ts[ts_burned]):.0f}" if n_ts else "–"),
]
ts_formula = [
    r"$\mathrm{years\ since\ last\ burn} = 2025 - \mathrm{max}\{\,y : B_y = 1\,\}$",
    "Only defined where the cell burned at least once, so the histogram covers only those locations.",
    "Cells with no fire in the record have no value.",
]
page = own_cell_page("Years since last burn",
                     f"{fmt_int(n_ts)} locations have fire in their cell's record; "
                     f"{fmt_int((ts_burned & (ts <= 9)).sum())} of them burned in 2016–2025.",
                     ts_grid, TSLB_NAMES, SIX_COLOURS, ts_pt_codes, "Years since last burn",
                     draw_nothing, ts_stats, ts_formula)
ax = page.add_axes(C_BOX)
if n_ts > 0:
    edges = np.arange(-0.5, 54.5, 1.0)
    hist, _ = np.histogram(ts[ts_burned], bins=edges)
    years = np.arange(0, 54)
    bar_cols = []
    for yv in years:
        code = bin_tslb(np.array([float(yv)]), np.array([True]))[0]
        bar_cols.append(SIX_COLOURS[code])
    ax.bar(years, hist, width=1.0, color=bar_cols, edgecolor=INK, linewidth=0.3)
    ax.set_xlim(-1, 54)
    ax.invert_xaxis()
ax.set_xlabel("Years since last burn (counted back from 2025; recent on the right)", fontsize=7.5, color=MUTED)
ax.set_ylabel("Locations", fontsize=7.5, color=MUTED)
style_axis(ax)
panel_label(page, C_BOX, "C", f"Our {fmt_int(n_ts)} locations with fire in the record")
PAGES.append(page)
del ts_grid
page_no = page_no + 1

print(f"page {page_no}: fire-history class")
cls_counts, _ = shares(cls_pt_codes, 6)
cls_stats = [
    ("No fire in record", f"{fmt_int(cls_counts[0])} ({pct(cls_counts[0], cls_counts.sum()):.1f}%)"),
    ("Very low / Low", fmt_int(cls_counts[1] + cls_counts[2])),
    ("Moderate", fmt_int(cls_counts[3])),
    ("High", fmt_int(cls_counts[4])),
    ("Very high", fmt_int(cls_counts[5])),
    ("Canadian land, any class", f"{100 - cls_land_share[0]:.1f}%"),
]
cls_formula = [
    "Frequency points from times burned:  1 → 1   2 → 2   3–4 → 3   5–7 → 4   8+ → 5        "
    "Recency points from years since last burn:  40+ → 1   20–39 → 2   10–19 → 3   5–9 → 4   0–4 → 5",
    "Total = frequency + recency (2–10):   2–3 Very low  ·  4–5 Low  ·  6–7 Moderate  ·  8 High  ·  9–10 Very high",
    "Uses times burned and years since last burn for the location's own cell. A cell with no fire in the record has no class.",
]
page = own_cell_page("Fire-history class",
                     f"{fmt_int(cls_counts[1:].sum())} locations ({pct(cls_counts[1:].sum(), cls_counts.sum()):.1f}%) "
                     f"have a class; {fmt_int(cls_counts[3:].sum())} are Moderate or higher.",
                     cls_grid, CLASS_NAMES, SIX_COLOURS, cls_pt_codes, "Fire-history class",
                     draw_nothing, cls_stats, cls_formula)
ax = page.add_axes(C_BOX)
bar_counts(ax, ["No fire", "Very low", "Low", "Moderate", "High", "Very high"], SIX_COLOURS,
           cls_counts.astype(float))
panel_label(page, C_BOX, "C", "Our locations by class")
PAGES.append(page)
del cls_grid
page_no = page_no + 1

# ---- watchlist ---------------------------------------------------------
print(f"page {page_no}: watchlist")
fire_free = score_land & (grids["cls"]["data"] == 0)
watch_land_share = pct(int((fire_free & (score_main >= WATCH_SCORE)).sum()), int(fire_free.sum()))
fig = plt.figure(figsize=(16, 9))
page_header(fig, "Watchlist: fire nearby, none in the location's own cell",
            f"{fmt_int(n_watch)} locations have no fire in their own 1 km cell but a smoothed score of "
            f"{WATCH_SCORE} or more (the top {watch_land_share:.0f}% of fire-free Canadian land).")
box = [0.02, 0.06, 0.44, 0.78]
ax = fig.add_axes(box)
if n_watch > 0:
    view_w = points_view(watch, (box[2] * 16) / (box[3] * 9))
else:
    view_w = view_b
draw_base(ax)
masked = np.ma.masked_where(~score_land, score_main.astype(np.float32))
ax.imshow(masked, cmap=SCORE_CMAP, norm=SCORE_NORM, extent=MAIN_EXTENT, origin="upper",
          interpolation="antialiased", alpha=0.75, zorder=2)
draw_outlines(ax)
others = ok & ~watch
ax.scatter(cell_x[others], cell_y[others], s=5, c="#7d8890", edgecolors="none", zorder=18)
ax.scatter(cell_x[watch], cell_y[watch], s=34, c="#6a1b9a", edgecolors="white", linewidths=0.6, zorder=22)
draw_cities(ax, view_w)
draw_scalebar(ax, view_w)
finish_map(ax, view_w)
map_legend(ax, ["Watchlist location", "Other location"], ["#6a1b9a", "#7d8890"], None, loc="upper left")
score_colorbar(ax, "Smoothed score")
panel_label(fig, box, "", f"Watchlist locations over the smoothed score{outside_note(view_w)}")

wl = pts[watch].copy()
wl = wl.sort_values(["wf_score_0_100", "wf_km_to_fire"], ascending=[False, True])
if id_col is not None:
    wl = wl.drop_duplicates(id_col)
cols_out = {}
if id_col is not None:
    cols_out["ID"] = wl[id_col].astype(str).str.slice(0, 14)
if name_col is not None:
    cols_out["Name"] = wl[name_col].astype(str).str.slice(0, 26)
if city_col is not None:
    cols_out["City"] = wl[city_col].astype(str).str.slice(0, 18)
if prov_col is not None:
    cols_out["Province"] = wl[prov_col].astype(str).str.slice(0, 18)
cols_out["Score"] = wl["wf_score_0_100"].map("{:.0f}".format)
cols_out["Km to nearest fire"] = wl["wf_km_to_fire"].map("{:.0f}".format)
if value_col is not None:
    cols_out["Value"] = pd.to_numeric(wl[value_col], errors="coerce").map("{:,.0f}".format)
wl_table = pd.DataFrame(cols_out).head(24)
ax = fig.add_axes([0.49, 0.06, 0.49, 0.76])
widths = []
for col in wl_table.columns:
    if col == "Score":
        widths.append(0.07)
    elif col == "Km to nearest fire":
        widths.append(0.16)
    elif col == "Name":
        widths.append(0.25)
    else:
        widths.append(0.15)
draw_table(ax, wl_table, widths)
ax.set_title(f"Top {len(wl_table)} by smoothed score (one row per location ID). Km to nearest fire = "
             "distance from the location's cell to the closest cell that burned in 1972–2025.",
             fontsize=8.5, color=MUTED, loc="left", wrap=True)
page_footer(fig, page_no)
PAGES.append(fig)
page_no = page_no + 1

# ---- 9. by province ----------------------------------------------------
if prov_col is not None:
    print(f"page {page_no}: provinces")
    fig = plt.figure(figsize=(16, 9))
    page_header(fig, "By province", "Darker shading = higher value within the column")
    sub = pts[ok].copy()
    sub["_prov"] = sub[prov_col].fillna("(blank)").astype(str)
    sub["_score10"] = (sub["wf_score_0_100"] >= 10).astype(int)
    sub["_fire"] = 100.0 * (sub["wf_times_burned"] >= 1)
    sub["_mod"] = (sub["wf_class_5cat"] >= 3).astype(int)
    sub["_high"] = (sub["wf_class_5cat"] >= 4).astype(int)
    sub["_watch"] = sub["wf_watchlist"].astype(int)
    grp = sub.groupby("_prov")
    tab = pd.DataFrame({
        "Locations": grp.size(),
        "Mean score": grp["wf_score_0_100"].mean(),
        "Max score": grp["wf_score_0_100"].max(),
        "Score 10+": grp["_score10"].sum(),
        "% fire in cell": grp["_fire"].mean(),
        "Moderate+": grp["_mod"].sum(),
        "High+": grp["_high"].sum(),
        "Watchlist": grp["_watch"].sum(),
    })
    if value_col is not None:
        sub["_val"] = pd.to_numeric(sub[value_col], errors="coerce")
        tab["Value"] = sub.groupby("_prov")["_val"].sum()
        tab["Value, score 10+"] = sub[sub["wf_score_0_100"] >= 10].groupby("_prov")["_val"].sum().reindex(tab.index).fillna(0)
    tab = tab.sort_values("Locations", ascending=False).head(16)
    shade = tab.copy()
    show = tab.reset_index().rename(columns={"_prov": "Province"})
    for col in show.columns:
        if col in ["Mean score", "% fire in cell"]:
            show[col] = show[col].map("{:.1f}".format)
        elif col != "Province":
            show[col] = show[col].map("{:,.0f}".format)
    ax = fig.add_axes([0.02, 0.06, 0.96, 0.78])
    widths = [0.16]
    for col in show.columns[1:]:
        widths.append(0.84 / (len(show.columns) - 1))
    table = draw_table(ax, show, widths, fontsize=9)
    if table is not None:
        cmap = plt.get_cmap("YlOrBr")
        for j, col in enumerate(tab.columns):
            if col in ["Locations", "Value"]:
                continue
            column = shade[col].to_numpy(dtype=float)
            top_v = np.nanmax(column) if len(column) else 0
            for i in range(len(column)):
                if top_v > 0 and column[i] > 0:
                    table[(i + 1, j + 1)].set_facecolor(mcolors.to_hex(cmap(0.08 + 0.5 * column[i] / top_v)))
    page_footer(fig, page_no)
    PAGES.append(fig)
    page_no = page_no + 1

# ---- 10. summary statistics ---------------------------------------------
print(f"page {page_no}: summary statistics")
fig = plt.figure(figsize=(16, 9))
page_header(fig, "Summary statistics", "Each value is read at the location's own 1 km cell")


def stat_row(name, values, applies, share_label_value):
    v = values[~np.isnan(values)]
    if len(v) == 0:
        return [name, applies, "0", "–", "–", "–", "–", "–"]
    return [name, applies, fmt_int(len(v)), f"{np.mean(v):.2f}", f"{np.median(v):.2f}",
            f"{np.percentile(v, 90):.2f}", f"{np.max(v):.2f}", share_label_value]


def share_pos(values):
    v = values[~np.isnan(values)]
    if len(v) == 0:
        return "–"
    return f"{100.0 * (v > 0).mean():.1f}%"


rows = [
    stat_row("Smoothed score (0–100)", sc[sc_ok], "all locations", share_pos(sc[sc_ok])),
    stat_row("Times burned, 1972–2025", nb[nb_ok], "all locations", share_pos(nb[nb_ok])),
    stat_row("Burn rate, 1972–2025", vals["rate_full"][ok], "all locations", share_pos(vals["rate_full"][ok])),
    stat_row("Burn rate, 2021–2025", vals["rate_recent"][ok], "all locations", share_pos(vals["rate_recent"][ok])),
    stat_row("Years since last burn", ts[ts_burned].astype(float), "fire in record only", "–"),
    stat_row("Km to nearest fire", vals["dist"][ok], "all locations", share_pos(vals["dist"][ok])),
]
stats_df = pd.DataFrame(rows, columns=["Measure", "Applies to", "Locations", "Mean", "Median",
                                       "90th pct", "Max", "Share > 0"])
ax = fig.add_axes([0.02, 0.45, 0.96, 0.40])
draw_table(ax, stats_df, [0.2, 0.13, 0.09, 0.09, 0.09, 0.09, 0.09, 0.09], fontsize=10)
notes = [
    "Burn rate = times burned / number of years in the window (54 for 1972–2025, 5 for 2021–2025).",
    "Km to nearest fire = straight-line distance from the location's cell to the closest cell that burned in 1972–2025 (0 = its own cell burned).",
    f"Not matched: {fmt_int(n_outside)} locations had no land cell within {SNAP_KM:g} km and are left blank in the spreadsheet.",
]
y = 0.36
for line in notes:
    fig.text(0.03, y, line, fontsize=10, color=INK)
    y = y - 0.04
page_footer(fig, page_no)
PAGES.append(fig)
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
