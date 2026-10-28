/**
 * The stage settings strip (UC-035 BR-010, #2289): the live game settings the
 * operator pad changes. Gauges first (VictoriaMetrics: shield mode, hold values,
 * sensitivity of the live primary game), then the menu's GetMenuStatus through
 * connect-proxy for what no gauge carries (team count) and for the lobby, when no
 * game gauge exists; its `operator_settings` says operator-set or automatic.
 */
import { vmQuery } from "./vm.js";
import type { VmQueryResponse } from "./vm.js";
import type { GetMenuStatusResponseLike } from "./gen/menu_pb.js";

/** `last_over_time[1m]` drops the series of an ended game within a minute (OTLP writes no staleness marker). */
export const STAGE_QUERY =
  'last_over_time({__name__=~"game_shield_activation_mode|game_shield_hold_setting|game_sensitivity",game_kind="primary"}[1m])';

const SENSITIVITY_NAMES = ["ultra slow", "slow", "medium", "fast", "ultra fast"];
const HOLD_SHOWN: Array<[string, string]> = [
  ["shield.energy_max_seconds", "max"],
  ["shield.energy_budget_seconds", "budget"],
];

export interface GaugeSettings {
  sensitivity: number | null;
  shieldMode: string | null;
  hold: Array<[string, number]>;
}

export interface MenuSettings {
  sensitivity: number | null;
  numTeams: number | null;
  shieldMode: string | null;
  /** setting -> "automatic" | variant name; a missing key = unknown. */
  operator: Record<string, string>;
}

export interface StageSettings {
  gauges: GaugeSettings | null;
  menu: MenuSettings | null;
}

export function parseGauges(body: VmQueryResponse | null | undefined): GaugeSettings {
  const out: GaugeSettings = { sensitivity: null, shieldMode: null, hold: [] };
  const result = body?.status === "success" ? body.data?.result : undefined;
  if (!Array.isArray(result)) return out;
  for (const series of result) {
    const labels = series?.metric ?? {};
    const value = Number(series?.value?.[1]);
    if (!Number.isFinite(value)) continue;
    // last_over_time keeps the metric name; plain queries too.
    const name = labels["__name__"];
    if (name === "game_sensitivity") out.sensitivity = value;
    else if (name === "game_shield_activation_mode" && labels["mode"]) out.shieldMode = labels["mode"];
    else if (name === "game_shield_hold_setting" && labels["setting"]) {
      const shown = HOLD_SHOWN.find(([key]) => key === labels["setting"]);
      if (shown) out.hold.push([shown[1], value]);
    }
  }
  out.hold.sort((a, b) => HOLD_SHOWN.findIndex(([, l]) => l === a[0]) - HOLD_SHOWN.findIndex(([, l]) => l === b[0]));
  return out;
}

export function parseMenu(status: GetMenuStatusResponseLike | null | undefined): MenuSettings | null {
  if (!status || typeof status !== "object") return null;
  const num = (v: unknown) => (typeof v === "number" && Number.isFinite(v) ? v : null);
  return {
    // Connect JSON omits zero: an absent sensitivity is 0 (ultra slow), but an absent
    // team count is unknown (the menu never reports fewer than 2).
    sensitivity: num(status.sensitivity) ?? 0,
    numTeams: num(status.numTeams) && (status.numTeams as number) > 0 ? (status.numTeams as number) : null,
    shieldMode: status.shieldActivation ? status.shieldActivation : null,
    operator: status.operatorSettings ?? {},
  };
}

/** "operator" / "automatic" / "" (unknown) for one setting. */
export function originOf(menu: MenuSettings | null, key: string): "operator" | "automatic" | "" {
  const v = menu?.operator[key];
  if (!v) return "";
  return v === "automatic" ? "automatic" : "operator";
}

export interface StageRow {
  label: string;
  value: string;
  origin: "operator" | "automatic" | "";
}

/** The four rows; a value nobody reported is "—". */
export function stageRows(s: StageSettings): StageRow[] {
  const g = s.gauges;
  const m = s.menu;
  const sens = g?.sensitivity ?? m?.sensitivity ?? null;
  const mode = g?.shieldMode ?? m?.shieldMode ?? null;
  const sensName = sens === null ? "—" : (SENSITIVITY_NAMES[sens] ?? String(sens));
  const hold = mode === "hold" && g && g.hold.length ? ` (${g.hold.map(([l, v]) => `${l} ${v}s`).join(", ")})` : "";
  return [
    { label: "Shield", value: mode ? `${mode}${hold}` : "—", origin: originOf(m, "shield.activation") },
    { label: "Sensitivity", value: sensName, origin: originOf(m, "sensitivity") },
    { label: "Teams", value: m?.numTeams ? String(m.numTeams) : "—", origin: originOf(m, "num_teams") },
  ];
}

/**
 * One poll. Either source may fail on its own (null); both failing throws so the
 * poller backs off and the panel is marked stale.
 */
export async function fetchStageSettings(
  getMenuStatus: (signal?: AbortSignal) => Promise<GetMenuStatusResponseLike>,
  signal?: AbortSignal,
): Promise<StageSettings> {
  const [vm, menu] = await Promise.all([
    vmQuery(STAGE_QUERY, signal, { nocache: true }),
    getMenuStatus(signal).then(parseMenu, () => null),
  ]);
  if (!vm && !menu) throw new Error("stage settings: VictoriaMetrics and the menu are unreachable");
  return { gauges: vm ? parseGauges(vm) : null, menu };
}
