{{/* Fixed naming, labels, and collision checks for the isolated broker. */}}
{{- define "agent-workloads.citrusBroker.name" -}}
{{- printf "%s-citrus-broker" (.Release.Name | trunc 49 | trimSuffix "-") -}}
{{- end }}

{{- define "agent-workloads.citrusBroker.selectorLabels" -}}
app.kubernetes.io/name: citrus-purchasing-broker
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: citrus-purchasing-broker
{{- end }}

{{- define "agent-workloads.citrusBroker.labels" -}}
helm.sh/chart: {{ include "agent-workloads.chart" . }}
{{ include "agent-workloads.citrusBroker.selectorLabels" . }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "agent-workloads.citrusBroker.validate" -}}
{{- if .Values.citrusPurchasingBroker.enabled -}}
{{- $brokerName := include "agent-workloads.citrusBroker.name" . -}}
{{- range $workerId, $worker := .Values.workers -}}
{{- if and $worker.enabled (eq (include "agent-workloads.workerMetadataName" (dict "root" $ "worker" $worker "workerId" $workerId)) $brokerName) -}}
{{- fail (printf "workers[%s].metadataName collides with citrus purchasing broker resource name %s" $workerId $brokerName) -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- end }}
