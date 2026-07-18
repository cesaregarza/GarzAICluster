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
{{- define "agent-workloads.workerIdentityName" -}}
{{- regexReplaceAll "[^a-z0-9]+" (lower .workerId) "-" | trimAll "-" -}}
{{- end }}

{{- define "agent-workloads.projectedIdentityWorkerName" -}}
{{- include "agent-workloads.workerIdentityName" (dict "workerId" .Values.projectedWorkloadIdentity.workerId) -}}
{{- end }}

{{/*
Build a release-scoped ServiceAccount name from the trusted immutable release
tuple. The 20-hex (80-bit) suffix is part of the verifier binding, so fail
rather than truncating it.
*/}}
{{- define "agent-workloads.releaseScopedServiceAccountName" -}}
{{- $root := .root -}}
{{- $release := required "release-scoped ServiceAccount requires an immutable release tuple" .release -}}
{{- $workerId := default $root.Values.projectedWorkloadIdentity.workerId .workerId -}}
{{- $serviceAccountNamePrefix := default $root.Values.projectedWorkloadIdentity.serviceAccountNamePrefix .serviceAccountNamePrefix -}}
{{- $identityLabel := default "projectedWorkloadIdentity" .identityLabel -}}
{{- $codeDigest := required "release-scoped ServiceAccount requires codeDigest" $release.codeDigest -}}
{{- $manifestDigest := required "release-scoped ServiceAccount requires manifestDigest" $release.manifestDigest -}}
{{- $imageDigest := required "release-scoped ServiceAccount requires imageDigest" $release.imageDigest -}}
{{- range $label, $digest := dict "codeDigest" $codeDigest "manifestDigest" $manifestDigest "imageDigest" $imageDigest -}}
{{- if not (regexMatch "^sha256:[a-f0-9]{64}$" $digest) -}}
{{- fail (printf "release-scoped ServiceAccount %s must be lowercase sha256:<64 hex>" $label) -}}
{{- end -}}
{{- end -}}
{{- $workerName := include "agent-workloads.workerIdentityName" (dict "workerId" $workerId) -}}
{{- if not (regexMatch "^[a-z0-9]+(-[a-z0-9]+)*$" $workerName) -}}
{{- fail (printf "%s.workerId must normalize to a DNS label" $identityLabel) -}}
{{- end -}}
{{- $bundlePayload := printf "{\"code_digest\":\"%s\",\"image_digest\":\"%s\",\"manifest_digest\":\"%s\",\"schema_version\":\"workload_identity_bundle.v1\"}" $codeDigest $imageDigest $manifestDigest -}}
{{- $digestSuffix := trunc 20 (sha256sum $bundlePayload) -}}
{{- $name := printf "%s-%s-%s" $serviceAccountNamePrefix $workerName $digestSuffix -}}
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

