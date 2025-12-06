# Vanity Hosts Chart

Simple ingress redirects for vanity hostnames (e.g., `foo.grafana.splat.top` → long dashboard URL). Each host becomes an ingress with a permanent redirect annotation; a dummy service is provided so ingress objects validate even though traffic is redirected.

## Values

```yaml
ingressClassName: nginx
defaultAnnotations: {}        # e.g., cert-manager.io/cluster-issuer: letsencrypt-prod
defaultTlsSecretName: ""      # use to share a TLS secret across hosts (optional)
defaultRedirectCode: 302      # per-host override with `code`
defaultCloudflareProxied: null # true/false to set external-dns annotation everywhere; null = skip

hosts: []
#  - name: grafana-foo          # optional resource name
#    host: foo.grafana.splat.top
#    target: https://grafana.splat.top/d/abc123/my-dashboard
#    code: 302                  # optional
#    tlsSecretName: grafana-tls # optional; falls back to defaultTlsSecretName
#    annotations: {}            # optional; merged on top of defaultAnnotations
#    cloudflareProxied: true    # optional; overrides defaultCloudflareProxied
```

Notes:
- TLS secrets must exist in the release namespace. If you rely on cert-manager, add the issuer annotation in `defaultAnnotations` so it provisions certs for these hosts.
- external-dns support: set `defaultCloudflareProxied` or per-host `cloudflareProxied` to emit `external-dns.alpha.kubernetes.io/cloudflare-proxied`.
