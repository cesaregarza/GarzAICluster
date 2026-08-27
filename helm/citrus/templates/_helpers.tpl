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
{{- $owner := required "paymentCredentials.owner is required when paymentCredentials.enabled=true" .Values.paymentCredentials.owner -}}
{{- if not .Values.paymentSafety.enabled -}}
{{- fail "paymentSafety.enabled must be true when paymentCredentials.enabled=true" -}}
{{- end -}}
{{- if eq $webhookEnvironmentVariable "STRIPE_WEBHOOK_SECRET_DEV" -}}
{{- if ne $owner "citrus-dev" -}}
{{- fail "paymentCredentials.owner must be citrus-dev for the dev webhook setting" -}}
{{- end -}}
{{- if ne $secretName "citrus-dev-payment-credentials" -}}
{{- fail "the dev webhook setting requires secretName citrus-dev-payment-credentials" -}}
{{- end -}}
{{- if or (ne .Values.paymentSafety.environment "development") (ne .Values.paymentSafety.owner "citrus-dev") -}}
{{- fail "dev payment credentials require paymentSafety.environment=development and paymentSafety.owner=citrus-dev" -}}
{{- end -}}
{{- else -}}
{{- if ne $owner "citrus" -}}
{{- fail "paymentCredentials.owner must be citrus for the production webhook setting" -}}
{{- end -}}
{{- if ne $secretName "citrus-prod-payment-credentials" -}}
{{- fail "the production webhook setting requires secretName citrus-prod-payment-credentials" -}}
{{- end -}}
{{- if or (ne .Values.paymentSafety.environment "production") (ne .Values.paymentSafety.owner "citrus") -}}
{{- fail "production payment credentials require paymentSafety.environment=production and paymentSafety.owner=citrus" -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- end }}

{{/*
Project only API authority into non-web payment consumers. Explicit env entries
override legacy envFrom keys during replacement-before-removal staging.
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
      name: {{ .Values.paymentCredentials.secretName }}
      key: STRIPE_WEBHOOK_SECRET
      optional: false
- name: STRIPE_WEBHOOK_SECRET_OWNER
  value: {{ .Values.paymentCredentials.owner | quote }}
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
