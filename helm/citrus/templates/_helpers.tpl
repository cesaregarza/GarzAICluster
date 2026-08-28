{{/*
Expand the name of the chart.
*/}}
{{- define "citrus.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "citrus.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Validate and project the disabled-by-default Cloudflare Access origin gate.
The Secret owns all confidential values; chart values contain only references
and an immutable source-image receipt.
*/}}
{{- define "citrus.cloudflareAccess.validate" -}}
{{- if not (kindIs "bool" .Values.cloudflareAccess.enabled) -}}
{{- fail "cloudflareAccess.enabled must be a boolean" -}}
{{- end -}}
{{- if .Values.cloudflareAccess.enabled -}}
{{- $owner := required "cloudflareAccess.owner is required when cloudflareAccess.enabled=true" .Values.cloudflareAccess.owner -}}
{{- $secretName := required "cloudflareAccess.secretName is required when cloudflareAccess.enabled=true" .Values.cloudflareAccess.secretName -}}
{{- $rolloutRevision := required "cloudflareAccess.rolloutRevision is required when cloudflareAccess.enabled=true" .Values.cloudflareAccess.rolloutRevision -}}
{{- $verifiedImageTag := required "cloudflareAccess.verifiedImageTag is required when cloudflareAccess.enabled=true" .Values.cloudflareAccess.verifiedImageTag -}}
{{- if not (regexMatch "^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$" $rolloutRevision) -}}
{{- fail "cloudflareAccess.rolloutRevision must be a semantic revision of 1-63 safe characters" -}}
{{- end -}}
{{- if not (regexMatch "^[0-9a-f]{40}$" $verifiedImageTag) -}}
{{- fail "cloudflareAccess.verifiedImageTag must be an immutable 40-character lowercase source SHA" -}}
{{- end -}}
{{- if ne $verifiedImageTag (toString .Values.image.tag) -}}
{{- fail "cloudflareAccess.verifiedImageTag must exactly match image.tag" -}}
{{- end -}}
{{- if eq $owner "citrus-dev" -}}
{{- if ne .Release.Namespace "citrus-dev" -}}
{{- fail "cloudflareAccess.owner=citrus-dev requires the citrus-dev namespace" -}}
{{- end -}}
{{- if ne $secretName "citrus-dev-cloudflare-access" -}}
{{- fail "cloudflareAccess.owner=citrus-dev requires secretName citrus-dev-cloudflare-access" -}}
{{- end -}}
{{- else if eq $owner "citrus" -}}
{{- if ne .Release.Namespace "default" -}}
{{- fail "cloudflareAccess.owner=citrus requires the default namespace" -}}
{{- end -}}
{{- if ne $secretName "citrus-cloudflare-access" -}}
{{- fail "cloudflareAccess.owner=citrus requires secretName citrus-cloudflare-access" -}}
{{- end -}}
{{- else -}}
{{- fail "cloudflareAccess.owner must be citrus-dev or citrus when enabled" -}}
{{- end -}}
{{- if and .Values.paymentSafety.enabled (eq .Values.paymentSafety.networkMode "deny") -}}
{{- fail "cloudflareAccess cannot be enabled with paymentSafety.networkMode=deny until exact JWK egress is authorized" -}}
{{- end -}}
{{- end -}}
{{- end }}

{{- define "citrus.cloudflareAccess.env" -}}
{{- if .Values.cloudflareAccess.enabled }}
- name: CLOUDFLARE_ACCESS_REQUIRED
  value: "true"
- name: CLOUDFLARE_ACCESS_TEAM_DOMAIN
  valueFrom:
    secretKeyRef:
      name: {{ .Values.cloudflareAccess.secretName }}
      key: CLOUDFLARE_ACCESS_TEAM_DOMAIN
      optional: false
- name: CLOUDFLARE_ACCESS_AUD
  valueFrom:
    secretKeyRef:
      name: {{ .Values.cloudflareAccess.secretName }}
      key: CLOUDFLARE_ACCESS_AUD
      optional: false
- name: CLOUDFLARE_ACCESS_ALLOWED_EMAILS
  valueFrom:
    secretKeyRef:
      name: {{ .Values.cloudflareAccess.secretName }}
      key: CLOUDFLARE_ACCESS_ALLOWED_EMAILS
      optional: false
{{- end }}
{{- end }}

