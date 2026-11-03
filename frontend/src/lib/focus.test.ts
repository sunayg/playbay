import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  applyTvMode,
  arrowDirection,
  focusByDirection,
  handleKeydown,
  installFocus,
  isSelectKey,
  isTextEntry,
  isTvUserAgent,
  mediaKeyAction,
  pickByDirection,
  registerMediaKeyHandler,
  uninstallFocus,
} from "./focus";

function key(init: KeyboardEventInit & { keyCode?: number }) {
  return new KeyboardEvent("keydown", { bubbles: true, cancelable: true, ...init });
}

// jsdom has no layout, so give elements a box.
function place(el: Element, left: number, top: number, w = 100, h = 40) {
  el.getBoundingClientRect = () =>
    ({ left, top, right: left + w, bottom: top + h, width: w, height: h, x: left, y: top, toJSON() {} }) as DOMRect;
}

describe("arrowDirection", () => {
  it("understands keys, TV remote codes and legacy keyCodes", () => {
    expect(arrowDirection({ key: "ArrowUp" } as KeyboardEvent)).toBe("up");
    expect(arrowDirection({ key: "Down" } as KeyboardEvent)).toBe("down");
    expect(arrowDirection({ key: "", keyCode: 21 } as KeyboardEvent)).toBe("left");
    expect(arrowDirection({ key: "", keyCode: 22 } as KeyboardEvent)).toBe("right");
    expect(arrowDirection({ key: "Unidentified", keyCode: 38 } as KeyboardEvent)).toBe("up");
    expect(arrowDirection({ key: "", code: "ArrowRight" } as KeyboardEvent)).toBe("right");
    expect(arrowDirection({ key: "a" } as KeyboardEvent)).toBe("");
  });
  it("select and media keys", () => {
    expect(isSelectKey({ key: "Enter" } as KeyboardEvent)).toBe(true);
    expect(isSelectKey({ key: "", keyCode: 23 } as KeyboardEvent)).toBe(true);
    expect(isSelectKey({ key: "a" } as KeyboardEvent)).toBe(false);
    expect(mediaKeyAction({ key: "MediaPlayPause" } as KeyboardEvent)).toBe("playpause");
    expect(mediaKeyAction({ key: "", keyCode: 126 } as KeyboardEvent)).toBe("play");
    expect(mediaKeyAction({ key: "", keyCode: 127 } as KeyboardEvent)).toBe("pause");
    expect(mediaKeyAction({ key: "", keyCode: 417 } as KeyboardEvent)).toBe("fastforward");
    expect(mediaKeyAction({ key: "MediaTrackNext" } as KeyboardEvent)).toBe("next");
    expect(mediaKeyAction({ key: "GoBack" } as KeyboardEvent)).toBe("back");
    expect(mediaKeyAction({ key: "a" } as KeyboardEvent)).toBeNull();
  });
});

describe("tv mode", () => {
  afterEach(() => document.documentElement.classList.remove("tv-ui"));
  it("detects Android TV user agents only", () => {
    expect(isTvUserAgent("Mozilla/5.0 (Linux; Android 11; BRAVIA 4K) AppleWebKit")).toBe(true);
    expect(isTvUserAgent("Mozilla/5.0 (Linux; Android 12; Pixel 6)")).toBe(false);
    expect(isTvUserAgent("Mozilla/5.0 (Linux; Tizen TV)")).toBe(false);
  });
  it("recognises the Hisense VIDAA browser, which has no TV word in its agent", () => {
    const hisense = "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/111.0.0.0 WebExplorer/8.3.2 Safari/537.36";
    expect(isTvUserAgent(hisense)).toBe(true);
    expect(isTvUserAgent("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36")).toBe(false);
  });
  it("adds the tv-ui class", () => {
    expect(applyTvMode("Mozilla/5.0 (Linux; Android 11; Google TV)")).toBe(true);
    expect(document.documentElement.classList.contains("tv-ui")).toBe(true);
  });
});

describe("isTextEntry", () => {
  it("is true for text inputs and textareas only", () => {
    expect(isTextEntry(document.createElement("textarea"))).toBe(true);
    const text = document.createElement("input");
    expect(isTextEntry(text)).toBe(true);
    const check = document.createElement("input");
    check.type = "checkbox";
    expect(isTextEntry(check)).toBe(false);
    expect(isTextEntry(document.createElement("button"))).toBe(false);
    expect(isTextEntry(null)).toBe(false);
  });
});

