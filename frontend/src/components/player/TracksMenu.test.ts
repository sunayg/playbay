import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";
import TracksMenu from "./TracksMenu.vue";

const base = { audioChoices: [], activeAudio: null, captionChoices: [], activeCaption: null };
const levels = [
  { index: 0, height: 1080 },
  { index: 1, height: 720 },
  { index: 2, height: 480 },
];

describe("TracksMenu quality", () => {
  it("has no quality section when the stream offers one rendition or none", () => {
    expect(mount(TracksMenu, { props: base }).text()).not.toContain("Quality");
    expect(mount(TracksMenu, { props: { ...base, qualityLevels: [levels[0]], selectedQuality: -1 } }).text()).not.toContain("Quality");
  });

  it("lists Auto and every rendition, best first, and marks the selected one", () => {
    const wrapper = mount(TracksMenu, { props: { ...base, qualityLevels: levels, selectedQuality: 1 } });
    const items = wrapper.findAll('[role="menuitemradio"]').map((b) => b.text());
    expect(items.slice(0, 4)).toEqual(["Auto", "1080p", "720p", "480p"]);
    expect(wrapper.findAll(".selected").map((b) => b.text())).toContain("720p");
  });

  it("shows which rendition Auto is currently playing", () => {
    const wrapper = mount(TracksMenu, { props: { ...base, qualityLevels: levels, selectedQuality: -1, playingQuality: 2 } });
    expect(wrapper.text()).toContain("Auto (480p)");
  });

  it("emits the rendition index, or -1 for Auto", async () => {
    const wrapper = mount(TracksMenu, { props: { ...base, qualityLevels: levels, selectedQuality: -1 } });
    const buttons = wrapper.findAll('[role="menuitemradio"]');
    await buttons[2].trigger("click");
    await buttons[0].trigger("click");
    expect(wrapper.emitted("quality")).toEqual([[1], [-1]]);
  });
});

describe("TracksMenu subtitle timing", () => {
  const shown = { ...base, captionChoices: [{ index: 0, label: "English" }], activeCaption: 0 };

  it("is offered only while a subtitle is showing", () => {
    expect(mount(TracksMenu, { props: { ...shown, subtitleDelay: 0 } }).text()).toContain("Subtitle timing");
    expect(mount(TracksMenu, { props: { ...shown, activeCaption: null, subtitleDelay: 0 } }).text()).not.toContain("Subtitle timing");
    expect(mount(TracksMenu, { props: { ...base, subtitleDelay: 0 } }).text()).not.toContain("Subtitle timing");
  });

  it("shows the delay and emits steps, and a reset", async () => {
    const wrapper = mount(TracksMenu, { props: { ...shown, subtitleDelay: 1.5 } });
    expect(wrapper.find(".player-delay-value").text()).toBe("+1.5 s");
    await wrapper.find("[aria-label='Show subtitles later']").trigger("click");
    await wrapper.find("[aria-label='Show subtitles earlier']").trigger("click");
    await wrapper.find(".player-delay-value").trigger("click");
    expect(wrapper.emitted("delay")).toEqual([[0.5], [-0.5], [null]]);
    expect(mount(TracksMenu, { props: { ...shown, subtitleDelay: 0 } }).find(".player-delay-value").text()).toBe("On time");
    expect(mount(TracksMenu, { props: { ...shown, subtitleDelay: -0.25 } }).find(".player-delay-value").text()).toBe("−0.25 s");
  });
});
