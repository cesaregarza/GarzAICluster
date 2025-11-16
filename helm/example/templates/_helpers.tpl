{{- define "discord-bot.name" -}}
{{- .Chart.Name -}}
{{- end -}}

{{- define "discord-bot.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- include "discord-bot.name" . | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end -}}

{{- define "discord-bot.labels" -}}
app.kubernetes.io/name: {{ include "discord-bot.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/part-of: discord-bots
{{- end -}}

{{- define "discord-bot.selectorLabels" -}}
app.kubernetes.io/name: {{ include "discord-bot.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "discord-bot.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- if .Values.serviceAccount.name }}
{{- .Values.serviceAccount.name }}
{{- else }}
{{- include "discord-bot.fullname" . }}
{{- end }}
{{- else }}
default
{{- end }}
{{- end -}}
