# Formulary Finder

Find close substitutes for a prescription, then check which ones your health plan's drug list (formulary) covers.

- Type the medication you take to see its closest substitutes: other versions of the same medicine, drugs in the same class, and other options for the same condition.
- Upload your plan's formulary PDF, or paste its text, and each substitute is checked for its tier and any prior authorization (PA), step therapy (ST), or quantity limit (QL).
- Get a short note to send your prescriber with the best-covered options.

Everything runs in the browser. Uploaded drug lists are read locally and never leave your device.

Not medical advice. Make any switch with your prescriber.

## Data

A GitHub Actions workflow (`.github/workflows/pages.yml`) rebuilds the site data twice a week and deploys the site to GitHub Pages:

- **Medicare plans and drug lists** from the CMS [Monthly Prescription Drug Plan Formulary and Pharmacy Network Information](https://catalog.data.gov/dataset/monthly-prescription-drug-plan-formulary-and-pharmacy-network-information) files: every stand-alone Part D plan and Medicare Advantage plan with drug coverage (not employer group plans), with each drug's tier and prior authorization, step therapy and quantity limit flags.
- **Medication names** for autocomplete and for readable Medicare drug lists from the National Library of Medicine's [RxNorm](https://www.nlm.nih.gov/research/umls/rxnorm/) via the RxNav API.

`tools/build_medicare.py` does the work; run it with `--zip PATH:YEAR:YYYYMMDD --names-file names.json` to test against local files.
