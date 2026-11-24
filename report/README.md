# Canada wildfire-history report

`canada_wildfire_report.py` is a single notebook cell that samples the six Canada rasters at
your locations and writes a 10-page 16:9 PDF report, one PNG per page for slides, and your rows back
out with the sampled `wf_*` columns.

## Run it

1. Put the six Canada rasters in one folder (the `tifs/` folder of
   `wildfire_CANADA_1km.zip`).
2. Copy the whole file into one notebook cell (Databricks, Jupyter or Colab).
3. Edit the four paths at the top: `BASE_DIR`, `POINTS_FILE` (.xlsx or .csv), `TIF_DIR`,
   `OUT_DIR`. Set `VALUE_COL` if you want insured value in the tables.
4. Run. It takes about 2 minutes, mostly drawing the maps.

Needs `numpy pandas rasterio pyproj scipy matplotlib openpyxl`, plus `geopandas` for the
province outlines.

**Columns are detected automatically:** `Latitude` / `Longitude`, `CountryCode` (keeps
CA / CAN / Canada), `LOCID`, `LOCNAME`, `CityName` and `Admin1Name`, or common variants such as
`lat`, `lon` and `province`. If a name differs, set `LAT_COL`, `LON_COL` and the others.

**Province outlines** are read from `boundaries/na_admin1.geojson`. The script looks next to
the `tifs/` folder first, then downloads the file from this repo. If neither works, it draws
the land-mask outline instead.

## The pages

| Page | Content |
|---|---|
| 1 | Summary numbers, and the score and class mix of our sampled locations against all Canadian land |
| 2 | Data, sampling, every variable and formula |
| 3 | Where the locations are: 40 km hexagon counts on a basemap, plus counts by province and city |
| 4 | Smoothed score: continuous raster, our locations coloured by score, histogram, key numbers, formula |
| 5 | Times burned: raster, burned locations on a basemap, bar chart, key numbers, formula |
| 6 | Years since last burn: same layout; the histogram covers only locations with fire in the record |
| 7 | Fire-history class: same layout |
| 8 | Watchlist: no fire in the location's own cell, smoothed score ≥ 10; map + table with km to nearest fire |
| 9 | By province |
| 10 | Summary statistics |

## Columns added to your rows

| Column | Meaning |
|---|---|
| `wf_matched` | the point found a land cell (directly or within `SNAP_KM`) |
| `wf_snap_km` | how far it was moved onto land (0 = not moved) |
| `wf_class_5cat`, `wf_class_label` | **final score**: 0 No fire in record, 1 Very low … 5 Very high |
| `wf_score_0_100` | smoothed score, relative to Canada's maximum |
| `wf_km_to_fire` | distance from the cell to the nearest cell that burned in 1972–2025 |
| `wf_times_burned` | years with fire, 1972–2025 |
| `wf_burn_rate_1972_2025`, `wf_burn_rate_2021_2025` | share of years with fire |
| `wf_tslb_yr`, `wf_last_burn_year` | years since the last burn (counted back from 2025), and that year; `-1` / blank = none in the record |
| `wf_watchlist` | class = No fire in record and smoothed score ≥ `WATCH_SCORE` (default 10) |

The distance layer is computed inside the script, with an exact Euclidean distance transform
of `nburns_canada_1km`. It is not a separate file.

## Preview

These pages were made from **synthetic** locations (jittered points around Canadian cities).
Some fall in lakes or the sea, which is why they show as unmatched.

![summary](preview/page_01.png)
![formulas](preview/page_02.png)
![score](preview/page_04.png)
![watchlist](preview/page_08.png)
