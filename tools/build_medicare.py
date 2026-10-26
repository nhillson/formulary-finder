#!/usr/bin/env python3
"""Build the Medicare Part D data and medication name list for Formulary Finder.

Sources
  CMS "Monthly Prescription Drug Plan Formulary and Pharmacy Network Information"
      plans, premiums, deductibles, service areas, drug lists, cost sharing by tier
  CMS "Quarterly Prescription Drug Plan Formulary, Pharmacy Network, and Pricing Information"
      average unit prices per plan and drug (only the quarterly files carry prices)
  NLM RxNorm (RxNav API)
      readable drug names and the medication list for autocomplete
  CMS Medicare Drug Price Negotiation Program "Selected Drug List and Negotiated Prices" file
      Medicare's negotiated price per unit, by NDC, for the selected drugs. Used for a year whose own
      plan prices aren't published yet (a new plan year's prices come out in January).

Output (OUT = --out)
  index.json                 years, release dates, data quality stats
  drugs.json                 medication names: [[generic, [brands]], ...]
  names.json                 RxCUI -> readable drug product name
  <year>/plans.json          every plan: IDs, names, drug list ID, states
  <year>/f/<id>.json         one drug list: [RxCUI, tier, flags(1 PA, 2 ST, 4 QL), QL amount, QL days]
  <year>/rx.json             RxCUIs on any plan's list that year
  <year>/rx/<rxcui>.json     one drug across plans: {"f": {listId: [tier, flags, qlAmt, qlDays]},
                             "p": {priceTable: unitCost}, "a": typical unit cost, "q": typical 30-day quantity,
                             "n": 1 when "a" is Medicare's negotiated price}
  <year>/cost/<ST>.json      plans sold in a state with premium, deductible, counties, cost sharing, price table

Usage
  python tools/build_medicare.py --resolve-only      writes key=... for the workflow cache
  python tools/build_medicare.py --out site-data     full build
Testing offline
  --zip PATH:YEAR:YYYYMMDD    monthly release zip (repeatable)
  --qzip PATH:YEAR:YYYYMMDD   quarterly release zip with prices (repeatable)
  --names-file PATH           JSON {rxcui: RxNorm name} instead of RxNav
  --mfp-file PATH             negotiated price CSV or zip instead of downloading it
"""
import argparse
import array
import csv
import datetime as dt
import hashlib
import io
import json
import math
import os
import re
import shutil
import statistics
import sys
import tempfile
import time
import urllib.request
import zipfile

CATALOG = "https://data.cms.gov/data.json"
RXNAV = "https://rxnav.nlm.nih.gov/REST"
UA = {"User-Agent": "formulary-finder-data-build (https://github.com/nhillson/formulary-finder)"}
csv.field_size_limit(10_000_000)

STATE_ABBR = {
    "ALABAMA": "AL", "ALASKA": "AK", "ARIZONA": "AZ", "ARKANSAS": "AR", "CALIFORNIA": "CA", "COLORADO": "CO",
    "CONNECTICUT": "CT", "DELAWARE": "DE", "DISTRICT OF COLUMBIA": "DC", "FLORIDA": "FL", "GEORGIA": "GA",
    "HAWAII": "HI", "IDAHO": "ID", "ILLINOIS": "IL", "INDIANA": "IN", "IOWA": "IA", "KANSAS": "KS",
    "KENTUCKY": "KY", "LOUISIANA": "LA", "MAINE": "ME", "MARYLAND": "MD", "MASSACHUSETTS": "MA",
    "MICHIGAN": "MI", "MINNESOTA": "MN", "MISSISSIPPI": "MS", "MISSOURI": "MO", "MONTANA": "MT",
    "NEBRASKA": "NE", "NEVADA": "NV", "NEW HAMPSHIRE": "NH", "NEW JERSEY": "NJ", "NEW MEXICO": "NM",
    "NEW YORK": "NY", "NORTH CAROLINA": "NC", "NORTH DAKOTA": "ND", "OHIO": "OH", "OKLAHOMA": "OK",
    "OREGON": "OR", "PENNSYLVANIA": "PA", "RHODE ISLAND": "RI", "SOUTH CAROLINA": "SC", "SOUTH DAKOTA": "SD",
    "TENNESSEE": "TN", "TEXAS": "TX", "UTAH": "UT", "VERMONT": "VT", "VIRGINIA": "VA", "WASHINGTON": "WA",
    "WEST VIRGINIA": "WV", "WISCONSIN": "WI", "WYOMING": "WY", "PUERTO RICO": "PR", "GUAM": "GU",
    "VIRGIN ISLANDS": "VI", "U.S. VIRGIN ISLANDS": "VI", "AMERICAN SAMOA": "AS",
    "NORTHERN MARIANA ISLANDS": "MP", "NORTHERN MARIANAS": "MP",
}
ALWAYS_SKIP = r"pharmac|network|insulin|indication|exclud|partial|senior"
MONTHLY_SKIP = re.compile(ALWAYS_SKIP + r"|pric", re.I)
QUARTERLY_SKIP = re.compile(ALWAYS_SKIP + r"|cost|geo|locator", re.I)
NAN = float("nan")


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def note(msg, level="notice"):
    """Log, and in GitHub Actions also post a run annotation (readable from the run page and the API)."""
    log(msg)
    if os.environ.get("GITHUB_ACTIONS"):
        print(f"::{level}::{msg}", flush=True)


