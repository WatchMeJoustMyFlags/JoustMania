package openai

import (
	"net/url"
	"strings"

	"go.opentelemetry.io/otel/semconv/v1.41.0/genaiconv"
)

// providerHosts maps a vendor's OpenAI-compatible API host to the
// gen_ai.provider.name well-known value of semconv v1.41.0. Matching is on the
// exact host or a subdomain of it (see ProviderName).
var providerHosts = []struct {
	host     string
	provider genaiconv.ProviderNameAttr
}{
	{"api.openai.com", genaiconv.ProviderNameOpenAI},
	{"generativelanguage.googleapis.com", genaiconv.ProviderNameGCPGemini},
	{"aiplatform.googleapis.com", genaiconv.ProviderNameGCPVertexAI},
	{"api.mistral.ai", genaiconv.ProviderNameMistralAI},
	{"api.groq.com", genaiconv.ProviderNameGroq},
	{"api.deepseek.com", genaiconv.ProviderNameDeepseek},
	{"api.x.ai", genaiconv.ProviderNameXAI},
	{"api.perplexity.ai", genaiconv.ProviderNamePerplexity},
	{"api.cohere.ai", genaiconv.ProviderNameCohere},
	{"api.cohere.com", genaiconv.ProviderNameCohere},
	{"openai.azure.com", genaiconv.ProviderNameAzureAIOpenAI},
	{"services.ai.azure.com", genaiconv.ProviderNameAzureAIInference},
	{"models.inference.ai.azure.com", genaiconv.ProviderNameAzureAIInference},
	{"ml.cloud.ibm.com", genaiconv.ProviderNameIBMWatsonxAI},
}

// The derived provider also selects the request shape (see shapeFor). Azure
// hosts map to their semconv values for telemetry, but Azure OpenAI itself is
// not supported (api-key header auth, deployment-name model ids, undetected
// reasoning deployments); see the package doc.
//
// ProviderName derives gen_ai.provider.name from an OpenAI-compatible base URL:
// the semconv well-known value for a known vendor host or a subdomain of it
// (e.g. "myres.openai.azure.com"), Vertex AI regional and AWS Bedrock hosts by
// pattern, and
// "openai" for everything else (a self-hosted vLLM/Ollama/LiteLLM speaks the
// OpenAI protocol, and semconv says to use the protocol's provider when the
// actual one is unknown).
func ProviderName(baseURL string) string {
	u, err := url.Parse(strings.TrimSpace(baseURL))
	if err != nil {
		return string(genaiconv.ProviderNameOpenAI)
	}
	host := strings.ToLower(strings.TrimSuffix(u.Hostname(), "."))
	if host == "" {
		return string(genaiconv.ProviderNameOpenAI)
	}
	for _, p := range providerHosts {
		if host == p.host || strings.HasSuffix(host, "."+p.host) {
			return string(p.provider)
		}
	}
	// Vertex AI regional endpoints: "<region>-aiplatform.googleapis.com".
	if strings.HasSuffix(host, "-aiplatform.googleapis.com") {
		return string(genaiconv.ProviderNameGCPVertexAI)
	}
	// Bedrock's OpenAI-compatible endpoints: "bedrock-runtime.<region>.amazonaws.com"
	// and "bedrock-mantle.<region>.api.aws".
	if strings.HasPrefix(host, "bedrock-") && (strings.HasSuffix(host, ".amazonaws.com") || strings.HasSuffix(host, ".api.aws")) {
		return string(genaiconv.ProviderNameAWSBedrock)
	}
	return string(genaiconv.ProviderNameOpenAI)
}