{{/*
Validate the disabled-by-default CES-844 payment credential projection.
*/}}
{{- define "citrus.paymentCredentials.validate" -}}
{{- if .Values.paymentCredentials.enabled -}}
{{- $secretName := required "paymentCredentials.secretName is required when paymentCredentials.enabled=true" .Values.paymentCredentials.secretName -}}
{{- if not (regexMatch "^[a-z0-9]([-a-z0-9]*[a-z0-9])?$" $secretName) -}}
{{- fail "paymentCredentials.secretName must be a valid Kubernetes Secret name" -}}
{{- end -}}
{{- $rolloutRevision := required "paymentCredentials.rolloutRevision is required when paymentCredentials.enabled=true" .Values.paymentCredentials.rolloutRevision -}}
{{- if not (regexMatch "^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$" $rolloutRevision) -}}
{{- fail "paymentCredentials.rolloutRevision must be a semantic revision of 1-63 safe characters" -}}
{{- end -}}
{{- $webhookEnvironmentVariable := required "paymentCredentials.webhookEnvironmentVariable is required when paymentCredentials.enabled=true" .Values.paymentCredentials.webhookEnvironmentVariable -}}
{{- if not (has $webhookEnvironmentVariable (list "STRIPE_WEBHOOK_SECRET_DEV" "STRIPE_WEBHOOK_SECRET_PROD")) -}}
{{- fail "paymentCredentials.webhookEnvironmentVariable must select the dev or production environment-specific webhook setting" -}}
{{- end -}}
{{- $webhookSecretName := required "paymentCredentials.webhookSecretName is required when paymentCredentials.enabled=true" .Values.paymentCredentials.webhookSecretName -}}
{{- if not (regexMatch "^[a-z0-9]([-a-z0-9]*[a-z0-9])?$" $webhookSecretName) -}}
{{- fail "paymentCredentials.webhookSecretName must be a valid Kubernetes Secret name" -}}
{{- end -}}
{{- $webhookSecretKey := required "paymentCredentials.webhookSecretKey is required when paymentCredentials.enabled=true" .Values.paymentCredentials.webhookSecretKey -}}
{{- if not (has $webhookSecretKey (list "STRIPE_WEBHOOK_SECRET" "STRIPE_WEBHOOK_SECRET_DEV" "STRIPE_WEBHOOK_SECRET_PROD")) -}}
{{- fail "paymentCredentials.webhookSecretKey must select an exact webhook key" -}}
{{- end -}}
{{- $owner := required "paymentCredentials.owner is required when paymentCredentials.enabled=true" .Values.paymentCredentials.owner -}}
{{- if eq $webhookEnvironmentVariable "STRIPE_WEBHOOK_SECRET_DEV" -}}
{{- if ne $owner "citrus-dev" -}}
{{- fail "paymentCredentials.owner must be citrus-dev for the dev webhook setting" -}}
{{- end -}}
{{- if ne $secretName "citrus-dev-payment-credentials" -}}
{{- fail "the dev webhook setting requires secretName citrus-dev-payment-credentials" -}}
{{- end -}}
{{- if ne $webhookSecretName .Values.application.secretName -}}
{{- fail "the dev webhook setting must use the existing application Secret as webhookSecretName" -}}
{{- end -}}
{{- if ne $webhookSecretKey "STRIPE_WEBHOOK_SECRET_DEV" -}}
{{- fail "the dev webhook setting requires webhookSecretKey STRIPE_WEBHOOK_SECRET_DEV" -}}
{{- end -}}
{{- else -}}
{{- if ne $owner "citrus" -}}
{{- fail "paymentCredentials.owner must be citrus for the production webhook setting" -}}
{{- end -}}
{{- if ne $secretName "citrus-prod-payment-credentials" -}}
{{- fail "the production webhook setting requires secretName citrus-prod-payment-credentials" -}}
{{- end -}}
{{- if or (ne $webhookSecretName $secretName) (ne $webhookSecretKey "STRIPE_WEBHOOK_SECRET") -}}
{{- fail "the production webhook setting requires the dedicated payment Secret and generic webhook key" -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- end }}

