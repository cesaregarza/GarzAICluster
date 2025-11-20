# Bot definitions

Each YAML file in this directory defines one bot for the ApplicationSet generators.

To onboard a new bot:
- Copy an existing `*.yaml` file and update the fields (`botName`, `valuesFile`, permissions).
- Keep repoURL/chartPath/targetRevision consistent unless the bot lives elsewhere.
- Run the secret-provisioning scripts before granting new permissions.

These files are consumed by:
- `argocd/appsets/bots-apps.yaml`
- `argocd/appsets/bots-netpol.yaml`
- `argocd/appsets/bots-secrets.yaml`