def http_get(url, timeout=120, retries=3):
    for i in range(retries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            if i == retries - 1:
                raise
            log(f"  retry {i + 1} for {url}: {e}")
            time.sleep(5 * (i + 1))


def download(url, path):
    log(f"Downloading {url}")
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=900) as r, open(path, "wb") as f:
        shutil.copyfileobj(r, f, 1 << 20)
    log(f"  {os.path.getsize(path) / 1e6:.0f} MB")


# ---------- Finding the releases ----------

def catalog_releases(datasets, kind):
    found = []
    for d in datasets:
        title = (d.get("title") or "").lower()
        if "prescription drug plan formulary" not in title or kind not in title:
            continue
        for dist in d.get("distribution") or []:
            url = dist.get("downloadURL") or dist.get("accessURL") or ""
            if not url.lower().split("?")[0].endswith(".zip"):
                continue
            m = re.search(r"(20\d\d)_(20\d{6})", f"{dist.get('title') or ''} {url}")
            if m:
                found.append({"year": int(m.group(1)), "date": m.group(2), "url": url})
    latest = {}
    for r in found:
        if r["year"] not in latest or r["date"] > latest[r["year"]]["date"]:
            latest[r["year"]] = r
    return found, latest


def find_releases():
    datasets = json.loads(http_get(CATALOG, timeout=300)).get("dataset", [])
    this_year = dt.date.today().year
    m_found, m_latest = catalog_releases(datasets, "monthly")
    q_found, q_latest = catalog_releases(datasets, "quarterly")
    note(f"CMS catalog: {len(m_found)} monthly and {len(q_found)} quarterly release files")
    if not m_latest:
        raise SystemExit("No monthly Part D formulary release found in the CMS catalog")
    years = [y for y in sorted(m_latest) if y in (this_year, this_year + 1)] or [max(m_latest)]
    monthly = [m_latest[y] for y in years]
    quarterly = [q_latest[y] for y in years if y in q_latest]
    if not quarterly and q_latest:
        quarterly = [q_latest[max(q_latest)]]  # last year's prices as a stand-in
    return monthly, quarterly


def emit_key(key):
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write(f"key={key}\n")
    else:
        print(f"key={key}")


def release_key(monthly, quarterly):
    return "-".join(f"{r['year']}_{r['date']}" for r in monthly) + "-q" + "-".join(f"{r['year']}_{r['date']}" for r in quarterly)


# ---------- Reading the zips ----------

def sniff(first_line):
    counts = {d: first_line.count(d) for d in ("|", "\t", ",")}
    return max(counts, key=counts.get)


def classify(header):
    h = set(header)
    if {"FORMULARY_ID", "RXCUI", "TIER_LEVEL_VALUE"} <= h:
        return "formulary"
    if {"CONTRACT_ID", "PLAN_ID", "FORMULARY_ID", "PLAN_NAME"} <= h:
        return "plans"
    if {"COUNTY_CODE", "STATENAME", "PDP_REGION_CODE"} <= h:
        return "geo"
    if {"CONTRACT_ID", "PLAN_ID", "COVERAGE_LEVEL", "TIER", "DAYS_SUPPLY", "COST_TYPE_PREF"} <= h:
        return "bencost"
    if {"CONTRACT_ID", "PLAN_ID", "NDC", "DAYS_SUPPLY", "UNIT_COST"} <= h:
        return "pricing"
    return None


def collect_tables(zip_path, workdir, want, skip, found=None, depth=0):
    """Find the wanted tables in a (possibly nested) zip and extract them. Returns {kind: path}."""
    found = {} if found is None else found
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            base = os.path.basename(info.filename)
            if info.is_dir() or not base:
                continue
            low = base.lower()
            if low.endswith(".zip"):
                if skip.search(base) or depth > 2:
                    log(f"  skip {base}")
                    continue
                nested = os.path.join(workdir, f"n{depth}_{len(os.listdir(workdir))}.zip")
                with zf.open(info) as src, open(nested, "wb") as dst:
                    shutil.copyfileobj(src, dst, 1 << 20)
                collect_tables(nested, workdir, want, skip, found, depth + 1)
                os.remove(nested)
                continue
            if not low.endswith((".txt", ".csv", ".dat")) or skip.search(base):
                continue
            with zf.open(info) as src:
                first = src.readline().decode("latin-1").strip("\r\n")
            header = [c.strip().strip('"').upper() for c in first.split(sniff(first))]
            kind = classify(header)
            log(f"  {base}: {kind or 'not needed'}")
            if kind in want and kind not in found:
                out = os.path.join(workdir, f"{kind}.txt")
                with zf.open(info) as src, open(out, "wb") as dst:
                    shutil.copyfileobj(src, dst, 1 << 20)
                found[kind] = out
    return found


def read_rows(path):
    with open(path, encoding="latin-1", newline="") as f:
        first = f.readline()
        delim = sniff(first)
        header = [c.strip().strip('"').upper() for c in first.strip("\r\n").split(delim)]
        reader = csv.reader(f, delimiter=delim, quotechar='"', quoting=csv.QUOTE_MINIMAL if delim == "," else csv.QUOTE_NONE)
        for row in reader:
            if not row or len(row) < len(header) - 2:
                continue
            yield {k: (row[i].strip().strip('"') if i < len(row) else "") for i, k in enumerate(header)}


