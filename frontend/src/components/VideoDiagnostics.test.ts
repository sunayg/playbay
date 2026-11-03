import { flushPromises, mount } from "@vue/test-utils";
import { afterEach, describe, expect, it, vi } from "vitest";
import VideoDiagnostics from "./VideoDiagnostics.vue";

const report = {
  ffmpeg: "ffmpeg version 7.0",
  hwaccels: ["vaapi"],
  cpu_count: 4,
  gpu_device: { path: "/dev/dri/renderD128", present: false },
  encoder: { setting: "auto", chosen: "libx264", low_power: false },
  decode: { setting: "auto", on_gpu: false },
  attempts: [{ what: "VAAPI device", worked: false, detail: "/dev/dri/renderD128 does not exist" }],
  speed: { state: "done", pixels_per_second: 1, full_ladder_speed: 0.8, target: 1.5, error: "" },
  configured_heights: [1080, 720],
  warnings: ["No hardware encoder works here, so video is encoded on the CPU."],
};

afterEach(() => vi.unstubAllGlobals());

describe("VideoDiagnostics", () => {
  it("loads the report when opened and says what was tried and why it failed", async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, status: 200, headers: new Headers(), json: async () => report });
    vi.stubGlobal("fetch", fetchMock);
    const wrapper = mount(VideoDiagnostics);
    expect(fetchMock).not.toHaveBeenCalled();
    const details = wrapper.find("details");
    (details.element as HTMLDetailsElement).open = true;
    await details.trigger("toggle");
    await flushPromises();
    expect(fetchMock).toHaveBeenCalledWith("/api/hls_diagnostics", expect.anything());
    const text = wrapper.text();
    expect(text).toContain("libx264");
    expect(text).toContain("does not exist");
    expect(text).toContain("0.8× real time");
    expect(text).toContain("encoded on the CPU");
  });

  it("shows the error when the report cannot be loaded", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false, status: 500, headers: new Headers() }));
    const wrapper = mount(VideoDiagnostics);
    const details = wrapper.find("details");
    (details.element as HTMLDetailsElement).open = true;
    await details.trigger("toggle");
    await flushPromises();
    expect(wrapper.find("[role=alert]").text()).toContain("500");
  });
});
