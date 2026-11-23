#!/usr/bin/env python3
"""Mirror the latest few BUFKIT runs from the Penn State BUFKIT server, plus an
ECMWF IFS 0.25 deg profile from Open-Meteo, for a list of Space Coast stations
into a folder that GitHub Pages serves next to the viewer.

Usage:  python3 scripts/fetch_bufkit.py site/data

Sources
  PSU (GFS, RAP, HRRR, RRFS)
      https://www.meteo.psu.edu/bufkit/data/<MODEL>/<HH>/<prefix>_<stn>.buf
      One folder per cycle hour, overwritten daily, so the last 24 h of cycles
      are always there. The run time is read from inside each file.
  ECMWF (Open-Meteo, model ecmwf_ifs025)
      https://api.open-meteo.com/v1/forecast  (pressure-level variables)
      https://api.open-meteo.com/data/ecmwf_ifs025/static/meta.json  (run time)
      Open-Meteo extracts the grid column at each station from the ECMWF open
      data GRIB files; this script writes it out in BUFKIT text format so the
      viewer reads it like any other model. Only the newest run can be fetched;
      older ECMWF runs come from what is already on the live site.

Output
  data/<MODEL>/<YYYYMMDDHH>/<STN>.buf.gz   one gzip file per station and run
  data/manifest.json                       the runs and stations the viewer offers

Runs already published on the live site (PAGES_URL) are copied from there, so
in a normal hour only the one new run is downloaded.
"""
import datetime as dt
import gzip
import json
import math
import os
import pathlib
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

PSU = os.environ.get("BUFKIT_BASE", "https://www.meteo.psu.edu/bufkit/data").rstrip("/")
OPEN_METEO = os.environ.get("OPEN_METEO_BASE", "https://api.open-meteo.com").rstrip("/")
KEEP = int(os.environ.get("BUFKIT_KEEP_RUNS", "3"))   # runs kept per model

# Cape Canaveral first (it is also the probe station), then the rest of the
# Space Coast / central Florida. PSU names XMR without the "K". TTS, COF, TIX,
# SFB, EVB and SUA are not BUFKIT sites. A station a model doesn't carry is skipped.
STATIONS = [
    "XMR",  "KMLB", "KVRB", "KFPR",          # Brevard / Treasure Coast
    "KMCO", "KISM", "KLEE", "KDAB",          # Orlando / Volusia
    "KLAL", "KTPA", "KPBI", "KJAX", "KGNV",  # regional context
]

# Station coordinates for the ECMWF extraction. Overwritten at run time with
# SLAT/SLON/SELV read from the PSU GFS files, so these are only a fallback.
COORDS = {
    "XMR":  (28.47, -80.55, 7.0),   "KMLB": (28.10, -80.65, 9.0),
    "KVRB": (27.65, -80.42, 13.0),  "KFPR": (27.50, -80.37, 9.0),
    "KMCO": (28.43, -81.32, 28.0),  "KISM": (28.29, -81.44, 25.0),
    "KLEE": (28.82, -81.81, 23.0),  "KDAB": (29.18, -81.05, 12.0),
    "KLAL": (27.99, -82.02, 43.0),  "KTPA": (27.96, -82.54, 6.0),
    "KPBI": (26.68, -80.10, 6.0),   "KJAX": (30.49, -81.69, 9.0),
    "KGNV": (29.69, -82.27, 46.0),
}

# Base address of the published site, set by the workflow. Files already there
# are copied instead of downloaded again.
PAGES_URL = os.environ.get("PAGES_URL", "").rstrip("/")

# dir/prefix: PSU folder and file prefix. step: hours between runs.
# max_age: hours since the newest run's initialization before the Action log
# shows a stale-data warning.
MODELS = {
    "GFS":   {"src": "psu", "dir": "GFS",  "prefix": "gfs3", "step": 6, "max_age": 14},
    "RAP":   {"src": "psu", "dir": "RAP",  "prefix": "rap",  "step": 1, "max_age": 4},
    "HRRR":  {"src": "psu", "dir": "HRRR", "prefix": "hrrr", "step": 1, "max_age": 4},
    "RRFS":  {"src": "psu", "dir": "RRFS", "prefix": "rrfs", "step": 6, "max_age": 14},
    "ECMWF": {"src": "openmeteo", "model": "ecmwf_ifs025", "step": 6, "max_age": 16},
}

