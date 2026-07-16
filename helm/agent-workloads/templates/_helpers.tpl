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
Image reference for a values image subtree.
*/}}
{{- define "agent-workloads.imageRef" -}}
{{- if .digest -}}
{{- printf "%s@%s" .repository .digest -}}
{{- else -}}
{{- printf "%s:%s" .repository .tag -}}
{{- end -}}
{{- end }}

{{/*
Checksum of release pins that should roll worker pods when registry pins move.
*/}}
{{- define "agent-workloads.releasePinsChecksum" -}}
{{- if .Values.mandateReleasePins -}}
{{- toJson .Values.mandateReleasePins | sha256sum -}}
{{- else -}}
absent
{{- end -}}
{{- end }}

{{/*
Checksum of the SOPS workload-identity-token Secret ciphertext.
*/}}
{{- define "agent-workloads.workloadIdentityTokenSecretChecksum" -}}
{{- default "absent" .Values.rolloutChecksums.workloadIdentityTokenSecret -}}
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
Normalize a worker id for a DNS-label ServiceAccount name.
*/}}
{{- define "agent-workloads.projectedIdentityWorkerName" -}}
{{- regexReplaceAll "[^a-z0-9]+" (lower .Values.projectedWorkloadIdentity.workerId) "-" | trimAll "-" -}}
{{- end }}

{{/*
Build a release-scoped ServiceAccount name from the trusted immutable release
tuple. The 20-hex (80-bit) suffix is part of the verifier binding, so fail
rather than truncating it.
*/}}
{{- define "agent-workloads.releaseScopedServiceAccountName" -}}
{{- $root := .root -}}
{{- $release := required "release-scoped ServiceAccount requires an immutable release tuple" .release -}}
{{- $codeDigest := required "release-scoped ServiceAccount requires codeDigest" $release.codeDigest -}}
{{- $manifestDigest := required "release-scoped ServiceAccount requires manifestDigest" $release.manifestDigest -}}
{{- $imageDigest := required "release-scoped ServiceAccount requires imageDigest" $release.imageDigest -}}
{{- range $label, $digest := dict "codeDigest" $codeDigest "manifestDigest" $manifestDigest "imageDigest" $imageDigest -}}
{{- if not (regexMatch "^sha256:[a-f0-9]{64}$" $digest) -}}
{{- fail (printf "release-scoped ServiceAccount %s must be lowercase sha256:<64 hex>" $label) -}}
{{- end -}}
{{- end -}}
{{- $workerName := include "agent-workloads.projectedIdentityWorkerName" $root -}}
{{- if not (regexMatch "^[a-z0-9]+(-[a-z0-9]+)*$" $workerName) -}}
{{- fail "projectedWorkloadIdentity.workerId must normalize to a DNS label" -}}
{{- end -}}
{{- $bundlePayload := printf "{\"code_digest\":\"%s\",\"image_digest\":\"%s\",\"manifest_digest\":\"%s\",\"schema_version\":\"workload_identity_bundle.v1\"}" $codeDigest $imageDigest $manifestDigest -}}
{{- $digestSuffix := trunc 20 (sha256sum $bundlePayload) -}}
{{- $name := printf "%s-%s-%s" $root.Values.projectedWorkloadIdentity.serviceAccountNamePrefix $workerName $digestSuffix -}}
{{- if gt (len $name) 63 -}}
{{- fail "release-scoped ServiceAccount name exceeds 63 characters" -}}
{{- end -}}
{{- if not (regexMatch "^[a-z0-9]([-a-z0-9]*[a-z0-9])?$" $name) -}}
{{- fail "release-scoped ServiceAccount name must be a DNS label" -}}
{{- end -}}
{{- $name -}}
{{- end }}

{{/* Current projected-identity ServiceAccount. */}}
{{- define "agent-workloads.projectedIdentityServiceAccountName" -}}
{{- $workerId := .Values.projectedWorkloadIdentity.workerId -}}
{{- $releasePins := required "projected identity requires mandateReleasePins" .Values.mandateReleasePins -}}
{{- $workerPins := required (printf "projected identity requires mandateReleasePins[%s]" $workerId) (index $releasePins $workerId) -}}
{{- include "agent-workloads.releaseScopedServiceAccountName" (dict "root" . "release" $workerPins) -}}
{{- end }}

{{/* Previous projected-identity ServiceAccount retained during rollout overlap. */}}
{{- define "agent-workloads.previousProjectedIdentityServiceAccountName" -}}
{{- include "agent-workloads.releaseScopedServiceAccountName" (dict "root" . "release" .Values.projectedWorkloadIdentity.previousRelease) -}}
{{- end }}

{{/* Full projected worker token path. */}}
{{- define "agent-workloads.projectedIdentityTokenPath" -}}
{{- printf "%s/%s" (trimSuffix "/" .Values.projectedWorkloadIdentity.token.mountPath) .Values.projectedWorkloadIdentity.token.fileName -}}
{{- end }}

{{/*
Runtime environment.
*/}}
{{- define "agent-workloads.runtimeEnv" -}}
{{- include "agent-workloads.envFromValues" (dict "root" . "values" .Values) }}
{{- end }}

{{/*
Runtime environment for a values subtree.
*/}}
{{- define "agent-workloads.envFromValues" -}}
{{- $root := .root }}
{{- $values := .values }}
{{- $secretEnvSecretName := default $root.Values.global.runtimeSecretName $values.secretEnvSecretName }}
{{- range $key := $values.secretKeys }}
- name: {{ $key }}
  valueFrom:
    secretKeyRef:
      name: {{ $root.Values.global.runtimeSecretName }}
      key: {{ $key }}
{{- end }}
{{- range $envName, $secretKey := $values.secretEnv }}
- name: {{ $envName }}
  valueFrom:
    secretKeyRef:
      name: {{ $secretEnvSecretName }}
      key: {{ $secretKey }}
{{- end }}
{{- range $key, $value := $values.env }}
- name: {{ $key }}
  value: {{ $value | quote }}
{{- end }}
{{- end }}

{{/*
OpenCode proposer object name.
*/}}
{{- define "agent-workloads.opencodeProposerName" -}}
{{- printf "%s-opencode-proposer" (include "agent-workloads.fullname" .) | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
OpenCode proposer selector labels.
*/}}
{{- define "agent-workloads.opencodeProposerSelectorLabels" -}}
app.kubernetes.io/name: opencode-proposer
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
OpenCode proposer labels.
*/}}
{{- define "agent-workloads.opencodeProposerLabels" -}}
helm.sh/chart: {{ include "agent-workloads.chart" . }}
{{ include "agent-workloads.opencodeProposerSelectorLabels" . }}
app.kubernetes.io/part-of: {{ include "agent-workloads.name" . }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/component: opencode-proposer
{{- end }}
