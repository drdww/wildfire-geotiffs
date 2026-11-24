# Prompt: sample wildfire history at my points

Paste the block below into Claude Code (or any assistant with a terminal) on the machine
that has your points file, after filling in the three `<...>` values. Or follow the steps
yourself.

```text
I need wildfire-history values for a CSV of lat/lon points in Canada and the US,
using the public repo https://github.com/drdww/wildfire-geotiffs.

My points file: <PATH_TO_MY_POINTS.csv>
Latitude column: <LAT_COLUMN>      Longitude column: <LON_COLUMN>

1. Download BOTH zips and unzip them into the same folder (they merge into one
   wildfire-geotiffs/ folder with 19 rasters):
     https://github.com/drdww/wildfire-geotiffs/releases/latest/download/wildfire_CANADA_1km.zip
     https://github.com/drdww/wildfire-geotiffs/releases/latest/download/wildfire_US_1km.zip
   If downloads are blocked, try: git clone https://github.com/drdww/wildfire-geotiffs.git
2. pip install rasterio pyproj pandas numpy
3. Read wildfire-geotiffs/README.md and wildfire-geotiffs/METHODS.md.
4. Smoke test, from wildfire-geotiffs/scripts:
     python sample_points.py example_points.csv test.csv --final-only
   It must match example_output_final_only.csv. In particular: Fort McMurray = Low,
   Malibu = Very high, Toronto = No fire in record with final_snap_km 1.03,
   Atlantic Ocean = blank. Stop and tell me if it does not match.
5. Run on my file:
     python sample_points.py <PATH_TO_MY_POINTS.csv> sampled.csv --lat <LAT_COLUMN> --lon <LON_COLUMN> --final-only
   (Drop --final-only to also keep one column per raster.)
6. Report: total rows; rows per region; count per final_class_label; how many points
   have final_snap_km > 0; and list any rows with region = outside.

Rules for interpreting the output:
- final_class_5cat / final_class_label is the final score: 1 Very low, 2 Low,
  3 Moderate, 4 High, 5 Very high, 0 No fire in record. 0 means unknown, not safe.
- Every final_* value comes from the point's own region (Canada = NBAC,
  California = CAL FIRE FRAP, rest of the lower 48 = MTBS). Do not mix in other
  regions' columns.
- final_tslb_yr = -1 means no fire in the record (censored): treat it as unknown,
  not as zero and not as the record length.
- Classes and final_score_0_100 are NOT comparable across regions (different record
  lengths; MTBS misses fires under 500-1,000 acres). Compare within a region.
- Blank final_* values mean the point is off land or outside Canada / the lower 48
  (Alaska and Hawaii are not covered).
- Do not modify the rasters or the script.
```

## What you get with `--final-only`

Your columns, then:

| Column | Meaning |
|---|---|
| `region` | Canada / California / CONUS / outside |
| `final_source` | NBAC 1972-2025, CAL FIRE FRAP 1878-2025, or MTBS 1984-2026 |
| `final_burn_rate_full` | share of the full record's years with fire, 0–1 |
| `final_burn_rate_recent` | same for the last 5 years (Canada and California 2021–25, CONUS 2020–24) |
| `final_tslb_yr` | years since last fire, counted back from 2025; `-1` = none in record |
| `final_times_burned` | number of years with fire |
| `final_score_0_100` | smoothed score, relative to the region's maximum |
| `final_class_5cat` | **final score**, 0–5 |
| `final_class_label` | the same as a word |
| `final_snap_km` | how far the point was moved onto land (0 = not moved) |

That is your columns + 10. Without `--final-only` you also get one `snap_km_<region>`
per region and one column per raster: your columns + 32 with both zips, or your columns
+ 17 with the Canada zip alone.