{{/*
When dedicated payment credentials are enabled, replace the broad application
Secret import with exact non-payment runtime references. The legacy encrypted
object stays unchanged, but none of its payment API or production webhook keys
enter the container environment.
*/}}
{{- define "citrus.applicationRuntimeSecretEnv" -}}
{{- if .Values.paymentCredentials.enabled }}
{{- range $key := list "DB_HOST" "DB_PORT" "DB_NAME" "DB_USER" "DB_PASSWORD" }}
- name: {{ $key }}
  valueFrom:
    secretKeyRef:
      name: {{ $.Values.application.secretName }}
      key: {{ $key }}
      optional: false
{{- end }}
{{- end }}
{{- end }}

{{/* Preserve the existing broad import only while isolation is disabled. */}}
{{- define "citrus.applicationRuntimeSecretEnvFrom" -}}
{{- if not .Values.paymentCredentials.enabled }}
- secretRef:
    name: {{ .Values.application.secretName }}
    optional: true
{{- end }}
{{- end }}

{{/*
Project only API authority into non-web payment consumers.
*/}}
{{- define "citrus.paymentCredentials.apiEnv" -}}
{{- if .Values.paymentCredentials.enabled }}
- name: STRIPE_SECRET_KEY
  valueFrom:
    secretKeyRef:
      name: {{ .Values.paymentCredentials.secretName }}
      key: STRIPE_SECRET_KEY
      optional: false
{{- end }}
{{- end }}

{{/* Project the exact web payment settings, including one environment webhook. */}}
{{- define "citrus.paymentCredentials.webEnv" -}}
{{- if .Values.paymentCredentials.enabled }}
{{ include "citrus.paymentCredentials.apiEnv" . }}
- name: STRIPE_PUBLISHABLE_KEY
  valueFrom:
    secretKeyRef:
      name: {{ .Values.paymentCredentials.secretName }}
      key: STRIPE_PUBLISHABLE_KEY
      optional: false
- name: {{ .Values.paymentCredentials.webhookEnvironmentVariable }}
  valueFrom:
    secretKeyRef:
      name: {{ .Values.paymentCredentials.webhookSecretName }}
      key: {{ .Values.paymentCredentials.webhookSecretKey }}
      optional: false
- name: STRIPE_WEBHOOK_SECRET_OWNER
  value: {{ .Values.paymentCredentials.owner | quote }}
{{- end }}
{{- end }}

{{/*
The dev threat model isolates dev from production, not one trusted dev process
from another. Every dev Django runtime therefore receives the same dedicated
test-mode API/publishable pair and exact dev webhook projection. Production
keeps the narrower prepared projection from PR #576.
*/}}
{{- define "citrus.paymentCredentials.devRuntimeEnv" -}}
{{- if and .Values.paymentCredentials.enabled (eq .Values.paymentCredentials.owner "citrus-dev") }}
{{ include "citrus.paymentCredentials.webEnv" . }}
{{- end }}
{{- end }}

{{/* Payment consumers stay API-only in production and use the full dev set. */}}
{{- define "citrus.paymentCredentials.consumerEnv" -}}
{{- if .Values.paymentCredentials.enabled }}
{{- if eq .Values.paymentCredentials.owner "citrus-dev" }}
{{ include "citrus.paymentCredentials.webEnv" . }}
{{- else }}
{{ include "citrus.paymentCredentials.apiEnv" . }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Validate one exact external hostname without resolving or contacting it. Stripe
destinations are never valid members of the nonproduction allowlist.
*/}}
{{- define "citrus.paymentSafety.validateHost" -}}
{{- $field := .field -}}
{{- $host := required (printf "%s is required" $field) .host -}}
{{- $numericAddress := regexMatch "^(0x[0-9a-f]+|[0-9]+)([.](0x[0-9a-f]+|[0-9]+))*$" $host -}}
{{- if or (contains "*" $host) (contains ".." $host) $numericAddress (not (regexMatch "^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$" $host)) -}}
{{- fail (printf "%s must be a lowercase exact DNS hostname with valid labels, not an IP address or wildcard" $field) -}}
{{- end -}}
{{- range $label := splitList "." $host -}}
{{- if not (regexMatch "^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$" $label) -}}
{{- fail (printf "%s must be a lowercase exact DNS hostname with valid labels, not an IP address or wildcard" $field) -}}
{{- end -}}
{{- end -}}
{{- if or (eq $host "stripe.com") (hasSuffix ".stripe.com" $host) (eq $host "stripe.network") (hasSuffix ".stripe.network" $host) -}}
{{- fail (printf "%s must not authorize a Stripe destination" $field) -}}
{{- end -}}
{{- end }}

