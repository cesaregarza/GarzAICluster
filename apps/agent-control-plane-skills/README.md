# Agent Control Plane Skill Bundle

This kustomization renders the operator-reviewed skill bundle consumed by
`agent-control-plane` through the `mandate-skill-packs` ConfigMap.

The source of truth remains the CI-published agent-workloads skill bundle.
GarzAICluster consumes the rendered bundle as deployment data so Argo CD does not
sync raw `agent-workloads/skills` source directly into production.