# ECMWF output. Open-Meteo interpolates to hourly; the IFS open data are
# 3-hourly to 144 h and 6-hourly after, so only those hours are written.
# 06Z/18Z runs are short, so they are cut at ECMWF_SHORT_HOURS to avoid
# showing the previous long run stitched on the end.
ECMWF_LEVELS = [1000, 925, 850, 700, 600, 500, 400, 300, 250, 200, 150, 100, 50]
ECMWF_LONG_HOURS = int(os.environ.get("ECMWF_LONG_HOURS", "240"))
ECMWF_SHORT_HOURS = int(os.environ.get("ECMWF_SHORT_HOURS", "90"))
# "land" (Open-Meteo default) picks the nearest grid cell of similar elevation,
# which keeps coastal sites like XMR off the ocean; "nearest" takes the closest.
ECMWF_CELL = os.environ.get("ECMWF_CELL_SELECTION", "land")

UA = "space-coast-bufkit-viewer/1.1 (GitHub Actions mirror)"
TIME_RE = re.compile(r"TIME\s*=\s*(\d{2})(\d{2})(\d{2})/(\d{2})(\d{2})")


# --------------------------------------------------------------------------- I/O

def get_bytes(url, tries=3):
    err = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=90) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code in (403, 404):
                return None
            err = e
        except Exception as e:  # network trouble: retry
            err = e
        time.sleep(2 * (i + 1))
    print(f"  ! {url}: {err}", file=sys.stderr)
    return None


def get_text(url, tries=3):
    b = get_bytes(url, tries)
    return b.decode("utf-8", "replace") if b is not None else None


def get_json(url, tries=3):
    t = get_text(url, tries)
    try:
        return json.loads(t) if t else None
    except ValueError:
        return None


def unpack(data):
    try:
        return gzip.decompress(data).decode("utf-8", "replace")
    except Exception:
        return None


def pack(txt):
    return gzip.compress(txt.encode("utf-8"), 9, mtime=0)


def init_time(txt):
    """First TIME in a .buf file (the model initialization), or None if the
    text doesn't look like a BUFKIT file."""
    if not txt or "STID" not in txt:
        return None
    m = TIME_RE.search(txt)
    if not m:
        return None
    y, mo, d, h, mi = map(int, m.groups())
    return dt.datetime(2000 + y, mo, d, h, mi, tzinfo=dt.timezone.utc)


def run_id(t):
    return t.strftime("%Y%m%d%H")


def floor_step(t, step):
    t = t.replace(minute=0, second=0, microsecond=0)
    return t - dt.timedelta(hours=t.hour % step)


def reuse(name, rid, stn, have, t):
    """The published copy of a station/run, if the live site has it."""
    if not (PAGES_URL and stn in have):
        return None
    b = get_bytes(f"{PAGES_URL}/data/{name}/{rid}/{stn}.buf.gz", tries=1)
    return b if b is not None and init_time(unpack(b)) == t else None


def finish(name, runs, fetched, reused, t0):
    print(f"{name}: newest {t0:%Y-%m-%d %H:%M}Z, runs kept: "
          + ", ".join(f"{r['id'][-2:]}Z({len(r['stations'])})" for r in runs)
          + f"; {fetched} downloaded, {reused} reused from live site")
    if not runs:
        return None
    return {"init": runs[0]["init"], "stations": runs[0]["stations"], "runs": runs}


def stale_check(name, cfg, t0, now):
    age = (now - t0).total_seconds() / 3600
    if age > cfg["max_age"]:
        print(f"::warning::{name} data looks stale: newest run {t0:%Y-%m-%d %H:%M}Z is {age:.0f} h old")


# --------------------------------------------------------------------------- PSU