def yes(v):
    return str(v).strip().upper() in ("Y", "1", "YES", "TRUE", "T")


def fnum(v):
    try:
        return float(str(v).strip().lstrip("$").replace(",", ""))
    except ValueError:
        return None


def num(v):
    x = fnum(v)
    if x is None:
        return ""
    return str(int(x)) if x.is_integer() else f"{x:g}"


def rx_id(v):
    v = str(v).strip()
    return str(int(v)) if v.isdigit() else v


def plan_key(c, p, s):
    return (c.strip(), p.strip().zfill(3), (s.strip() or "0").zfill(3))


def plan_id(key):
    c, p, s = key
    return f"{c}-{p}" + (f"-{s}" if s != "000" else "")


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(obj, f, separators=(",", ":"))


# ---------- Prices (quarterly release) ----------

def build_pricing(zip_path, year, date, workdir):
    tables = collect_tables(zip_path, workdir, {"pricing", "formulary"}, QUARTERLY_SKIP)
    if "pricing" not in tables or "formulary" not in tables:
        note(f"{year} quarterly release has no {'pricing' if 'pricing' not in tables else 'formulary'} table; skipping prices", "warning")
        return None
    ndc2rx, rx_index = {}, {}
    for r in read_rows(tables["formulary"]):
        rx = rx_id(r.get("RXCUI", ""))
        ndc = re.sub(r"\D", "", r.get("NDC", "")).zfill(11)
        if not rx:
            continue
        if rx not in rx_index:
            rx_index[rx] = len(rx_index)
        if ndc.strip("0"):
            ndc2rx.setdefault(ndc, rx)
    n = len(rx_index)
    arrays, rows, used, misses = {}, 0, 0, 0
    blank = array.array("f", [NAN]) * n
    with open(tables["pricing"], encoding="latin-1") as f:
        first = f.readline()
        delim = sniff(first)
        hdr = [c.strip().strip('"').upper() for c in first.strip("\r\n").split(delim)]
        ic, ip, iseg = hdr.index("CONTRACT_ID"), hdr.index("PLAN_ID"), (hdr.index("SEGMENT_ID") if "SEGMENT_ID" in hdr else None)
        indc, ids, iu = hdr.index("NDC"), hdr.index("DAYS_SUPPLY"), hdr.index("UNIT_COST")
        imail = hdr.index("IS_MAIL") if "IS_MAIL" in hdr else None
        for line in f:
            rows += 1
            parts = line.rstrip("\r\n").split(delim)
            if len(parts) <= max(ic, ip, indc, ids, iu):
                continue
            if parts[ids].strip().strip('"').lstrip("0") != "30":
                continue
            if imail is not None and parts[imail].strip().strip('"') not in ("", "0", "N"):
                continue
            rx = ndc2rx.get(re.sub(r"\D", "", parts[indc]).zfill(11))
            if rx is None:
                misses += 1
                continue
            try:
                val = float(parts[iu].strip().strip('"').lstrip("$"))
            except ValueError:
                continue
            key = plan_key(parts[ic].strip('"'), parts[ip].strip('"'), parts[iseg].strip('"') if iseg is not None else "0")
            arr = arrays.get(key)
            if arr is None:
                arr = arrays[key] = array.array("f", blank)
            i = rx_index[rx]
            if arr[i] != arr[i] or val < arr[i]:
                arr[i] = val
            used += 1
    for k in list(tables):
        os.remove(tables[k])
    uniq, plan_table = {}, {}
    for key, arr in arrays.items():
        tid = hashlib.sha1(arr.tobytes()).hexdigest()[:7]
        uniq.setdefault(tid, arr)
        plan_table[key] = tid
    rx_list = [None] * n
    for rx, i in rx_index.items():
        rx_list[i] = rx
    avg = {}
    tabs = list(uniq.values())
    for i in range(n):
        vals = [t[i] for t in tabs if t[i] == t[i]]
        if vals:
            avg[rx_list[i]] = round(statistics.median(vals), 4)
    note(f"{year} prices (release {date}): {rows:,} rows, {used:,} 30-day retail prices used, {misses:,} unmatched NDCs; "
         f"{len(arrays):,} plans share {len(uniq):,} price tables; {len(avg):,} drugs priced")
    return {"year": year, "release": date, "tables": uniq, "plan_table": plan_table, "rx_index": rx_index, "avg": avg}


# ---------- Medicare negotiated prices ----------

MFP_PAGE = "https://www.cms.gov/priorities/medicare-prescription-drug-affordability/overview/medicare-drug-price-negotiation-program/selected-drugs-and-negotiated-prices"
MFP_ZIP = "https://www.cms.gov/files/zip/selected-drug-list-negotiated-prices-also-known-maximum-fair-prices-statutezip.zip"


