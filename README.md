# Space Coast BUFKIT Skew-T Viewer (GitHub Pages)

Adapted from andykren08/skew-t-viewer for Cape Canaveral / KSC and surrounding
Florida sites: XMR, MLB, VRB, FPR, MCO, ISM, LEE, DAB, LAL, TPA, PBI, JAX, GNV.

`index.html` is the viewer. A GitHub Action downloads the latest three runs of
each model for the stations listed in `scripts/fetch_bufkit.py`, compresses
them, and publishes them next to the viewer. The station, model and **run**
dropdowns then work.

## Where the data comes from

| Model | Source | Path / model | Runs |
|-------|--------|--------------|------|
| GFS   | Penn State BUFKIT | `data/GFS/<HH>/gfs3_<stn>.buf`  | 00/06/12/18Z |
| RAP   | Penn State BUFKIT | `data/RAP/<HH>/rap_<stn>.buf`   | hourly |
| HRRR  | Penn State BUFKIT | `data/HRRR/<HH>/hrrr_<stn>.buf` | hourly |
| RRFS  | Penn State BUFKIT | `data/RRFS/<HH>/rrfs_<stn>.buf` | 00/06/12/18Z |
| ECMWF | Open-Meteo        | `ecmwf_ifs025` pressure levels  | 00/06/12/18Z |

- PSU base: `https://www.meteo.psu.edu/bufkit/data/`. There is one folder per
  cycle hour, overwritten daily, so the last 24 hours of cycles are always
  there. PSU spells Cape Canaveral `xmr` (no K).
- ECMWF is not a BUFKIT file. Open-Meteo extracts the IFS 0.25 deg grid column
  nearest each station from the ECMWF open data. The script writes it in BUFKIT
  text format: a surface row plus 13 levels (1000, 925, 850, 700, 600, 500, 400,
  300, 250, 200, 150, 100 and 50 hPa). Each level has T, Td, wind and height;
  Tw and theta-e are calculated, and omega is converted from Open-Meteo's w in
  m/s, which is rounded to 0.01 m/s, so it is coarse.
  - 3-hourly to 144 h, then 6-hourly. 00Z/12Z runs are cut at 240 h and 06Z/18Z
    runs at 90 h (`ECMWF_LONG_HOURS`, `ECMWF_SHORT_HOURS`).
  - Grid cell choice is `ECMWF_CELL_SELECTION=land` (Open-Meteo's default, which
    keeps coastal sites off ocean points). XMR maps to 28.50N 80.75W.
    Set it to `nearest` to take the closest point instead.
  - Open-Meteo only serves its newest run. Older ECMWF runs in the dropdown are
    the copies already on the live site, so they fill in after a day of updates.
  - Open-Meteo's free API is for non-commercial use (about 10,000 calls a day).
    The ECMWF pull is one request per new run for all stations.

Published layout: `data/<MODEL>/<YYYYMMDDHH>/<STATION>.buf.gz` plus
`data/manifest.json` (the runs and stations the dropdowns offer).

## How it stays light

- Each run copies files that are already on the live site and only downloads
  what is new, normally one new run per model.
- Files are gzip-compressed (about 3x smaller). The page opens one file at a time.
- The first deploy downloads all three runs for every model and station, so it
  takes several minutes. Later runs are much shorter.
- To change how many runs are kept, set `BUFKIT_KEEP_RUNS` or edit `KEEP` near
  the top of `scripts/fetch_bufkit.py`. PSU only holds 24 h, so HRRR/RAP can keep
  up to 24 runs and GFS/RRFS up to 4.

## Setup

1. Create a GitHub repository and upload everything in this folder, with
   `index.html` at the top level, `fetch_bufkit.py` in a `scripts` folder, and
   `update.yml` in `.github/workflows`, to the `main` branch.
2. Repository **Settings > Pages > Build and deployment > Source: GitHub Actions**.
3. Open the **Actions** tab, choose "Update BUFKIT data and deploy", and click
   **Run workflow**. When it finishes, the site is at
   `https://<account>.github.io/<repo>/`.
4. The workflow runs on push and on demand. For hourly updates, trigger it from
   an external scheduler (e.g. cron-job.org calling the workflow_dispatch API)
   or add a `schedule:` cron to `.github/workflows/update.yml`.

## Notes

- The Action log shows a stale-data warning (a yellow annotation on the run) when
  a model's newest run is older than expected, for example an HRRR more than 4
  hours old.
- A model that fails entirely is skipped with a warning; the others still publish.
- A station a model doesn't carry is left out of that model's station list.
- GitHub schedules can start 10-30 minutes late.
- The data files are compressed, so a current Chrome, Edge, Firefox or Safari
  is needed. Dragging a plain .buf file into the page still works anywhere.

## Changing stations or models

Edit `STATIONS` (and `COORDS` for ECMWF) and `MODELS` in
`scripts/fetch_bufkit.py`. Coordinates for ECMWF are read from the PSU GFS files
when available, so `COORDS` is only a fallback. The dropdowns follow the
generated `data/manifest.json`, so nothing changes in `index.html`.
