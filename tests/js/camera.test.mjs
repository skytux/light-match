// The rough camera calibration's pure functions, against simulated lamps and cameras.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const source = readFileSync(new URL("../../custom_components/light_match/frontend/light-match-panel.js", import.meta.url), "utf8");
const ctx = vm.createContext({
  HTMLElement: class {},
  customElements: { get: () => undefined, define: () => {} },
  localStorage: { getItem: () => null, setItem: () => {} },
  crypto: globalThis.crypto,
});
vm.runInContext(source, ctx);
const {
  srgbToLinear, regionStats, camToXyY, xyToLinear, cameraError, cameraStep, runCameraLoop, exposureFactor,
  relativeToAnchor, nextExposure, lockPlan, fitMatrix, hsToXy, cctOf, linearToSrgb8,
} = ctx;

const near = (a, b, tol, msg) => assert.ok(Math.abs(a - b) <= tol, `${msg ?? ""} ${a} vs ${b}`);
const mul = (m, v) => [0, 1, 2].map((i) => m[i][0] * v[0] + m[i][1] * v[1] + m[i][2] * v[2]);

// A camera that is not quite sRGB: a mild cross-talk matrix.
const CAMERA = [[0.9, 0.12, 0.02], [0.05, 0.92, 0.06], [0.01, 0.1, 0.85]];
const see = (lin, k = 1) => camToXyY(mul(CAMERA, lin).map((c) => c * k));

// An RGB lamp with its own gamma and a brightness that scales linearly.
const rgbLamp = (cmd) => cmd.rgb_color.map((c) => Math.pow(c / 255, 2.2) * (cmd.brightness / 255));

// Planckian locus (Kim et al.), for a colour-temperature lamp.
function planck(T) {
  const x = T <= 4000
    ? -0.2661239e9 / T ** 3 - 0.2343589e6 / T ** 2 + 0.8776956e3 / T + 0.17991
    : -3.0258469e9 / T ** 3 + 2.1070379e6 / T ** 2 + 0.2226347e3 / T + 0.24039;
  const y = T <= 2222
    ? -1.1063814 * x ** 3 - 1.3481102 * x ** 2 + 2.18555832 * x - 0.20219683
    : T <= 4000
      ? -0.9549476 * x ** 3 - 1.37418593 * x ** 2 + 2.09137015 * x - 0.16748867
      : 3.081758 * x ** 3 - 5.8733867 * x ** 2 + 3.75112997 * x - 0.37001483;
  return [x, y];
}

test("sRGB decoding and region averages leave out clipped pixels", () => {
  assert.equal(srgbToLinear(0), 0);
  near(srgbToLinear(255), 1, 1e-9);
  near(srgbToLinear(128), 0.2158, 1e-3);
  assert.equal(linearToSrgb8(srgbToLinear(77)), 77);
  // 4x2 image: left half mid grey, right half one clipped pixel and one dark
  const w = 4, px = [[128, 128, 128], [128, 128, 128], [255, 10, 10], [20, 20, 20], [128, 128, 128], [128, 128, 128], [20, 20, 20], [20, 20, 20]];
  const data = new Uint8ClampedArray(px.flatMap((p) => [...p, 255]));
  const left = regionStats(data, w, { x: 0, y: 0, w: 2, h: 2 });
  assert.equal(left.n, 4);
  near(left.rgb[0], srgbToLinear(128), 1e-9);
  const right = regionStats(data, w, { x: 2, y: 0, w: 2, h: 2 });
  assert.equal(right.clipped, 1);
  assert.equal(right.n, 3);
  near(right.clipFraction, 0.25, 1e-9);
  near(right.rgb[0], srgbToLinear(20), 1e-9); // the clipped red is not averaged in
  // a rectangle partly outside the image is cut
  assert.equal(regionStats(data, w, { x: 3, y: 1, w: 5, h: 5 }).total, 1);
});

test("camera RGB to chromaticity: sRGB white is D65", () => {
  const w = camToXyY([1, 1, 1]);
  near(w.x, 0.3127, 0.001);
  near(w.y, 0.329, 0.001);
  near(w.Y, 1, 0.001);
  const back = camToXyY(xyToLinear([0.5, 0.4], 0.3));
  near(back.x, 0.5, 0.002);
  near(back.Y, 0.3, 0.002);
  near(cctOf(planck(2700)), 2700, 40);
  near(cctOf(planck(6500)), 6500, 120);
});

