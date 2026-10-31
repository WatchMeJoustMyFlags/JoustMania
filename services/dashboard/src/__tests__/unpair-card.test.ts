/**
 * Unpair on the dashboard ControllerCard (UC-005 main steps 1 and 7): the
 * Unpair button starts the request, the result shows on the card and in the
 * event feed (via UNPAIR_NOTICE_EVENT, which main.ts adds to the feed).
 * No DOM library is installed, so the card runs against a minimal fake DOM.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const SERIAL = "00:06:F7:AA:BB:CC";

class FakeElement {
  hidden = false;
  disabled = false;
  textContent = "";
  className = "";
  private readonly classes = new Set<string>();
  private readonly handlers = new Map<string, Array<() => void>>();
  readonly children = new Map<string, FakeElement>();
  classList = {
    toggle: (name: string, force?: boolean) => {
      const on = force ?? !this.classes.has(name);
      if (on) this.classes.add(name);
      else this.classes.delete(name);
      return on;
    },
    contains: (name: string) => this.classes.has(name),
  };
  set innerHTML(_html: string) {}
  addEventListener(type: string, handler: () => void) {
    this.handlers.set(type, [...(this.handlers.get(type) ?? []), handler]);
  }
  click() {
    for (const handler of this.handlers.get("click") ?? []) handler();
  }
  querySelector(selector: string): FakeElement {
    let child = this.children.get(selector);
    if (!child) {
      child = new FakeElement();
      this.children.set(selector, child);
    }
    return child;
  }
}

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

const feed: string[] = [];
let fakeDocument: EventTarget & { createElement: () => FakeElement };

async function newCard(serial = SERIAL) {
  const { ControllerCard } = await import("../components/ControllerCard.js");
  const card = new ControllerCard(serial);
  const element = card.element as unknown as FakeElement;
  return {
    button: element.querySelector(".unpair-btn"),
    result: element.querySelector(".unpair-result"),
  };
}

async function settle() {
  await vi.waitFor(() => expect(feed.length).toBeGreaterThan(0));
}

beforeEach(() => {
  feed.length = 0;
  fakeDocument = Object.assign(new EventTarget(), { createElement: () => new FakeElement() });
  fakeDocument.addEventListener("joustmania:unpair-notice", (event) => {
    feed.push(String((event as CustomEvent<string>).detail));
  });
  vi.stubGlobal("document", fakeDocument);
  vi.stubGlobal("location", { origin: "http://pi" });
  vi.stubGlobal("confirm", vi.fn(() => true));
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("UC-005: Unpair a Controller (dashboard card)", () => {
  it("UC-005/main step 1 clicking Unpair sends POST /pairing/unpair with the card's address and disables the button while pending", async () => {
    let finish!: (response: Response) => void;
    const fetchMock = vi.fn(() => new Promise<Response>((resolve) => (finish = resolve)));
    vi.stubGlobal("fetch", fetchMock);
    const { button } = await newCard();
    expect(button.hidden).toBe(false);
    expect(button.disabled).toBe(false);

    button.click();

    expect(button.disabled).toBe(true);
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toMatch(/\/pairing\/unpair$/);
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({ address: SERIAL });
    expect(button.disabled).toBe(true);

    finish(jsonResponse(200, { removed: 1, adapters: [] }));
    await settle();
    await vi.waitFor(() => expect(button.disabled).toBe(false));
  });

  it("UC-005/main step 1 a real controller's bare 12-hex serial shows the button and posts the colon form; a mock serial hides it", async () => {
    const fetchMock = vi.fn(async () => jsonResponse(200, { removed: 1, adapters: [] }));
    vi.stubGlobal("fetch", fetchMock);
    const { button } = await newCard("E0AE5EE112E4");
    expect(button.hidden).toBe(false);
    expect((await newCard("MOCK0001")).button.hidden).toBe(true);

    button.click();
    await settle();

    const init = (fetchMock.mock.calls[0] as unknown as [string, RequestInit])[1];
    expect(JSON.parse(String(init.body))).toEqual({ address: "E0:AE:5E:E1:12:E4" });
  });

  it("UC-005/main step 1 a declined confirmation sends nothing and re-enables the button", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("confirm", vi.fn(() => false));
    const { button, result } = await newCard();
    result.hidden = true;

    button.click();

    await vi.waitFor(() => expect(button.disabled).toBe(false));
    expect(fetchMock).not.toHaveBeenCalled();
    expect(result.hidden).toBe(true);
    expect(feed).toEqual([]);
  });

  it("UC-005/main step 7 success shows the result on the card and in the event feed", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(200, { removed: 2, adapters: ["AA:00:00:00:00:00"], on_usb: false })));
    const { button, result } = await newCard();
    result.hidden = true;

    button.click();
    await settle();

    const expected = `Unpaired ${SERIAL}: 2 registration(s) removed (AA:00:00:00:00:00)`;
    expect(result.hidden).toBe(false);
    expect(result.textContent).toBe(expected);
    expect(result.classList.contains("failed")).toBe(false);
    expect(feed).toEqual([expected]);
  });

  it("UC-005/main step 7 already_absent is shown as success", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(200, { removed: 0, already_absent: 2, failed: 0, adapters: [], on_usb: false })));
    const { button, result } = await newCard();

    button.click();
    await settle();

    const expected = `Unpaired ${SERIAL}: 0 registration(s) removed, 2 already absent`;
    expect(result.textContent).toBe(expected);
    expect(result.classList.contains("failed")).toBe(false);
    expect(feed).toEqual([expected]);
  });

  it("UC-005/main step 7 an error is shown as failed on the card and in the event feed", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(404, { error: `Controller ${SERIAL} is not registered on any adapter.` })));
    const { button, result } = await newCard();

    button.click();
    await settle();

    const expected = `Controller ${SERIAL} is not registered on any adapter.`;
    expect(result.hidden).toBe(false);
    expect(result.textContent).toBe(expected);
    expect(result.classList.contains("failed")).toBe(true);
    expect(feed).toEqual([expected]);
    expect(button.disabled).toBe(false);
  });

  it("UC-005/A5 a mock serial's card hides the Unpair button", async () => {
    vi.stubGlobal("fetch", vi.fn());
    const { button } = await newCard("MOCK0001");
    expect(button.hidden).toBe(true);
  });
});