def fetch_mfp(path=None):
    """Negotiated prices: [(ndc11, unit price, start date, end date or None)]. Empty if the file can't be read."""
    try:
        if path:
            raw = open(path, "rb").read()
        else:
            url = MFP_ZIP
            try:
                page = http_get(MFP_PAGE, timeout=60, retries=2).decode("utf-8", "replace")
                m = re.search(r'href="([^"]*selected-drug-list-negotiated-prices[^"]*\.zip)"', page)
                if m:
                    url = m.group(1) if m.group(1).startswith("http") else "https://www.cms.gov" + m.group(1)
            except Exception as e:  # noqa: BLE001
                log(f"  couldn't read the negotiated price page, using the known file link: {e}")
            raw = http_get(url, timeout=300)
        if raw[:2] == b"PK":
            with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                name = next(n for n in zf.namelist() if n.lower().endswith(".csv"))
                raw = zf.read(name)
        text = raw.decode("utf-8-sig", "replace")
    except Exception as e:  # noqa: BLE001  negotiated prices are optional
        note(f"Negotiated prices unavailable: {type(e).__name__}: {e}", "warning")
        return []

    def date(v):
        try:
            return dt.datetime.strptime(v.strip(), "%m/%d/%Y").date()
        except ValueError:
            return None
    out = []
    for r in csv.DictReader(io.StringIO(text)):
        r = {k.strip().upper(): (v or "").strip() for k, v in r.items() if k}
        ndc = re.sub(r"\D", "", r.get("NDC-11", ""))
        unit = fnum(r.get("NDC-9 MFP PER UNIT PRICE", ""))
        start = date(r.get("MFP EFFECTIVE DATE", ""))
        if len(ndc) != 11 or not unit or not start:
            continue
        out.append((ndc, unit, start, date(r.get("MFP END DATE", ""))))
    note(f"Negotiated prices: {len(out)} NDC rows")
    return out


def mfp_by_rx(mfp, ndc2rx, year):
    """Unit negotiated price per RxCUI in effect on January 1 of the year (median across the RxCUI's NDCs)."""
    day = dt.date(year, 1, 1)
    vals = {}
    for ndc, unit, start, end in mfp:
        rx = ndc2rx.get(ndc)
        if rx and start <= day and (end is None or end >= day):
            vals.setdefault(rx, []).append(unit)
    return {rx: round(statistics.median(v), 4) for rx, v in vals.items()}


# ---------- Plans, drug lists, cost sharing (monthly release) ----------

