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

{{/* The shared secrets Secret name (matches the main chart's <fullname>-secrets) */}}
{{- define "probo.secretsName" -}}
{{ include "mcp.fullname" . }}-secrets
{{- end }}
