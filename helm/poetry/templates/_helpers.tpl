{{/* Expand the chart name. */}}
{{- define "poetry.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/* Create a fully qualified application name. */}}
{{- define "poetry.fullname" -}}
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

{{/* Common labels. */}}
{{- define "poetry.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{ include "poetry.selectorLabels" . }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/* Selector labels. */}}
{{- define "poetry.selectorLabels" -}}
app.kubernetes.io/name: {{ include "poetry.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/* Image reference. Tags are commit pins; an optional digest takes precedence. */}}
{{- define "poetry.image" -}}
{{- $repository := required "image.repository is required" .Values.image.repository -}}
{{- if .Values.image.digest -}}
{{- if not (regexMatch "^sha256:[a-f0-9]{64}$" .Values.image.digest) -}}
{{- fail "image.digest must be a sha256 digest" -}}
{{- end -}}
{{- printf "%s@%s" $repository .Values.image.digest -}}
{{- else -}}
{{- $tag := required "image.tag is required when image.digest is empty" .Values.image.tag -}}
{{- if not (regexMatch "^sha-[a-f0-9]{40}$" $tag) -}}
{{- fail "image.tag must be an immutable sha-<40 lowercase hex> commit pin" -}}
{{- end -}}
{{- printf "%s:%s" $repository $tag -}}
{{- end -}}
{{- end }}

{{/* Shared environment sources. */}}
{{- define "poetry.envFrom" -}}
- configMapRef:
    name: {{ .Values.application.configMapName }}
{{- range .Values.application.secretRefs }}
- secretRef:
    name: {{ . }}
{{- end }}
{{- end }}

{{/* Shared image pull secrets. */}}
{{- define "poetry.imagePullSecrets" -}}
{{- with .Values.imagePullSecrets }}
imagePullSecrets:
{{- range . }}
  - name: {{ . }}
{{- end }}
{{- end }}
{{- end }}
