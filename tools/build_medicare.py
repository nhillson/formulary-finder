#!/usr/bin/env python3
"""Build Medicare Part D drug-list data and the medication name list for Formulary Finder.

Downloads the latest CMS "Monthly Prescription Drug Plan Formulary and Pharmacy
Network Information" release for the current and next plan year, and writes:

  OUT/index.json               years available, release dates, counts
  OUT/drugs.json               medication names for autocomplete: [[generic, [brands]], ...] (NLM RxNorm)
  OUT/names.json               RxCUI -> readable drug name (from NLM RxNorm)
  OUT/<year>/plans.json        every plan: contract/plan/segment IDs, names, drug list ID, states
  OUT/<year>/f/<id>.json       one drug list (formulary): RxCUI, tier, PA/ST/QL flags, QL amount/days

Usage:
  python tools/build_medicare.py --resolve-only        print "key=..." for the workflow cache
  python tools/build_medicare.py --out medicare-data   full build
Testing offline:
  --zip PATH:YEAR:YYYYMMDD (repeatable) uses local release zips instead of CMS
  --names-file PATH        JSON {rxcui: RxNorm name} instead of calling RxNav
"""
import argparse
import csv
import datetime as dt
import io
import json
import os
import re
import shutil
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
SKIP_NAME = re.compile(r"pharmac|network|cost|pric|insulin|indication|exclud", re.I)


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


# ---------- Finding the release ----------

def find_releases():
    cat = json.loads(http_get(CATALOG, timeout=300))
    datasets = cat.get("dataset", [])
    found = []
    for kind in ("monthly", "quarterly"):
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
        if found:
            note(f"Found {len(found)} {kind} release files in the CMS catalog")
            break
    if not found:
        raise SystemExit("No Part D formulary release found in the CMS catalog")
    latest = {}
    for r in found:
        if r["year"] not in latest or r["date"] > latest[r["year"]]["date"]:
            latest[r["year"]] = r
    this_year = dt.date.today().year
    years = [y for y in sorted(latest) if y in (this_year, this_year + 1)] or [max(latest)]
    return [latest[y] for y in years]


def emit_key(key):
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write(f"key={key}\n")
    else:
        print(f"key={key}")


def release_key(releases):
    return "-".join(f"{r['year']}_{r['date']}" for r in releases)


# ---------- Reading the zip ----------

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
    return None


def collect_tables(zip_path, workdir, found=None, depth=0):
    """Find the plan, formulary and geography tables in a (possibly nested) zip. Returns {kind: path}."""
    found = {} if found is None else found
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            name = info.filename
            base = os.path.basename(name)
            if info.is_dir() or not base:
                continue
            low = base.lower()
            if low.endswith(".zip"):
                if SKIP_NAME.search(base) or depth > 2:
                    log(f"  skip {base}")
                    continue
                nested = os.path.join(workdir, f"n{depth}_{len(os.listdir(workdir))}_{base}")
                with zf.open(info) as src, open(nested, "wb") as dst:
                    shutil.copyfileobj(src, dst, 1 << 20)
                collect_tables(nested, workdir, found, depth + 1)
                os.remove(nested)
                continue
            if not low.endswith((".txt", ".csv", ".dat")) or SKIP_NAME.search(base):
                continue
            with zf.open(info) as src:
                first = src.readline().decode("latin-1").strip("\r\n")
            delim = sniff(first)
            header = [c.strip().strip('"').upper() for c in first.split(delim)]
            kind = classify(header)
            log(f"  {base}: {kind or 'not needed'}")
            if kind and kind not in found:
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


def num(v):
    try:
        x = float(str(v).strip())
    except ValueError:
        return ""
    return str(int(x)) if x.is_integer() else f"{x:g}"


def rx_id(v):
    v = str(v).strip()
    return str(int(v)) if v.isdigit() else v


# ---------- Building ----------

