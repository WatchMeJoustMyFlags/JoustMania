module github.com/joustmania/connect-proxy

go 1.22

require (
	connectrpc.com/connect v1.17.0
	github.com/rs/cors v1.11.0
	go.opentelemetry.io/contrib/bridges/otelslog v0.6.0
	go.opentelemetry.io/contrib/instrumentation/net/http/otelhttp v0.56.0
	go.opentelemetry.io/otel v1.31.0
	go.opentelemetry.io/otel/exporters/otlp/otlplog/otlploggrpc v0.7.0
	go.opentelemetry.io/otel/exporters/otlp/otlpmetric/otlpmetricgrpc v1.31.0
	go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracegrpc v1.31.0
	go.opentelemetry.io/otel/metric v1.31.0
	go.opentelemetry.io/otel/sdk v1.31.0
	go.opentelemetry.io/otel/sdk/log v0.7.0
	go.opentelemetry.io/otel/sdk/metric v1.31.0
	go.opentelemetry.io/otel/trace v1.31.0
	google.golang.org/grpc v1.67.1
)

require (
	github.com/cenkalti/backoff/v4 v4.3.0 // indirect
	github.com/felixge/httpsnoop v1.0.4 // indirect
	github.com/go-logr/logr v1.4.2 // indirect
	github.com/go-logr/stdr v1.2.2 // indirect
	github.com/google/uuid v1.6.0 // indirect
	github.com/grpc-ecosystem/grpc-gateway/v2 v2.22.0 // indirect
	go.opentelemetry.io/otel/exporters/otlp/otlptrace v1.31.0 // indirect
	go.opentelemetry.io/otel/log v0.7.0 // indirect
	go.opentelemetry.io/proto/otlp v1.3.1 // indirect
	golang.org/x/net v0.30.0 // indirect
	golang.org/x/sys v0.26.0 // indirect
	golang.org/x/text v0.19.0 // indirect
	google.golang.org/genproto/googleapis/api v0.0.0-20241007155032-5fefd90f89a9 // indirect
	google.golang.org/genproto/googleapis/rpc v0.0.0-20241007155032-5fefd90f89a9 // indirect
	google.golang.org/protobuf v1.35.1 // indirect
)

// Local generated packages - resolved during Docker build
replace (
	github.com/joustmania/connect-proxy/gen/audio => ./gen/audio
	github.com/joustmania/connect-proxy/gen/controller_manager => ./gen/controller_manager
	github.com/joustmania/connect-proxy/gen/controller_manager_mock => ./gen/controller_manager_mock
	github.com/joustmania/connect-proxy/gen/game_coordinator => ./gen/game_coordinator
	github.com/joustmania/connect-proxy/gen/menu => ./gen/menu
	github.com/joustmania/connect-proxy/gen/settings => ./gen/settings
)
