{{- define "opsrabbit.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 50 | trimSuffix "-" }}
{{- end }}

{{- define "opsrabbit.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 50 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name (include "opsrabbit.name" .) | trunc 50 | trimSuffix "-" }}
{{- end }}
{{- end }}

{{- define "opsrabbit.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 50 | trimSuffix "-" }}
{{- end }}

{{- define "opsrabbit.labels" -}}
helm.sh/chart: {{ include "opsrabbit.chart" . }}
{{ include "opsrabbit.selectorLabels" . }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "opsrabbit.selectorLabels" -}}
app.kubernetes.io/name: {{ include "opsrabbit.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "opsrabbit.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "opsrabbit.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{- define "opsrabbit.secretName" -}}
{{- default (include "opsrabbit.fullname" .) .Values.secrets.existingSecret }}
{{- end }}

{{- define "opsrabbit.imagePullPolicy" -}}
{{- default .Values.image.pullPolicy .value }}
{{- end }}
