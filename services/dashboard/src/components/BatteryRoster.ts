/**
 * Controller battery roster panel (UC-035 A5, #2111): renders RosterModel and
 * wires each row's Identify button to the card's identify helper (UC-004 BR-006).
 */
import { controllerClient } from "../client.js";
import { identifyController, type IdentifyStatus } from "../identify.js";
import { RosterModel, identifyTarget, renderRoster } from "../roster.js";
import type { GetConnectedControllersResponse } from "../gen/controller_manager_pb.js";

export class BatteryRoster {
  private readonly model = new RosterModel();
  private readonly container: HTMLElement;
  private readonly statusTimers = new Map<string, ReturnType<typeof setTimeout>>();

  constructor(
    containerId: string,
    private readonly identify: (serial: string) => Promise<IdentifyStatus> = (serial) =>
      identifyController(serial, (req, signal) => controllerClient.identifyController(req, signal)),
  ) {
    const container = document.getElementById(containerId);
    if (!container) throw new Error(`Container element not found: ${containerId}`);
    this.container = container;
    this.container.addEventListener("click", (event) => {
      const serial = identifyTarget(event.target as HTMLElement | null);
      if (serial) void this.onIdentify(serial);
    });
    this.render();
  }

  update(response: GetConnectedControllersResponse): void {
    this.model.update(response);
    this.render();
  }

  fail(): void {
    this.model.fail();
    this.render();
  }

  private render(): void {
    this.container.innerHTML = renderRoster(this.model);
  }

  private async onIdentify(serial: string): Promise<void> {
    if (this.model.identifying.has(serial)) return;
    this.model.startIdentify(serial);
    this.render();
    const status = await this.identify(serial);
    this.model.finishIdentify(serial, status);
    this.render();
    clearTimeout(this.statusTimers.get(serial));
    this.statusTimers.set(
      serial,
      setTimeout(() => {
        this.model.clearStatus(serial);
        this.render();
      }, status.showForMs),
    );
  }
}
