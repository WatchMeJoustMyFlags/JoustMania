/**
 * Experiments data source (issue #981).
 *
 * The agent runs the cohort/experiment framework (epic #982) and emits its
 * progress as structured slog lines on stdout:
 *
 *   experiment.game_assigned   experiment_id, game_id, arm, pair_index
 *   experiment.game_concluded  experiment_id, game_id, arm, fitness
 *   experiment.concluded       experiment_id, outcome
 *   experiment.torn_down       experiment_id, status, reason
 *
 * Those container logs are tailed by the OTEL collector's `logs/containers`
 * pipeline and shipped to Loki labeled `service_name="agent"` (see
 * services/otel-collector/config.yaml + services/envoy/envoy.yaml `/loki/*`).
 *
 * LOKI LABEL CONTRACT — `service_name="agent"`
 * --------------------------------------------
 * This view is empty unless the agent's container logs arrive in Loki under the
 * stream label `service_name="agent"`. That label is produced by a chain that
 * this query is tightly coupled to:
 *
 *   1. docker-compose.yml defines the agent under the compose service name
 *      `agent` and the json-file log driver tags it with the docker label
 *      `com.docker.compose.service=agent` (see the logging `labels:` anchor).
 *   2. The collector's `transform/docker_logs` processor promotes that docker
 *      label to the OTEL resource attribute `service.name` (config.yaml ~L195).
 *   3. The `otlphttp/loki` exporter ships to Loki's native OTLP endpoint, which
 *      maps the `service.name` resource attribute to the `service_name` stream
 *      label.
 *
 * If any link changes (compose service renamed, the transform dropped, or Loki's
 * resource→label mapping retuned) the selector below silently returns zero
 * streams and the view goes blank with no error. Keep the matching comment in
 * services/otel-collector/config.yaml (`transform/docker_logs`) in sync.
 *
 * The dashboard already embeds the same observability stack (Grafana / Jaeger
 * iframes) and reaches it through envoy. This module reuses that exact data path
 * — it queries the Loki HTTP API at `/loki/...` — rather than inventing a new
 * transport. Loki has no aggregation for our slog-text bodies, so we pull the raw
 * agent log lines over a window and fold them into per-experiment / per-arm
 * aggregates here, mirroring what the agent's own journal summary.json holds.
 */

const BASE_URL = import.meta.env.DEV ? "" : globalThis.location.origin;

/**
 * Loki LogQL selector for the agent's experiment lifecycle log lines.
 *
 * `service_name="agent"` is a load-bearing contract — see the LOKI LABEL
 * CONTRACT note in the module header before changing it.
 */
const EXPERIMENT_LOG_QUERY =
  '{service_name="agent"} |= "experiment."';

/** How far back to look for experiment activity. */
const LOOKBACK_SECONDS = 60 * 60; // 1 hour

/**
 * Max log lines Loki returns per query.
 *
 * The fold is sample-additive: every `experiment.game_concluded` /
 * `experiment.game_assigned` line contributes to a count/sum, and the mean &
 * delta are only correct if we see ALL of them. On a busy K×N run a 1h window
 * can exceed this cap. We fetch with `direction: forward` (see fetchExperiments)
 * so that, if the window is ever truncated, we keep the OLDEST lines — i.e. the
 * complete prefix of each experiment's lifecycle (assignments + early
 * conclusions + the terminal verdict for already-finished experiments) — rather
 * than the newest tail. That biases toward correct historical aggregates; the
 * cost is that the very latest in-flight lines of a still-running experiment may
 * be dropped once the window is saturated. 5000 lines comfortably covers a
 * typical demo/shadow run; raise this if a single 1h window legitimately emits
 * more experiment lifecycle lines.
 */
const QUERY_LIMIT = 5000;

/** Per-arm rolling aggregate folded from `experiment.game_concluded` lines. */
export interface ArmAggregate {
  arm: string;
  /** Games concluded with a fitness sample for this arm. */
  sampleCount: number;
  /** Mean fitness across this arm's samples (0 when no samples yet). */
  meanFitness: number;
  /** Games assigned (bound to a shadow game) for this arm. */
  assignedCount: number;
}

/** Folded live state for a single experiment. */
export interface ExperimentView {
  experimentId: string;
  arms: ArmAggregate[];
  /**
   * experimental_mean − control_mean when both named arms have samples;
   * null when the delta cannot be computed yet.
   */
  delta: number | null;
  /** "running" | "promote" | "discard" | "inconclusive" | "torn_down". */
  verdict: string;
  /** Free-text verdict/teardown reason (carries d_z=... / effect=... when present). */
  reason: string;
  /** Epoch ms of the most recent log line touching this experiment. */
  lastUpdatedMs: number;
  /** True until a terminal (concluded / torn_down) line is seen. */
  live: boolean;
}

