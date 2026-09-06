# Agent Control Plane Skill Bundle

This kustomization owns the scoped materializer for the
`mandate-skill-packs` ConfigMap consumed by `agent-control-plane`.

`agent-workloads` CI publishes reviewed `skills/` content as the
`registry.digitalocean.com/sendouq/agent-workloads-skills:main` bundle image.
The materializer verifies the bundle checksum index and replaces only `/data`
on the single named `mandate-skill-packs` ConfigMap using JSON Patch. Replacing
the full map removes retired skills while preserving Argo-owned labels and
annotations. Server-side apply is deliberately avoided: kubectl can migrate
client-side apply ownership and remove that metadata during materialization. It does not let
`agent-workloads` provide arbitrary Argo CD manifests.
