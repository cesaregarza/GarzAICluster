# Agent Control Plane Skill Bundle

This kustomization owns the scoped materializer for the
`mandate-skill-packs` ConfigMap consumed by `agent-control-plane`.

`agent-workloads` CI publishes reviewed `skills/` content as the
`registry.digitalocean.com/sendouq/agent-workloads-skills:main` bundle image.
The materializer verifies the bundle checksum index and server-side applies only
the single named `mandate-skill-packs` ConfigMap. It does not let
`agent-workloads` provide arbitrary Argo CD manifests.
