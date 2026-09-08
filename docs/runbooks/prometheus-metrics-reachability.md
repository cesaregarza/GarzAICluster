# Prometheus infrastructure metrics reachability

The production Prometheus egress policy allows the cert-manager controller on
TCP 9402 and the Mandate API metrics listener on TCP 9090. Each peer combines
its namespace and workload selector in one rule. The destination Mandate
NetworkPolicy must also continue allowing the production Prometheus pods.

Cilium runs its metrics listener on the host network. The separate
`prometheus-egress-node-metrics` CiliumNetworkPolicy therefore permits only the
production Prometheus selector to the `host` and `remote-node` identities on
TCP 9090. It does not open arbitrary node ports, public destinations or other
monitoring workloads. Node identities avoid hardcoding addresses that change
during DOKS node replacements.

Both additions are opt-in defaults and enabled in production values. Existing
scrape discovery, intervals, sample caps, application images and pod templates
stay unchanged. The API-server policy remains separately owned.

After GitOps reconciliation, inspect `/api/v1/targets` through a localhost-only
Prometheus port forward. Require the cert-manager controller, Mandate API and
both Cilium node targets to report `up` across several scrape intervals, with
all previously healthy targets and rule groups still healthy. A TCP timeout
alone is not proof that a listener is broken: check both source egress and
destination ingress policy before changing metrics annotations or ports.

For rollback, revert the two destination-specific rules and disable
`monitoring.cilium.prometheusEgressNodeMetrics.enabled`, then reconcile the
observability Application. No workload or volume restart is required.

Reference: [Cilium entity policy semantics](https://docs.cilium.io/en/stable/security/policy/layer3/).
