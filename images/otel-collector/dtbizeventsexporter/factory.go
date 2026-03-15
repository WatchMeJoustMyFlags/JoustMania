// Copyright 2026 JoustMania Authors
// SPDX-License-Identifier: Apache-2.0

package dtbizeventsexporter

import (
	"context"

	"go.opentelemetry.io/collector/component"
	"go.opentelemetry.io/collector/exporter"
	"go.opentelemetry.io/collector/exporter/exporterhelper"
)

const (
	typeStr = "dtbizevents"
)

// NewFactory creates a factory for the dtbizevents exporter.
func NewFactory() exporter.Factory {
	return exporter.NewFactory(
		component.MustNewType(typeStr),
		createDefaultConfig,
		exporter.WithMetrics(createMetricsExporter, component.StabilityLevelDevelopment),
	)
}

func createDefaultConfig() component.Config {
	return &Config{
		BatchIntervalMS: 1000,
	}
}

func createMetricsExporter(
	ctx context.Context,
	set exporter.Settings,
	cfg component.Config,
) (exporter.Metrics, error) {
	oCfg := cfg.(*Config)
	exp := newExporter(oCfg, set.Logger)

	return exporterhelper.NewMetrics(
		ctx,
		set,
		cfg,
		exp.ConsumeMetrics,
		exporterhelper.WithStart(exp.Start),
		exporterhelper.WithShutdown(exp.Shutdown),
	)
}
