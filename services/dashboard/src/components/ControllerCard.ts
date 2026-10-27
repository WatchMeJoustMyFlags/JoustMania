/**
 * Controller Card Component
 *
 * Displays a single controller with LED color, battery, accelerometer visualization.
 */
import type { ConnectionQuality, GameplayData, RGB } from "../gen/controller_manager_pb.js";
import { accelDotOffset, batteryView, normalizeGameplay, statusView } from "../motion.js";
import type { StatusView } from "../motion.js";
import { qualityBadge } from "../quality.js";
import { holdEnergyBar, shieldBadge } from "../shield.js";
import type { ShieldView } from "../shield.js";
import { controllerClient } from "../client.js";
import { identifyController } from "../identify.js";
import { UNPAIR_NOTICE_EVENT, canUnpair, requestUnpair } from "../unpair.js";

/**
 * CSS for the LED circle. `color` is the colour last written to the LED, so a
 * shield shows as white fading to dim white, the last-second blink as off frames
 * (#1301). Proto3 JSON omits zero fields, so missing r/g/b mean 0.
 */
export function ledColorCss(color: Partial<Pick<RGB, "r" | "g" | "b">> | undefined): {
  background: string;
  glow: string;
} {
  if (!color) return { background: "#333", glow: "transparent" };
  const r = color.r ?? 0;
  const g = color.g ?? 0;
  const b = color.b ?? 0;
  return { background: `rgb(${r}, ${g}, ${b})`, glow: `rgba(${r}, ${g}, ${b}, 0.6)` };
}

export class ControllerCard {
  readonly element: HTMLElement;
  private readonly serial: string;
  private readonly ledElement: HTMLElement;
  private readonly batteryElement: HTMLElement;
  private readonly accelDot: HTMLElement;
  private readonly statusBadge: HTMLElement;
  private readonly shieldElement: HTMLElement;
  private readonly holdElement: HTMLElement;
  private readonly holdFill: HTMLElement;
  private readonly holdBudget: HTMLElement;
  private readonly holdText: HTMLElement;
  private readonly qualityElement: HTMLElement;
  private readonly adapterElement: HTMLElement;
  private readonly identifyButton: HTMLButtonElement;
  private readonly identifyElement: HTMLElement;
  private identifyTimer: ReturnType<typeof setTimeout> | undefined;
  private readonly unpairButton: HTMLButtonElement;
  private readonly unpairResult: HTMLElement;

  constructor(serial: string) {
    this.serial = serial;
    this.element = this.createElement();

    this.ledElement = this.element.querySelector(".led-color")!;
    this.batteryElement = this.element.querySelector(".battery-level")!;
    this.accelDot = this.element.querySelector(".accel-dot")!;
    this.statusBadge = this.element.querySelector(".status-badge")!;
    this.shieldElement = this.element.querySelector(".shield-badge")!;
    this.holdElement = this.element.querySelector(".hold-energy")!;
    this.holdFill = this.element.querySelector(".hold-energy-fill")!;
    this.holdBudget = this.element.querySelector(".hold-budget-fill")!;
    this.holdText = this.element.querySelector(".hold-energy-text")!;
    this.qualityElement = this.element.querySelector(".quality-badge")!;
    this.adapterElement = this.element.querySelector(".adapter-badge")!;
    this.identifyButton = this.element.querySelector(".identify-btn")!;
    this.identifyElement = this.element.querySelector(".identify-status")!;
    this.identifyButton.addEventListener("click", () => void this.identify());
    this.unpairButton = this.element.querySelector(".unpair-btn")!;
    this.unpairResult = this.element.querySelector(".unpair-result")!;
    // UC-005: only real controllers (serial = Bluetooth address) can be unpaired
    this.unpairButton.hidden = !canUnpair(this.serial);
    this.unpairButton.addEventListener("click", () => void this.unpair());
  }

  /** UC-004: flash + rumble this controller; show the answer on the card. */
  private async identify() {
    this.identifyButton.disabled = true;
    const status = await identifyController(this.serial, (req, signal) => controllerClient.identifyController(req, signal));
    clearTimeout(this.identifyTimer);
    this.identifyElement.hidden = false;
    this.identifyElement.textContent = status.text;
    this.identifyElement.classList.toggle("refused", !status.ok);
    this.identifyButton.disabled = false;
    this.identifyTimer = setTimeout(() => {
      this.identifyElement.hidden = true;
    }, status.showForMs);
  }

  private async unpair() {
    this.unpairButton.disabled = true;
    try {
      const outcome = await requestUnpair(this.serial, {
        confirm: (message) => globalThis.confirm(message),
        fetch: (input, init) => globalThis.fetch(input, init),
        baseUrl: import.meta.env.DEV ? "" : globalThis.location.origin,
      });
      if (outcome.kind === "cancelled") return;
      // The card disappears when the controller disconnects: also log to the event feed
      document.dispatchEvent(new CustomEvent(UNPAIR_NOTICE_EVENT, { detail: outcome.message }));
      this.unpairResult.hidden = false;
      this.unpairResult.textContent = outcome.message;
      this.unpairResult.classList.toggle("failed", outcome.kind === "failed");
    } finally {
      this.unpairButton.disabled = false;
    }
  }

