{{/*
Expand the name of the chart.
*/}}
{{- define "agent-workloads.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "agent-workloads.fullname" -}}
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
{{- define "agent-workloads.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels.
*/}}
{{- define "agent-workloads.labels" -}}
helm.sh/chart: {{ include "agent-workloads.chart" . }}
{{ include "agent-workloads.selectorLabels" . }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels.
*/}}
{{- define "agent-workloads.selectorLabels" -}}
app.kubernetes.io/name: {{ include "agent-workloads.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Image pull secrets.
*/}}
{{- define "agent-workloads.imagePullSecrets" -}}
{{- if .Values.global.imagePullSecrets }}
imagePullSecrets:
{{- range .Values.global.imagePullSecrets }}
  - name: {{ . }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Service account name.
*/}}
{{- define "agent-workloads.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- include "agent-workloads.fullname" . }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Runtime environment.
*/}}
{{- define "agent-workloads.runtimeEnv" -}}
{{- $root := . }}
{{- range $key := .Values.secretKeys }}
- name: {{ $key }}
  valueFrom:
    secretKeyRef:
      name: {{ $root.Values.global.runtimeSecretName }}
      key: {{ $key }}
{{- end }}
{{- range $key, $value := .Values.env }}
- name: {{ $key }}
  value: {{ $value | quote }}
{{- end }}
{{- end }}
