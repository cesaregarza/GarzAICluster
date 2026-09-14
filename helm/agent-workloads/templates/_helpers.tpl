{{/*
Common chart naming and worker rendering helpers.  The workers map is the
only per-worker source of truth; helpers receive a worker explicitly so no
canonical worker id is encoded in chart logic.
*/}}
{{- define "agent-workloads.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end }}

{{- define "agent-workloads.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end }}

{{- define "agent-workloads.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end }}

{{/* Return the image reference for an image values subtree. */}}
{{- define "agent-workloads.imageRef" -}}
{{- if .digest -}}
{{- printf "%s@%s" .repository .digest -}}
{{- else -}}
{{- printf "%s:%s" .repository .tag -}}
{{- end -}}
{{- end }}

{{/* Checksum the complete release pin map, including every worker. */}}
{{- define "agent-workloads.releasePinsChecksum" -}}
{{- if .Values.mandateReleasePins -}}
{{- toJson .Values.mandateReleasePins | sha256sum -}}
{{- else -}}
absent
{{- end -}}
{{- end }}

{{- define "agent-workloads.workloadIdentityTokenSecretChecksum" -}}
{{- default "absent" .Values.rolloutChecksums.workloadIdentityTokenSecret -}}
{{- end }}

{{- define "agent-workloads.imagePullSecrets" -}}
{{- if .Values.global.imagePullSecrets }}
imagePullSecrets:
{{- range .Values.global.imagePullSecrets }}
  - name: {{ . }}
{{- end }}
{{- end }}
{{- end }}

{{- define "agent-workloads.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- include "agent-workloads.fullname" . -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end }}

{{- define "agent-workloads.workerIdentityName" -}}
{{- regexReplaceAll "[^a-z0-9]+" (lower .workerId) "-" | trimAll "-" -}}
{{- end }}

{{/* Build a release-scoped ServiceAccount name from the immutable tuple. */}}
{{- define "agent-workloads.releaseScopedServiceAccountName" -}}
{{- $release := required "release-scoped ServiceAccount requires an immutable release tuple" .release -}}
{{- $workerId := required "release-scoped ServiceAccount requires workerId" .workerId -}}
{{- $prefix := required "release-scoped ServiceAccount requires serviceAccountNamePrefix" .serviceAccountNamePrefix -}}
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
{{- fail "identity.workerId must normalize to a DNS label" -}}
{{- end -}}
{{- $bundlePayload := printf "{\"code_digest\":\"%s\",\"image_digest\":\"%s\",\"manifest_digest\":\"%s\",\"schema_version\":\"workload_identity_bundle.v1\"}" $codeDigest $imageDigest $manifestDigest -}}
{{- $digestSuffix := trunc 20 (sha256sum $bundlePayload) -}}
{{- $name := printf "%s-%s-%s" $prefix $workerName $digestSuffix -}}
{{- if gt (len $name) 63 -}}
{{- fail "release-scoped ServiceAccount name exceeds 63 characters" -}}
{{- end -}}
{{- if not (regexMatch "^[a-z0-9]([-a-z0-9]*[a-z0-9])?$" $name) -}}
{{- fail "release-scoped ServiceAccount name must be a DNS label" -}}
{{- end -}}
{{- $name -}}
{{- end }}

{{- define "agent-workloads.workerCurrentServiceAccountName" -}}
{{- $worker := .worker -}}
{{- $workerId := required "identity.workerId is required" $worker.identity.workerId -}}
{{- $pins := required "projected identity requires mandateReleasePins" .root.Values.mandateReleasePins -}}
{{- $release := required (printf "projected identity requires mandateReleasePins[%s]" $workerId) (index $pins $workerId) -}}
{{- include "agent-workloads.releaseScopedServiceAccountName" (dict "release" $release "workerId" $workerId "serviceAccountNamePrefix" $worker.identity.serviceAccountNamePrefix) -}}
{{- end }}

{{- define "agent-workloads.workerPreviousServiceAccountName" -}}
{{- include "agent-workloads.releaseScopedServiceAccountName" (dict "release" .worker.identity.previousRelease "workerId" .worker.identity.workerId "serviceAccountNamePrefix" .worker.identity.serviceAccountNamePrefix) -}}
{{- end }}

{{- define "agent-workloads.workerTokenPath" -}}
{{- printf "%s/%s" (trimSuffix "/" .worker.identity.token.mountPath) .worker.identity.token.fileName -}}
{{- end }}

{{- define "agent-workloads.workerMetadataName" -}}
{{- if .worker.metadataName -}}
{{- .worker.metadataName | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" (include "agent-workloads.fullname" .root) (include "agent-workloads.workerIdentityName" (dict "workerId" .workerId)) | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end }}

{{- define "agent-workloads.workerSelectorLabels" -}}
{{- $workerName := include "agent-workloads.workerMetadataName" . -}}
{{- toYaml (default (dict "app.kubernetes.io/name" $workerName "app.kubernetes.io/instance" .root.Release.Name) .worker.selectorLabels) -}}
{{- end }}

{{/* Standard labels composed with explicit per-worker labels. */}}
{{- define "agent-workloads.workerLabels" -}}
helm.sh/chart: {{ include "agent-workloads.chart" .root }}
{{ include "agent-workloads.workerSelectorLabels" . }}
app.kubernetes.io/managed-by: {{ .root.Release.Service }}
{{ with .worker.labels }}
{{- toYaml . }}
{{- end }}
{{- end }}

{{- define "agent-workloads.envFromWorker" -}}
{{- $root := .root -}}
{{- $worker := .worker -}}
{{- $secretName := default $root.Values.global.runtimeSecretName $worker.secretEnvSecretName -}}
{{- range $key := $worker.secretKeys }}
- name: {{ $key }}
  valueFrom:
    secretKeyRef:
      name: {{ $root.Values.global.runtimeSecretName }}
      key: {{ $key }}
{{- end }}
{{- range $envName, $secretKey := $worker.secretEnv }}
- name: {{ $envName }}
  valueFrom:
    secretKeyRef:
      name: {{ $secretName }}
      key: {{ $secretKey }}
{{- end }}
{{- range $key, $value := $worker.env }}
- name: {{ $key }}
  value: {{ $value | quote }}
{{- end }}
{{- end }}

{{/* Fail closed before rendering a worker with a mismatched release or
credential identity. */}}
{{- define "agent-workloads.validateWorker" -}}
{{- $root := .root -}}
{{- $workerId := .workerId -}}
{{- $worker := .worker -}}
{{- $identity := required (printf "workers[%s].identity is required" $workerId) $worker.identity -}}
{{- if ne (required (printf "workers[%s].identity.workerId is required" $workerId) $identity.workerId) $workerId -}}
{{- fail (printf "workers[%s].identity.workerId must match its map key" $workerId) -}}
{{- end -}}
{{- if ne (required (printf "workers[%s].identity.mode is required" $workerId) $identity.mode) "projected" -}}
{{- fail (printf "workers[%s].identity.mode must be projected" $workerId) -}}
{{- end -}}
{{- if ne (required (printf "workers[%s].env.AGENT_WORKLOADS_WORKER_ID is required" $workerId) $worker.env.AGENT_WORKLOADS_WORKER_ID) $workerId -}}
{{- fail (printf "workers[%s] identity must match AGENT_WORKLOADS_WORKER_ID" $workerId) -}}
{{- end -}}
{{- if and $worker.handoffMode (or (ne (len $worker.secretKeys) 0) (ne (len $worker.secretEnv) 0)) -}}
{{- fail (printf "workers[%s] governed projected identity must not inject static credentials" $workerId) -}}
{{- end -}}
{{- $pins := required "mandateReleasePins is required" $root.Values.mandateReleasePins -}}
{{- $release := required (printf "mandateReleasePins[%s] is required" $workerId) (index $pins $workerId) -}}
{{- $imageDigest := required (printf "workers[%s].image.digest is required" $workerId) $worker.image.digest -}}
{{- if not (regexMatch "^sha256:[a-f0-9]{64}$" $imageDigest) -}}
{{- fail (printf "workers[%s].image.digest must be lowercase sha256:<64 hex>" $workerId) -}}
{{- end -}}
{{- if ne $imageDigest (required (printf "mandateReleasePins[%s].imageDigest is required" $workerId) $release.imageDigest) -}}
{{- fail (printf "workers[%s].image.digest must equal mandateReleasePins[%s].imageDigest" $workerId $workerId) -}}
{{- end -}}
{{- $audience := required (printf "workers[%s].identity.token.audience is required" $workerId) $identity.token.audience -}}
{{- if ne $audience (trim $audience) -}}
{{- fail (printf "workers[%s].identity.token.audience must not have surrounding whitespace" $workerId) -}}
{{- end -}}
{{- $expirationSeconds := int $identity.token.expirationSeconds -}}
{{- if or (lt $expirationSeconds 600) (gt $expirationSeconds 3600) -}}
{{- fail (printf "workers[%s].identity.token.expirationSeconds must be between 600 and 3600" $workerId) -}}
{{- end -}}
{{- $mountPath := required (printf "workers[%s].identity.token.mountPath is required" $workerId) $identity.token.mountPath -}}
{{- if or (ne $mountPath (trim $mountPath)) (not (regexMatch "^/[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$" $mountPath)) (regexMatch "(^|/)\\.\\.?(/|$)" $mountPath) -}}
{{- fail (printf "workers[%s].identity.token.mountPath must be a normalized absolute path" $workerId) -}}
{{- end -}}
{{- $fileName := required (printf "workers[%s].identity.token.fileName is required" $workerId) $identity.token.fileName -}}
{{- if or (ne $fileName (trim $fileName)) (not (regexMatch "^[A-Za-z0-9._-]+$" $fileName)) (eq $fileName ".") (eq $fileName "..") -}}
{{- fail (printf "workers[%s].identity.token.fileName must be a normalized basename" $workerId) -}}
{{- end -}}
{{- $rollback := $identity.hmacRollbackRelease -}}
{{- $rollbackKey := $identity.hmacRollbackTokenKey -}}
{{- if or (and $rollback (not $rollbackKey)) (and (not $rollback) $rollbackKey) -}}
{{- fail (printf "workers[%s].identity.hmacRollbackRelease and hmacRollbackTokenKey must appear together" $workerId) -}}
{{- end -}}
{{- range $envName := list "MANDATE_WORKLOAD_IDENTITY_TOKEN" "MANDATE_WORKLOAD_IDENTITY_TOKEN_FILE" -}}
{{- if hasKey $worker.env $envName -}}
{{- fail (printf "workers[%s].env.%s is chart-owned" $workerId $envName) -}}
{{- end -}}
{{- if hasKey $worker.secretEnv $envName -}}
{{- fail (printf "workers[%s].secretEnv.%s is chart-owned" $workerId $envName) -}}
{{- end -}}
{{- if has $envName $worker.secretKeys -}}
{{- fail (printf "workers[%s].secretKeys must not contain %s" $workerId $envName) -}}
{{- end -}}
{{- end -}}
{{- if hasKey $worker.env "AGENT_WORKLOADS_OPENCODE_ARTIFACT_HANDOFF_MODE" -}}
{{- fail (printf "workers[%s].env.AGENT_WORKLOADS_OPENCODE_ARTIFACT_HANDOFF_MODE is chart-owned" $workerId) -}}
{{- end -}}
{{- if and $worker.handoffMode (not $worker.networkPolicy.enabled) -}}
{{- fail (printf "workers[%s] requires an enabled networkPolicy for governed handoff" $workerId) -}}
{{- end -}}
{{- range $mount := $worker.volumeMounts -}}
{{- $extraPath := clean (required (printf "workers[%s].volumeMounts requires mountPath" $workerId) $mount.mountPath) -}}
{{- if or (eq $extraPath "/") (eq $extraPath $mountPath) (hasPrefix (printf "%s/" $mountPath) $extraPath) (hasPrefix (printf "%s/" $extraPath) $mountPath) -}}
{{- fail (printf "workers[%s].volumeMounts must not overlap the projected identity token path" $workerId) -}}
{{- end -}}
{{- end -}}
{{- range $volume := $worker.volumes -}}
{{- with $volume.secret -}}
{{- if eq .secretName "agent-workloads-workload-identity-tokens" -}}
{{- fail (printf "workers[%s] must not mount the legacy identity Secret" $workerId) -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- with $identity.previousRelease -}}
{{- range $label, $digest := dict "codeDigest" .codeDigest "manifestDigest" .manifestDigest "imageDigest" .imageDigest -}}
{{- if not (regexMatch "^sha256:[a-f0-9]{64}$" $digest) -}}
{{- fail (printf "workers[%s].identity.previousRelease.%s must be lowercase sha256:<64 hex>" $workerId $label) -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- end }}