def build_year(zip_path, year, date, out_dir, workdir, pricing=None, fallback=None, mfp=()):
    tables = collect_tables(zip_path, workdir, {"plans", "formulary", "geo", "bencost"}, MONTHLY_SKIP)
    for need in ("plans", "formulary"):
        if need not in tables:
            raise SystemExit(f"{year}: couldn't find the {need} table in the release")

    # Geography: counties, and which states each region covers
    counties, region_states, ma_region_states = {}, {}, {}
    if "geo" in tables:
        for r in read_rows(tables["geo"]):
            raw_st = r.get("STATENAME", "").strip().upper()
            st = raw_st if raw_st in STATE_ABBR.values() else STATE_ABBR.get(raw_st)
            if not st:
                continue
            code = r.get("COUNTY_CODE", "").strip()
            if code:
                counties[code] = (st, r.get("COUNTY", "").strip().title())
            if r.get("PDP_REGION_CODE"):
                region_states.setdefault(r["PDP_REGION_CODE"].lstrip("0"), set()).add(st)
            if r.get("MA_REGION_CODE"):
                ma_region_states.setdefault(r["MA_REGION_CODE"].lstrip("0"), set()).add(st)

    # Plans
    plans = {}
    for r in read_rows(tables["plans"]):
        if yes(r.get("PLAN_SUPPRESSED_YN", "")):
            continue
        c, p, fid = r.get("CONTRACT_ID", ""), r.get("PLAN_ID", ""), r.get("FORMULARY_ID", "")
        if not c or not p or not fid:
            continue
        key = plan_key(c, p, r.get("SEGMENT_ID", ""))
        pl = plans.setdefault(key, {"n": r.get("PLAN_NAME", ""), "o": r.get("CONTRACT_NAME", ""), "f": fid.zfill(8), "st": set(),
                                    "cty": set(), "wide": False, "snp": r.get("SNP", ""),
                                    "prem": fnum(r.get("PREMIUM")), "ded": fnum(r.get("DEDUCTIBLE"))})
        st = r.get("STATE", "").upper()
        county = r.get("COUNTY_CODE", "").strip()
        if county:
            pl["cty"].add(county)
            if county in counties:
                pl["st"].add(counties[county][0])
        else:
            pl["wide"] = True
        if len(st) == 2:
            pl["st"].add(st)
        elif not county and c.startswith("S") and r.get("PDP_REGION_CODE"):
            pl["st"] |= region_states.get(r["PDP_REGION_CODE"].lstrip("0"), set())
        elif not county and r.get("MA_REGION_CODE"):
            pl["st"] |= ma_region_states.get(r["MA_REGION_CODE"].lstrip("0"), set())
    wanted = {pl["f"] for pl in plans.values()}
    note(f"{year}: {len(plans)} plans using {len(wanted)} drug lists; tables found: {', '.join(sorted(tables))}")

    # Cost sharing: initial coverage, 30-day supply, per tier: [tier, deductible applies, specialty, pref, std, mail]
    cost = {}
    if "bencost" in tables:
        for r in read_rows(tables["bencost"]):
            if r.get("COVERAGE_LEVEL", "").strip() != "1" or r.get("DAYS_SUPPLY", "").strip() != "1":
                continue
            key = plan_key(r.get("CONTRACT_ID", ""), r.get("PLAN_ID", ""), r.get("SEGMENT_ID", ""))
            try:
                tier = int(float(r.get("TIER") or 0))
            except ValueError:
                continue

            def share(sfx):
                t = r.get(f"COST_TYPE_{sfx}", "").strip()
                if t not in ("1", "2"):
                    return 0
                vals = [fnum(r.get(f"COST_AMT_{sfx}")), fnum(r.get(f"COST_MIN_AMT_{sfx}")), fnum(r.get(f"COST_MAX_AMT_{sfx}"))]
                # CMS writes 0 for "no minimum" and "no maximum" on coinsurance; keep only real limits
                if t == "2":
                    vals[1:] = [v if v and v > 0 else None for v in vals[1:]]
                return [int(t)] + [round(v, 4) if v is not None else None for v in vals]

            ded = r.get("DED_APPLIES_YN", r.get("DED APPLIES YN", ""))
            cost.setdefault(key, {})[tier] = [tier, 1 if yes(ded) else 0, 1 if yes(r.get("TIER_SPECIALTY_YN", "")) else 0,
                                              share("PREF"), share("NONPREF"), share("MAIL_PREF") or share("MAIL_NONPREF")]

    # Drug lists
    forms, versions, ndc2rx = {}, {}, {}
    for r in read_rows(tables["formulary"]):
        fid = r.get("FORMULARY_ID", "").zfill(8)
        if fid not in wanted:
            continue
        rx = rx_id(r.get("RXCUI", ""))
        if not rx:
            continue
        ndc = re.sub(r"\D", "", r.get("NDC", "")).zfill(11)
        if ndc.strip("0"):
            ndc2rx.setdefault(ndc, rx)
        try:
            tier = int(float(r.get("TIER_LEVEL_VALUE") or 0))
        except ValueError:
            tier = 0
        flags = (1 if yes(r.get("PRIOR_AUTHORIZATION_YN")) else 0) | (2 if yes(r.get("STEP_THERAPY_YN")) else 0) | (4 if yes(r.get("QUANTITY_LIMIT_YN")) else 0)
        rec = [int(rx) if rx.isdigit() else rx, tier, flags,
               num(r.get("QUANTITY_LIMIT_AMOUNT")) if flags & 4 else "",
               num(r.get("QUANTITY_LIMIT_DAYS")) if flags & 4 else ""]
        d = forms.setdefault(fid, {})
        prev = d.get(rx)
        if prev is None or (tier or 99, bin(flags & 3).count("1")) < (prev[1] or 99, bin(prev[2] & 3).count("1")):
            d[rx] = rec
        versions[fid] = r.get("FORMULARY_VERSION", "")
    for k in list(tables):
        os.remove(tables[k])

    ydir = os.path.join(out_dir, str(year))
    for fid, d in forms.items():
        rows = sorted(d.values(), key=lambda x: (isinstance(x[0], str), x[0]))
        write_json(os.path.join(ydir, "f", f"{fid}.json"), {"id": fid, "v": versions.get(fid, ""), "d": rows})
    live = {k: pl for k, pl in plans.items() if pl["f"] in forms}
    snp_flag = lambda pl: 1 if yes(pl["snp"]) or pl["snp"] not in ("", "0", "N") else 0  # noqa: E731
    plan_rows = [[c, p, s, pl["n"], pl["o"], pl["f"], ",".join(sorted(pl["st"])), snp_flag(pl)] for (c, p, s), pl in sorted(live.items())]
    write_json(os.path.join(ydir, "plans.json"), {"year": year, "release": date, "plans": plan_rows})

    # Prices for this year's plans
    price_src = pricing if pricing and pricing["year"] == year else None
    avg_src = price_src or fallback
    plan_tid = {k: price_src["plan_table"].get(k) for k in live} if price_src else {}
    # Without this year's own prices, use Medicare's negotiated price where there is one, since it's the same on every plan
    negotiated = {} if price_src else mfp_by_rx(mfp, ndc2rx, year)
    used_tids = {t for t in plan_tid.values() if t}

    # Per-state plan files for the cost estimator
    state_counties = {}
    for code, (st, name) in counties.items():
        state_counties.setdefault(st, []).append([code, name])
    by_state = {}
    for key, pl in sorted(live.items()):
        for st in pl["st"]:
            cty = 0 if pl["wide"] or not pl["cty"] else sorted(c for c in pl["cty"] if counties.get(c, (st,))[0] == st)
            if cty == []:
                continue
            by_state.setdefault(st, []).append([
                plan_id(key), pl["n"], pl["o"], pl["prem"], pl["ded"], pl["f"], plan_tid.get(key), snp_flag(pl), cty,
                "PDP" if key[0].startswith("S") else "MA", [cost[key][t] for t in sorted(cost.get(key, {}))]])
    for st, rows in by_state.items():
        write_json(os.path.join(ydir, "cost", f"{st}.json"), {
            "year": year, "state": st, "release": date,
            "prices": {"year": avg_src["year"], "release": avg_src["release"], "own": bool(price_src), "negotiated": len(negotiated)} if avg_src else None,
            "counties": sorted(state_counties.get(st, []), key=lambda x: x[1]), "plans": rows})

    # Per-drug files
    by_rx = {}
    for fid, d in forms.items():
        for rx, rec in d.items():
            by_rx.setdefault(rx, {})[fid] = rec[1:]
    for rx, fmap in by_rx.items():
        qs = [fnum(v[2]) * 30 / fnum(v[3]) for v in fmap.values() if v[1] & 4 and fnum(v[2]) and fnum(v[3])]
        obj = {"f": fmap, "q": round(statistics.median(qs), 2) if qs else None}
        if price_src and rx in price_src["rx_index"]:
            i = price_src["rx_index"][rx]
            p = {t: round(price_src["tables"][t][i], 4) for t in used_tids if price_src["tables"][t][i] == price_src["tables"][t][i]}
            if p:
                obj["p"] = p
        if rx in negotiated:
            obj["a"], obj["n"] = negotiated[rx], 1
        elif avg_src and rx in avg_src["avg"]:
            obj["a"] = avg_src["avg"][rx]
        write_json(os.path.join(ydir, "rx", f"{rx}.json"), obj)
    write_json(os.path.join(ydir, "rx.json"), sorted((int(r) if r.isdigit() else r for r in by_rx), key=lambda x: (isinstance(x, str), x)))

    # Quality stats
    states_count = {}
    for row in plan_rows:
        for st in filter(None, row[6].split(",")):
            states_count[st] = states_count.get(st, 0) + 1
    tiers = {}
    for d in forms.values():
        for r in d.values():
            tiers[str(r[1])] = tiers.get(str(r[1]), 0) + 1
    qa = {"pdp": sum(1 for r in plan_rows if r[0].startswith("S")), "ma": sum(1 for r in plan_rows if not r[0].startswith("S")),
          "noStates": sum(1 for r in plan_rows if not r[6]), "snp": sum(1 for r in plan_rows if r[7]),
          "plansByState": dict(sorted(states_count.items())), "tiers": dict(sorted(tiers.items())),
          "withPA": sum(1 for d in forms.values() for r in d.values() if r[2] & 1),
          "withST": sum(1 for d in forms.values() for r in d.values() if r[2] & 2),
          "withQL": sum(1 for d in forms.values() for r in d.values() if r[2] & 4),
          "rows": sum(len(d) for d in forms.values()),
          "withCostSharing": sum(1 for k in live if k in cost), "withPremium": sum(1 for pl in live.values() if pl["prem"] is not None),
          "withPriceTable": sum(1 for t in plan_tid.values() if t), "priceTables": len(used_tids),
          "drugsWithAvgPrice": sum(1 for rx in by_rx if avg_src and rx in avg_src["avg"]),
          "drugsWithNegotiatedPrice": sum(1 for rx in by_rx if rx in negotiated),
          "countyLimitedPlans": sum(1 for pl in live.values() if pl["cty"] and not pl["wide"]),
          "sampleCostSharing": [[plan_id(k)] + v for k, v in list(cost.items())[:1] for v in [list(v.values())[:3]]],
          "samplePlans": [r[:7] for r in plan_rows if r[0].startswith("S")][:3]}
    note(f"{year}: {qa['pdp']} PDP + {qa['ma']} MA plans; cost sharing for {qa['withCostSharing']}, price tables for {qa['withPriceTable']} "
         f"({qa['priceTables']} distinct); {qa['drugsWithAvgPrice']} of {len(by_rx)} drugs with a typical price")
    info = {"year": year, "release": date, "plans": len(plan_rows), "formularies": len(forms), "drugs": len(by_rx),
            "prices": {"year": avg_src["year"], "release": avg_src["release"], "own": bool(price_src), "negotiated": len(negotiated)} if avg_src else None, "qa": qa}
    return info, set(by_rx)


