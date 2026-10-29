/**
 * UC-004 BR-007 (#2399): the controller card's title is the controller's name,
 * the short id is the subtitle. Minimal fake DOM as in unpair-card.test.ts.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { GameplayData } from "../gen/controller_manager_pb.js";

const SERIAL = "00:06:F7:AA:BB:CC";

class FakeElement {
  hidden = false;
  disabled = false;
  textContent = "";
  className = "";
  innerHtmlWrites = 0;
  readonly style = { setProperty: (_k: string, _v: string) => {} } as Record<string, unknown>;
  readonly children = new Map<string, FakeElement>();
  classList = { toggle: () => false, contains: () => false, add: () => {}, remove: () => {} };
  set innerHTML(_html: string) {
    this.innerHtmlWrites += 1;
  }
  setAttribute() {}
  addEventListener() {}
  querySelector(selector: string): FakeElement {
    let child = this.children.get(selector);
    if (!child) {
      child = new FakeElement();
      this.children.set(selector, child);
    }
    return child;
  }
}

function frame(name?: string): GameplayData {
  return { serial: SERIAL, moveNum: 1, battery: 100, team: 0, rssi: 0, name } as GameplayData;
}

async function newCard() {
  const { ControllerCard } = await import("../components/ControllerCard.js");
  const card = new ControllerCard(SERIAL);
  const element = card.element as unknown as FakeElement;
  return { card, title: element.querySelector(".controller-name"), subtitle: element.querySelector(".controller-id") };
}

beforeEach(() => {
  vi.stubGlobal("document", { createElement: () => new FakeElement(), dispatchEvent: () => true });
});
afterEach(() => vi.unstubAllGlobals());

describe("UC-004: controller card names the controller (BR-007)", () => {
  it("shows the name as title and the short id as subtitle", async () => {
    const { card, title, subtitle } = await newCard();
    card.update(frame("Red Fox"));
    expect(title.textContent).toBe("Red Fox");
    expect(subtitle.textContent).toBe("PB:CC");
  });

  it("follows a changed name in the stream", async () => {
    const { card, title } = await newCard();
    card.update(frame("Red Fox"));
    card.update(frame("Blue Owl"));
    expect(title.textContent).toBe("Blue Owl");
  });

  it("falls back to P<short id> when the name is empty or missing", async () => {
    const { card, title } = await newCard();
    expect(title.textContent).toBe("PB:CC");
    card.update(frame("Red Fox"));
    card.update(frame(""));
    expect(title.textContent).toBe("PB:CC");
    card.update(frame(undefined));
    expect(title.textContent).toBe("PB:CC");
  });

  it("writes the name as text, never as HTML", async () => {
    const { card, title } = await newCard();
    card.update(frame("<img src=x onerror=alert(1)>"));
    expect(title.textContent).toBe("<img src=x onerror=alert(1)>");
    expect(title.innerHtmlWrites).toBe(0);
  });
});
