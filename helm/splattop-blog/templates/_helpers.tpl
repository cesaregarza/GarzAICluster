{{/*
Expand the name of the chart.
*/}}
{{- define "splattop-blog.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "splattop-blog.fullname" -}}
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
{{- define "splattop-blog.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "splattop-blog.labels" -}}
helm.sh/chart: {{ include "splattop-blog.chart" . }}
{{ include "splattop-blog.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "splattop-blog.selectorLabels" -}}
app.kubernetes.io/name: {{ include "splattop-blog.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Image pull secrets
*/}}
{{- define "splattop-blog.imagePullSecrets" -}}
{{- if .Values.global.imagePullSecrets }}
imagePullSecrets:
{{- range .Values.global.imagePullSecrets }}
  - name: {{ . }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Resolve image tag with fallback to global
*/}}
{{- define "splattop-blog.imageTag" -}}
{{- .Values.blog.image.tag | default .Values.global.appImageTag | default "latest" }}
{{- end }}

{{/*
Resolve the complete image reference. An immutable digest takes precedence.
*/}}
{{- define "splattop-blog.image" -}}
{{- $repository := required "blog.image.repository is required" .Values.blog.image.repository -}}
{{- if .Values.blog.image.digest -}}
{{- if not (regexMatch "^sha256:[a-f0-9]{64}$" .Values.blog.image.digest) -}}
{{- fail "blog.image.digest must be a lowercase sha256:<64 hex> digest" -}}
{{- end -}}
{{- printf "%s@%s" $repository .Values.blog.image.digest -}}
{{- else -}}
{{- printf "%s:%s" $repository (include "splattop-blog.imageTag" .) -}}
{{- end -}}
{{- end }}
