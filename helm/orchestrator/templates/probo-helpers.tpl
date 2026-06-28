{{/*
Probo GRC subsystem helpers — match the mcp.* convention in _helpers.tpl.
Reuses mcp.fullname / mcp.namespace / mcp.labels; adds probo-scoped names.
All resources are gated behind .Values.probo.enabled by their templates.
*/}}

{{/* probod resource base name: e.g. mcp-orchestrator-probod */}}
{{- define "probo.probodName" -}}
{{ include "mcp.fullname" . }}-probod
{{- end }}

{{/* Probo's own postgres name: e.g. mcp-orchestrator-probo-postgres */}}
{{- define "probo.postgresName" -}}
{{ include "mcp.fullname" . }}-probo-postgres
{{- end }}

{{/* Probo's postgres in-cluster host */}}
{{- define "probo.postgresHost" -}}
{{ include "probo.postgresName" . }}.{{ include "mcp.namespace" . }}.svc.cluster.local
{{- end }}

{{/* Chrome name */}}
{{- define "probo.chromeName" -}}
{{ include "mcp.fullname" . }}-probo-chrome
{{- end }}

{{/* MinIO (S3-compatible object store) name */}}
{{- define "probo.minioName" -}}
{{ include "mcp.fullname" . }}-probo-minio
{{- end }}

{{/* MinIO in-cluster S3 endpoint */}}
{{- define "probo.minioEndpoint" -}}
http://{{ include "probo.minioName" . }}.{{ include "mcp.namespace" . }}.svc.cluster.local:9000
{{- end }}

{{/* Common Probo labels — extend mcp.labels with a subsystem tag */}}
{{- define "probo.labels" -}}
{{ include "mcp.labels" . }}
magertron.io/subsystem: grc
{{- end }}

{{/* probod selector labels */}}
{{- define "probo.probodSelectorLabels" -}}
app: probod
app.kubernetes.io/name: {{ include "mcp.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: probod
{{- end }}

{{/* probo-postgres selector labels */}}
{{- define "probo.postgresSelectorLabels" -}}
app: probo-postgres
app.kubernetes.io/name: {{ include "mcp.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: probo-postgres
{{- end }}

{{/* chrome selector labels */}}
{{- define "probo.chromeSelectorLabels" -}}
app: probo-chrome
app.kubernetes.io/name: {{ include "mcp.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: probo-chrome
{{- end }}

{{/* minio selector labels */}}
{{- define "probo.minioSelectorLabels" -}}
app: probo-minio
app.kubernetes.io/name: {{ include "mcp.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: probo-minio
{{- end }}

{{/* The shared secrets Secret name (matches the main chart's <fullname>-secrets) */}}
{{- define "probo.secretsName" -}}
{{ include "mcp.fullname" . }}-secrets
{{- end }}
