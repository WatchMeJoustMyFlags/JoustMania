package main

import (
	"bytes"
	"fmt"
	"strings"
	"text/template"
)

// collectorConfigTemplate is the effective config pushed to the collector via OpAMP.
// It only overrides processor parameters controlled by flags, preserving the
// base pipeline structure so config.dynatrace.yaml overlay still works.
//
// The template produces a partial config that the opamp extension merges with
// the collector's existing config. Only the sections that change are included.
const collectorConfigTemplate = `processors:
  # Probabilistic (head-based) sampling — controlled by collector_tail_sampling_rate flag.
  # Applied to the traces pipeline. Rate 1.0 = pass all traces through.
  probabilistic_sampler:
    sampling_percentage: {{ .SamplingPercentage }}

  # Log filter — minimum severity controlled by collector_log_filter_severity flag.
  # Applied to the logs pipeline. Drops logs below the configured severity.
  filter/log_severity:
    logs:
      log_record:
        - severity_number < {{ .SeverityNumber }}
`

// severityToNumber maps OTEL log severity names to their numeric values.
// See: https://opentelemetry.io/docs/specs/otel/logs/data-model/#severity-fields
var severityToNumber = map[string]int{
	"DEBUG": 5,
	"INFO":  9,
	"WARN":  13,
	"ERROR": 17,
}

// parsedTemplate is parsed once at init time to avoid re-parsing on every call.
var parsedTemplate = template.Must(template.New("collector").Parse(collectorConfigTemplate))

type templateData struct {
	SamplingPercentage float64
	SeverityNumber     int
}

// RenderCollectorConfig produces YAML config bytes from current flag values.
func RenderCollectorConfig(flags FlagValues) ([]byte, error) {
	sevNum, ok := severityToNumber[strings.ToUpper(flags.LogFilterSeverity)]
	if !ok {
		sevNum = severityToNumber["INFO"]
	}

	// Clamp rate to [0, 1.0] — values outside this range from flagd would
	// produce invalid sampling percentages (negative or >100).
	rate := flags.TailSamplingRate
	if rate < 0 {
		rate = 0
	}
	if rate > 1.0 {
		rate = 1.0
	}

	data := templateData{
		SamplingPercentage: rate * 100,
		SeverityNumber:     sevNum,
	}

	var buf bytes.Buffer
	if err := parsedTemplate.Execute(&buf, data); err != nil {
		return nil, fmt.Errorf("failed to execute collector config template: %w", err)
	}

	return buf.Bytes(), nil
}
