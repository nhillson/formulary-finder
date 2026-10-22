# Formulary Finder

Two pages:

- **Part D Cost Estimator** (`index.html`, the home page) ranks every Medicare drug plan in your county by estimated yearly cost. You add each medicine by brand or generic name and type your total daily dose; the estimator tries every mix of tablet strengths that adds up to that dose, generic and brand versions, and similar medicines in the same class, and uses the cheapest option per plan. Any switch can be refused or swapped for another choice, and every plan is recalculated. Built for older users: large text with a size control, plain language, and a print button.
- **Formulary Finder** (`finder.html`) finds close substitutes for a prescription and checks them against any insurance's drug list (PDF, pasted text, or a Medicare plan).

Built-in drug classes live in `classes.js`, shared by both pages. `cost.html` redirects to the home page.

Find close substitutes for a prescription, then check which ones your health plan's drug list (formulary) covers.

- Type the medication you take to see its closest substitutes: other versions of the same medicine, drugs in the same class, and other options for the same condition.
- Upload your plan's formulary PDF, or paste its text, and each substitute is checked for its tier and any prior authorization (PA), step therapy (ST), or quantity limit (QL).
- Get a short note to send your prescriber with the best-covered options.

Everything runs in the browser. Uploaded drug lists are read locally and never leave your device.

Not medical advice. Make any switch with your prescriber.

## Data

A GitHub Actions workflow (`.github/workflows/pages.yml`) rebuilds the site data twice a week and deploys the site to GitHub Pages:

- **Medicare plans and drug lists** from the CMS [Monthly Prescription Drug Plan Formulary and Pharmacy Network Information](https://catalog.data.gov/dataset/monthly-prescription-drug-plan-formulary-and-pharmacy-network-information) files: every stand-alone Part D plan and Medicare Advantage plan with drug coverage (not employer group plans), with each drug's tier and prior authorization, step therapy and quantity limit flags.
- **Costs** from the same monthly files (premiums, deductibles, copays and coinsurance by tier) and the CMS [Quarterly Prescription Drug Plan Formulary, Pharmacy Network, and Pricing Information](https://catalog.data.gov/dataset/quarterly-prescription-drug-plan-formulary-pharmacy-network-and-pricing-information) files (each plan's average unit price per drug). Prices for a new plan year appear in January; until then the estimator uses the previous year's typical prices.
- **Medication names** for autocomplete and for readable Medicare drug lists from the National Library of Medicine's [RxNorm](https://www.nlm.nih.gov/research/umls/rxnorm/) via the RxNav API.

`tools/build_medicare.py` does the work; run it with `--zip PATH:YEAR:YYYYMMDD --qzip PATH:YEAR:YYYYMMDD --names-file names.json` to test against local files.
