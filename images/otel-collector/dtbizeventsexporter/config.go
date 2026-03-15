// Copyright 2026 JoustMania Authors
// SPDX-License-Identifier: Apache-2.0

package dtbizeventsexporter

import "fmt"

// Config defines the configuration for the dtbizevents exporter.
type Config struct {
	// Endpoint is the Dynatrace bizevents ingest URL.
	Endpoint string `mapstructure:"endpoint"`

	// APIToken is the Dynatrace API token. Supports ${env:VAR} expansion.
	APIToken string `mapstructure:"api_token"`

	// BatchIntervalMS is the flush interval in milliseconds. Must be > 0.
	BatchIntervalMS int `mapstructure:"batch_interval_ms"`
}

// Validate checks that the configuration is valid.
func (c *Config) Validate() error {
	if c.BatchIntervalMS <= 0 {
		return fmt.Errorf("batch_interval_ms must be > 0, got %d", c.BatchIntervalMS)
	}
	return nil
}
