"""Diagnostics: raw CMS 2027 rows behind the Wegovy tablet tier on S4802-158, and a global tier-conflict check."""
import json, os, sys, collections, tempfile, zipfile, re
sys.path.insert(0, "tools")
import build_medicare as B

OUT = "diag/out"
os.makedirs(OUT, exist_ok=True)
SEMA = {"2200650","2200654","2200658","2707549","2707553","2707557","2730161","2730165","2730167","2730169",
        "2736944","2736946","2736948","2200644","2200652","2200656","2707547","2707551","2707555","2730163"}
PLAN = ("S4802", "158")

monthly, quarterly = B.find_releases()
r = [m for m in monthly if m["year"] == 2027][0]
tmp = tempfile.mkdtemp()
path = B.fetch_zip(r, tmp, "m")
rep = open(f"{OUT}/report.txt", "w")
def p(*a):
    print(*a); print(*a, file=rep); rep.flush()
p("release", r)
with zipfile.ZipFile(path) as zf:
    p("zip members:", [i.filename for i in zf.infolist()])
work = os.path.join(tmp, "w"); os.makedirs(work)
tables = B.collect_tables(path, work, {"plans", "formulary", "geo", "bencost"}, re.compile(B.ALWAYS_SKIP + r"|pric", re.I))
p("tables", tables)

fid = None
for row in B.read_rows(tables["plans"]):
    if row.get("CONTRACT_ID") == PLAN[0] and row.get("PLAN_ID", "").zfill(3) == PLAN[1]:
        if fid is None: p("PLAN ROW", row)
        fid = row["FORMULARY_ID"].zfill(8)
p("formulary", fid)

p("\nBENCOST rows for plan:")
for row in B.read_rows(tables["bencost"]):
    if row.get("CONTRACT_ID") == PLAN[0] and row.get("PLAN_ID", "").zfill(3) == PLAN[1]:
        p(json.dumps(row))

tiers = collections.defaultdict(set)   # (fid, rx) -> tiers
rows_n = collections.Counter()
sema_rows = []
wegovy_tiers = collections.Counter()
for row in B.read_rows(tables["formulary"]):
    f = row.get("FORMULARY_ID", "").zfill(8); rx = B.rx_id(row.get("RXCUI", ""))
    t = row.get("TIER_LEVEL_VALUE")
    tiers[(f, rx)].add(t); rows_n[(f, rx)] += 1
    if f == fid and rx in SEMA: sema_rows.append(row)
    if rx == "2730167": wegovy_tiers[t] += 1
p("\nRAW formulary rows, semaglutide, this plan:")
for row in sema_rows: p(json.dumps(row))
p("\nWegovy 4 mg tablet raw tiers across all formulary rows:", dict(wegovy_tiers))
multi = {k: v for k, v in tiers.items() if len(v) > 1}
dup = sum(1 for k, n in rows_n.items() if n > 1)
p(f"\n(fid,rxcui) pairs: {len(tiers):,}; with >1 row: {dup:,}; with >1 distinct tier: {len(multi):,}")
by_rx = collections.Counter(rx for (_, rx) in multi)
p("rxcuis with tier conflicts (top 40):", by_rx.most_common(40))
json.dump({f"{k[0]}|{k[1]}": sorted(v) for k, v in multi.items()}, open(f"{OUT}/tier_conflicts.json", "w"))
p("header of formulary table:", open(tables["formulary"], encoding="latin-1").readline().strip())