{{/*
Validate the disabled-by-default CES-845 runtime attestation and Cilium policy.
Named cluster runtimes must render a policy in the same release as the env
attestation; an env-only claim is intentionally rejected.
*/}}
{{- define "citrus.paymentSafety.validate" -}}
{{- if .Values.paymentSafety.enabled -}}
{{- $environment := required "paymentSafety.environment is required when paymentSafety.enabled=true" .Values.paymentSafety.environment -}}
{{- $owner := required "paymentSafety.owner is required when paymentSafety.enabled=true" .Values.paymentSafety.owner -}}
{{- $networkMode := required "paymentSafety.networkMode is required when paymentSafety.enabled=true" .Values.paymentSafety.networkMode -}}
{{- if not .Values.paymentSafety.policy.required -}}
{{- fail "paymentSafety.policy.required must be true when paymentSafety.enabled=true" -}}
{{- end -}}
{{- if ne .Values.paymentSafety.policy.provider "cilium" -}}
{{- fail "paymentSafety.policy.provider must be cilium when paymentSafety.enabled=true" -}}
{{- end -}}
{{- $revision := required "paymentSafety.policy.revision is required when paymentSafety.enabled=true" .Values.paymentSafety.policy.revision -}}
{{- if not (regexMatch "^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$" $revision) -}}
{{- fail "paymentSafety.policy.revision must be a semantic revision of 1-63 safe characters" -}}
{{- end -}}
{{- if not .Values.paymentSafety.networkPolicy.enabled -}}
{{- fail "paymentSafety.networkPolicy.enabled must be true when paymentSafety.enabled=true" -}}
{{- end -}}
{{- $policyWave := int .Values.paymentSafety.networkPolicy.syncWave -}}
{{- $configWave := int .Values.syncWaves.config -}}
{{- if ge $policyWave $configWave -}}
{{- fail "paymentSafety.networkPolicy.syncWave must precede syncWaves.config" -}}
{{- end -}}
{{- if eq $environment "development" -}}
{{- if or (ne .Release.Name "citrus-dev") (ne .Release.Namespace "citrus-dev") -}}
{{- fail "development payment safety requires release citrus-dev in namespace citrus-dev" -}}
{{- end -}}
{{- if ne $owner "citrus-dev" -}}
{{- fail "paymentSafety.owner must be citrus-dev when paymentSafety.environment=development" -}}
{{- end -}}
{{- if ne $networkMode "deny" -}}
{{- fail "paymentSafety.networkMode must be deny when paymentSafety.environment=development" -}}
{{- end -}}
{{- if not .Values.redis.enabled -}}
{{- fail "redis.enabled must be true for the development payment egress policy" -}}
{{- end -}}
{{- $redisPort := int .Values.redis.service.port -}}
{{- if or (lt $redisPort 1) (gt $redisPort 65535) -}}
{{- fail "redis.service.port must be between 1 and 65535 for the development payment egress policy" -}}
{{- end -}}
{{- include "citrus.paymentSafety.validateHost" (dict "field" "paymentSafety.networkPolicy.database.host" "host" .Values.paymentSafety.networkPolicy.database.host) -}}
{{- $databasePort := int .Values.paymentSafety.networkPolicy.database.port -}}
{{- if or (lt $databasePort 1) (gt $databasePort 65535) -}}
{{- fail "paymentSafety.networkPolicy.database.port must be between 1 and 65535" -}}
{{- end -}}
{{- $storageEndpointURL := required "application.configData.AWS_S3_ENDPOINT_URL is required for the development payment egress policy" .Values.application.configData.AWS_S3_ENDPOINT_URL -}}
{{- if not (regexMatch "^https://[a-z0-9][a-z0-9.-]*[a-z0-9]/?$" $storageEndpointURL) -}}
{{- fail "application.configData.AWS_S3_ENDPOINT_URL must be an exact lowercase HTTPS origin for the development payment egress policy" -}}
{{- end -}}
{{- $storageEndpointHost := trimSuffix "/" (trimPrefix "https://" $storageEndpointURL) -}}
{{- include "citrus.paymentSafety.validateHost" (dict "field" "application.configData.AWS_S3_ENDPOINT_URL host" "host" $storageEndpointHost) -}}
{{- $storageBucket := required "application.configData.AWS_STORAGE_BUCKET_NAME is required for the development payment egress policy" .Values.application.configData.AWS_STORAGE_BUCKET_NAME -}}
{{- if not (regexMatch "^[a-z0-9][a-z0-9.-]*[a-z0-9]$" $storageBucket) -}}
{{- fail "application.configData.AWS_STORAGE_BUCKET_NAME must be a lowercase DNS-compatible bucket name for the development payment egress policy" -}}
{{- end -}}
{{- $storageBucketHost := printf "%s.%s" $storageBucket $storageEndpointHost -}}
{{- include "citrus.paymentSafety.validateHost" (dict "field" "derived object-storage bucket host" "host" $storageBucketHost) -}}
{{- include "citrus.paymentSafety.validateHost" (dict "field" "application.configData.AWS_S3_CUSTOM_DOMAIN" "host" .Values.application.configData.AWS_S3_CUSTOM_DOMAIN) -}}
{{- $emailBackend := default "django.core.mail.backends.smtp.EmailBackend" .Values.application.configData.EMAIL_BACKEND -}}
{{- if eq $emailBackend "django.core.mail.backends.smtp.EmailBackend" -}}
{{- include "citrus.paymentSafety.validateHost" (dict "field" "application.configData.EMAIL_HOST" "host" .Values.application.configData.EMAIL_HOST) -}}
{{- $emailPort := int .Values.application.configData.EMAIL_PORT -}}
{{- if or (lt $emailPort 1) (gt $emailPort 65535) -}}
{{- fail "application.configData.EMAIL_PORT must be between 1 and 65535 for the development payment egress policy" -}}
{{- end -}}
{{- end -}}
{{- range $index, $destination := .Values.paymentSafety.networkPolicy.additionalExternalEgress -}}
{{- include "citrus.paymentSafety.validateHost" (dict "field" (printf "paymentSafety.networkPolicy.additionalExternalEgress[%d].host" $index) "host" $destination.host) -}}
{{- range $portIndex, $port := $destination.ports -}}
{{- $externalPort := int $port -}}
{{- if or (lt $externalPort 1) (gt $externalPort 65535) -}}
{{- fail (printf "paymentSafety.networkPolicy.additionalExternalEgress[%d].ports[%d] must be between 1 and 65535" $index $portIndex) -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- else if eq $environment "production" -}}
{{- if or (ne .Release.Name "citrus") (ne .Release.Namespace "default") -}}
{{- fail "production payment safety requires release citrus in namespace default" -}}
{{- end -}}
{{- if ne $owner "citrus" -}}
{{- fail "paymentSafety.owner must be citrus when paymentSafety.environment=production" -}}
{{- end -}}
{{- if ne $networkMode "allow" -}}
{{- fail "paymentSafety.networkMode must be allow when paymentSafety.environment=production" -}}
{{- end -}}
{{- else -}}
{{- fail "paymentSafety.environment must be development or production when paymentSafety.enabled=true" -}}
{{- end -}}
{{- end -}}
{{- end }}