test("an RGB lamp converges on a reference lamp seen in the same frame", async () => {
  const target = { x: 0.52, y: 0.4 };
  const refLin = xyToLinear([target.x, target.y], 0.18);
  let cmd = { brightness: 128, rgb_color: [255, 255, 255] };
  const res = await runCameraLoop({
    command: cmd,
    send: async (c) => (cmd = c),
    measure: async () => ({ meas: see(rgbLamp(cmd)), target: see(refLin) }),
  });
  assert.ok(res.converged, JSON.stringify(res));
  assert.ok(res.steps <= 10);
  const truth = camToXyY(rgbLamp(res.command));
  near(truth.x, target.x, 0.02, "x");
  near(truth.Y / 0.18, 1, 0.12, "Y");
});

test("hue/saturation and colour temperature lamps converge", async () => {
  // hs lamp
  const hsLamp = (c) => xyToLinear(hsToXy(c.hs_color), c.brightness / 255);
  const goal = { x: 0.33, y: 0.5 };
  let cmd = { brightness: 200, hs_color: [60, 40] };
  let res = await runCameraLoop({
    command: cmd, send: async (c) => (cmd = c),
    measure: async () => ({ meas: camToXyY(hsLamp(cmd)), target: { ...goal, Y: 0.3 } }),
    maxSteps: 12,
  });
  assert.ok(res.converged, JSON.stringify(res));
  // colour temperature lamp
  const ctLamp = (c) => xyToLinear(planck(c.color_temp_kelvin), (c.brightness / 255) ** 2);
  const want = planck(2700);
  cmd = { brightness: 255, color_temp_kelvin: 5000 };
  res = await runCameraLoop({
    command: cmd, send: async (c) => (cmd = c),
    measure: async () => ({ meas: camToXyY(ctLamp(cmd)), target: { x: want[0], y: want[1], Y: 0.25 } }),
    opts: { minKelvin: 2200, maxKelvin: 6500 }, maxSteps: 12,
  });
  assert.ok(res.converged, JSON.stringify(res));
  near(res.command.color_temp_kelvin, 2700, 150);
});

test("the loop stops when asked and reports a stuck lamp", async () => {
  let n = 0;
  const res = await runCameraLoop({
    command: { brightness: 10, rgb_color: [255, 0, 0] }, send: async () => n++,
    measure: async () => ({ meas: { x: 0.3, y: 0.3, Y: 1 }, target: { x: 0.3, y: 0.3, Y: 2 } }),
    stop: () => n >= 2,
  });
  assert.ok(res.stopped && !res.converged);
  const stuck = await runCameraLoop({
    command: { brightness: 255, rgb_color: [255, 0, 0] }, send: async () => {},
    measure: async () => ({ meas: { x: 0.64, y: 0.33, Y: 0.1 }, target: { x: 0.64, y: 0.33, Y: 1 } }),
  });
  assert.ok(stuck.stuck, JSON.stringify(stuck)); // already at full brightness
});

test("absolute mode: brightness relative to the 100 % anchor, rescaled across exposures", async () => {
  const e1 = { exposureTime: 100, iso: 100 };
  const e2 = { exposureTime: 400, iso: 100 }; // 4x more light for a dim cell
  assert.equal(exposureFactor(e1, e2), 0.25);
  assert.equal(exposureFactor({ exposureTime: 100, iso: 200 }, { exposureTime: 200, iso: 100 }), 1);
  const anchor = { Y: 0.5, exposure: e1 };
  assert.equal(relativeToAnchor(0.5, anchor), 1);
  assert.equal(relativeToAnchor(0.2, anchor, e2), 0.1); // 0.2 at 4x exposure is 0.05 at e1
  assert.equal(relativeToAnchor(0.2, null), 0);

  // the lamp's 100 % white measured first; then a 5 % orange cell at a longer exposure
  // a corrected camera (see the matrix test); its unknown scale cancels out through the anchor
  const seeCorrected = (lin, k = 1) => camToXyY(lin.map((c) => c * k));
  const camK = 0.8; // unknown camera scale: cancels out through the anchor
  const white = seeCorrected(rgbLamp({ brightness: 255, rgb_color: [255, 255, 255] }), camK);
  const anchored = { Y: white.Y, exposure: e1 };
  const target = { x: 0.55, y: 0.4, Y: 0.05 };
  let cmd = { brightness: 40, rgb_color: [255, 255, 255] };
  const res = await runCameraLoop({
    command: cmd, send: async (c) => (cmd = c), maxSteps: 12,
    measure: async () => {
      const m = seeCorrected(rgbLamp(cmd), camK * 4);
      return { meas: { ...m, Y: relativeToAnchor(m.Y, anchored, e2) }, target };
    },
  });
  assert.ok(res.converged, JSON.stringify(res));
  const truthWhite = seeCorrected(rgbLamp({ brightness: 255, rgb_color: [255, 255, 255] })).Y;
  near(seeCorrected(rgbLamp(res.command)).Y / truthWhite, 0.05, 0.006, "relative output");
});

