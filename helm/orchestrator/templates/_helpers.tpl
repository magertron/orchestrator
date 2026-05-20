{{/*
MCP Orchestrator Helm chart helpers
*/}}

{{- define "mcp.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
mcp.fullname — release+chart name, deduplicated when one is a prefix of the other.

  Three cases:
    1. Release name is a prefix of chart name (release="mcp", chart="mcp-orchestrator")
       -> use chart name alone (no "mcp-mcp-orchestrator" doubling)
    2. Release name already contains chart name (release="mcp-orchestrator-prod")
       -> use release name alone
    3. Otherwise concatenate (release="prod", chart="mcp-orchestrator")
       -> "prod-mcp-orchestrator"
*/}}
{{- define "mcp.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if hasPrefix .Release.Name $name }}
{{- $name | trunc 63 | trimSuffix "-" }}
{{- else if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{- define "mcp.namespace" -}}
{{- default .Release.Namespace }}
{{- end }}

{{- define "mcp.labels" -}}
app.kubernetes.io/name: {{ include "mcp.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "mcp.selectorLabels" -}}
app: mcp-orchestrator
app.kubernetes.io/name: {{ include "mcp.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "mcp.postgresHost" -}}
{{ include "mcp.fullname" . }}-postgres.{{ include "mcp.namespace" . }}.svc.cluster.local
{{- end }}
