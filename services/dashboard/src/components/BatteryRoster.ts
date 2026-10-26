/**
 * Controller battery roster panel (UC-035 A5, #2111): renders RosterModel and
 * wires each row's Identify button to the card's identify helper (UC-004 BR-006).
 */
import { controllerClient } from "../client.js";
import { identifyController, type IdentifyStatus } from "../identify.js";
import { RosterModel, renderRoster } from "../roster.js";
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
      const button = (event.target as HTMLElement).closest?.(".roster-identify") as HTMLButtonElement | null;
      if (button?.dataset.serial) void this.onIdentify(button, button.dataset.serial);
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

  private async onIdentify(button: HTMLButtonElement, serial: string): Promise<void> {
    button.disabled = true;
    const status = await this.identify(serial);
    // A poll may have re-rendered the row while the call ran: look it up again.
    const row = this.container.querySelector(`.roster-row[data-serial="${CSS.escape(serial)}"]`);
    const label = row?.querySelector(".roster-status") as HTMLElement | null;
    const rowButton = row?.querySelector(".roster-identify") as HTMLButtonElement | null;
    if (rowButton) rowButton.disabled = false;
    if (!label) return;
    label.hidden = false;
    label.textContent = status.text;
    label.classList.toggle("refused", !status.ok);
    clearTimeout(this.statusTimers.get(serial));
    this.statusTimers.set(
      serial,
      setTimeout(() => {
        label.hidden = true;
      }, status.showForMs),
    );
  }
}