{{/*
Validate the disabled-by-default CES-850 recurring runtime topology. Any one
runtime component being requested makes the complete topology mandatory; a
partial render must never create a controller that bypasses the preflight.
*/}}
{{- define "citrus.recurringRuntime.validate" -}}
{{- $runtimeRequested := or .Values.billingWorker.enabled .Values.recurringRuntime.enabled .Values.recurringRuntime.preflight.enabled -}}
{{- if .Values.recurringRuntime.enabled -}}
{{- if not .Values.billingWorker.enabled -}}
{{- fail "recurringRuntime.enabled requires billingWorker.enabled so published work always has a consumer" -}}
{{- end -}}
{{- if not .Values.billingWorker.metrics.enabled -}}
{{- fail "recurringRuntime.enabled requires billingWorker.metrics.enabled so runtime health cannot fail open" -}}
{{- end -}}
{{- end -}}
{{- if $runtimeRequested -}}
{{- if not .Values.billingWorker.enabled -}}
{{- fail "the recurring runtime topology requires billingWorker.enabled=true" -}}
{{- end -}}
{{- if ne (int .Values.billingWorker.replicas) 1 -}}
{{- fail "billingWorker.replicas must be exactly 1 for the first recurring runtime activation" -}}
{{- end -}}
{{- if not .Values.recurringRuntime.enabled -}}
{{- fail "the recurring runtime topology requires recurringRuntime.enabled=true" -}}
{{- end -}}
{{- if not .Values.recurringRuntime.preflight.enabled -}}
{{- fail "the recurring runtime topology requires recurringRuntime.preflight.enabled=true" -}}
{{- end -}}
{{- if not .Values.recurringRuntime.health.enabled -}}
{{- fail "the recurring runtime topology requires recurringRuntime.health.enabled=true" -}}
{{- end -}}
{{- if not .Values.billingWorker.metrics.enabled -}}
{{- fail "the recurring runtime topology requires billingWorker.metrics.enabled=true" -}}
{{- end -}}
{{- if not .Values.paymentSafety.enabled -}}
{{- fail "the recurring runtime topology requires paymentSafety.enabled=true" -}}
{{- end -}}
{{- if not .Values.migrations.enabled -}}
{{- fail "the recurring runtime topology requires migrations.enabled=true" -}}
{{- end -}}
{{- if not .Values.redis.enabled -}}
{{- fail "the recurring runtime topology requires redis.enabled=true" -}}
{{- end -}}
{{- $imageTag := required "image.tag is required when the recurring runtime topology is enabled" .Values.image.tag -}}
{{- if not (regexMatch "^[0-9a-f]{40}$" (toString $imageTag)) -}}
{{- fail "image.tag must be an exact 40-character lowercase source revision when the recurring runtime topology is enabled" -}}
{{- end -}}
{{- $expectedSourceRevision := required "recurringRuntime.expectedSourceRevision is required when the recurring runtime topology is enabled" .Values.recurringRuntime.expectedSourceRevision -}}
{{- if not (regexMatch "^[0-9a-f]{40}$" (toString $expectedSourceRevision)) -}}
{{- fail "recurringRuntime.expectedSourceRevision must be an exact 40-character lowercase source revision when the recurring runtime topology is enabled" -}}
{{- end -}}
{{- if ne (toString $expectedSourceRevision) (toString $imageTag) -}}
{{- fail "recurringRuntime.expectedSourceRevision must match image.tag exactly when the recurring runtime topology is enabled" -}}
{{- end -}}

