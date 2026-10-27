/**
 * VictoriaMetrics instant queries through envoy's `/victoria-metrics/` (UC-035).
 * Shared by the Agent tab's intervention counts and the controller cards'
 * adapter labels (BR-009).
 */

const BASE_URL = import.meta.env.DEV ? "" : globalThis.location.origin;

export interface VmQueryResponse {
  status: string;
  data?: {
    resultType: string;
    result: { metric: Record<string, string>; value: [number, string] }[];
  };
}

/**
 * One instant query; null when VictoriaMetrics is unreachable or answers non-2xx.
 * `nocache` skips VictoriaMetrics' 30 s latency offset (2.5 to 4 s late instead).
 */
export async function vmQuery(
  query: string,
  signal?: AbortSignal,
  options: { nocache?: boolean } = {},
): Promise<VmQueryResponse | null> {
  try {
    const params = new URLSearchParams({ query });
    if (options.nocache) params.set("nocache", "1");
    const url = `${BASE_URL}/victoria-metrics/api/v1/query?${params.toString()}`;
    const response = await fetch(url, { headers: { Accept: "application/json" }, signal });
    if (!response.ok) return null;
    return (await response.json()) as VmQueryResponse;
  } catch {
    return null;
  }
}
