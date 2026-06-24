{{/*
Expand the name of the chart.
*/}}
{{- define "garzObservability.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "garzObservability.fullname" -}}
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
Create chart name and version as used by the chart label.
*/}}
{{- define "garzObservability.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "garzObservability.labels" -}}
helm.sh/chart: {{ include "garzObservability.chart" . }}
{{ include "garzObservability.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "garzObservability.selectorLabels" -}}
{{- $legacy := default dict .Values.legacySelectorLabels -}}
app.kubernetes.io/name: {{ default (include "garzObservability.name" .) (index $legacy "name") }}
app.kubernetes.io/instance: {{ default .Release.Name (index $legacy "instance") }}
{{- end }}

{{/*
Component-specific labels
*/}}
{{- define "garzObservability.componentLabels" -}}
{{- $component := .component -}}
{{- $root := .root -}}
{{ include "garzObservability.labels" $root }}
app.kubernetes.io/component: {{ $component }}
{{- end }}

{{/*
Component-specific selector labels
*/}}
{{- define "garzObservability.componentSelectorLabels" -}}
{{- $component := .component -}}
{{- $root := .root -}}
{{ include "garzObservability.selectorLabels" $root }}
app.kubernetes.io/component: {{ $component }}
{{- end }}

{{/*
Create the name of the service account to use
*/}}
{{- define "garzObservability.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "garzObservability.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Image pull secrets
*/}}
{{- define "garzObservability.imagePullSecrets" -}}
{{- if .Values.global.imagePullSecrets }}
imagePullSecrets:
{{- range .Values.global.imagePullSecrets }}
  - name: {{ . }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Database secret name
*/}}
{{- define "garzObservability.databaseSecretName" -}}
{{- if .Values.databaseSecret.name }}
{{- .Values.databaseSecret.name }}
{{- else }}
{{- default "db-secrets" .Values.global.databaseSecretName }}
{{- end }}
{{- end }}

{{/*
Namespace to use for monitoring resources
*/}}
{{- define "garzObservability.monitoringNamespace" -}}
{{- if .Values.monitoring.namespace }}
{{- .Values.monitoring.namespace }}
{{- else }}
{{- .Release.Namespace }}
{{- end }}
{{- end }}

{{/*
Namespace for a given component (app vs monitoring)
*/}}
{{- define "garzObservability.componentNamespace" -}}
{{- $component := .component -}}
{{- $root := .root -}}
{{- if or (eq $component "prometheus") (eq $component "grafana") (eq $component "alertmanager") }}
{{- include "garzObservability.monitoringNamespace" $root }}
{{- else }}
{{- $root.Release.Namespace }}
{{- end }}
{{- end }}

{{/*
Resolve the image tag for an application component. Falls back to the
global.appImageTag value and finally the chart appVersion if the component
does not declare its own tag override.
*/}}
{{- define "garzObservability.imageTag" -}}
{{- $component := .component -}}
{{- $root := .root -}}
{{- $componentValues := (index $root.Values $component) -}}
{{- $tag := "" -}}
{{- if and $componentValues $componentValues.image $componentValues.image.tag }}
{{- $tag = $componentValues.image.tag }}
{{- end }}
{{- if not $tag }}
{{- $tag = default "" $root.Values.global.appImageTag }}
{{- end }}
{{- if not $tag }}
{{- $tag = default "" $root.Chart.AppVersion }}
{{- end }}
{{- $tag -}}
{{- end }}