# ---------- Drug names ----------

UNIT_START = re.compile(r"\s(\d[\d.,]*\s*[A-Z%][A-Za-z%/]*(?:\s[A-Z%][A-Z%/]+)?)")
SALT_WORDS = set("""hydrochloride hcl sodium potassium calcium magnesium sulfate maleate besylate succinate tartrate
mesylate acetate bromide xinafoate propionate furoate fumarate dihydrate monohydrate trihydrate hemihydrate citrate
phosphate hydrobromide aspartate saccharate dipropionate bitartrate dimesylate chloride carbonate gluconate lactate
oxalate valerate benzoate hyclate monosodium disodium anhydrous hydrate pamoate tosylate napsylate""".split())


APP_PREFIX = re.compile(r"^((?:NDA|BLA|ANDA)\d+)\s+")
# RxNorm puts a word in front of some names, e.g. "Abuse-Deterrent 12 HR oxycodone ..." or "Once-Daily gabapentin ...".
# Keep in step with TAG_WORDS in prices.html.
TAG_WORDS = ("Abuse-Deterrent|Osmotic|Sprinkle|Modified|Smoking Cessation|Breath-Actuated|BX Rating|Pediatric|Niacinamide|Augmented|Once-Daily|"
             "Twice-Daily|Microencapsulated|Preservative-Free|PMDD|Pulmonary Hypertension|Atrial Fibrillation|Hyponatremia|Emollient|Sugar-Free|Soft Gelatin")
TAG_PREFIX = re.compile(r"^(" + TAG_WORDS + r")\s+(?=[a-z\d])")
TAG_ANY = re.compile(r"^([A-Z][A-Za-z-]*(?: [A-Z][A-Za-z-]*)*)\s+(?=\d)")


def strip_prefix(name):
    """Remove a leading FDA application number and/or version word. Returns (prefix text, rest)."""
    pre = []
    m = APP_PREFIX.match(name)
    if m:
        pre.append(m.group(1))
        name = name[m.end():]
    m = TAG_PREFIX.match(name) or TAG_ANY.match(name)
    if m:
        pre.append(m.group(1))
        name = name[m.end():]
    return " ".join(pre), name