  private createElement(): HTMLElement {
    const card = document.createElement("div");
    card.className = "controller-card connected";
    card.innerHTML = `
      <div class="controller-id">P${this.getShortId()}</div>
      <div class="led-color"></div>
      <div class="battery-bar">
        <div class="battery-level high" style="width: 100%"></div>
      </div>
      <div class="accel-viz">
        <div class="accel-dot"></div>
      </div>
      <div class="status-badge connected">Connected</div>
      <div class="shield-badge" hidden></div>
      <div class="hold-energy" hidden>
        <div class="hold-energy-bar"><div class="hold-energy-fill"></div></div>
        <div class="hold-budget-bar"><div class="hold-budget-fill"></div></div>
        <div class="hold-energy-text"></div>
      </div>
      <div class="quality-badge" hidden></div>
      <div class="adapter-badge" hidden></div>
      <button type="button" class="identify-btn" title="Flash and rumble this controller">Identify</button>
      <div class="identify-status" hidden></div>
      <button type="button" class="unpair-btn" title="Remove this controller from every Bluetooth adapter">Unpair</button>
      <div class="unpair-result" hidden></div>
    `;
    return card;
  }

  private getShortId(): string {
    // Use last 4 characters of serial as short ID
    return this.serial.slice(-4);
  }

  update(
    frame: GameplayData,
    shield: ShieldView | null = null,
    nowMs: number = Date.now(),
    alive: boolean | undefined = undefined,
  ) {
    // Proto3 JSON omits zero scalars: default them so nothing renders as NaN (#1604)
    const data = normalizeGameplay(frame);
    // Update LED color: the colour last written to the LED (shield fade included, #1301)
    this.updateLedColor(data.color);

    // Agent shield badge (UC-040 A16)
    this.updateShield(shield, nowMs);

    // Hold-mode energy bar (UC-035 BR-008)
    this.updateHoldEnergy(shield);

    // Connection-quality badge (UC-035 BR-005)
    this.updateQuality(data.connectionQuality);

    // Update battery level
    this.updateBattery(data.battery);

    // Update accelerometer visualization
    this.updateAccelerometer(data.accel);

    // Status: a followed shadow game's roster says alive/dead (UC-070 BR-005);
    // otherwise the team (-1 = admin, > 0 = team n, 0 or unset = alive)
    this.updateStatus(statusView(data.team, alive));
  }

  private updateLedColor(color: RGB | undefined) {
    const css = ledColorCss(color);
    this.ledElement.style.backgroundColor = css.background;
    this.ledElement.style.setProperty("--led-glow", css.glow);
    this.ledElement.style.boxShadow = css.glow === "transparent" ? "" : `0 0 20px ${css.glow}`;
  }

  private updateShield(shield: ShieldView | null, nowMs: number) {
    const badge = shieldBadge(shield, nowMs);
    this.element.classList.toggle("shielded", shield?.active != null && badge !== null);
    this.element.classList.toggle("charged", shield?.active == null && badge !== null);
    if (!badge) {
      this.shieldElement.hidden = true;
      return;
    }
    this.shieldElement.hidden = false;
    this.shieldElement.className = badge.className;
    this.shieldElement.textContent = badge.text;
  }

  private updateHoldEnergy(shield: ShieldView | null) {
    const bar = holdEnergyBar(shield);
    if (!bar) {
      this.holdElement.hidden = true;
      return;
    }
    this.holdElement.hidden = false;
    this.holdElement.className = bar.className;
    this.holdFill.style.width = `${bar.fillPercent}%`;
    this.holdBudget.style.width = `${bar.budgetPercent}%`;
    this.holdText.textContent = `${bar.text} · ${bar.label}${bar.source ? ` · ${bar.source}` : ""}`;
  }

  /** The HCI adapter badge (UC-035 BR-009); undefined hides it. `stale`: VictoriaMetrics is down (A3). */
  updateAdapter(adapter: string | undefined, stale = false) {
    if (!adapter) {
      this.adapterElement.hidden = true;
      return;
    }
    this.adapterElement.hidden = false;
    this.adapterElement.textContent = adapter;
    this.adapterElement.classList.toggle("stale", stale);
    this.adapterElement.title = stale ? `Bluetooth adapter ${adapter} (stale: VictoriaMetrics unreachable)` : `Bluetooth adapter ${adapter}`;
  }

  private updateQuality(quality: ConnectionQuality | undefined) {
    const badge = qualityBadge(quality);
    if (!badge) {
      this.qualityElement.hidden = true;
      return;
    }
    this.qualityElement.hidden = false;
    this.qualityElement.className = badge.className;
    this.qualityElement.textContent = badge.text;
    this.qualityElement.title = badge.title;
  }

  private updateBattery(level: number | undefined) {
    // Battery level is 0-5, convert to percentage
    const { percent, className } = batteryView(level);
    this.batteryElement.style.width = `${percent}%`;
    this.batteryElement.classList.remove("high", "medium", "low");
    this.batteryElement.classList.add(className);
  }

  private updateAccelerometer(accel: { x?: number; y?: number; z?: number } | undefined) {
    const { x, y } = accelDotOffset(accel);
    this.accelDot.style.transform = `translate(calc(-50% + ${x}px), calc(-50% + ${y}px))`;
  }

  private updateStatus(view: StatusView) {
    this.element.classList.remove("alive", "dead", "admin");
    this.statusBadge.classList.remove("connected", "alive", "dead", "admin");
    this.element.classList.add(view.state);
    this.statusBadge.classList.add(view.state);
    this.statusBadge.textContent = view.text;
  }

  remove() {
    clearTimeout(this.identifyTimer);
    this.element.remove();
  }
}