/** The two canonical arm names (mirror journal.ArmExperimental / ArmControl). */
const ARM_EXPERIMENTAL = "experimental";
const ARM_CONTROL = "control";

interface LokiStreamResult {
  stream: Record<string, string>;
  values: [string, string][]; // [nanosecond-ts-string, log line]
}

interface LokiQueryResponse {
  status: string;
  data: {
    resultType: string;
    result: LokiStreamResult[];
  };
}

/**
 * Parse a slog TextHandler line into a flat key→value map.
 *
 * slog emits logfmt-style `key=value` pairs, quoting values that contain
 * spaces, e.g.:
 *   time=2026-06-14T... level=INFO msg=experiment.game_concluded \
 *     experiment_id=exp_a1b2 arm=control fitness=0.42
 *   ...msg=experiment.torn_down ... reason="paired: d_z=0.81 (pairs=6)"
 *
 * Container-log shippers may prefix the line (stream/timestamp); we tolerate
 * that by scanning for `key=value` tokens anywhere in the line.
 */
export function parseLogfmt(line: string): Record<string, string> {
  const out: Record<string, string> = {};
  // key=value where value is either a double-quoted string or a run of
  // non-space characters.
  const re = /([\w.]+)=("(?:[^"\\]|\\.)*"|[^\s]+)/g;
  let match: RegExpExecArray | null;
  while ((match = re.exec(line)) !== null) {
    const key = match[1];
    let value = match[2];
    if (value.startsWith('"') && value.endsWith('"')) {
      value = value.slice(1, -1).replace(/\\"/g, '"').replace(/\\\\/g, "\\");
    }
    out[key] = value;
  }
  return out;
}

interface MutableArm {
  arm: string;
  sampleCount: number;
  fitnessSum: number;
  assignedCount: number;
}

interface MutableExperiment {
  experimentId: string;
  arms: Map<string, MutableArm>;
  /**
   * Terminal verdict signals, captured INDEPENDENTLY so precedence is resolved
   * explicitly in finalizeExperiment rather than depending on which terminal
   * line happens to be folded last.
   *
   *   concludedOutcome  ← experiment.concluded   `outcome` (promote/discard/inconclusive)
   *   tornDownStatus    ← experiment.torn_down   `status`  (done/discarded/aborted)
   *
   * Both fire at terminal; the agent's `concluded.outcome` is the authoritative
   * statistical verdict, while `torn_down.status` describes lifecycle disposition
   * (and is the ONLY signal for experiments that are torn down without ever
   * concluding, e.g. aborted). See the precedence rule in finalizeExperiment.
   */
  concludedOutcome: string | null;
  tornDownStatus: string | null;
  reason: string;
  lastUpdatedMs: number;
  live: boolean;
}

function getArm(exp: MutableExperiment, arm: string): MutableArm {
  let a = exp.arms.get(arm);
  if (!a) {
    a = { arm, sampleCount: 0, fitnessSum: 0, assignedCount: 0 };
    exp.arms.set(arm, a);
  }
  return a;
}

function touch(exp: MutableExperiment, tsMs: number) {
  if (tsMs > exp.lastUpdatedMs) exp.lastUpdatedMs = tsMs;
}

/**
 * Fold a batch of agent log lines into per-experiment aggregates.
 *
 * Each entry is [logLine, epochMs]. The fold is ORDER-INDEPENDENT by construction
 * and does not rely on the Loki query direction:
 *   - sample counts/sums are additive (commutative),
 *   - `live` is sticky-false (any terminal line wins, regardless of position),
 *   - the rendered verdict is derived from the two terminal signals
 *     (concludedOutcome / tornDownStatus) by an EXPLICIT precedence rule in
 *     finalizeExperiment — NOT by "whichever terminal line was folded last".
 * This means a future change to `direction` (forward/backward) cannot silently
 * flip the verdict.
 */
export function aggregateLogLines(
  lines: { line: string; tsMs: number }[],
): ExperimentView[] {
  const experiments = new Map<string, MutableExperiment>();

  const getExp = (id: string): MutableExperiment => {
    let e = experiments.get(id);
    if (!e) {
      e = {
        experimentId: id,
        arms: new Map(),
        concludedOutcome: null,
        tornDownStatus: null,
        reason: "",
        lastUpdatedMs: 0,
        live: true,
      };
      experiments.set(id, e);
    }
    return e;
  };

  for (const { line, tsMs } of lines) {
    const fields = parseLogfmt(line);
    const msg = fields["msg"];
    const id = fields["experiment_id"];
    if (!msg || !id || !msg.startsWith("experiment.")) continue;

    const exp = getExp(id);
    touch(exp, tsMs);

    switch (msg) {
      case "experiment.game_assigned": {
        if (fields["arm"]) getArm(exp, fields["arm"]).assignedCount += 1;
        break;
      }
      case "experiment.game_concluded": {
        const arm = fields["arm"];
        const fitness = Number.parseFloat(fields["fitness"]);
        if (arm && Number.isFinite(fitness)) {
          const a = getArm(exp, arm);
          a.sampleCount += 1;
          a.fitnessSum += fitness;
        }
        break;
      }
      case "experiment.concluded": {
        // Authoritative statistical verdict; recorded independently of teardown.
        if (fields["outcome"]) exp.concludedOutcome = fields["outcome"];
        exp.live = false;
        break;
      }
      case "experiment.torn_down": {
        // Lifecycle disposition; recorded independently of the conclusion.
        if (fields["status"]) exp.tornDownStatus = fields["status"];
        if (fields["reason"]) exp.reason = fields["reason"];
        exp.live = false;
        break;
      }
      default:
        break;
    }
  }

  return Array.from(experiments.values())
    .map(finalizeExperiment)
    .sort((a, b) => b.lastUpdatedMs - a.lastUpdatedMs);
}