def build_year(zip_path, year, date, out_dir, workdir):
    tables = collect_tables(zip_path, workdir)
    for need in ("plans", "formulary"):
        if need not in tables:
            raise SystemExit(f"{year}: couldn't find the {need} table in the release")

    region_states, ma_region_states = {}, {}
    if "geo" in tables:
        for r in read_rows(tables["geo"]):
            st = STATE_ABBR.get(r.get("STATENAME", "").upper())
            if not st:
                continue
            if r.get("PDP_REGION_CODE"):
                region_states.setdefault(r["PDP_REGION_CODE"].lstrip("0"), set()).add(st)
            if r.get("MA_REGION_CODE"):
                ma_region_states.setdefault(r["MA_REGION_CODE"].lstrip("0"), set()).add(st)

    plans = {}
    for r in read_rows(tables["plans"]):
        if yes(r.get("PLAN_SUPPRESSED_YN", "")):
            continue
        c, p, s = r.get("CONTRACT_ID", ""), r.get("PLAN_ID", ""), r.get("SEGMENT_ID", "") or "0"
        fid = r.get("FORMULARY_ID", "")
        if not c or not p or not fid:
            continue
        key = (c, p.zfill(3), s.zfill(3))
        pl = plans.setdefault(key, {"n": r.get("PLAN_NAME", ""), "o": r.get("CONTRACT_NAME", ""), "f": fid.zfill(8), "st": set(), "snp": r.get("SNP", "")})
        st = r.get("STATE", "").upper()
        if len(st) == 2:
            pl["st"].add(st)
        elif c.startswith("S") and r.get("PDP_REGION_CODE"):
            pl["st"] |= region_states.get(r["PDP_REGION_CODE"].lstrip("0"), set())
        elif r.get("MA_REGION_CODE"):
            pl["st"] |= ma_region_states.get(r["MA_REGION_CODE"].lstrip("0"), set())
    wanted = {pl["f"] for pl in plans.values()}
    note(f"{year}: {len(plans)} plans using {len(wanted)} drug lists; tables found: {', '.join(sorted(tables))}")

    forms, versions = {}, {}
    for r in read_rows(tables["formulary"]):
        fid = r.get("FORMULARY_ID", "").zfill(8)
        if fid not in wanted:
            continue
        rx = rx_id(r.get("RXCUI", ""))
        if not rx:
            continue
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

    ydir = os.path.join(out_dir, str(year))
    os.makedirs(os.path.join(ydir, "f"), exist_ok=True)
    for fid, d in forms.items():
        rows = sorted(d.values(), key=lambda x: (isinstance(x[0], str), x[0]))
        with open(os.path.join(ydir, "f", f"{fid}.json"), "w") as f:
            json.dump({"id": fid, "v": versions.get(fid, ""), "d": rows}, f, separators=(",", ":"))
    plan_rows = []
    for (c, p, s), pl in sorted(plans.items()):
        if pl["f"] not in forms:
            continue
        plan_rows.append([c, p, s, pl["n"], pl["o"], pl["f"], ",".join(sorted(pl["st"])), 1 if yes(pl["snp"]) or pl["snp"] not in ("", "0", "N") else 0])
    with open(os.path.join(ydir, "plans.json"), "w") as f:
        json.dump({"year": year, "release": date, "plans": plan_rows}, f, separators=(",", ":"))
    rxcuis = {str(r[0]) for d in forms.values() for r in d.values()}
    for k in list(tables):
        os.remove(tables[k])
    return {"year": year, "release": date, "plans": len(plan_rows), "formularies": len(forms), "drugs": len(rxcuis)}, rxcuis


# ---------- Drug names ----------

UNIT_START = re.compile(r"\s(\d[\d.,]*\s*[A-Z%][A-Za-z%/]*(?:\s[A-Z%][A-Z%/]+)?)")
SALT_WORDS = set("""hydrochloride hcl sodium potassium calcium magnesium sulfate maleate besylate succinate tartrate
mesylate acetate bromide xinafoate propionate furoate fumarate dihydrate monohydrate trihydrate hemihydrate citrate
phosphate hydrobromide aspartate saccharate dipropionate bitartrate dimesylate chloride carbonate gluconate lactate
oxalate valerate benzoate hyclate monosodium disodium anhydrous hydrate pamoate tosylate napsylate""".split())