{{/*
OpenCode apply executor object name and labels.
*/}}
{{- define "agent-workloads.opencodeApplyExecutorName" -}}
{{- printf "%s-opencode-apply-executor" (include "agent-workloads.fullname" .) | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "agent-workloads.opencodeApplyExecutorSelectorLabels" -}}
app.kubernetes.io/name: opencode-apply-executor
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "agent-workloads.opencodeApplyExecutorLabels" -}}
helm.sh/chart: {{ include "agent-workloads.chart" . }}
{{ include "agent-workloads.opencodeApplyExecutorSelectorLabels" . }}
app.kubernetes.io/part-of: {{ include "agent-workloads.name" . }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/component: opencode-apply-executor
{{- end }}

{{/*
Release-scoped identity helpers for one split OpenCode worker.
*/}}
{{- define "agent-workloads.opencodeIdentityServiceAccountName" -}}
{{- $root := .root -}}
{{- $identity := .identity -}}
{{- $workerId := required "OpenCode identity requires workerId" $identity.workerId -}}
{{- $releasePins := required "OpenCode identity requires mandateReleasePins" $root.Values.mandateReleasePins -}}
{{- $workerPins := required (printf "OpenCode identity requires mandateReleasePins[%s]" $workerId) (index $releasePins $workerId) -}}
{{- include "agent-workloads.releaseScopedServiceAccountName" (dict "root" $root "release" $workerPins "workerId" $workerId "serviceAccountNamePrefix" $identity.serviceAccountNamePrefix "identityLabel" .identityLabel) -}}
{{- end }}

{{- define "agent-workloads.previousOpencodeIdentityServiceAccountName" -}}
{{- include "agent-workloads.releaseScopedServiceAccountName" (dict "root" .root "release" .identity.previousRelease "workerId" .identity.workerId "serviceAccountNamePrefix" .identity.serviceAccountNamePrefix "identityLabel" .identityLabel) -}}
{{- end }}

{{- define "agent-workloads.opencodeIdentityTokenPath" -}}
{{- printf "%s/%s" (trimSuffix "/" .identity.token.mountPath) .identity.token.fileName -}}
{{- end }}

{{/*
Fail closed when a split OpenCode worker could collapse release or credential
identity. This helper emits no manifest content.
*/}}
{{- define "agent-workloads.validateOpencodeIdentity" -}}
{{- $root := .root -}}
{{- $values := .values -}}
{{- $identity := $values.identity -}}
{{- $label := .label -}}
{{- $workerId := required (printf "%s identity requires workerId" $label) $identity.workerId -}}
{{- if ne $workerId $values.env.AGENT_WORKLOADS_WORKER_ID -}}
{{- fail (printf "%s identity workerId must match AGENT_WORKLOADS_WORKER_ID" $label) -}}
{{- end -}}
{{- $releasePins := required (printf "%s identity requires mandateReleasePins" $label) $root.Values.mandateReleasePins -}}
{{- $workerPins := required (printf "%s identity requires mandateReleasePins[%s]" $label $workerId) (index $releasePins $workerId) -}}
{{- $runtimeImageDigest := required (printf "%s governed identity requires immutable image.digest" $label) $values.image.digest -}}
{{- if not (regexMatch "^sha256:[a-f0-9]{64}$" $runtimeImageDigest) -}}
{{- fail (printf "%s image.digest must be lowercase sha256:<64 hex>" $label) -}}
{{- end -}}
{{- if ne $runtimeImageDigest $workerPins.imageDigest -}}
{{- fail (printf "%s image.digest must equal mandateReleasePins[%s].imageDigest" $label $workerId) -}}
{{- end -}}
{{- if hasKey $values.env "AGENT_WORKLOADS_OPENCODE_ARTIFACT_HANDOFF_MODE" -}}
{{- fail (printf "%s artifact handoff mode env is chart-owned" $label) -}}
{{- end -}}
{{- if not (has $identity.mode (list "hmac" "projected")) -}}
{{- fail (printf "%s identity mode must be hmac or projected" $label) -}}
{{- end -}}
{{- if eq $identity.mode "hmac" -}}
{{- if or (ne (len $values.secretKeys) 0) (ne (len $values.secretEnv) 1) (not (hasKey $values.secretEnv "MANDATE_WORKLOAD_IDENTITY_TOKEN")) -}}
{{- fail (printf "%s hmac identity must inject only MANDATE_WORKLOAD_IDENTITY_TOKEN" $label) -}}
{{- end -}}
{{- if or (hasKey $values.env "MANDATE_WORKLOAD_IDENTITY_TOKEN") (hasKey $values.env "MANDATE_WORKLOAD_IDENTITY_TOKEN_FILE") -}}
{{- fail (printf "%s hmac identity token env is chart-owned" $label) -}}
{{- end -}}
{{- else -}}
{{- if or (ne (len $values.secretKeys) 0) (ne (len $values.secretEnv) 0) (hasKey $values.env "MANDATE_WORKLOAD_IDENTITY_TOKEN") (hasKey $values.env "MANDATE_WORKLOAD_IDENTITY_TOKEN_FILE") -}}
{{- fail (printf "%s projected identity must not inject static credentials" $label) -}}
{{- end -}}
{{- $audience := required (printf "%s projected identity audience is required" $label) $identity.token.audience -}}
{{- if ne $audience (trim $audience) -}}
{{- fail (printf "%s projected identity audience must not have surrounding whitespace" $label) -}}
{{- end -}}
{{- $expirationSeconds := int $identity.token.expirationSeconds -}}
{{- if or (lt $expirationSeconds 600) (gt $expirationSeconds 3600) -}}
{{- fail (printf "%s projected identity expirationSeconds must be between 600 and 3600" $label) -}}
{{- end -}}
{{- $mountPath := required (printf "%s projected identity mountPath is required" $label) $identity.token.mountPath -}}
{{- if or (ne $mountPath (trim $mountPath)) (not (regexMatch "^/[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$" $mountPath)) (regexMatch "(^|/)\\.\\.?(/|$)" $mountPath) -}}
{{- fail (printf "%s projected identity mountPath must be a normalized absolute path" $label) -}}
{{- end -}}
{{- $fileName := required (printf "%s projected identity fileName is required" $label) $identity.token.fileName -}}
{{- if or (ne $fileName (trim $fileName)) (not (regexMatch "^[A-Za-z0-9._-]+$" $fileName)) (eq $fileName ".") (eq $fileName "..") -}}
{{- fail (printf "%s projected identity fileName must be a normalized basename" $label) -}}
{{- end -}}
{{- end -}}
{{- end }}