{{- $environment := .Values.paymentSafety.environment -}}
{{- if eq $environment "development" -}}
{{- if or (ne .Release.Name "citrus-dev") (ne .Release.Namespace "citrus-dev") (ne .Values.paymentSafety.owner "citrus-dev") -}}
{{- fail "development recurring runtime requires release citrus-dev in namespace citrus-dev owned by citrus-dev" -}}
{{- end -}}
{{- else if eq $environment "production" -}}
{{- if or (ne .Release.Name "citrus") (ne .Release.Namespace "default") (ne .Values.paymentSafety.owner "citrus") -}}
{{- fail "production recurring runtime requires release citrus in namespace default owned by citrus" -}}
{{- end -}}
{{- else -}}
{{- fail "the recurring runtime topology requires a named development or production paymentSafety.environment" -}}
{{- end -}}

{{- if ne .Values.recurringRuntime.scheduler "kubernetes-cronjob" -}}
{{- fail "recurringRuntime.scheduler must be kubernetes-cronjob" -}}
{{- end -}}
{{- $topologyRevision := required "recurringRuntime.topologyRevision is required when the recurring runtime topology is enabled" .Values.recurringRuntime.topologyRevision -}}
{{- if not (regexMatch "^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$" $topologyRevision) -}}
{{- fail "recurringRuntime.topologyRevision must be a semantic revision of 1-63 safe characters" -}}
{{- end -}}
{{- $workerRevision := required "billingWorker.topologyRevision is required when the recurring runtime topology is enabled" .Values.billingWorker.topologyRevision -}}
{{- $preflightRevision := required "recurringRuntime.preflight.topologyRevision is required when the recurring runtime topology is enabled" .Values.recurringRuntime.preflight.topologyRevision -}}
{{- $healthRevision := required "recurringRuntime.health.topologyRevision is required when the recurring runtime topology is enabled" .Values.recurringRuntime.health.topologyRevision -}}
{{- if or (ne $workerRevision $topologyRevision) (ne $preflightRevision $topologyRevision) (ne $healthRevision $topologyRevision) -}}
{{- fail "billing worker, preflight, tick, and health topology revisions must match exactly" -}}
{{- end -}}

