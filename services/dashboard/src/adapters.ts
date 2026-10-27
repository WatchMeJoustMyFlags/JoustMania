/**
 * The HCI adapter of each controller (UC-035 BR-009, #2133): the controller
 * manager's `controller_adapter_info{serial,adapter}` series read from
 * VictoriaMetrics. `timestamp()` makes the value the sample's time, so when a
 * moved controller still has both series in the lookback window (OTLP writes no
 * staleness marker) the newest one wins.
 */
import { vmQuery } from "./vm.js";
import type { VmQueryResponse } from "./vm.js";

export const ADAPTER_QUERY = 'timestamp(controller_adapter_info)';

/** serial -> adapter name (`hci0`). A serial without a series is absent. */
export type AdapterMap = ReadonlyMap<string, string>;

export function parseAdapters(body: VmQueryResponse | null | undefined): Map<string, string> {
  const adapters = new Map<string, string>();
  const newest = new Map<string, number>();
  const result = body?.status === "success" ? body.data?.result : undefined;
  if (!Array.isArray(result)) return adapters;
  for (const series of result) {
    const serial = series?.metric?.["serial"];
    const adapter = series?.metric?.["adapter"];
    if (!serial || !adapter) continue;
    const ts = Number(series.value?.[1]);
    if (!Number.isFinite(ts)) continue;
    if (ts >= (newest.get(serial) ?? -Infinity)) {
      newest.set(serial, ts);
      adapters.set(serial, adapter);
    }
  }
  return adapters;
}

/** Controllers per adapter, sorted by adapter name; unknown serials are not counted. */
export function adapterCounts(adapters: AdapterMap, serials?: Iterable<string>): Array<[string, number]> {
  const counts = new Map<string, number>();
  const wanted = serials ? new Set(serials) : null;
  for (const [serial, adapter] of adapters) {
    if (wanted && !wanted.has(serial)) continue;
    counts.set(adapter, (counts.get(adapter) ?? 0) + 1);
  }
  return Array.from(counts.entries()).sort(([a], [b]) => a.localeCompare(b, undefined, { numeric: true }));
}

/** "hci0: 5 · hci1: 4"; empty when no adapter is known. */
export function adapterCountsText(counts: Array<[string, number]>): string {
  return counts.map(([adapter, n]) => `${adapter}: ${n}`).join(" · ");
}

/** The badge shows only when at least two adapters are in use. */
export function showAdapterBadge(counts: Array<[string, number]>): boolean {
  return counts.length > 1;
}

/** One poll. null = VictoriaMetrics unreachable (keep the last map, mark stale). Never throws. */
export async function fetchAdapters(signal?: AbortSignal): Promise<Map<string, string> | null> {
  const body = await vmQuery(ADAPTER_QUERY, signal, { nocache: true });
  return body ? parseAdapters(body) : null;
}
