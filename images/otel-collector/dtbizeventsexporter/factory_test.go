// Copyright 2026 JoustMania Authors
// SPDX-License-Identifier: Apache-2.0

package dtbizeventsexporter

import (
	"context"
	"testing"

	"go.opentelemetry.io/collector/component"
	"go.opentelemetry.io/collector/exporter/exportertest"
)

func TestNewFactory_ReturnsNonNil(t *testing.T) {
	f := NewFactory()
	if f == nil {
		t.Fatal("NewFactory() returned nil")
	}
	if f.Type().String() != "dtbizevents" {
		t.Errorf("factory type = %q, want %q", f.Type().String(), "dtbizevents")
	}
}

func TestDefaultConfig(t *testing.T) {
	f := NewFactory()
	cfg := f.CreateDefaultConfig()

	c, ok := cfg.(*Config)
	if !ok {
		t.Fatal("default config is not *Config")
	}
	if c.BatchIntervalMS != 1000 {
		t.Errorf("BatchIntervalMS = %d, want 1000", c.BatchIntervalMS)
	}
	if c.Endpoint != "" {
		t.Errorf("Endpoint = %q, want empty", c.Endpoint)
	}
	if c.APIToken != "" {
		t.Errorf("APIToken = %q, want empty", c.APIToken)
	}
}

func TestCreateMetricsExporter_Success(t *testing.T) {
	f := NewFactory()
	cfg := f.CreateDefaultConfig().(*Config)
	cfg.Endpoint = "http://localhost:9999"
	cfg.APIToken = "test-token"

	set := exportertest.NewNopSettings(component.MustNewType("dtbizevents"))

	exp, err := f.CreateMetrics(context.Background(), set, cfg)
	if err != nil {
		t.Fatalf("CreateMetrics failed: %v", err)
	}
	if exp == nil {
		t.Fatal("CreateMetrics returned nil exporter")
	}
}