{{- if ne (toString .Values.application.configData.RECURRING_BILLING_QUEUE) "billing" -}}
{{- fail "application.configData.RECURRING_BILLING_QUEUE must be billing for the recurring runtime topology" -}}
{{- end -}}
{{- if ne (lower (toString .Values.application.configData.RECURRING_ORDER_ENROLLMENT_MODE)) "off" -}}
{{- fail "RECURRING_ORDER_ENROLLMENT_MODE must remain off during runtime preflight" -}}
{{- end -}}
{{- if ne (toString .Values.application.configData.RECURRING_ORDER_ENROLLMENT_ALLOWLIST) "" -}}
{{- fail "RECURRING_ORDER_ENROLLMENT_ALLOWLIST must remain empty during runtime preflight" -}}
{{- end -}}
{{- if ne (lower (toString .Values.application.configData.RECURRING_ORDER_COHORT_MODE)) "off" -}}
{{- fail "RECURRING_ORDER_COHORT_MODE must remain off during runtime preflight" -}}
{{- end -}}
{{- if ne (toString .Values.application.configData.RECURRING_ORDER_COHORT_ALLOWLIST) "" -}}
{{- fail "RECURRING_ORDER_COHORT_ALLOWLIST must remain empty during runtime preflight" -}}
{{- end -}}
{{- if ne (lower (toString .Values.application.configData.RECURRING_REMINDERS_ENABLED)) "false" -}}
{{- fail "RECURRING_REMINDERS_ENABLED must remain false during runtime preflight" -}}
{{- end -}}
{{- if ne (lower (toString .Values.application.configData.RECURRING_CHARGING_ENABLED)) "false" -}}
{{- fail "RECURRING_CHARGING_ENABLED must remain false during runtime preflight" -}}
{{- end -}}
{{- if ne (lower (toString .Values.application.configData.RECURRING_CHARGE_EMERGENCY_STOP)) "true" -}}
{{- fail "RECURRING_CHARGE_EMERGENCY_STOP must remain true during runtime preflight" -}}
{{- end -}}
{{- if ne (lower (toString .Values.application.configData.CELERY_TASK_ALWAYS_EAGER)) "false" -}}
{{- fail "CELERY_TASK_ALWAYS_EAGER must remain false for the recurring runtime scheduler" -}}
{{- end -}}
{{- $brokerURL := printf "redis://%s:%d/1" .Values.redis.name (int .Values.redis.service.port) -}}
{{- if ne (toString .Values.application.configData.CELERY_BROKER_URL) $brokerURL -}}
{{- fail "CELERY_BROKER_URL must select the same-release Redis billing broker" -}}
{{- end -}}

{{- $preflightCommand := list "python" "manage.py" "preflight_recurring_runtime" "--include-broker" "--format=json" -}}
{{- if not (deepEqual .Values.recurringRuntime.preflight.command $preflightCommand) -}}
{{- fail "recurringRuntime.preflight.command must use the provider-free recurring runtime preflight" -}}
{{- end -}}
{{- if ne (int .Values.recurringRuntime.preflight.backoffLimit) 0 -}}
{{- fail "recurringRuntime.preflight.backoffLimit must be 0" -}}
{{- end -}}
{{- $healthCommand := list "python" "manage.py" "check_recurring_runtime" "--include-broker" "--format=json" "--fail-on-alert" -}}
{{- if not (deepEqual .Values.recurringRuntime.health.command $healthCommand) -}}
{{- fail "recurringRuntime.health.command must use the fail-closed recurring runtime health check" -}}
{{- end -}}
{{- $tickCommand := list "python" "manage.py" "tick_recurring_orders" "--scan-limit=100" "--dispatch-limit=100" -}}
{{- if not (deepEqual .Values.recurringRuntime.command $tickCommand) -}}
{{- fail "recurringRuntime.command must use the bounded recurring runtime tick" -}}
{{- end -}}
{{- if ne .Values.recurringRuntime.schedule "*/5 * * * *" -}}
{{- fail "recurringRuntime.schedule must be exactly */5 * * * *" -}}
{{- end -}}
{{- if ne .Values.recurringRuntime.health.schedule "2-59/5 * * * *" -}}
{{- fail "recurringRuntime.health.schedule must be exactly 2-59/5 * * * *" -}}
{{- end -}}
{{- if ne .Values.recurringRuntime.timeZone "Etc/UTC" -}}
{{- fail "recurringRuntime.timeZone must be Etc/UTC" -}}
{{- end -}}
{{- if ne .Values.recurringRuntime.concurrencyPolicy "Forbid" -}}
{{- fail "recurringRuntime.concurrencyPolicy must be Forbid" -}}
{{- end -}}

