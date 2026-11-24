# Wildfire history GeoTIFFs — Canada, California, CONUS (1 km)

Nineteen 1 km rasters describing observed wildfire history, plus a script that samples
them at lat/lon points. `MANIFEST.csv` lists each file's region, product, period, CRS,
dtype, nodata and value meaning. **[METHODS.md](METHODS.md)** gives the exact formula
for every layer, including the smoothed risk score.

## Download

Easiest: the two zips under **Releases** (each holds the rasters for one country plus
the script). Or clone the repo:

```
git clone https://github.com/drdww/wildfire-geotiffs.git
```

## Sample your points

**Quickest route: [PROMPT.md](PROMPT.md)** — a ready-to-paste prompt for Claude Code (or
steps to follow yourself) that downloads both zips, checks the setup and samples your file.

Unzip **both** release zips into the same folder, then:

```
pip install rasterio pyproj pandas numpy
python scripts/sample_points.py my_points.csv sampled.csv --lat lat --lon lon --final-only
```

**The answer is in the `final_*` columns.** For every point the script reads the rasters
of the region the point falls in (Canada = NBAC, California = CAL FIRE FRAP, rest of the
lower 48 = MTBS) and writes one consistent set of fields, so Canadian and US rows line up:
`final_source`, `final_burn_rate_full`, `final_burn_rate_recent`, `final_tslb_yr`,
`final_times_burned`, `final_score_0_100`, **`final_class_5cat`** / **`final_class_label`**
(the final score) and `final_snap_km`.

`--final-only` gives your columns + `region` + those 10. Without it you also get every
raster as its own column: your columns + 32 with both zips. Check your setup against
`scripts/example_points.csv` → `scripts/example_output_final_only.csv`.

## PDF reports for a set of locations (Canada and USA)

**[report/canada_wildfire_report.py](report/canada_wildfire_report.py)** and
**[report/usa_wildfire_report.py](report/usa_wildfire_report.py)** are one notebook cell each.
Edit the paths, run it, and you get a 10-page 16:9 PDF, slide-ready PNGs and an .xlsx with
the sampled columns. The smoothed score leads. Times burned, years since last burn and the class
each get a page with the raster, your locations on a basemap, a distribution and the formula.
The pages also include where your locations are, the top 25 by score, a by-province table and
summary statistics. See
[report/README.md](report/README.md).

## What the sampler handles

The script handles three traps a plain `rasterio.sample` call falls into:

1. **Different projections.** Canada is EPSG:3978, California EPSG:3310, CONUS
   EPSG:5070. Lat/lon is reprojected into each raster's CRS.
2. **Nodata that is also a real value.** In `tslb_*`, `-1` means off land *or* no fire
   in the record. In `nburns_*`, `0` means off land *or* never burned. On/off land is
   decided from the burn-rate raster of the same region, and on land `-1` / `0` are kept.
3. **Coastlines.** The land masks use a coarse coastline, so waterfront points can fall
   "in the water". Such points move to the nearest land cell within 3 km (`--snap-km`);
   the distance is recorded in `snap_km_<region>`.

## Files

| Region | Burn rate, full record | Burn rate, recent 5 yr | Time since last burn | Times burned | Smoothed score | 5-category class |
|---|---|---|---|---|---|---|
| Canada | `risk_rate_1972_2025_1km` | `risk_rate_2021_2025_1km` | `tslb_canada_1km` | `nburns_canada_1km` | `wildfire_risk_score_canada_1km` | `risk_class_5cat_canada_1km` |
| California | `ca_risk_rate_1950_2025_1km` | `ca_risk_rate_2021_2025_1km` | `tslb_california_1km` | `nburns_california_1km` | `wildfire_risk_score_california_1km` | `risk_class_5cat_california_1km` |
| CONUS | `risk_rate_conus_1984_2026_1km` | `risk_rate_conus_2020_2024_1km` | `tslb_conus_1km` | `nburns_conus_1km` | `wildfire_risk_score_conus_1km` | `risk_class_5cat_conus_1km` |

Plus `fhsz_california_1km`: CAL FIRE Fire Hazard Severity Zones (1 Moderate, 2 High,
3 Very High; 0 = not zoned, which includes federal land).

California points also fall inside the CONUS rasters; use the California ones, which
include small fires.

## Reading the values

- **Burn rate**: share of the window's years in which a fire touched the cell
  (0.05 = fire in 1 year out of 20).
- **Time since last burn**: years before 2025. `-1` on land means no fire in the record —
  unknown, but at least the record length. Treat it as censored, not as zero or as the
  record length.
- **Times burned**: number of separate years with fire.
- **Smoothed score** (0–100):
  `round(100 · G(0.5·freq + 0.5·recency) / max)`, where `G` is a Gaussian blur with
  σ = 15 km, `freq` is the share of years burned, and `recency` is the same share with
  later years weighted more (see [METHODS.md](METHODS.md)). Scaled to each region's own
  maximum, so not comparable across regions; use burn rate for anything quantitative.

- **5-category class**: frequency points (times burned) + recency points (time since
  last burn), binned to 1 Very low … 5 Very high. `0` = no fire in the record (on land),
  kept separate from class 1 on purpose. Full rule in [METHODS.md](METHODS.md).

These are observed history, not a hazard forecast, and area burned is not loss.

## Sources and limits

| Region | Source | Record | Small fires |
|---|---|---|---|
| Canada | NBAC — National Burned Area Composite, Natural Resources Canada | 1972–2025 | included |
| California | CAL FIRE FRAP fire perimeters; CAL FIRE FHSZ 2024 | 1878–2025 (rates from 1950) | included |
| CONUS | MTBS — Monitoring Trends in Burn Severity (USGS/USFS), wildfires only | 1984–2026 | **missing** under 1,000 ac (West) / 500 ac (East) |

- MTBS lags 1–2 years, so 2025–2026 are incomplete and the CONUS recent window is
  2020–2024.
- Gridded with an all-touched rule: a 1 km cell counts if any fire perimeter touches it.

Source data: NBAC under the Open Government Licence – Canada; MTBS and CAL FIRE data are
public US government data. Please cite the original providers.
