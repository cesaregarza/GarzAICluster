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
