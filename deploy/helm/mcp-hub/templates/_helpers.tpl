{{/*
Expand the name of the chart.
*/}}
{{- define "mcp-hub.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "mcp-hub.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- printf "%s" $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}

{{/*
Common labels.
*/}}
{{- define "mcp-hub.labels" -}}
helm.sh/chart: {{ include "mcp-hub.name" . }}-{{ .Chart.Version | replace "+" "_" }}
{{ include "mcp-hub.selectorLabels" . }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels.
*/}}
{{- define "mcp-hub.selectorLabels" -}}
app.kubernetes.io/name: {{ include "mcp-hub.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}
