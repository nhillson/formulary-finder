"""How far the coinsurance-max bug reaches: CMS rows, drug-plan pairs, and yearly cost before/after for common brand drugs."""
import json, os, sys, collections, tempfile, re, statistics, urllib.request
sys.path.insert(0, "tools")
import build_medicare as B
OUT = "diag/out"; rep = open(f"{OUT}/report2.txt", "w")
def p(*a):
    print(*a); print(*a, file=rep); rep.flush()

monthly, _ = B.find_releases()
tier_type = {}   # (plan key) -> {tier: (type, max)}
for r in monthly:
    tmp = tempfile.mkdtemp(); path = B.fetch_zip(r, tmp, "m"); work = os.path.join(tmp, "w"); os.makedirs(work)
    t = B.collect_tables(path, work, {"plans", "formulary", "bencost"}, re.compile(B.ALWAYS_SKIP + r"|pric", re.I))
    cnt = collections.Counter(); plan_tiers = {}
    for row in B.read_rows(t["bencost"]):
        if row.get("COVERAGE_LEVEL") != "1" or row.get("DAYS_SUPPLY") != "1": continue
        ty = row.get("COST_TYPE_PREF"); mx = B.fnum(row.get("COST_MAX_AMT_PREF")) or 0; mn = B.fnum(row.get("COST_MIN_AMT_PREF")) or 0
        cnt[(ty, "max>0" if mx > 0 else "max=0", "min>0" if mn > 0 else "min=0")] += 1
        k = B.plan_key(row["CONTRACT_ID"], row["PLAN_ID"], row.get("SEGMENT_ID", ""))
        plan_tiers.setdefault(k, {})[row.get("TIER")] = (ty, mx)
    p(f"\n== {r['year']} preferred-pharmacy 30-day rows by (cost type 1=copay 2=coinsurance, max, min):", dict(cnt))
    fplans = collections.defaultdict(list)
    for row in B.read_rows(t["plans"]):
        if B.yes(row.get("PLAN_SUPPRESSED_YN", "")): continue
        k = B.plan_key(row["CONTRACT_ID"], row["PLAN_ID"], row.get("SEGMENT_ID", ""))
        if k in plan_tiers: fplans[row["FORMULARY_ID"].zfill(8)].append(k)
    for f in fplans: fplans[f] = sorted(set(fplans[f]))
    pairs = hit = 0; drugs_hit = set()
    for row in B.read_rows(t["formulary"]):
        f = row["FORMULARY_ID"].zfill(8); tier = row.get("TIER_LEVEL_VALUE")
        for k in fplans.get(f, ()):
            pairs += 1; ty, mx = plan_tiers[k].get(tier, (None, 0))
            if ty == "2" and mx == 0: hit += 1; drugs_hit.add(row["RXCUI"])
    p(f"{r['year']}: drug-plan pairs {pairs:,}; on a coinsurance tier with CMS max 0 (priced at $0 after the deductible): {hit:,} ({hit/pairs:.0%}); distinct drugs affected: {len(drugs_hit):,}")

# Yearly cost before/after on MN 2027 drug plans for common brand drugs, using the live site data
BASE = "https://nhillson.github.io/formulary-finder/data/"
get = lambda u: json.loads(urllib.request.urlopen(BASE + u, timeout=60).read())
names = get("names.json"); mn = get("2027/cost/MN.json")
CAP = 2400
def cs(share, price, fixed):
    if not share: return None
    ty, amt, mi, ma = share
    if ty == 1: return min(amt or 0, price)
    v = (amt or 0) * price
    if fixed:
        if mi and mi > 0: v = max(v, min(mi, price))
        if ma and ma > 0: v = min(v, ma)
    else:
        if mi is not None: v = max(v, min(mi, price))
        if ma is not None: v = min(v, ma)
    return min(v, price)
def year(plan, rx, fixed):
    fid, cst, ded = plan[5], plan[10], plan[4] or 0
    info = rx["f"].get(fid)
    if not info or rx.get("a") is None: return None
    row = {r[0]: r for r in cst}.get(info[0])
    if not row: return None
    price = rx["a"] * (rx.get("q") or 30); oop = 0
    for m in range(12):
        rem, out = price, 0
        if row[1] and ded > 0: d = min(rem, ded); out += d; ded -= d; rem -= d
        if rem > 0: out += cs(row[3] or row[4], rem, fixed)
        out = min(out, max(0, CAP - oop)); oop += out
    return oop
WANT = ["Eliquis 5 MG Oral Tablet", "Xarelto 20 MG Oral Tablet", "Jardiance 10 MG Oral Tablet", "Farxiga 10 MG Oral Tablet",
        "Januvia 100 MG Oral Tablet", "Entresto", "Trelegy", "Breo", "Symbicort", "Linzess 145", "Ozempic", "Trulicity", "Mounjaro",
        "Wegovy 4 MG Oral Tablet", "Rybelsus", "Xifaxan", "Vraylar", "Otezla", "Tradjenta", "Lantus", "Humira", "Spiriva", "Restasis", "Myrbetriq"]
pdps = [pl for pl in mn["plans"] if pl[9] == "PDP"]
p(f"\n== MN 2027, {len(pdps)} drug plans: yearly cost for one product alone, 30-day retail, typical price (median across plans that cover it)")
for w in WANT:
    rxs = [k for k, v in names.items() if w.split()[0] in v and all(x in v for x in w.split()[1:])][:1]
    if not rxs: p(f"{w}: not found"); continue
    try: rx = get(f"2027/rx/{rxs[0]}.json")
    except Exception as e: p(f"{w}: no data"); continue
    old = [year(pl, rx, False) for pl in pdps]; new = [year(pl, rx, True) for pl in pdps]
    pr = [(o, n) for o, n in zip(old, new) if o is not None]
    if not pr: p(f"{w}: not priced"); continue
    ch = sum(1 for o, n in pr if abs(o - n) > 1)
    p(f"{names[rxs[0]][:60]:60}  plans {len(pr):3}  changed {ch:3}  median before ${statistics.median(o for o,_ in pr):,.0f}  after ${statistics.median(n for _,n in pr):,.0f}")
