import { beforeEach, describe, expect, it } from "vitest";
import { resetViewerForTests, viewerId } from "./viewer";

describe("viewerId", () => {
  beforeEach(() => {
    window.localStorage.clear();
    resetViewerForTests();
  });

  it("is the same across calls and reloads, and fit for a URL", () => {
    const first = viewerId();
    expect(first).toMatch(/^[a-z0-9]{16}$/);
    expect(viewerId()).toBe(first);
    resetViewerForTests();
    expect(viewerId()).toBe(first); // read back from storage
  });

  it("differs between devices (empty storage)", () => {
    const first = viewerId();
    window.localStorage.clear();
    resetViewerForTests();
    expect(viewerId()).not.toBe(first);
  });

  it("replaces a stored value that is not usable", () => {
    window.localStorage.setItem("playbay.viewer", "bad id!");
    expect(viewerId()).toMatch(/^[a-z0-9]{16}$/);
  });
});