test("exposure: halve when clipping, double when too dark", () => {
  const ok = { clipFraction: 0, rgb: [0.3, 0.3, 0.3] };
  assert.equal(nextExposure([ok, { clipFraction: 0.2, rgb: [0.9, 0.9, 0.9] }], { exposureTime: 100 }), 50);
  assert.equal(nextExposure([{ clipFraction: 0, rgb: [0.01, 0.01, 0.01] }, { clipFraction: 0, rgb: [0.005, 0, 0] }], { exposureTime: 100 }), 200);
  assert.equal(nextExposure([ok, ok], { exposureTime: 100 }), null);
  assert.equal(nextExposure([{ clipFraction: 0.5, rgb: [1, 1, 1] }], { exposureTime: 10 }, { min: 8 }), null);
});

test("what gets locked depends on the camera's capabilities", () => {
  const android = {
    whiteBalanceMode: ["continuous", "manual"], colorTemperature: { min: 2850, max: 7000, step: 50 },
    exposureMode: ["continuous", "manual"], exposureTime: { min: 1, max: 1250, step: 1 }, iso: { min: 50, max: 3200, step: 1 },
    focusMode: ["continuous", "manual"], torch: true,
  };
  const plan = lockPlan(android, { exposureTime: 333.3, iso: 400 });
  assert.ok(plan.wb && plan.exposure && plan.focus);
  assert.equal(plan.advanced[0].colorTemperature, 5000);
  assert.equal(plan.advanced[1].exposureTime, 333);
  assert.equal(plan.advanced[1].iso, 400);
  assert.equal(JSON.stringify(plan.advanced.at(-1)), JSON.stringify({ torch: false }));
  const ios = lockPlan({}, {});
  assert.ok(!ios.wb && !ios.exposure && ios.advanced.length === 0);
});

test("a correction matrix from screen patches undoes the camera's cross-talk", () => {
  const patches = [[1, 0, 0], [0, 1, 0], [0, 0, 1], [1, 1, 1]];
  const scale = 0.7; // screen brightness and exposure: only the ratios matter
  const M = fitMatrix(patches.map((p) => [mul(CAMERA, p).map((c) => c * scale), p]));
  assert.ok(M);
  const orange = xyToLinear([0.52, 0.4], 0.2);
  const raw = camToXyY(mul(CAMERA, orange));
  const fixed = camToXyY(mul(CAMERA, orange), M);
  assert.ok(Math.hypot(raw.x - 0.52, raw.y - 0.4) > 0.005, "the camera alone is off");
  near(fixed.x, 0.52, 0.001);
  near(fixed.y, 0.4, 0.001);
  assert.equal(fitMatrix([[[0, 0, 0], [1, 1, 1]]]), null);
});

test("errors: chromaticity distance and brightness ratio", () => {
  const e = cameraError({ x: 0.3, y: 0.3, Y: 1.03 }, { x: 0.303, y: 0.302, Y: 1 });
  assert.ok(e.ok);
  assert.ok(!cameraError({ x: 0.3, y: 0.3, Y: 1.2 }, { x: 0.3, y: 0.3, Y: 1 }).ok);
  const step = cameraStep({ brightness: 100, rgb_color: [255, 128, 0], extra: 1 }, { x: 0.5, y: 0.4, Y: 2 }, { x: 0.5, y: 0.4, Y: 1 });
  assert.ok(step.brightness < 100 && step.extra === 1);
  assert.equal(JSON.stringify(step.rgb_color), "[255,128,0]"); // colour already right: channels untouched
});
