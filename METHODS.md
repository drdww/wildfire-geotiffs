# Methods — how every raster is computed

All rasters are 1 km grids. A cell **burned in year y** if any fire perimeter from that
year touches it (all-touched rasterization), so a 1 km cell counts even when a fire only
clips its edge.

Notation, per cell:

```
B_y  = 1 if the cell burned in year y, else 0
Y    = the years in the window (only years with at least one mapped fire in the source)
n    = number of years in Y
```

## Burn rate — `risk_rate_*`, `ca_risk_rate_*`

```
burn_rate = Σ_{y in Y} B_y / n
```

Share of the window's years in which the cell burned, 0–1. No smoothing.

| File | Window | n |
|---|---|---|
| `risk_rate_1972_2025_1km` (Canada) | 1972–2025 | 54 |
| `risk_rate_2021_2025_1km` (Canada) | 2021–2025 | 5 |
| `ca_risk_rate_1950_2025_1km` | 1950–2025 | 76 |
| `ca_risk_rate_2021_2025_1km` | 2021–2025 | 5 |
| `risk_rate_conus_1984_2026_1km` | 1984–2026 | 43 |
| `risk_rate_conus_2020_2024_1km` | 2020–2024 | 5 |

A 5-year window can only take the values 0, 0.2, 0.4, 0.6, 0.8 and 1.0. The CONUS recent
window is 2020–2024 rather than 2022–2026 because MTBS lags 1–2 years: it holds 54 fires
for 2025 and 20 for 2026, against 339–530 in a normal year.

`-1` = off the land mask.

## Times burned — `nburns_*`

```
nburns = Σ_y B_y        over the full record
```

`0` = never burned (on land) or off land.

## Time since last burn — `tslb_*`

```
tslb = 2025 − (last year with B_y = 1)
```

Measured back from 2025 in every region.

- `-1` on land = **no fire in the record**. The true value is unknown but at least the
  record length (Canada 54 yr, California 148 yr, CONUS 42 yr). Treat it as censored,
  not as zero and not as the record length.
- `0` = burned in 2025, or in CONUS also 2026 (MTBS includes some 2026 fires).

## Smoothed risk score — `wildfire_risk_score_*`

```
w_y     = rank of y in Y / n                 (oldest year = 1/n, newest = 1)

freq    = Σ B_y / n
recency = Σ w_y · B_y / Σ w_y

raw     = 0.5 · freq + 0.5 · recency
smooth  = GaussianFilter(raw, σ = 15 km)
score   = round(100 · smooth / max(smooth))
```

Stored as uint8, 0–100; `255` = off land.

- **Equal weight on how often and how recently** a cell burned.
- **The Gaussian (σ = 15 km)** spreads risk into neighbouring unburned cells, on the
  view that fuels, climate and ignition patterns extend beyond past perimeters. The
  score is therefore non-zero well beyond burned ground: 59% of Canada and 84% of
  California, against 18% and 36% that actually burned.
- **Normalised to each region's own maximum.** 100 is the most fire-intense
  neighbourhood in that region. Scores are not comparable across regions; use burn
  rate for anything quantitative.

| File | Source | Y | n |
|---|---|---|---|
| `wildfire_risk_score_canada_1km` | NBAC | 1972–2025 | 54 |
| `wildfire_risk_score_california_1km` | CAL FIRE FRAP | 1878–2025, years with a mapped fire | 128 |
| `wildfire_risk_score_conus_1km` | MTBS | 1984–2026 | 43 |

California's record spans 148 calendar years, but only 128 have a mapped fire, and the
score uses those 128. The early decades are sparse, so this barely changes the map.

## 5-category fire-history class — `risk_class_5cat_*`

Built from times burned and time since last burn on each burned cell
(`scripts/make_risk_class_5cat.py` rebuilds it from the rasters in this repo):