{{- if not (regexMatch "^-?[0-9]+$" (toString .Values.syncWaves.migrations)) -}}
{{- fail "syncWaves.migrations must be an integer string" -}}
{{- end -}}
{{- if not (regexMatch "^-?[0-9]+$" (toString .Values.syncWaves.recurringPreflight)) -}}
{{- fail "syncWaves.recurringPreflight must be an integer string" -}}
{{- end -}}
{{- if not (regexMatch "^-?[0-9]+$" (toString .Values.syncWaves.billingWorker)) -}}
{{- fail "syncWaves.billingWorker must be an integer string" -}}
{{- end -}}
{{- if not (regexMatch "^-?[0-9]+$" (toString .Values.syncWaves.recurringRuntime)) -}}
{{- fail "syncWaves.recurringRuntime must be an integer string" -}}
{{- end -}}
{{- $migrationWave := int .Values.syncWaves.migrations -}}
{{- $preflightWave := int .Values.syncWaves.recurringPreflight -}}
{{- $workerWave := int .Values.syncWaves.billingWorker -}}
{{- $runtimeWave := int .Values.syncWaves.recurringRuntime -}}
{{- if or (le $preflightWave $migrationWave) (ge $preflightWave $workerWave) (ge $preflightWave $runtimeWave) -}}
{{- fail "syncWaves.recurringPreflight must be after migrations and before billingWorker and recurringRuntime" -}}
{{- end -}}
{{- end -}}
{{- end }}

{{/* Same nonsecret topology attestation for each recurring runtime process. */}}
{{- define "citrus.recurringRuntime.env" -}}
- name: RECURRING_RUNTIME_TOPOLOGY_REVISION
  value: {{ .revision | quote }}
- name: RECURRING_RUNTIME_SCHEDULER
  value: "kubernetes-cronjob"
- name: CITRUS_EXPECTED_SOURCE_REVISION
  value: {{ .expectedSourceRevision | quote }}
{{- end }}

{{/* Runtime ownership and policy attestation for every Citrus process. */}}
{{- define "citrus.paymentSafety.env" -}}
{{- if .Values.paymentSafety.enabled }}
- name: DJANGO_ENV
  value: {{ .Values.paymentSafety.environment | quote }}
- name: CITRUS_ENVIRONMENT_OWNER
  value: {{ .Values.paymentSafety.owner | quote }}
- name: PAYMENT_NETWORK_MODE
  value: {{ .Values.paymentSafety.networkMode | quote }}
- name: PAYMENT_EGRESS_POLICY_REQUIRED
  value: {{ ternary "true" "false" .Values.paymentSafety.policy.required | quote }}
- name: PAYMENT_EGRESS_POLICY_PROVIDER
  value: {{ .Values.paymentSafety.policy.provider | quote }}
- name: PAYMENT_EGRESS_POLICY_REVISION
  value: {{ .Values.paymentSafety.policy.revision | quote }}
{{- end }}
{{- end }}

{{/* Common label selected by the CES-845 Cilium policy, never Redis. */}}
{{- define "citrus.paymentSafety.podLabel" -}}
{{- if .Values.paymentSafety.enabled }}
citrus.grace/payment-egress-boundary: enabled
{{- end }}
{{- end }}

{{/*
Common labels.
*/}}
{{- define "citrus.labels" -}}
helm.sh/chart: {{ .Chart.Name }}
{{ include "citrus.selectorLabels" . }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels.
*/}}
{{- define "citrus.selectorLabels" -}}
app.kubernetes.io/name: {{ include "citrus.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Image pull secrets.
*/}}
{{- define "citrus.imagePullSecrets" -}}
{{- if .Values.global.imagePullSecrets }}
imagePullSecrets:
{{- range .Values.global.imagePullSecrets }}
  - name: {{ . }}
{{- end }}
{{- end }}
{{- end }}