def split_name(name):
    """Split an RxNorm clinical drug name into (brand, quantity, ingredients, strengths, dose form)."""
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
    """Reorder an RxNorm name so ingredients come first: 'a / b 1 MG / 2 MG Oral Tablet [Brand]'."""
    full = name
    brand, qty, ings, strengths, rest = split_name(name)
    if ings is None or len(ings) < 2:
        out = re.sub(r"\s*\[[^\]]+\]\s*$", "", full)
    else:
        merged = []
        for ing in ings:
            base = ing.split(" ")[0]
            if merged and merged[-1][0] == base:
                extra = ing[len(base):].strip()
                if extra:
                    merged[-1][1].append(extra)
            else:
                merged.append([base, [ing[len(base):].strip()] if ing[len(base):].strip() else []])
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


def build_drug_list(concepts):
    """Medication names for autocomplete: [[generic, [brands]], ...] from current clinical drugs."""
    generics = {}
    for name, tty in concepts.values():
        if tty not in ("SCD", "SBD") or "allergenic" in name.lower():
            continue
        brand, _, ings, _, _ = split_name(name)
        if not ings or len(ings) > 4:
            continue
        bases = []
        for ing in ings:
            b = base_ingredient(ing)
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
    return [[g, sorted(b)] for g, b in sorted(generics.items())]


def normalize_brand(b):
    return re.sub(r"\s+", " ", b).strip()


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

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="medicare-data")
    ap.add_argument("--resolve-only", action="store_true")
    ap.add_argument("--zip", action="append", default=[], help="PATH:YEAR:YYYYMMDD local release (testing)")
    ap.add_argument("--names-file")
    a = ap.parse_args()

    if a.zip:
        releases = [{"year": int(z.split(":")[1]), "date": z.split(":")[2], "path": z.split(":")[0]} for z in a.zip]
    else:
        try:
            releases = find_releases()
        except BaseException as e:  # noqa: BLE001
            if a.resolve_only:
                emit_key(f"unresolved-{dt.date.today():%Y%m%d}")
                note(f"Couldn't read the CMS catalog: {e}", "warning")
                return
            raise
    if a.resolve_only:
        emit_key(release_key(releases))
        for r in releases:
            note(f"{r['year']} plan year: CMS release {r['date']} {r.get('url', '')}")
        return

    out = a.out
    if os.path.exists(out):
        shutil.rmtree(out)
    os.makedirs(out)
    concepts = fetch_concepts(a.names_file)  # fail early if RxNav is down
    drugs = build_drug_list(concepts)
    with open(os.path.join(out, "drugs.json"), "w") as f:
        json.dump(drugs, f, separators=(",", ":"))
    note(f"Medication list: {len(drugs)} generics, {sum(len(b) for _, b in drugs)} brand names")
    years, all_rx = [], set()
    with tempfile.TemporaryDirectory() as tmp:
        for r in releases:
            path = r.get("path")
            if not path:
                path = os.path.join(tmp, f"{r['year']}.zip")
                download(r["url"], path)
            work = os.path.join(tmp, f"w{r['year']}")
            os.makedirs(work)
            info, rx = build_year(path, r["year"], r["date"], out, work)
            years.append(info)
            all_rx |= rx
            if not r.get("path"):
                os.remove(path)
    names = fetch_names(all_rx, concepts, offline=bool(a.names_file))
    with open(os.path.join(out, "names.json"), "w") as f:
        json.dump(names, f, separators=(",", ":"))
    with open(os.path.join(out, "index.json"), "w") as f:
        json.dump({"generated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                   "source": "CMS Monthly Prescription Drug Plan Formulary and Pharmacy Network Information; drug names from NLM RxNorm",
                   "years": sorted(years, key=lambda y: y["year"])}, f, indent=1)
    size = sum(os.path.getsize(os.path.join(dp, fn)) for dp, _, fns in os.walk(out) for fn in fns)
    note(f"Done: {len(names)} of {len(all_rx)} drug names, {size / 1e6:.1f} MB total; " + "; ".join(f"{y['year']}: {y['plans']} plans, {y['formularies']} drug lists" for y in years))


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