describe("pickByDirection", () => {
  const p = (el: string, x: number, y: number) => ({ el, x, y });
  it("goes to the nearest row, then the closest column", () => {
    const places = [p("a", 0, 100), p("b", 200, 100), p("far", 0, 300)];
    expect(pickByDirection(180, 0, places, "down")).toBe("b");
    expect(pickByDirection(0, 400, places, "up")).toBe("far");
    expect(pickByDirection(0, 200, places, "up")).toBe("a");
    expect(pickByDirection(0, 300, places, "down")).toBeNull();
  });
  it("moves sideways within a row band and ignores near-misses", () => {
    const places = [p("l", 0, 0), p("r", 200, 10), p("other", 400, 200), p("close", 110, 0)];
    expect(pickByDirection(100, 0, places, "right")).toBe("r");
    expect(pickByDirection(100, 0, places, "left")).toBe("l");
    expect(pickByDirection(105, 0, [p("close", 110, 0)], "right")).toBeNull();
  });
});

describe("focus handling in the DOM", () => {
  let buttons: HTMLButtonElement[];
  beforeEach(() => {
    document.body.innerHTML = `<button id="a">a</button><button id="b">b</button><button id="c">c</button><input id="i" />`;
    buttons = ["a", "b", "c"].map((id) => document.getElementById(id) as HTMLButtonElement);
    place(buttons[0], 0, 0);
    place(buttons[1], 200, 0);
    place(buttons[2], 0, 200);
    place(document.getElementById("i")!, 200, 200);
  });
  afterEach(() => {
    uninstallFocus();
    document.body.innerHTML = "";
    vi.restoreAllMocks();
  });

  it("moves focus spatially and falls back to document order", () => {
    buttons[0].focus();
    expect(focusByDirection("right")).toBe(true);
    expect(document.activeElement).toBe(buttons[1]);
    focusByDirection("down");
    expect(document.activeElement?.id).toBe("i");
    focusByDirection("left");
    expect(document.activeElement).toBe(buttons[2]);
    focusByDirection("up");
    expect(document.activeElement).toBe(buttons[0]);
    // nothing above: falls back to the previous in document order, which is none
    expect(focusByDirection("up")).toBe(false);
  });

  it("focuses the first target when nothing is focused", () => {
    focusByDirection("down");
    expect(document.activeElement).toBe(buttons[0]);
  });

  it("handles arrows, Enter and Backspace through the installed listener", () => {
    installFocus();
    buttons[0].focus();
    const click = vi.fn();
    buttons[1].addEventListener("click", click);
    const right = key({ key: "ArrowRight" });
    document.dispatchEvent(right);
    expect(right.defaultPrevented).toBe(true);
    expect(document.activeElement).toBe(buttons[1]);
    document.dispatchEvent(key({ key: "Enter" }));
    expect(click).toHaveBeenCalled();
    const back = vi.spyOn(window.history, "back").mockImplementation(() => {});
    document.dispatchEvent(key({ key: "Backspace" }));
    expect(back).toHaveBeenCalled();
  });

  it("an unconsumed Back key goes back; Escape only on a TV", () => {
    installFocus();
    const back = vi.spyOn(window.history, "back").mockImplementation(() => {});
    for (const code of [4, 461, 10009]) document.dispatchEvent(key({ key: "Unidentified", keyCode: code }));
    document.dispatchEvent(key({ key: "GoBack" }));
    expect(back).toHaveBeenCalledTimes(4);
    document.dispatchEvent(key({ key: "Escape" }));
    expect(back).toHaveBeenCalledTimes(4);
    document.documentElement.classList.add("tv-ui");
    document.dispatchEvent(key({ key: "Escape" }));
    expect(back).toHaveBeenCalledTimes(5);
    // A text field keeps Escape for itself.
    (document.getElementById("i") as HTMLInputElement).focus();
    document.getElementById("i")!.dispatchEvent(key({ key: "Escape" }));
    expect(back).toHaveBeenCalledTimes(5);
    document.documentElement.classList.remove("tv-ui");
  });

  it("leaves text fields alone for Backspace and sideways arrows but not up/down", () => {
    installFocus();
    const input = document.getElementById("i") as HTMLInputElement;
    input.focus();
    const back = vi.spyOn(window.history, "back").mockImplementation(() => {});
    const bs = key({ key: "Backspace" });
    input.dispatchEvent(bs);
    expect(bs.defaultPrevented).toBe(false);
    expect(back).not.toHaveBeenCalled();
    const left = key({ key: "ArrowLeft" });
    input.dispatchEvent(left);
    expect(left.defaultPrevented).toBe(false);
    const enter = key({ key: "Enter" });
    input.dispatchEvent(enter);
    expect(enter.defaultPrevented).toBe(false);
    input.dispatchEvent(key({ key: "ArrowUp" }));
    expect(document.activeElement).not.toBe(input);
  });

  it("ArrowRight at the end of a plain text field keeps the caret there", () => {
    installFocus();
    const input = document.getElementById("i") as HTMLInputElement;
    input.value = "abc";
    input.focus();
    input.setSelectionRange(3, 3);
    place(buttons[1], 300, 200); // a neighbour to the right
    const right = key({ key: "ArrowRight" });
    input.dispatchEvent(right);
    expect(right.defaultPrevented).toBe(false);
    expect(document.activeElement).toBe(input);
  });

  it("a data-exit-right field lets ArrowRight leave only from the end of the text", () => {
    installFocus();
    const input = document.getElementById("i") as HTMLInputElement;
    input.dataset.exitRight = "";
    input.value = "abc";
    place(buttons[1], 300, 200);
    input.focus();
    input.setSelectionRange(1, 1);
    input.dispatchEvent(key({ key: "ArrowRight" }));
    expect(document.activeElement).toBe(input);
    input.setSelectionRange(3, 3);
    input.dispatchEvent(key({ key: "ArrowRight" }));
    expect(document.activeElement).toBe(buttons[1]);
  });

  it("gives media keys to the registered handler first", () => {
    installFocus();
    const seen: string[] = [];
    const off = registerMediaKeyHandler((action) => {
      seen.push(action);
      return true;
    });
    const e = key({ key: "MediaPlayPause" });
    document.dispatchEvent(e);
    expect(seen).toEqual(["playpause"]);
    expect(e.defaultPrevented).toBe(true);
    off();
    const e2 = key({ key: "MediaPlayPause" });
    document.dispatchEvent(e2);
    expect(seen).toHaveLength(1);
  });

  it("install is idempotent and uninstall stops handling", () => {
    installFocus();
    installFocus();
    const spy = vi.fn();
    buttons[0].addEventListener("click", spy);
    buttons[0].focus();
    uninstallFocus();
    const e = key({ key: "Enter" });
    document.dispatchEvent(e);
    expect(spy).not.toHaveBeenCalled();
    expect(handleKeydown).toBeTypeOf("function");
  });

  it("blurs the focused element when the mouse moves, except inputs", () => {
    installFocus();
    buttons[0].focus();
    document.dispatchEvent(new MouseEvent("mousemove"));
    expect(document.activeElement).not.toBe(buttons[0]);
    const input = document.getElementById("i") as HTMLInputElement;
    input.focus();
    document.dispatchEvent(new MouseEvent("mousemove"));
    expect(document.activeElement).toBe(input);
  });
});

describe("home screen navigation", () => {
  afterEach(() => (document.body.innerHTML = ""));
  it("jumps from the search box to the first card of the next row", () => {
    document.body.innerHTML = `
      <input class="form-control" id="s" />
      <div class="home-rows">
        <section class="home-row" id="r1"><a class="poster-card" id="p1" href="#"></a></section>
        <section class="home-row" id="r2"><a class="poster-card" id="p2" href="#"></a></section>
      </div>`;
    const q = (id: string) => document.getElementById(id)!;
    place(q("s"), 0, 0, 300, 40);
    place(q("r1"), 0, 100, 300, 100);
    place(q("p1"), 0, 100, 100, 100);
    place(q("r2"), 0, 300, 300, 100);
    place(q("p2"), 0, 300, 100, 100);
    q("s").focus();
    focusByDirection("down");
    expect(document.activeElement).toBe(q("p1"));
    focusByDirection("down");
    expect(document.activeElement).toBe(q("p2"));
    focusByDirection("up");
    expect(document.activeElement).toBe(q("p1"));
  });
});
