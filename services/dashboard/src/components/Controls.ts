/**
 * Controls Component
 *
 * Game mode selection and start/stop controls.
 */
import type { ModeChoices } from "../modes.js";

interface ControlsOptions {
  onStartGame: () => void | Promise<void>;
  onStopGame: () => void | Promise<void>;
  /** Optional: the selected mode is read at start time (getSelectedMode). */
  onModeChange?: (mode: string) => void;
}

export class Controls {
  private readonly modeSelect: HTMLSelectElement;
  private readonly startBtn: HTMLButtonElement;
  private readonly stopBtn: HTMLButtonElement;
  private readonly options: ControlsOptions;

  constructor(options: ControlsOptions) {
    this.options = options;

    this.modeSelect = document.getElementById("game-mode") as HTMLSelectElement;
    this.startBtn = document.getElementById("start-btn") as HTMLButtonElement;
    this.stopBtn = document.getElementById("stop-btn") as HTMLButtonElement;

    this.setupEventListeners();
  }

  private setupEventListeners() {
    this.modeSelect.addEventListener("change", () => {
      this.options.onModeChange?.(this.modeSelect.value);
    });

    this.startBtn.addEventListener("click", () => {
      Promise.resolve(this.options.onStartGame()).catch(console.error);
    });

    this.stopBtn.addEventListener("click", () => {
      Promise.resolve(this.options.onStopGame()).catch(console.error);
    });
  }

  /** Replace the mode options (built from ListGameModes, UC-011 BR-002). */
  setModeChoices(choices: ModeChoices) {
    this.modeSelect.replaceChildren(
      ...choices.options.map(({ value, label }) => new Option(label, value, false, value === choices.selected))
    );
    this.modeSelect.value = choices.selected;
  }

  getSelectedMode(): string {
    return this.modeSelect.value;
  }

  setGameRunning(running: boolean) {
    this.startBtn.disabled = running;
    this.stopBtn.disabled = !running;
    this.modeSelect.disabled = running;
  }
}
