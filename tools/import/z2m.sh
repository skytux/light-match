#!/bin/sh
# Dump zigbee-herdsman-converters (MIT) light definitions to $1/z2m/lights.json. Runs in node:22-alpine.
set -eu
OUT="$(cd "${1:-sources}" && pwd)/z2m"
mkdir -p "$OUT" && cd "$OUT"
[ -f package.json ] || npm init -y >/dev/null
npm install --silent --no-audit --no-fund --ignore-scripts zigbee-herdsman-converters@latest >/dev/null
cp node_modules/zigbee-herdsman-converters/LICENSE ./LICENSE 2>/dev/null || true
node --input-type=module -e '
import * as zhc from "zigbee-herdsman-converters";
import fs from "node:fs";
const pkg = JSON.parse(fs.readFileSync("node_modules/zigbee-herdsman-converters/package.json"));
import { createRequire } from "node:module";
const require = createRequire(process.cwd() + "/");
const dir = "node_modules/zigbee-herdsman-converters/dist/devices";
const defs = [];
for (const file of fs.readdirSync(dir).filter((f) => f.endsWith(".js"))) {
  const mod = require(`${process.cwd()}/${dir}/${file}`);
  for (const d of mod.definitions || mod.default || []) {
    try { defs.push(zhc.prepareDefinition(d)); } catch { /* skip broken definition */ }
  }
}
const out = [];
const flat = (e) => [e, ...((e && e.features) || []).flatMap(flat)];
for (const d of defs) {
  let exposes = d.exposes;
  if (typeof exposes === "function") { try { exposes = exposes(undefined, {}); } catch { continue; } }
  const all = (exposes || []).flatMap(flat);
  const light = (exposes || []).find((e) => e && e.type === "light");
  if (!light) continue;
  const f = flat(light);
  const ct = f.find((x) => x.name === "color_temp");
  out.push({
    model: d.model, vendor: d.vendor, description: d.description,
    zigbeeModel: d.zigbeeModel || [],
    whiteLabel: (d.whiteLabel || []).map((w) => ({ vendor: w.vendor, model: w.model, description: w.description })),
    brightness: f.some((x) => x.name === "brightness"),
    color_temp: ct ? [ct.value_min, ct.value_max] : null,
    color_xy: f.some((x) => x.name === "color_xy"),
    color_hs: f.some((x) => x.name === "color_hs"),
  });
}
fs.writeFileSync("lights.json", JSON.stringify({ version: pkg.version, lights: out }));
console.log(`z2m ${pkg.version}: ${out.length} lights`);
'