def split_name(name):
    """Split an RxNorm clinical drug name into (brand, quantity, ingredients, strengths, dose form).

    Some products start with the FDA application number ("NDA020503 200 ACTUAT albuterol ...") or a
    version word ("Abuse-Deterrent 12 HR oxycodone ..."). That prefix is skipped here."""
    name = strip_prefix(name)[1]
    brand = ""
    m = re.search(r"\s*\[([^\]]+)\]\s*$", name)
    if m:
        brand, name = m.group(1), name[: m.start()]
    if name.startswith("{"):
        return brand, "", None, [], name
    qty = ""
    q = re.match(r"^(\d[\d.,]*\s+[A-Z][A-Z/]*)\s+(?=[a-z])", name)
    if q:
        qty, name = q.group(1), name[q.end():]
    ings, strengths, rest = [], [], ""
    parts = name.split(" / ")
    for i, part in enumerate(parts):
        m2 = UNIT_START.search(" " + part)
        if not m2:
            words = part.split(" ")
            k = next((j for j, w in enumerate(words) if w[:1].isupper()), len(words))
            ings.append(" ".join(words[:k]).strip() or part.strip())
            if i == len(parts) - 1:
                rest = " ".join(words[k:])
            continue
        ings.append((" " + part)[: m2.start()].strip())
        strengths.append(m2.group(1).strip())
        if i == len(parts) - 1:
            rest = (" " + part)[m2.end():].strip()
    return brand, qty, ings, strengths, rest


def base_ingredient(ing):
    words = ing.lower().split()
    while len(words) > 1 and words[-1] in SALT_WORDS and not all(w in SALT_WORDS for w in words[:-1]):
        words.pop()
    return " ".join(words)


def make_label(name):
    """Reorder an RxNorm name so ingredients come first: 'a / b 1 MG / 2 MG Oral Tablet [Brand]'.
    A leading FDA application number or version word stays at the front so look-alike products stay distinct."""
    pre, rest = strip_prefix(name)
    if pre:
        return pre + " " + make_label(rest)
    full = name
    brand, qty, ings, strengths, rest = split_name(name)
    if ings is None or len(ings) < 2:
        out = re.sub(r"\s*\[[^\]]+\]\s*$", "", full)
    else:
        merged = []
        for ing in ings:
            base = ing.split(" ")[0]
            extra = ing[len(base):].strip()
            if merged and merged[-1][0] == base:
                if extra:
                    merged[-1][1].append(extra)
            else:
                merged.append([base, [extra] if extra else []])
        ing_text = " / ".join(b + (" " + ", ".join(s) if s else "") for b, s in merged)
        out = " ".join(x for x in (ing_text, " / ".join(strengths), rest) if x)
        if qty:
            out += f", {qty}"
    return out + (f" [{brand}]" if brand else "")


def fetch_concepts(names_file=None):
    """All current RxNorm clinical drugs: {rxcui: (name, tty)}."""
    if names_file:
        return {k: (v, "SBD" if "[" in v else "SCD") for k, v in json.load(open(names_file)).items()}
    log("Fetching drug names from RxNav")
    j = json.loads(http_get(f"{RXNAV}/allconcepts.json?tty=SCD+SBD+GPCK+BPCK", timeout=600))
    return {str(c.get("rxcui")): (c.get("name") or "", c.get("tty") or "") for c in (j.get("minConceptGroup") or {}).get("minConcept") or []}


def fetch_ingredients(offline=False):
    """Lowercased RxNorm ingredient (IN) and precise ingredient (PIN) names, or None offline."""
    if offline:
        return None
    j = json.loads(http_get(f"{RXNAV}/allconcepts.json?tty=IN+PIN", timeout=600))
    return {(c.get("name") or "").lower() for c in (j.get("minConceptGroup") or {}).get("minConcept") or []}


def normalize_brand(b):
    b = re.sub(r"\s+", " ", b).strip()
    if b.isupper() and len(b) > 4 and " " not in b:
        b = b.title()
    return b


def build_drug_list(concepts, ingredients=None):
    """Medication names for autocomplete: [[generic, [brands]], ...] from current clinical drugs."""
    generics, dropped = {}, 0
    for name, tty in concepts.values():
        if tty not in ("SCD", "SBD") or "allergenic" in name.lower():
            continue
        brand, _, ings, _, _ = split_name(name)
        if not ings or len(ings) > 4:
            continue
        bases = []
        for ing in ings:
            b = base_ingredient(ing)
            if ingredients is not None and b not in ingredients and ing.lower() not in ingredients:
                dropped += 1
                continue
            if b and b not in bases:
                bases.append(b)
        if not bases:
            continue
        g = " / ".join(bases)
        if len(g) > 70:
            continue
        brands = generics.setdefault(g, set())
        if brand and tty == "SBD" and normalize_brand(brand).lower() != g.replace(" / ", "/"):
            brands.add(normalize_brand(brand))
    if dropped:
        log(f"  dropped {dropped} name parts that aren't RxNorm ingredients")
    return [[g, sorted(b)] for g, b in sorted(generics.items())]


