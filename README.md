# real-estate-crm-backend — deploy mirror

Private deploy mirror of `simbatechplc/real-estate-crm-backend`. The GitHub Action on this
`mirror` branch (the default) force-pushes the client repo's `main` onto this repo's `main`
every 30 minutes and on manual dispatch — so **never commit to `main` here**, it gets
overwritten wholesale. Render deploys from `main` (blueprint in the mirrored `render.yaml`).

Needs one repository secret: `COMPANY_PAT`, a PAT with read access to the client repo.
