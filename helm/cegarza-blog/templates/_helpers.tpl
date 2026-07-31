{{/*
Expand the name of the chart.
*/}}
{{- define "cegarza-blog.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "cegarza-blog.fullname" -}}
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
{{- define "cegarza-blog.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "cegarza-blog.labels" -}}
helm.sh/chart: {{ include "cegarza-blog.chart" . }}
{{ include "cegarza-blog.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Resolve the immutable pod-selector name. A release may temporarily preserve
the previous selector while its Deployment is replaced in a later revision.
*/}}
{{- define "cegarza-blog.selectorName" -}}
{{- $name := default (include "cegarza-blog.name" .) .Values.blog.selectorName -}}
{{- if or (gt (len $name) 63) (not (regexMatch "^[a-z0-9]([-a-z0-9]*[a-z0-9])?$" $name)) -}}
{{- fail "blog.selectorName must be a valid Kubernetes label value of at most 63 characters" -}}
{{- end -}}
{{- $name -}}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "cegarza-blog.selectorLabels" -}}
app.kubernetes.io/name: {{ include "cegarza-blog.selectorName" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Image pull secrets
*/}}
{{- define "cegarza-blog.imagePullSecrets" -}}
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
{{- define "cegarza-blog.imageTag" -}}
{{- .Values.blog.image.tag | default .Values.global.appImageTag | default "latest" }}
{{- end }}

{{/*
Resolve the complete image reference. An immutable digest takes precedence.
*/}}
{{- define "cegarza-blog.image" -}}
{{- $repository := required "blog.image.repository is required" .Values.blog.image.repository -}}
{{- if .Values.blog.image.digest -}}
{{- if not (regexMatch "^sha256:[a-f0-9]{64}$" .Values.blog.image.digest) -}}
{{- fail "blog.image.digest must be a lowercase sha256:<64 hex> digest" -}}
{{- end -}}
{{- printf "%s@%s" $repository .Values.blog.image.digest -}}
{{- else -}}
{{- printf "%s:%s" $repository (include "cegarza-blog.imageTag" .) -}}
{{- end -}}
{{- end }}
