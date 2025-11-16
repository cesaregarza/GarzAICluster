{{- define "agent-8s.name" -}}
{{- .Chart.Name -}}
{{- end -}}

{{- define "agent-8s.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- include "agent-8s.name" . | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end -}}

{{- define "agent-8s.labels" -}}
app.kubernetes.io/name: {{ include "agent-8s.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/part-of: discord-bots
{{- end -}}

{{- define "agent-8s.selectorLabels" -}}
app.kubernetes.io/name: {{ include "agent-8s.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "agent-8s.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- if .Values.serviceAccount.name }}
{{- .Values.serviceAccount.name }}
{{- else }}
{{- include "agent-8s.fullname" . }}
{{- end }}
{{- else }}
default
{{- end }}
{{- end -}}
