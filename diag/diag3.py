import zipfile, urllib.request, io, os, csv
URL = "https://www.cms.gov/files/zip/selected-drug-list-negotiated-prices-also-known-maximum-fair-prices-statutezip.zip"
os.makedirs("diag/out", exist_ok=True)
rep = open("diag/out/report3.txt", "w")
def p(*a): print(*a); print(*a, file=rep)
data = urllib.request.urlopen(urllib.request.Request(URL, headers={"User-Agent": "Mozilla/5.0"}), timeout=300).read()
open("diag/out/mfp.zip", "wb").write(data)
z = zipfile.ZipFile(io.BytesIO(data))
for i in z.infolist():
    p("MEMBER", i.filename, i.file_size)
    if i.filename.lower().endswith((".csv", ".txt")):
        lines = z.read(i).decode("latin-1").splitlines()
        p("\n".join(lines[:8]))
        p("...semaglutide rows:"); [p(l) for l in lines if "emaglutide" in l or "WEGOVY" in l.upper()][:40]