| Times burned | Frequency points | | Time since last burn | Recency points |
|---|---|---|---|---|
| 1 | 1 | | 40+ yr | 1 |
| 2 | 2 | | 20–39 yr | 2 |
| 3–4 | 3 | | 10–19 yr | 3 |
| 5–7 | 4 | | 5–9 yr | 4 |
| 8+ | 5 | | 0–4 yr | 5 |

```
total = frequency points + recency points        (2 to 10)
```

| Total | Code | Class |
|---|---|---|
| 2–3 | 1 | Very low |
| 4–5 | 2 | Low |
| 6–7 | 3 | Moderate |
| 8 | 4 | High |
| 9–10 | 5 | Very high |

`0` = on land but **no fire in the record**, deliberately its own code rather than
class 1: an unburned cell is unknown, not low. `255` = off land.

Share of land by class:

| Region | No fire | Very low | Low | Moderate | High | Very high |
|---|---:|---:|---:|---:|---:|---:|
| Canada | 82.1% | 7.66 | 4.71 | 5.22 | 0.32 | 0.01 |
| California | 63.8% | 10.17 | 11.00 | 11.72 | 2.53 | 0.76 |
| CONUS | 91.5% | 2.09 | 4.05 | 2.17 | 0.16 | 0.02 |

Classes are **not comparable across regions**. Longer records have more chances to
accumulate burns (Canada 54 yr, California 148 yr, CONUS 43 yr), and CONUS misses fires
under 500–1,000 acres. For a California point, use the California class rather than the
CONUS one: at Malibu the California class is Very high (9 burns) while the CONUS class is
Moderate (2 mapped burns).

This is a history class, not a hazard forecast: recently burned ground can carry less
fuel in the near term.

## CAL FIRE hazard zones — `fhsz_california_1km`

CAL FIRE Fire Hazard Severity Zones 2024, rasterized onto the California 1 km grid,
highest class winning where zones overlap: 1 = Moderate, 2 = High, 3 = Very High.
`0` = not zoned. FHSZ is mapped only in State and Local Responsibility Areas, so federal
land (most national forest and park land) is 0.

## Land masks and projections

| Region | CRS | Land mask |
|---|---|---|
| Canada | EPSG:3978 (NAD83 / Canada Atlas Lambert) | Natural Earth 1:50M Canada |
| California | EPSG:3310 (NAD83 / California Albers) | Natural Earth 1:50M California |
| CONUS | EPSG:5070 (NAD83 / CONUS Albers) | Natural Earth 1:50M, lower 48 + DC |

The coarse coastline is why waterfront points can fall off the mask;
`scripts/sample_points.py` moves such points to the nearest land cell within 3 km.

## Distance to the nearest mapped fire (computed in the report)

```
d = Euclidean distance from the cell centre to the nearest cell with nburns ≥ 1   (km)
```

`report/canada_wildfire_report.py` computes this from `nburns_canada_1km` with an exact
distance transform. It is not shipped as a raster. `0` = the cell itself burned.

## Change log

- **v1.4** — added `report/canada_wildfire_report.py` (a one-cell PDF report for Canadian
  locations) and `boundaries/na_admin1.geojson` (Natural Earth 1:50M provinces and states,
  public domain).

- **v1.3** — `sample_points.py` writes `final_*` columns: the same fields for every
  point, read from the point's own region (Canada NBAC, California FRAP, rest of CONUS
  MTBS), with `final_class_5cat` / `final_class_label` as the final score. New
  `--final-only` flag. Added `PROMPT.md`.
- **v1.2** — added `risk_class_5cat_{canada,california,conus}_1km` and
  `scripts/make_risk_class_5cat.py`. The sampler now writes `snap_km_*` columns only for
  regions whose rasters are present.
- **v1.1** — `tslb_conus_1km`: 7,330 cells whose last burn was in 2026 were coded `-1`
  ("no fire in record") because 2025 − 2026 = −1. They are now `0`.
- **v1.0** — first release.