def fetch_names(rxcuis, concepts, offline=False):
    raw = {k: v[0] for k, v in concepts.items()}
    if not offline:
        missing = sorted(r for r in rxcuis if r not in raw)
        log(f"  {len(raw)} current names; looking up {min(len(missing), 5000)} of {len(missing)} retired codes")
        for r in missing[:5000]:
            try:
                h = json.loads(http_get(f"{RXNAV}/rxcui/{r}/historystatus.json", timeout=30, retries=2)).get("rxcuiStatusHistory") or {}
                nm = (h.get("attributes") or {}).get("name") or ""
                if not nm:
                    rem = ((h.get("derivedConcepts") or {}).get("remappedConcept") or [])
                    nm = rem[0].get("remappedName", "") if rem else ""
                if nm:
                    raw[r] = nm
            except Exception as e:  # noqa: BLE001
                log(f"  no name for {r}: {e}")
            time.sleep(0.07)
    return {r: make_label(raw[r]) for r in sorted(rxcuis, key=lambda x: (not x.isdigit(), int(x) if x.isdigit() else 0, x)) if raw.get(r)}


# ---------- Main ----------

def parse_local(specs):
    return [{"year": int(z.split(":")[1]), "date": z.split(":")[2], "path": z.split(":")[0]} for z in specs]


def fetch_zip(r, tmp, tag):
    if r.get("path"):
        return r["path"]
    path = os.path.join(tmp, f"{tag}{r['year']}.zip")
    download(r["url"], path)
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="site-data")
    ap.add_argument("--resolve-only", action="store_true")
    ap.add_argument("--zip", action="append", default=[], help="PATH:YEAR:YYYYMMDD monthly release (testing)")
    ap.add_argument("--qzip", action="append", default=[], help="PATH:YEAR:YYYYMMDD quarterly release (testing)")
    ap.add_argument("--names-file")
    ap.add_argument("--mfp-file")
    a = ap.parse_args()

    if a.zip:
        monthly, quarterly = parse_local(a.zip), parse_local(a.qzip)
    else:
        try:
            monthly, quarterly = find_releases()
        except BaseException as e:  # noqa: BLE001
            if a.resolve_only:
                emit_key(f"unresolved-{dt.date.today():%Y%m%d}")
                note(f"Couldn't read the CMS catalog: {e}", "warning")
                return
            raise
    if a.resolve_only:
        emit_key(release_key(monthly, quarterly))
        for r in monthly:
            note(f"{r['year']} plan year: monthly release {r['date']} {r.get('url', '')}")
        for r in quarterly:
            note(f"{r['year']} prices: quarterly release {r['date']} {r.get('url', '')}")
        return

    out = a.out
    if os.path.exists(out):
        shutil.rmtree(out)
    os.makedirs(out)
    concepts = fetch_concepts(a.names_file)  # fail early if RxNav is down
    drugs = build_drug_list(concepts, fetch_ingredients(offline=bool(a.names_file)))
    write_json(os.path.join(out, "drugs.json"), drugs)
    note(f"Medication list: {len(drugs)} generics, {sum(len(b) for _, b in drugs)} brand names")

    mfp = fetch_mfp(a.mfp_file)
    years, all_rx = [], set()
    with tempfile.TemporaryDirectory() as tmp:
        pricing = {}
        for r in quarterly:
            path = fetch_zip(r, tmp, "q")
            work = os.path.join(tmp, f"qw{r['year']}")
            os.makedirs(work)
            try:
                pr = build_pricing(path, r["year"], r["date"], work)
            except Exception as e:  # noqa: BLE001  prices are optional; keep going without them
                note(f"{r['year']} prices failed: {type(e).__name__}: {e}", "warning")
                pr = None
            if pr:
                pricing[r["year"]] = pr
            if not r.get("path"):
                os.remove(path)
        latest_prices = pricing[max(pricing)] if pricing else None
        for r in monthly:
            path = fetch_zip(r, tmp, "m")
            work = os.path.join(tmp, f"w{r['year']}")
            os.makedirs(work)
            info, rx = build_year(path, r["year"], r["date"], out, work, pricing.get(r["year"]), latest_prices, mfp)
            years.append(info)
            all_rx |= rx
            if not r.get("path"):
                os.remove(path)
    names = fetch_names(all_rx, concepts, offline=bool(a.names_file))
    write_json(os.path.join(out, "names.json"), names)
    with open(os.path.join(out, "index.json"), "w") as f:
        json.dump({"generated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                   "source": "CMS Part D formulary, cost sharing and pricing public use files; drug names from NLM RxNorm",
                   "medications": {"generics": len(drugs), "brands": sum(len(b) for _, b in drugs),
                                   "samples": [d for d in drugs if d[0] in ("atorvastatin", "semaglutide", "levothyroxine", "amphetamine / dextroamphetamine", "amoxicillin")]},
                   "names": {"found": len(names), "needed": len(all_rx), "samples": [names[k] for k in sorted(names)[:: max(1, len(names) // 6)]][:6]},
                   "years": sorted(years, key=lambda y: y["year"])}, f, indent=1)
    size = sum(os.path.getsize(os.path.join(dp, fn)) for dp, _, fns in os.walk(out) for fn in fns)
    nfiles = sum(len(fns) for _, _, fns in os.walk(out))
    note(f"Done: {len(names)} of {len(all_rx)} drug names, {size / 1e6:.1f} MB in {nfiles:,} files; "
         + "; ".join(f"{y['year']}: {y['plans']} plans, {y['formularies']} drug lists" for y in years))


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        if e.code not in (None, 0):
            note(f"Data build stopped: {e.code}", "error")
        raise
    except Exception as e:  # noqa: BLE001
        note(f"Data build failed: {type(e).__name__}: {e}", "error")
        raise
