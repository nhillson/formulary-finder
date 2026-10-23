// Rerun the live estimator's cost code on Nick's three medicines for S4802-158 (2027, MN) using the live data.
const fs = require("fs");
const src = fs.readFileSync("index.html", "utf8");
const grab = n => { const i = src.indexOf("function " + n + "("); let d = 0; for (let k = src.indexOf("{", i); ; k++) { if (src[k] == "{") d++; if (src[k] == "}" && --d == 0) return src.slice(i, k + 1); } };
eval(["shareAt", "costShare", "itemInfo", "simulate"].map(grab).join("\n"));
const BASE = "https://nhillson.github.io/formulary-finder/data/2027/";
const get = async u => (await fetch(BASE + u)).json();
(async () => {
  const mn = await get("cost/MN.json");
  const out = [];
  out.push("prices: " + JSON.stringify(mn.prices));
  const names = await (await fetch("https://nhillson.github.io/formulary-finder/data/names.json")).json();
  const find = (...w) => Object.keys(names).filter(k => w.every(x => names[k].includes(x)));
  const ator = find("atorvastatin 80 MG Oral Tablet").find(k => !names[k].includes("["));
  const vera = find("verapamil", "180 MG", "Extended Release Oral Tablet").find(k => !names[k].includes("["));
  const items = [];
  for (const [rx, qty] of [[ator, 30], [vera, 30], ["2730167", 30]]) items.push({ rx: +rx, qty, data: await get(`rx/${rx}.json`) });
  out.push("items: " + items.map(i => `${names[i.rx]} a=${i.data.a} n=${i.data.n || 0}`).join(" | "));
  for (const id of ["S4802-158", "S5884-145", "S5884-204"]) {
    const plan = mn.plans.find(p => p[0] === id); if (!plan) continue;
    for (const ph of [0, 1]) {
      const r = simulate(plan, items, ph, 2400);
      out.push(`${id} ${plan[1]} pharmacy=${ph ? "standard" : "preferred"}: drugs $${r.total.toFixed(2)} + premiums $${(plan[3] * 12).toFixed(2)} = $${(r.total + plan[3] * 12).toFixed(2)}; per drug ${r.per.map(x => x.total.toFixed(2)).join(", ")}`);
    }
  }
  fs.mkdirSync("diag/out", { recursive: true });
  fs.writeFileSync("diag/out/report4.txt", out.join("\n") + "\n");
  console.log(out.join("\n"));
})();