def fetch_psu(name, cfg, out, old, now):
    def url(stn, t):
        return f"{PSU}/{cfg['dir']}/{t:%H}/{cfg['prefix']}_{stn.lower()}.buf"

    step = cfg["step"]
    probe = STATIONS[0]

    # 1. Newest run: walk back through cycle folders until one holds the run
    #    it should (a folder still holding yesterday's run hasn't updated yet).
    t0 = probe_txt = None
    start = floor_step(now, step)
    for k in range(24 // step + 1):
        t = start - dt.timedelta(hours=k * step)
        txt = get_text(url(probe, t))
        if init_time(txt) == t:
            t0, probe_txt = t, txt
            break
    if t0 is None:
        print(f"WARNING: no data found for {name}", file=sys.stderr)
        return None
    stale_check(name, cfg, t0, now)

    # 2. The newest run and the previous ones, one folder per run.
    runs, reused, fetched = [], 0, 0
    for k in range(KEEP):
        t = t0 - dt.timedelta(hours=k * step)
        if now - t >= dt.timedelta(hours=24):   # PSU folder already overwritten
            break
        rid = run_id(t)
        have = set(old.get(rid, []))
        folder = out / name / rid
        folder.mkdir(parents=True, exist_ok=True)
        done = []
        for stn in STATIONS:
            data = reuse(name, rid, stn, have, t)
            if data is not None:
                reused += 1
            else:
                if k == 0 and stn == probe:
                    txt = probe_txt
                else:
                    txt = get_text(url(stn, t))
                    time.sleep(0.3)
                    fetched += 1
                if init_time(txt) != t:
                    print(f"WARNING: {name} {stn} {rid} missing or from a different run", file=sys.stderr)
                    continue
                data = pack(txt)
                if name == "GFS":
                    remember_coords(stn, txt)
            (folder / f"{stn}.buf.gz").write_bytes(data)
            done.append(stn)
        if done:
            runs.append({"id": rid, "init": t.isoformat(), "stations": done})
        else:
            folder.rmdir()
            print(f"WARNING: {name} run {rid} not available", file=sys.stderr)
    return finish(name, runs, fetched, reused, t0)


def remember_coords(stn, txt):
    m = re.search(r"SLAT\s*=\s*(\S+)\s+SLON\s*=\s*(\S+)\s+SELV\s*=\s*(\S+)", txt)
    if m:
        try:
            COORDS[stn] = tuple(float(x) for x in m.groups())
        except ValueError:
            pass


# ------------------------------------------------------------------------ ECMWF

def es(tc):
    """Saturation vapour pressure over water, hPa (Bolton 1980)."""
    return 6.112 * math.exp(17.67 * tc / (tc + 243.5))


def wetbulb(p, t, td):
    """Wet-bulb temperature, C, by bisection on the psychrometric equation."""
    if t is None or td is None:
        return None
    e = es(td)
    lo, hi = td, t
    for _ in range(40):
        tw = 0.5 * (lo + hi)
        # e = es(Tw) - gamma * p * (T - Tw), gamma = 6.6e-4 * (1 + 0.00115 Tw)
        f = es(tw) - 6.6e-4 * (1 + 0.00115 * tw) * p * (t - tw) - e
        if f > 0:
            hi = tw
        else:
            lo = tw
    return 0.5 * (lo + hi)


def theta_e(p, t, td):
    """Equivalent potential temperature, K (Bolton 1980)."""
    if t is None or td is None:
        return None
    tk, e = t + 273.15, es(td)
    r = 0.622 * e / (p - e)
    tl = 1 / (1 / (td + 273.15 - 56) + math.log(tk / (td + 273.15)) / 800) + 56
    return (tk * (1000 / p) ** (0.2854 * (1 - 0.28 * r))
            * math.exp((3.376 / tl - 0.00254) * r * 1000 * (1 + 0.81 * r)))


def omega(p, t, w):
    """Vertical velocity w (m/s) to omega in microbar/s (BUFKIT OMEG units)."""
    if w is None or t is None:
        return None
    rho = p * 100 / (287.05 * (t + 273.15))
    return -rho * 9.80665 * w * 10   # Pa/s -> ubar/s


def fmt(v, nd=2):
    return f"{-9999.0:.2f}" if v is None else f"{v:.{nd}f}"


def ecmwf_buf(stn, lat, lon, elev, t0, hourly, keep_idx):
    """One station's ECMWF column as BUFKIT text the viewer can read."""
    lines = [
        "SNPARM = PRES;TMPC;TMWC;DWPC;THTE;DRCT;SKNT;OMEG;CFRL;HGHT",
        "STNPRM = SHOW;LIFT;SWET;KINX;LCLP;PWAT;TOTL;CAPE;LCLT;CINS;EQLV;LFCT;BRCH",
        "",
        f"# ECMWF IFS 0.25 deg via Open-Meteo, run {t0:%Y-%m-%d %H}Z, grid point "
        f"{lat:.2f} {lon:.2f}. Not a real BUFKIT file: {len(ECMWF_LEVELS)} pressure levels only.",
        "",
    ]
    h = hourly
    for i in keep_idx:
        vt = dt.datetime.fromisoformat(h["time"][i]).replace(tzinfo=dt.timezone.utc)
        fh = int((vt - t0).total_seconds() // 3600)
        rows = []
        # Surface row first, from the 2 m / 10 m fields.
        ps, t2, d2 = h["surface_pressure"][i], h["temperature_2m"][i], h["dew_point_2m"][i]
        if ps is not None and t2 is not None:
            rows.append((ps, t2, d2, h["wind_direction_10m"][i], h["wind_speed_10m"][i], None, elev))
        for p in ECMWF_LEVELS:
            if ps is not None and p >= ps - 1:   # level below ground
                continue
            g = lambda v: h.get(f"{v}_{p}hPa", [None] * (i + 1))[i]
            t = g("temperature")
            if t is None:
                continue
            rows.append((p, t, g("dew_point"), g("wind_direction"), g("wind_speed"),
                         omega(p, t, g("vertical_velocity")), g("geopotential_height")))
        if len(rows) < 3:
            continue
        lines += [
            f"STID = {stn} STNM = 0 TIME = {vt:%y%m%d/%H%M}",
            f"SLAT = {lat:.2f} SLON = {lon:.2f} SELV = {elev:.1f}",
            f"STIM = {fh}",
            "",
            "PRES TMPC TMWC DWPC THTE DRCT SKNT OMEG",
            "CFRL HGHT",
        ]
        for p, t, td, d, s, om, z in rows:
            lines.append(" ".join([fmt(p), fmt(t), fmt(wetbulb(p, t, td)), fmt(td),
                                   fmt(theta_e(p, t, td)), fmt(d), fmt(s), fmt(om)]))
            lines.append(" ".join([fmt(None), fmt(z)]))
        lines.append("")
    return "\n".join(lines) + "\n"


def fetch_openmeteo(name, cfg, out, old, now):
    meta = get_json(f"{OPEN_METEO}/data/{cfg['model']}/static/meta.json")
    if not meta or "last_run_initialisation_time" not in meta:
        print(f"WARNING: no run information for {name} from Open-Meteo", file=sys.stderr)
        return None
    t0 = dt.datetime.fromtimestamp(meta["last_run_initialisation_time"], dt.timezone.utc)
    end_data = dt.datetime.fromtimestamp(meta.get("data_end_time", 0), dt.timezone.utc)
    stale_check(name, cfg, t0, now)

    runs, reused, fetched = [], 0, 0
    for k in range(KEEP):
        t = t0 - dt.timedelta(hours=k * cfg["step"])
        rid = run_id(t)
        have = set(old.get(rid, []))
        folder = out / name / rid
        folder.mkdir(parents=True, exist_ok=True)
        done, need = [], []
        for stn in STATIONS:
            data = reuse(name, rid, stn, have, t)
            if data is not None:
                (folder / f"{stn}.buf.gz").write_bytes(data)
                done.append(stn)
                reused += 1
            elif k == 0:
                need.append(stn)
        if need:   # only the newest run can be fetched from Open-Meteo
            got = fetch_ecmwf_run(cfg, t, end_data, need)
            fetched += 1 if got else 0
            for stn, txt in got.items():
                (folder / f"{stn}.buf.gz").write_bytes(pack(txt))
                done.append(stn)
        done = [s for s in STATIONS if s in done]
        if done:
            runs.append({"id": rid, "init": t.isoformat(), "stations": done})
        else:
            folder.rmdir()
            if k == 0:
                print(f"WARNING: {name} run {rid} not available", file=sys.stderr)
    return finish(name, runs, fetched, reused, t0)


def fetch_ecmwf_run(cfg, t0, end_data, stations):
    long_run = t0.hour in (0, 12)
    t_end = t0 + dt.timedelta(hours=ECMWF_LONG_HOURS if long_run else ECMWF_SHORT_HOURS)
    if end_data > t0:
        t_end = min(t_end, end_data)
    pl = ["temperature", "dew_point", "wind_speed", "wind_direction",
          "geopotential_height", "vertical_velocity"]
    hourly = [f"{v}_{p}hPa" for v in pl for p in ECMWF_LEVELS] + [
        "surface_pressure", "temperature_2m", "dew_point_2m",
        "wind_speed_10m", "wind_direction_10m"]
    q = {
        "latitude": ",".join(f"{COORDS[s][0]:.3f}" for s in stations),
        "longitude": ",".join(f"{COORDS[s][1]:.3f}" for s in stations),
        "hourly": ",".join(hourly),
        "models": cfg["model"],
        "start_hour": f"{t0:%Y-%m-%dT%H:%M}",
        "end_hour": f"{t_end:%Y-%m-%dT%H:%M}",
        "timezone": "GMT",
        "wind_speed_unit": "kn",
        "cell_selection": ECMWF_CELL,
    }
    js = get_json(f"{OPEN_METEO}/v1/forecast?" + urllib.parse.urlencode(q, safe=",:"))
    if js is None:
        print("WARNING: ECMWF request to Open-Meteo failed", file=sys.stderr)
        return {}
    if isinstance(js, dict):
        if js.get("error"):
            print(f"WARNING: Open-Meteo: {js.get('reason')}", file=sys.stderr)
            return {}
        js = [js]
    got = {}
    for stn, loc in zip(stations, js):
        h = loc.get("hourly") or {}
        times = h.get("time") or []
        keep = []
        for i, s in enumerate(times):
            fh = int((dt.datetime.fromisoformat(s).replace(tzinfo=dt.timezone.utc) - t0)
                     .total_seconds() // 3600)
            if fh >= 0 and fh % (3 if fh <= 144 else 6) == 0:
                keep.append(i)
        if not keep:
            continue
        txt = ecmwf_buf(stn, loc["latitude"], loc["longitude"],
                        loc.get("elevation", COORDS[stn][2]), t0, h, keep)
        if init_time(txt) == t0:
            got[stn] = txt
        else:
            print(f"WARNING: ECMWF {stn} produced no usable profiles", file=sys.stderr)
    return got


# ------------------------------------------------------------------------- main

def main():
    out = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "site/data")
    now = dt.datetime.now(dt.timezone.utc)
    manifest = {"updated": now.isoformat(timespec="minutes"), "stations": STATIONS, "models": {}}

    prev = {}
    if PAGES_URL:
        try:
            m = json.loads(get_text(f"{PAGES_URL}/data/manifest.json", tries=1) or "{}")
            for name, mi in m.get("models", {}).items():
                prev[name] = {r["id"]: r["stations"] for r in mi.get("runs", [])}
        except Exception:
            prev = {}

    # PSU first: the GFS files supply station coordinates for the ECMWF pull.
    for name, cfg in MODELS.items():
        fn = fetch_psu if cfg["src"] == "psu" else fetch_openmeteo
        try:
            res = fn(name, cfg, out, prev.get(name, {}), now)
        except Exception as e:   # one model failing shouldn't sink the rest
            print(f"::warning::{name} failed: {e}")
            res = None
        if res and res["runs"]:
            manifest["models"][name] = res

    if not manifest["models"]:
        sys.exit("No data downloaded; not publishing so the existing site stays as it is.")
    out.mkdir(parents=True, exist_ok=True)
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
