// Panel helpers, run with `node --test tests/js` (no browser: the custom
// element class is defined against stubs and the module-level helpers are read
// from the script's context).
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const source = readFileSync(new URL("../../custom_components/light_match/frontend/light-match-panel.js", import.meta.url), "utf8");
const store = new Map();
const ctx = vm.createContext({
  HTMLElement: class {},
  customElements: { get: () => undefined, define: () => {} },
  localStorage: { getItem: (k) => store.get(k) ?? null, setItem: (k, v) => store.set(k, String(v)) },
  crypto: globalThis.crypto,
  Math,
  Date,
});
vm.runInContext(source, ctx);
const { screenLevel, xyToCss, autoName, fileName, screenFloor, lightness } = ctx;

const D65 = [0.3127, 0.329];
const maxChannel = (css) => Math.max(...css.match(/\d+/g).map(Number));

test("lightness follows CIE L*", () => {
  assert.equal(lightness(0), 0);
  assert.ok(Math.abs(lightness(1) - 100) < 1e-9);
  assert.ok(Math.abs(lightness(0.18) - 49.5) < 0.1);
});

test("a lit lamp never looks black: 1 % starts near the floor, 100 % is screen white", () => {
  const low = screenLevel(0.01);
  assert.ok(low > 0.54 && low < 0.56, String(low));
  assert.equal(screenLevel(1), 1);
  assert.equal(screenLevel(0.01, 0), lightness(0.01) / 100);
  // monotonic
  let last = 0;
  for (const out of [0.001, 0.01, 0.05, 0.2, 0.5, 1]) {
    const v = screenLevel(out);
    assert.ok(v > last);
    last = v;
  }
});

test("the swatch's strongest channel is the screen level", () => {
  assert.equal(xyToCss(D65, 1), "rgb(255, 255, 255)");
  const low = maxChannel(xyToCss(D65, 0.01));
  assert.ok(Math.abs(low - Math.round(255 * screenLevel(0.01))) <= 1, String(low));
  assert.ok(maxChannel(xyToCss(D65, 0.01, 0)) < 30);
  // a saturated red keeps its hue at low output
  const [r, g, b] = xyToCss([0.68, 0.31], 0.05).match(/\d+/g).map(Number);
  assert.ok(r > 150 && g < r / 2 && b < r / 2);
});

test("the screen floor is remembered, with a default", () => {
  assert.equal(screenFloor(), 50);
  store.set("light_match_screen_floor", "30");
  assert.equal(screenFloor(), 30);
  store.set("light_match_screen_floor", "nonsense");
  assert.equal(screenFloor(), 50);
});

test("auto names: make_model_date_random", () => {
  const name = autoName("WiZ", "ESP01 SHRGB/03", new Date(2026, 10, 22));
  assert.match(name, /^WiZ_ESP01_SHRGB_03_2026-11-22_[a-z0-9]{5}$/);
  assert.match(autoName("", "", new Date(2026, 0, 2)), /^profile_2026-01-02_[a-z0-9]{5}$/);
  assert.notEqual(autoName("A", "B"), autoName("A", "B"));
  assert.equal(fileName("My lamp"), "My_lamp.light-match.json");
  assert.equal(fileName(""), "profile.light-match.json");
});

test("the panel renders the export, profile and group cards", async () => {
  let Panel;
  const shadow = { innerHTML: "", querySelector: () => null, querySelectorAll: () => [] };
  const ctx2 = vm.createContext({
    HTMLElement: class {
      attachShadow() {
        this.shadowRoot = shadow;
        return shadow;
      }
    },
    customElements: { get: () => undefined, define: (_n, cls) => (Panel = cls) },
    localStorage: { getItem: () => null, setItem: () => {} },
    crypto: globalThis.crypto,
    structuredClone: globalThis.structuredClone,
  });
  vm.runInContext(source, ctx2);
  const entry = (id, title) => ({
    entry_id: id, title, entity_id: `light.${id}`, make: "Acme", model: "Glow 9", modes: { color: "hs", white: "color_temp" },
    floor: 0, calibrated: 1, total: 2, behaviour: "smart", device: {}, model_profile: null,
  });
  const replies = {
    "light_match/entries": { entries: [entry("a", "Desk"), entry("b", "Hall")] },
    "light_match/settings": { online_update: false, online_profiles: [] },
    "light_match/profiles/browse": {
      mine: [{ id: "m1", name: "Acme_Glow_9_2026-11-22_ab12c", make: "Acme", model: "Glow 9", cells: 3, date: "2026-11-22" }],
      builtin: [{ id: "wiz/x", name: "WiZ X", make: "WiZ", models: ["X"], cells: 5, method: "by_eye" }],
      community: [{ id: "imported/y", name: "Y", make: "Y", models: ["Y1"], cells: 0, method: "imported" }],
    },
    "light_match/groups": { auto: [{ id: "auto:acme:glow9", name: "Acme Glow 9", entry_ids: ["a", "b"], auto: true }], manual: [] },
    "light_match/profile": {
      calibrated: 1, total: 1, quick: [], min_kelvin: 2200, max_kelvin: 6500, modes: { color: "hs", white: "color_temp" },
      cells: [{ key: "color:red:1", kind: "color", name: "red", hue: 0, brightness: 1, source: "user", calibrated: true, mode: "hs",
        command: { brightness: 3, hs_color: [0, 100] }, target: { xy: [0.68, 0.31], relative_output: 0.01 } }],
    },
  };
  const panel = new Panel();
  panel._hass = { callWS: async (msg) => structuredClone(replies[msg.type]), states: {} };
  await panel._init();
  const html = shadow.innerHTML;
  for (const text of ["My calibrations", "Built-in", "Community", "Lamp groups", "Apply to", "Acme Glow 9 (2, same make and model)", "Save to my library", "Screen floor"]) {
    assert.ok(html.includes(text), text);
  }
  assert.match(html, /id="pname" value="Acme_Glow_9_\d{4}-\d\d-\d\d_[a-z0-9]{5}"/);
  assert.ok(html.includes('data-target="a"') && html.includes('data-target="b"'));
  // the current lamp is the default target; the uncalibrated community profile has no apply link
  assert.match(html, /checked> Desk/);
  assert.ok(!html.includes('data-db-apply="imported/y"') && html.includes('data-db-apply="wiz/x"'));
});