function finalizeExperiment(exp: MutableExperiment): ExperimentView {
  const arms: ArmAggregate[] = Array.from(exp.arms.values())
    .map((a) => ({
      arm: a.arm,
      sampleCount: a.sampleCount,
      assignedCount: a.assignedCount,
      meanFitness: a.sampleCount > 0 ? a.fitnessSum / a.sampleCount : 0,
    }))
    // Stable, readable order: experimental, control, then any others.
    .sort((x, y) => armRank(x.arm) - armRank(y.arm) || x.arm.localeCompare(y.arm));

  const experimental = exp.arms.get(ARM_EXPERIMENTAL);
  const control = exp.arms.get(ARM_CONTROL);
  let delta: number | null = null;
  if (
    experimental &&
    control &&
    experimental.sampleCount > 0 &&
    control.sampleCount > 0
  ) {
    delta =
      experimental.fitnessSum / experimental.sampleCount -
      control.fitnessSum / control.sampleCount;
  }

  return {
    experimentId: exp.experimentId,
    arms,
    delta,
    verdict: resolveVerdict(exp),
    reason: exp.reason,
    lastUpdatedMs: exp.lastUpdatedMs,
    live: exp.live,
  };
}

/**
 * Resolve the rendered verdict from the captured terminal signals using an
 * EXPLICIT precedence, so it never depends on log-line ordering:
 *
 *   1. `experiment.concluded` outcome (promote/discard/inconclusive) — the
 *      authoritative statistical verdict — wins whenever present.
 *   2. else `experiment.torn_down` status (done/discarded/aborted) — covers
 *      experiments torn down WITHOUT a conclusion (e.g. aborted).
 *   3. else "running" — no terminal line seen yet.
 *
 * `normalizeVerdict()` in ExperimentsView maps both vocabularies to a display
 * label; this function only decides WHICH raw signal to surface.
 */
function resolveVerdict(exp: MutableExperiment): string {
  if (exp.concludedOutcome) return exp.concludedOutcome;
  if (exp.tornDownStatus) return exp.tornDownStatus;
  return "running";
}

function armRank(arm: string): number {
  if (arm === ARM_EXPERIMENTAL) return 0;
  if (arm === ARM_CONTROL) return 1;
  return 2;
}

/**
 * Query Loki for recent agent experiment logs and fold them into per-experiment
 * views. Throws on transport / HTTP error so the caller can surface a retry.
 */
export async function fetchExperiments(): Promise<ExperimentView[]> {
  const endNs = BigInt(Date.now()) * 1_000_000n;
  const startNs = endNs - BigInt(LOOKBACK_SECONDS) * 1_000_000_000n;

  const params = new URLSearchParams({
    query: EXPERIMENT_LOG_QUERY,
    start: startNs.toString(),
    end: endNs.toString(),
    limit: String(QUERY_LIMIT),
    // forward = oldest-first. If the QUERY_LIMIT cap truncates the window we want
    // to keep the complete lifecycle prefix (assignments + conclusions) for
    // correct counts/means, not the newest tail. See QUERY_LIMIT note above.
    direction: "forward",
  });

  const url = `${BASE_URL}/loki/loki/api/v1/query_range?${params.toString()}`;
  const response = await fetch(url, { headers: { Accept: "application/json" } });
  if (!response.ok) {
    throw new Error(`Loki query failed: ${response.status} ${response.statusText}`);
  }

  const body = (await response.json()) as LokiQueryResponse;
  const lines: { line: string; tsMs: number }[] = [];
  for (const stream of body.data?.result ?? []) {
    for (const [tsNs, line] of stream.values) {
      // ns string → ms number (precision loss here is irrelevant for display).
      const tsMs = Number(BigInt(tsNs) / 1_000_000n);
      lines.push({ line, tsMs });
    }
  }

  return aggregateLogLines(lines);
}
