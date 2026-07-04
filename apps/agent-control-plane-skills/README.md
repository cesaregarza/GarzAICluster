# Agent Control Plane Skill Bundle

This kustomization renders the operator-reviewed skill bundle consumed by
`agent-control-plane` through the `mandate-skill-packs` ConfigMap.

This bundle is an operator-curated copy of the reviewed `agent-workloads/skills`
content until the CES-374 publish automation lands. GarzAICluster consumes the
rendered bundle as deployment data so Argo CD does not sync raw
`agent-workloads/skills` source directly into production.
