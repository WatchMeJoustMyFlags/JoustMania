module github.com/joustmania/connect-proxy

go 1.26.0

require (
	connectrpc.com/connect v1.21.1
	github.com/joustmania/connect-proxy/gen/controller_manager v0.0.0-00010101000000-000000000000
	github.com/joustmania/connect-proxy/gen/game_coordinator v0.0.0-00010101000000-000000000000
	github.com/joustmania/connect-proxy/gen/menu v0.0.0-00010101000000-000000000000
	github.com/rs/cors v1.11.1
	go.opentelemetry.io/contrib/bridges/otelslog v0.21.0
	go.opentelemetry.io/contrib/instrumentation/google.golang.org/grpc/otelgrpc v0.72.0
	go.opentelemetry.io/contrib/instrumentation/net/http/otelhttp v0.72.0
	go.opentelemetry.io/otel v1.47.0
	go.opentelemetry.io/otel/exporters/otlp/otlplog/otlploggrpc v0.23.0
	go.opentelemetry.io/otel/exporters/otlp/otlpmetric/otlpmetricgrpc v1.47.0
	go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracegrpc v1.47.0
	go.opentelemetry.io/otel/metric v1.47.0
	go.opentelemetry.io/otel/sdk v1.47.0
	go.opentelemetry.io/otel/sdk/log v1.47.0
	go.opentelemetry.io/otel/sdk/metric v1.47.0
	google.golang.org/grpc v1.85.0
)

require (
	github.com/cenkalti/backoff/v5 v5.0.3 // indirect
	github.com/cespare/xxhash/v2 v2.3.0 // indirect
	github.com/felixge/httpsnoop v1.1.0 // indirect
	github.com/go-logr/logr v1.4.4 // indirect
	github.com/go-logr/stdr v1.2.2 // indirect
	github.com/google/uuid v1.6.0 // indirect
	github.com/grpc-ecosystem/grpc-gateway/v2 v2.33.0 // indirect
	go.opentelemetry.io/auto/sdk v1.2.1 // indirect
	go.opentelemetry.io/otel/exporters/otlp/otlptrace v1.47.0 // indirect
	go.opentelemetry.io/otel/log v1.47.0 // indirect
	go.opentelemetry.io/otel/trace v1.47.0 // indirect
	go.opentelemetry.io/proto/otlp v1.11.1 // indirect
	golang.org/x/net v0.62.0 // indirect
	golang.org/x/sys v0.49.0 // indirect
	golang.org/x/text v0.43.0 // indirect
	google.golang.org/genproto/googleapis/api v0.0.0-20261026165753-2ffab32bce7e // indirect
	google.golang.org/genproto/googleapis/rpc v0.0.0-20261026165753-2ffab32bce7e // indirect
	google.golang.org/protobuf v1.36.12 // indirect
)

// Local generated packages - resolved during Docker build
replace (
	github.com/joustmania/connect-proxy/gen/audio => ./gen/audio
	github.com/joustmania/connect-proxy/gen/controller_manager => ./gen/controller_manager
	github.com/joustmania/connect-proxy/gen/controller_manager_mock => ./gen/controller_manager_mock
	github.com/joustmania/connect-proxy/gen/game_coordinator => ./gen/game_coordinator
	github.com/joustmania/connect-proxy/gen/menu => ./gen/menu
)
