//! gRPC server tracing layer for tonic.
//!
//! Creates SERVER spans with RPC semantic convention attributes when the
//! `grpc_rpc_spans` feature flag is enabled. Always extracts W3C trace
//! context from incoming gRPC metadata for context propagation.

use std::future::Future;
use std::pin::Pin;
use std::task::{Context, Poll};

use http::{Request, Response};
use opentelemetry::propagation::Extractor;
use tonic::body::BoxBody;
use tower::{Layer, Service};
use tracing::{info_span, Instrument};
use tracing_opentelemetry::OpenTelemetrySpanExt;

use crate::feature_flags;

/// Extractor for W3C trace context from HTTP headers.
struct HeaderExtractor<'a>(&'a http::HeaderMap);

impl Extractor for HeaderExtractor<'_> {
    fn get(&self, key: &str) -> Option<&str> {
        self.0.get(key).and_then(|v| v.to_str().ok())
    }

    fn keys(&self) -> Vec<&str> {
        self.0.keys().map(|k| k.as_str()).collect()
    }
}

/// Parse gRPC method path `/pkg.ServiceName/MethodName` into (service, method).
fn parse_grpc_method(path: &str) -> (&str, &str) {
    let path = path.strip_prefix('/').unwrap_or(path);
    match path.split_once('/') {
        Some((service_part, method)) => {
            // Extract just the service name after the last dot
            let service = service_part.rsplit('.').next().unwrap_or(service_part);
            (service, method)
        }
        None => ("unknown", path),
    }
}

/// Tower layer that adds gRPC server tracing.
#[derive(Clone)]
pub struct GrpcTracingLayer;

impl<S> Layer<S> for GrpcTracingLayer {
    type Service = GrpcTracingService<S>;

    fn layer(&self, inner: S) -> Self::Service {
        GrpcTracingService { inner }
    }
}

/// Tower service that wraps each request with optional tracing.
#[derive(Clone)]
pub struct GrpcTracingService<S> {
    inner: S,
}

impl<S, ReqBody> Service<Request<ReqBody>> for GrpcTracingService<S>
where
    S: Service<Request<ReqBody>, Response = Response<BoxBody>> + Clone + Send + 'static,
    S::Future: Send + 'static,
    ReqBody: Send + 'static,
{
    type Response = S::Response;
    type Error = S::Error;
    type Future = Pin<Box<dyn Future<Output = Result<Self::Response, Self::Error>> + Send>>;

    fn poll_ready(&mut self, cx: &mut Context<'_>) -> Poll<Result<(), Self::Error>> {
        self.inner.poll_ready(cx)
    }

    fn call(&mut self, req: Request<ReqBody>) -> Self::Future {
        // Clone and swap so self retains the ready instance
        let mut svc = self.inner.clone();
        std::mem::swap(&mut self.inner, &mut svc);

        Box::pin(async move {
            // Extract parent trace context from incoming headers
            let parent_cx = opentelemetry::global::get_text_map_propagator(|prop| {
                prop.extract(&HeaderExtractor(req.headers()))
            });

            let path = req.uri().path().to_string();

            // Skip tracing for health check probes — they run every 2s
            // and would flood traces with noise
            if path.contains("grpc.health") {
                let _guard = parent_cx.attach();
                return svc.call(req).await;
            }

            let (service, method) = parse_grpc_method(&path);

            // Check feature flag to decide whether to create spans
            let rpc_spans = feature_flags::grpc_rpc_spans_enabled().await;

            if rpc_spans {
                let span = info_span!(
                    "grpc.server",
                    otel.kind = "Server",
                    rpc.system = "grpc",
                    rpc.service = %service,
                    rpc.method = %method,
                    rpc.grpc.status_code = tracing::field::Empty,
                );
                span.set_parent(parent_cx);

                let response = svc.call(req).instrument(span.clone()).await;

                // Record gRPC status code from response headers
                if let Ok(ref resp) = response {
                    if let Some(status) = resp.headers().get("grpc-status") {
                        span.record(
                            "rpc.grpc.status_code",
                            status.to_str().unwrap_or(""),
                        );
                    } else {
                        span.record("rpc.grpc.status_code", "0"); // OK
                    }
                }

                response
            } else {
                // No spans, but still propagate context
                let _guard = parent_cx.attach();
                svc.call(req).await
            }
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_parse_grpc_method_full_path() {
        let (svc, method) = parse_grpc_method("/joustmania.psmove_hid.PairingService/PairController");
        assert_eq!(svc, "PairingService");
        assert_eq!(method, "PairController");
    }

    #[test]
    fn test_parse_grpc_method_no_package() {
        let (svc, method) = parse_grpc_method("/PairingService/GetUsbControllers");
        assert_eq!(svc, "PairingService");
        assert_eq!(method, "GetUsbControllers");
    }

    #[test]
    fn test_parse_grpc_method_no_slash() {
        let (svc, method) = parse_grpc_method("unknown");
        assert_eq!(svc, "unknown");
        assert_eq!(method, "unknown");
    }
}
