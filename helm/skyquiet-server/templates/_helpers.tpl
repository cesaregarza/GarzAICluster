{{/*
Expand the name of the chart.
*/}}
{{- define "skyquiet-server.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "skyquiet-server.fullname" -}}
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
Runtime environment variables.
*/}}
{{- define "skyquiet-server.env" -}}
{{- range $key := .Values.secretKeys }}
- name: {{ $key }}
  valueFrom:
    secretKeyRef:
      name: {{ $.Values.global.runtimeSecretName }}
      key: {{ $key }}
{{- end }}
{{- range $key := .Values.optionalSecretKeys }}
- name: {{ $key }}
  valueFrom:
    secretKeyRef:
      name: {{ $.Values.global.runtimeSecretName }}
      key: {{ $key }}
      optional: true
{{- end }}
{{- range $key, $value := .Values.env }}
- name: {{ $key }}
  value: {{ $value | quote }}
{{- end }}
{{- end }}

{{/*
Create chart name and version as used by the chart label.
*/}}
{{- define "skyquiet-server.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels.
*/}}
{{- define "skyquiet-server.labels" -}}
helm.sh/chart: {{ include "skyquiet-server.chart" . }}
{{ include "skyquiet-server.selectorLabels" . }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels.
*/}}
{{- define "skyquiet-server.selectorLabels" -}}
app.kubernetes.io/name: {{ include "skyquiet-server.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Image pull secrets.
*/}}
{{- define "skyquiet-server.imagePullSecrets" -}}
{{- if .Values.global.imagePullSecrets }}
imagePullSecrets:
{{- range .Values.global.imagePullSecrets }}
  - name: {{ . }}
{{- end }}
{{- end }}
{{- end }}
