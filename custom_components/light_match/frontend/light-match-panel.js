// Light Match calibration panel. Plain web component, no build step.
// Talks to the integration over the websocket API (light_match/*).

const LEVELS = [1, 5, 20, 50, 100];

// CIE lightness L* (0-100) of a relative luminance (0-1).
function lightness(Y) {
  Y = Math.max(0, Math.min(1, Y));
  return Y > 216 / 24389 ? 116 * Math.cbrt(Y) - 16 : (24389 / 27) * Y;
}

// How bright the screen shows a relative output: the sRGB-encoded value of the
// strongest channel. A lit lamp never looks black, even at 1 %, so the scale
// starts at a floor (default half brightness) and rises with perceived lightness.
function screenLevel(output, floor = 0.5) {
  const f = Math.max(0, Math.min(1, floor));
  return f + ((1 - f) * lightness(output)) / 100;
}

const SCREEN_FLOOR_DEFAULT = 50;

function screenFloor() {
  const v = Number(storageGet("light_match_screen_floor"));
  return storageGet("light_match_screen_floor") !== "" && v >= 0 && v <= 90 ? v : SCREEN_FLOOR_DEFAULT;
}

const decodeSrgb = (v) => (v <= 0.04045 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4));

// CIE xy + relative output -> CSS colour on an sRGB screen (see screenLevel).
function xyToCss(xy, output = 1, floor = 0.5) {
  const [x, y] = xy;
  if (!(y > 0)) return "rgb(0,0,0)";
  const X = x / y, Z = (1 - x - y) / y;
  let r = 3.2406 * X - 1.5372 - 0.4986 * Z;
  let g = -0.9689 * X + 1.8758 + 0.0415 * Z;
  let b = 0.0557 * X - 0.204 + 1.057 * Z;
  r = Math.max(0, r); g = Math.max(0, g); b = Math.max(0, b);
  const m = Math.max(r, g, b) || 1;
  const k = decodeSrgb(screenLevel(output, floor)) / m;
  const enc = (c) => {
    c = Math.min(1, c * k);
    return Math.round(255 * (c <= 0.0031308 ? 12.92 * c : 1.055 * Math.pow(c, 1 / 2.4) - 0.055));
  };
  return `rgb(${enc(r)}, ${enc(g)}, ${enc(b)})`;
}
function storageGet(key) {
  try {
    return localStorage.getItem(key) || "";
  } catch (e) {
    return "";
  }
}

function storageSet(key, value) {
  try {
    localStorage.setItem(key, value);
  } catch (e) {
    /* private mode: nothing remembered */
  }
}

function slug(value) {
  return String(value || "").replace(/[^A-Za-z0-9.-]+/g, "_").replace(/^[_.]+|[_.]+$/g, "");
}

// <make>_<model>_<YYYY-MM-DD>_<random>, like the integration names exports.
function autoName(make, model, day = new Date()) {
  const parts = [slug(make), slug(model)].filter(Boolean);
  if (!parts.length) parts.push("profile");
  const pad = (n) => String(n).padStart(2, "0");
  const date = `${day.getFullYear()}-${pad(day.getMonth() + 1)}-${pad(day.getDate())}`;
  const chars = "abcdefghijklmnopqrstuvwxyz0123456789";
  let token = "";
  for (const b of crypto.getRandomValues(new Uint8Array(5))) token += chars[b % chars.length];
  return [...parts, date, token].join("_").slice(0, 120);
}

function fileName(name) {
  return `${slug(name) || "profile"}.light-match.json`;
}

function download(filename, data) {
  const blob = new Blob([JSON.stringify(data, null, 2) + "\n"], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

const SOURCE_LABEL = { user: "calibrated", model: "from the model profile", default: "default", inferred: "inferred" };
const METHOD_LABEL = { spec: "spec-based", by_eye: "calibrated by eye", lux_meter: "lux meter", colorimeter: "colorimeter", imported: "imported (partly calibrated, unverified)" };
const CHANNEL_NAMES = {
  rgb: ["Red", "Green", "Blue"],
  rgbw: ["Red", "Green", "Blue", "White"],
  rgbww: ["Red", "Green", "Blue", "Cold white", "Warm white"],
};
const MODE_ATTR = {
  rgb: "rgb_color",
  rgbw: "rgbw_color",
  rgbww: "rgbww_color",
  hs: "hs_color",
  color_temp: "color_temp_kelvin",
  brightness: null,
};

const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

class LightMatchPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._entries = [];
    this._entryId = null;
    this._reference = "";
    this._profile = null;
    this._index = 0;
    this._command = null;
    this._autoShow = true;
    this._message = "";
    this._previewTimer = null;
    this._loaded = false;
    this._quick = storageGet("light_match_quick") === "1";
    this._screenFloor = screenFloor();
    this._targets = new Set();
    this._browse = null;
    this._groups = null;
    this._filter = "";
    this._applyResults = null;
  }

  _order() {
    const cells = this._profile ? this._profile.cells : [];
    if (this._quick && this._profile.quick && this._profile.quick.length) {
      return this._profile.quick.map((k) => cells.findIndex((c) => c.key === k)).filter((i) => i >= 0);
    }
    return cells.map((_, i) => i);
  }

  _step(delta) {
    const order = this._order();
    const pos = order.indexOf(this._index);
    if (pos < 0) return this._goto(order[delta > 0 ? 0 : order.length - 1] ?? 0);
    const next = order[pos + delta];
    if (next === undefined) {
      if (delta > 0 && this._quick) this._flash("Quick calibration done: the other cells are inferred from these.");
      return this._goto(this._index);
    }
    return this._goto(next);
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (first) this._init();
  }

  get hass() {
    return this._hass;
  }

  set narrow(value) {
    this._narrow = value;
    const btn = this.shadowRoot.querySelector("ha-menu-button");
    if (btn) btn.narrow = value;
  }

  async _ws(type, data = {}) {
    return this._hass.callWS({ type: `light_match/${type}`, ...data });
  }

  async _init() {
    try {
      const res = await this._ws("entries");
      this._entries = res.entries;
      try {
        this._settings = await this._ws("settings");
      } catch (err) {
        this._settings = null;
      }
      await this._loadLibrary();
      if (this._entries.length) {
        await this._selectEntry(this._entries[0].entry_id);
      }
    } catch (err) {
      this._message = `Could not load: ${err.message || err}`;
    }
    this._loaded = true;
    this._render();
  }

  get _entry() {
    return this._entries.find((e) => e.entry_id === this._entryId);
  }

  get _cell() {
    return this._profile ? this._profile.cells[this._index] : null;
  }

  _defaultReference(entry) {
    if (entry.reference_entity) return entry.reference_entity;
    const ref = this._entries.find((e) => e.behaviour === "reference" && e.entry_id !== entry.entry_id);
    return ref && ref.entity_id ? ref.entity_id : "";
  }

  async _selectEntry(entryId) {
    const previous = this._entryId;
    this._entryId = entryId;
    this._jsonText = "";
    const entry = this._entry;
    this._make = entry.make || "";
    this._model = entry.model || "";
    this._name = autoName(this._make, this._model);
    this._nameEdited = false;
    if (this._targets.size === 0 || (this._targets.size === 1 && this._targets.has(previous))) this._targets = new Set([entryId]);
    this._reference = this._defaultReference(entry);
    await this._loadProfile();
    const order = this._order();
    const firstOpen = order.find((i) => !this._profile.cells[i].calibrated);
    this._index = firstOpen ?? order[0] ?? 0;
    this._loadCell();
  }

  async _loadProfile() {
    this._profile = await this._ws("profile", { entry_id: this._entryId });
    const summary = this._entries.find((e) => e.entry_id === this._entryId);
    if (summary) Object.assign(summary, { calibrated: this._profile.calibrated, total: this._profile.total });
  }

  async _goto(index, show = this._autoShow) {
    if (!this._profile) return;
    const count = this._profile.cells.length;
    this._index = ((index % count) + count) % count;
    this._loadCell();
    this._message = "";
    this._render();
    if (show) {
      await this._showTarget();
      await this._preview(true);
    }
  }

  // The cell's command and the mode it is edited in (a white can use the colour LEDs).
  _loadCell() {
    const cell = this._cell;
    this._mode = cell ? cell.mode : null;
    this._command = cell ? structuredClone(cell.command) : null;
  }

  // The colour mode a white cell can switch to, if it has one.
  _colourMode(cell) {
    return cell && cell.kind === "white" && cell.modes && cell.modes.length > 1
      ? cell.modes.find((m) => m !== "color_temp") || null
      : null;
  }

  _setColourMode(on) {
    const cell = this._cell;
    const colour = this._colourMode(cell);
    if (!colour) return;
    this._mode = on ? colour : "color_temp";
    this._command = structuredClone(cell.commands[this._mode]);
    this._render();
    this._preview(true);
  }

  _colourToggle(cell) {
    const colour = this._colourMode(cell);
    if (!colour) return "";
    const choice = this._entry && this._entry.white_color;
    const preset =
      cell.default_mode === colour
        ? choice === "always"
          ? "preset: colour LEDs for every white (lamp option)"
          : "preset: colour LEDs below the brightness floor (lamp option)"
        : "preset: colour temperature (lamp option)";
    return `<label class="toggle"><input type="checkbox" id="colourmode" ${this._mode === colour ? "checked" : ""}>
      Calibrate in colour mode <span class="hint">(${esc(colour)} channels, can go dimmer than the floor; ${esc(preset)})</span></label>`;
  }

  async _showTarget() {
    if (!this._reference || !this._cell) return;
    try {
      await this._ws("show_target", { entity_id: this._reference, key: this._cell.key });
    } catch (err) {
      this._flash(`Reference: ${err.message || err}`);
    }
  }

  _schedulePreview() {
    clearTimeout(this._previewTimer);
    this._previewTimer = setTimeout(() => this._preview(), 150);
  }

  async _preview(now = false) {
    if (now) clearTimeout(this._previewTimer);
    if (!this._command) return;
    try {
      await this._ws("preview", { entry_id: this._entryId, command: this._command });
    } catch (err) {
      this._flash(`Preview: ${err.message || err}`);
    }
  }

  async _save() {
    const cell = this._cell;
    try {
      const mode = this._colourMode(cell) ? { mode: this._mode } : {};
      await this._ws("cell/set", { entry_id: this._entryId, key: cell.key, command: this._command, ...mode });
      await this._loadProfile();
      this._flash(`Saved ${this._label(cell)}.`);
      await this._step(1);
    } catch (err) {
      this._flash(`Not saved: ${err.message || err}`);
    }
  }

  async _copyModel(key) {
    try {
      const res = await this._ws("cell/copy_model", { entry_id: this._entryId, key });
      await this._loadProfile();
      await this._goto(this._index, false);
      this._flash(`Copied ${res.copied} cell${res.copied === 1 ? "" : "s"} from the model profile.`);
    } catch (err) {
      this._flash(`Not copied: ${err.message || err}`);
    }
  }

  async _reset(all = false) {
    if (all && !confirm("Forget every calibrated cell of this lamp?")) return;
    const key = all ? "*" : this._cell.key;
    await this._ws("cell/reset", { entry_id: this._entryId, key });
    await this._loadProfile();
    await this._goto(this._index);
    this._flash(all ? "All cells reset." : "Cell reset to its default.");
  }

  // Phone browsers and the companion app ignore blob downloads: sign the HTTP
  // path and follow it (as Home Assistant does for backups); blob as fallback.
  async _downloadSigned(path, filename, getData) {
    let signed = null;
    try {
      signed = (await this._hass.callWS({ type: "auth/sign_path", path, expires: 60 })).path;
    } catch (err) {
      signed = null;
    }
    if (signed) {
      const a = document.createElement("a");
      a.href = signed;
      a.download = filename;
      a.rel = "noopener";
      document.body.appendChild(a);
      a.click();
      a.remove();
      return;
    }
    download(filename, await getData());
  }

  // Library, browser sections and groups; absent until Home Assistant has
  // loaded a version with them (the panel still works without).
  async _loadLibrary() {
    try {
      this._browse = await this._ws("profiles/browse");
    } catch (err) {
      this._browse = null;
    }
    try {
      this._groups = await this._ws("groups");
    } catch (err) {
      this._groups = null;
    }
  }

  _exportArgs() {
    const out = {};
    for (const key of ["make", "model", "name"]) {
      const v = String(this[`_${key}`] || "").trim();
      if (v) out[key] = v;
    }
    return out;
  }

  async _export() {
    const args = this._exportArgs();
    const query = new URLSearchParams(args).toString();
    try {
      await this._downloadSigned(`/api/light_match/export/${this._entryId}${query ? `?${query}` : ""}`, fileName(args.name), async () =>
        (await this._ws("profile/export", { entry_id: this._entryId, ...args })).profile
      );
    } catch (err) {
      this._flash(`Export failed: ${err.message || err}`);
    }
  }

  async _showJson(profile) {
    const text = JSON.stringify(profile, null, 2) + "\n";
    try {
      await navigator.clipboard.writeText(text);
      this._jsonText = "";
      this._render();
      this._flash("Profile JSON copied to the clipboard.");
      return;
    } catch (err) {
      // no clipboard (plain http, some WebViews): show it to copy by hand
    }
    this._jsonText = text;
    this._render();
    const area = this.shadowRoot.querySelector("#jsonout");
    if (area) {
      area.focus();
      area.select();
    }
    this._flash("Copy the JSON below (the clipboard is not available here).");
  }

  async _copyJson() {
    try {
      const res = await this._ws("profile/export", { entry_id: this._entryId, ...this._exportArgs() });
      await this._showJson(res.profile);
    } catch (err) {
      this._flash(`Copy failed: ${err.message || err}`);
    }
  }

  async _saveToLibrary() {
    try {
      const res = await this._ws("library/save", { entry_id: this._entryId, ...this._exportArgs() });
      await this._loadLibrary();
      this._name = autoName(this._make, this._model);
      this._nameEdited = false;
      this._render();
      this._flash(`Saved to My calibrations as ${res.profile.name}.`);
    } catch (err) {
      this._flash(`Not saved: ${err.message || err}`);
    }
  }

  _targetIds() {
    return [...this._targets].filter((id) => this._entries.some((e) => e.entry_id === id));
  }

  _title(entryId) {
    const e = this._entries.find((x) => x.entry_id === entryId);
    return e ? e.title : entryId;
  }

  // Apply a profile (source: {profile} | {from_entry_id} | {library_id} | {db_id})
  // to every chosen lamp, each converted for its own capabilities.
  async _apply(source, label) {
    const ids = this._targetIds();
    if (!ids.length) {
      this._flash("Choose at least one lamp to apply to.");
      return false;
    }
    const merge = Boolean(this._merge);
    if (!merge && !confirm(`Replace every calibrated cell of ${ids.map((id) => this._title(id)).join(", ")} with ${label}?`)) return false;
    try {
      const res = await this._ws("profile/apply", { entry_ids: ids, merge, ...source });
      this._applyResults = { label, results: res.results };
      await this._reload();
      await this._goto(this._index, false);
      const ok = res.results.filter((r) => !r.error).length;
      this._flash(`Applied to ${ok} of ${res.results.length} lamp${res.results.length === 1 ? "" : "s"}.`);
      return true;
    } catch (err) {
      this._flash(`Not applied: ${err.message || err}`);
      return false;
    }
  }

  async _copyFrom() {
    const from = this._copyFromId;
    if (!this._entries.some((e) => e.entry_id === from)) return;
    await this._apply({ from_entry_id: from }, `the calibration of ${this._title(from)}`);
  }

  _targetPicker() {
    const box = (id, label, checked, extra = "") =>
      `<label class="check"><input type="checkbox" ${extra} ${checked ? "checked" : ""}> ${esc(label)}</label>`;
    const lamps = this._entries
      .map((e) => box(e.entry_id, e.title, this._targets.has(e.entry_id), `data-target="${esc(e.entry_id)}"`))
      .join("");
    const groups = this._groups ? [...this._groups.auto, ...this._groups.manual] : [];
    const groupBoxes = groups
      .map((g) => {
        const ids = g.entry_ids.filter((id) => this._entries.some((e) => e.entry_id === id));
        if (!ids.length) return "";
        const all = ids.every((id) => this._targets.has(id));
        return box(g.id, `${g.name} (${ids.length}${g.auto ? ", same make and model" : ""})`, all, `data-group="${esc(g.id)}"`);
      })
      .join("");
    return `<div class="targets">
        <p class="meta">Apply to</p>
        <div class="checks">${lamps}</div>
        ${groupBoxes ? `<p class="meta">Groups</p><div class="checks">${groupBoxes}</div>` : ""}
        <label class="check"><input type="checkbox" id="merge" ${this._merge ? "checked" : ""}> merge with the lamps' existing cells (otherwise replace them)</label>
      </div>`;
  }

  _applyResultsList() {
    const r = this._applyResults;
    if (!r) return "";
    return `<p class="meta">Applied ${esc(r.label)}:</p><ul class="results">${r.results
      .map((x) => `<li>${esc(x.title || x.entry_id)}: ${x.error ? `<span class="bad">not changed (${esc(x.error)})</span>` : `${x.imported} cells, ${x.skipped} skipped`}</li>`)
      .join("")}</ul>`;
  }

  _filesCard() {
    const others = this._entries;
    if (!others.some((e) => e.entry_id === this._copyFromId)) {
      const other = others.find((e) => e.entry_id !== this._entryId);
      this._copyFromId = other ? other.entry_id : null;
    }
    const lib = Boolean(this._browse);
    return `<div class="card files">
      <h2>Export and save</h2>
      <div class="pickers">
        <label>Make<input id="make" value="${esc(this._make || "")}" maxlength="100" placeholder="manufacturer"></label>
        <label>Model<input id="model" value="${esc(this._model || "")}" maxlength="100" placeholder="model"></label>
      </div>
      <label>Profile name<input id="pname" value="${esc(this._name || "")}" maxlength="120"></label>
      <div class="links">
        <a href="#" id="export">Export profile</a> ·
        <a href="#" id="copyjson">Copy JSON</a> ·
        <a href="#" id="share">Share profile</a>${lib ? ` · <a href="#" id="savelib">Save to my library</a>` : ""}
      </div>
      ${this._jsonText ? `<textarea id="jsonout" readonly rows="8">${esc(this._jsonText)}</textarea>` : ""}
      <h2 class="sub">Import and copy</h2>
      ${lib ? this._targetPicker() : `<p class="meta">Applies to this lamp. Restart Home Assistant to apply to several lamps.</p><label class="check"><input type="checkbox" id="merge" ${this._merge ? "checked" : ""}> merge with existing cells</label>`}
      <div class="links">
        <label class="file">Import profile file <input type="file" id="import" accept=".json,application/json"></label> ·
        <a href="#" id="pastetoggle">Paste JSON</a>
        ${lib ? `<label class="check"><input type="checkbox" id="keeplib" ${this._keepLib ? "checked" : ""}> also save imported profiles to my library</label>` : ""}
      </div>
      ${
        this._pasteOpen
          ? `<textarea id="jsonin" rows="8" placeholder="Paste an exported Light Match profile here">${esc(this._pasteText || "")}</textarea>
        <div class="buttons"><button id="pastego" class="primary">Import</button></div>`
          : ""
      }
      ${
        lib && others.length > 1
          ? `<div class="copyfrom">
        <label>Copy calibration from<select id="copyfrom">${others
          .map((e) => `<option value="${esc(e.entry_id)}" ${e.entry_id === this._copyFromId ? "selected" : ""}>${esc(e.title)}</option>`)
          .join("")}</select></label>
        <button id="copygo">Copy</button>
      </div>`
          : ""
      }
      ${this._applyResultsList()}
      <p class="message">${esc(this._message)}</p>
    </div>`;
  }

  _matches(item) {
    const q = this._filter.trim().toLowerCase();
    if (!q) return true;
    const hay = [item.name, item.make, item.model, ...(item.models || []), item.id].filter(Boolean).join(" ").toLowerCase();
    return q.split(/\s+/).every((w) => hay.includes(w));
  }

  _section(title, hint, items, row) {
    const shown = items.filter((i) => this._matches(i));
    const LIMIT = 30;
    return `<h3>${esc(title)} <small>${shown.length}${shown.length !== items.length ? ` of ${items.length}` : ""}</small></h3>
      <p class="hint">${hint}</p>
      ${shown.length ? `<ul class="plist">${shown.slice(0, LIMIT).map(row).join("")}</ul>` : `<p class="meta">None${items.length ? " match the filter" : ""}.</p>`}
      ${shown.length > LIMIT ? `<p class="meta">${shown.length - LIMIT} more: type in the filter to narrow.</p>` : ""}`;
  }

  _libraryCard() {
    const b = this._browse;
    const what = (i) => [i.make, i.model].filter(Boolean).join(" ");
    const mine = (i) => `<li><b>${esc(i.name)}</b> <span class="meta">${esc(what(i))}${what(i) ? " · " : ""}${i.cells} cells · ${esc(i.date || "")}${i.source ? ` · from ${esc(i.source)}` : ""}</span>
        <span class="acts"><a href="#" data-lib-apply="${esc(i.id)}">Apply to chosen lamps</a> · <a href="#" data-lib-export="${esc(i.id)}">Export</a> · <a href="#" data-lib-json="${esc(i.id)}">Copy JSON</a> · <a href="#" data-lib-rename="${esc(i.id)}">Rename</a> · <a href="#" data-lib-delete="${esc(i.id)}">Delete</a></span></li>`;
    const db = (i) => `<li><b>${esc(i.name)}</b> <span class="meta">${esc(i.make || "")} ${esc((i.models || []).join(", "))} · ${i.cells} cells · ${esc(METHOD_LABEL[i.method] || i.method || "")}${i.verified ? ", verified" : ""}${i.origin === "online" ? ", online" : ""}</span>
        ${i.cells ? `<span class="acts"><a href="#" data-db-apply="${esc(i.id)}">Apply to chosen lamps</a></span>` : ""}</li>`;
    return `<div class="card">
      <h2>Profiles</h2>
      <label>Filter by make or model<input id="filter" value="${esc(this._filter)}" placeholder="e.g. wiz, hue, ESP01"></label>
      ${this._section("My calibrations", "Your own saved calibrations, kept on this Home Assistant.", b.mine, mine)}
      ${this._section("Built-in", "Calibrated profiles shipped with Light Match.", b.builtin, db)}
      ${this._section("Community", "Imported public lamp data and profiles from the online database.", b.community, db)}
      <p class="message">${esc(this._message)}</p>
    </div>`;
  }

  _groupsCard() {
    const g = this._groups;
    const names = (ids) => ids.map((id) => this._title(id)).join(", ") || "no lamps";
    const manual = g.manual
      .map((x) => `<li><b>${esc(x.name)}</b> <span class="meta">${esc(names(x.entry_ids))}</span>
        <span class="acts"><a href="#" data-grp-set="${esc(x.id)}">Set to the chosen lamps</a> · <a href="#" data-grp-rename="${esc(x.id)}">Rename</a> · <a href="#" data-grp-delete="${esc(x.id)}">Delete</a></span></li>`)
      .join("");
    const auto = g.auto.map((x) => `<li><b>${esc(x.name)}</b> <span class="meta">${esc(names(x.entry_ids))}</span></li>`).join("");
    return `<div class="card">
      <h2>Lamp groups</h2>
      <p class="hint">Groups pick several lamps at once under "Apply to". Lamps of the same make and model are grouped automatically, from the device registry, and the groups update by themselves.</p>
      ${auto ? `<h3>Same make and model</h3><ul class="plist">${auto}</ul>` : ""}
      <h3>My groups</h3>
      ${manual ? `<ul class="plist">${manual}</ul>` : `<p class="meta">None yet.</p>`}
      <div class="copyfrom">
        <label>New group from the chosen lamps<input id="grpname" maxlength="60" placeholder="group name"></label>
        <button id="grpadd">Create</button>
      </div>
    </div>`;
  }

  async _libAction(kind, id) {
    try {
      if (kind === "apply") {
        const item = this._browse.mine.find((i) => i.id === id);
        await this._apply({ library_id: id }, `the saved profile ${item ? item.name : ""}`);
      } else if (kind === "db") {
        const item = [...this._browse.builtin, ...this._browse.community].find((i) => i.id === id);
        await this._apply({ db_id: id }, `the profile ${item ? item.name : id}`);
      } else if (kind === "export") {
        const item = this._browse.mine.find((i) => i.id === id);
        await this._downloadSigned(`/api/light_match/library/${id}`, fileName(item && item.name), async () =>
          (await this._ws("library/get", { item_id: id })).profile
        );
      } else if (kind === "json") {
        await this._showJson((await this._ws("library/get", { item_id: id })).profile);
      } else if (kind === "rename") {
        const item = this._browse.mine.find((i) => i.id === id);
        const name = prompt("New name:", item ? item.name : "");
        if (!name || !name.trim()) return;
        await this._ws("library/rename", { item_id: id, name: name.trim() });
        await this._loadLibrary();
        this._render();
      } else if (kind === "delete") {
        const item = this._browse.mine.find((i) => i.id === id);
        if (!confirm(`Delete ${item ? item.name : "this profile"} from My calibrations? Lamps keep their cells.`)) return;
        await this._ws("library/delete", { item_id: id });
        await this._loadLibrary();
        this._render();
      }
    } catch (err) {
      this._flash(`Not done: ${err.message || err}`);
    }
  }

  async _groupAction(kind, id) {
    try {
      const group = this._groups.manual.find((x) => x.id === id);
      if (kind === "add") {
        const name = String(this.shadowRoot.querySelector("#grpname")?.value || "").trim();
        if (!name) return this._flash("Give the group a name.");
        this._groups = await this._ws("groups/set", { name, entry_ids: this._targetIds() });
      } else if (kind === "set" && group) {
        this._groups = await this._ws("groups/set", { group_id: id, name: group.name, entry_ids: this._targetIds() });
      } else if (kind === "rename" && group) {
        const name = prompt("New group name:", group.name);
        if (!name || !name.trim()) return;
        this._groups = await this._ws("groups/set", { group_id: id, name: name.trim(), entry_ids: group.entry_ids });
      } else if (kind === "delete" && group) {
        if (!confirm(`Delete the group ${group.name}? The lamps stay as they are.`)) return;
        this._groups = await this._ws("groups/delete", { group_id: id });
      }
      this._render();
    } catch (err) {
      this._flash(`Not done: ${err.message || err}`);
    }
  }

  _shareCard() {
    const nick = esc(storageGet("light_match_nickname"));
    const gh = esc(storageGet("light_match_github"));
    const r = this._shareResult;
    let result = "";
    if (r) {
      const where = r.target === "lm-ref-1"
        ? "Calibrated against a reference lamp (Hue gamut C), so it can go into the shared database."
        : "<b>Calibrated against a lamp of your own (target \"house\")</b>: it works in your home, but the database only takes profiles matched to a reference lamp (a Hue colour lamp with gamut C). Choose such a lamp as the reference and calibrate again to share it.";
      result = `<p class="meta">${r.cells} cells · ${where}</p>
        <p class="meta">${r.json_in_url ? "The profile is filled into the issue." : `The profile is too long for the link: attach the downloaded file ${esc(r.filename)} to the issue.`}</p>
        <div class="buttons"><a class="button primary" href="${esc(r.url)}" target="_blank" rel="noopener">Open the GitHub issue</a></div>`;
    }
    return `<div class="card">
      <h2>Share this profile</h2>
      <p class="hint">Creates the profile file with your own cells and the lamp's make and model, and opens a prefilled GitHub issue to add it to the lamp database. Nothing is sent until you submit the issue on GitHub yourself.</p>
      <div class="pickers">
        <label>Nickname (optional, <b>public</b>)<input id="nick" value="${nick}" maxlength="40" placeholder="how to credit you"></label>
        <label>GitHub name (optional, public)<input id="gh" value="${gh}" maxlength="39"></label>
      </div>
      <label>Notes (optional, public)<input id="notes" maxlength="300" placeholder="e.g. bulb bought 2024, compared in daylight"></label>
      <p class="meta">Your nickname is stored in the file and shown with the profile. Do not use your real name or email unless you want them public. Profiles are shared under CC BY 4.0: submitting one means you agree to that licence.</p>
      <div class="buttons"><button id="sharego" class="primary">Create profile</button><button id="shareclose">Close</button></div>
      ${result}
    </div>`;
  }

  async _share() {
    const $ = (sel) => this.shadowRoot.querySelector(sel);
    const nickname = $("#nick").value.trim();
    const github = $("#gh").value.trim();
    storageSet("light_match_nickname", nickname);
    storageSet("light_match_github", github);
    try {
      const res = await this._ws("profile/share", {
        entry_id: this._entryId,
        nickname,
        github,
        notes: $("#notes").value.trim(),
        reference: this._reference || null,
      });
      const query = new URLSearchParams();
      for (const [key, value] of Object.entries({ nickname, github, notes: $("#notes").value.trim(), reference: this._reference || "" })) {
        if (value) query.set(key, value);
      }
      const qs = query.toString();
      await this._downloadSigned(`/api/light_match/share/${this._entryId}${qs ? `?${qs}` : ""}`, res.filename, async () => res.profile);
      this._shareResult = res;
      this._render();
    } catch (err) {
      this._flash(`Share failed: ${err.message || err}`);
    }
  }

  async _import(file, merge) {
    let text;
    try {
      text = await file.text();
    } catch (err) {
      this._flash(`Import failed: ${err.message || err}`);
      return;
    }
    await this._importText(text, merge);
  }

  async _importText(text, merge) {
    let profile;
    try {
      profile = JSON.parse(text);
    } catch (err) {
      this._flash(`Import failed: not JSON (${err.message || err})`);
      return;
    }
    try {
      if (this._keepLib && this._browse) {
        await this._ws("library/save", { profile });
        await this._loadLibrary();
      }
      let done;
      if (this._browse) {
        done = await this._apply({ profile }, `the imported profile${profile && profile.name ? ` ${profile.name}` : ""}`);
      } else {
        const res = await this._ws("profile/import", { entry_id: this._entryId, profile, merge });
        await this._loadProfile();
        await this._goto(this._index, false);
        this._flash(`Imported ${res.imported} cells, skipped ${res.skipped}.`);
        done = true;
      }
      if (done) {
        this._pasteText = "";
        this._pasteOpen = false;
        this._render();
      }
    } catch (err) {
      this._flash(`Import failed: ${err.message || err}`);
    }
  }

  _flash(text) {
    this._message = text;
    this.shadowRoot.querySelectorAll(".message").forEach((el) => (el.textContent = text));
  }

  _label(cell) {
    const name = cell.kind === "white" ? `White ${cell.kelvin} K` : cell.name[0].toUpperCase() + cell.name.slice(1);
    return `${name} at ${cell.brightness} %`;
  }

  _targetCss(cell) {
    if (cell.target && cell.target.xy) return xyToCss(cell.target.xy, cell.target.relative_output ?? 1, this._screenFloor / 100);
    return this._swatch(cell);
  }

  _fullScreen() {
    const cell = this._cell;
    if (!cell) return;
    const div = document.createElement("div");
    div.className = "fullscreen";
    div.style.background = this._targetCss(cell);
    div.innerHTML = `<span>${esc(this._label(cell))} · tap to close</span>`;
    const close = () => {
      if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
      div.remove();
    };
    div.addEventListener("click", close);
    this.shadowRoot.appendChild(div);
    if (div.requestFullscreen) div.requestFullscreen().catch(() => {});
  }

  _swatch(cell) {
    if (cell.kind === "white") {
      const k = cell.kelvin;
      const warm = Math.max(0, Math.min(1, (6500 - k) / 4300));
      return `rgb(255, ${Math.round(255 - 70 * warm)}, ${Math.round(255 - 150 * warm)})`;
    }
    return `hsl(${cell.hue}, 100%, 50%)`;
  }

  _sliders() {
    const cell = this._cell;
    const cmd = this._command;
    const rows = [];
    const row = (id, label, min, max, value, step = 1) =>
      `<label class="row"><span>${esc(label)}</span>
        <input type="range" data-id="${id}" min="${min}" max="${max}" step="${step}" value="${value}">
        <input type="number" data-id="${id}" min="${min}" max="${max}" step="${step}" value="${value}"></label>`;
    rows.push(row("brightness", "Brightness", 1, 255, cmd.brightness));
    const mode = this._mode || cell.mode;
    if (CHANNEL_NAMES[mode]) {
      CHANNEL_NAMES[mode].forEach((n, i) => rows.push(row(`ch${i}`, n, 0, 255, cmd[MODE_ATTR[mode]][i])));
    } else if (mode === "hs") {
      rows.push(row("hue", "Hue", 0, 360, Math.round(cmd.hs_color[0])));
      rows.push(row("sat", "Saturation", 0, 100, Math.round(cmd.hs_color[1])));
    } else if (mode === "color_temp") {
      const p = this._profile;
      rows.push(row("kelvin", "Colour temperature (K)", p.min_kelvin, p.max_kelvin, cmd.color_temp_kelvin, 10));
    }
    return rows.join("");
  }

  _applyInput(id, value) {
    const cmd = this._command;
    const mode = this._mode || this._cell.mode;
    const v = Number(value);
    if (Number.isNaN(v)) return;
    if (id === "brightness") cmd.brightness = Math.max(1, Math.min(255, Math.round(v)));
    else if (id.startsWith("ch")) cmd[MODE_ATTR[mode]][Number(id.slice(2))] = Math.max(0, Math.min(255, Math.round(v)));
    else if (id === "hue") cmd.hs_color[0] = v % 360;
    else if (id === "sat") cmd.hs_color[1] = Math.max(0, Math.min(100, v));
    else if (id === "kelvin") cmd.color_temp_kelvin = Math.round(v);
    this.shadowRoot.querySelectorAll(`input[data-id="${id}"]`).forEach((el) => {
      if (Number(el.value) !== v) el.value = v;
    });
    this.shadowRoot.querySelector(".cmd").textContent = JSON.stringify(cmd);
    this._schedulePreview();
  }

  _grid() {
    const p = this._profile;
    const rows = new Map();
    p.cells.forEach((cell, i) => {
      const rowKey = cell.kind === "white" ? `${cell.kelvin} K` : cell.name;
      if (!rows.has(rowKey)) rows.set(rowKey, { cell, items: [] });
      rows.get(rowKey).items.push({ cell, i });
    });
    let html = `<table class="grid"><tr><th></th>${LEVELS.map((l) => `<th>${l} %</th>`).join("")}</tr>`;
    for (const [name, { cell, items }] of rows) {
      html += `<tr><th><span class="dot" style="background:${this._swatch(cell)}"></span>${esc(name)}</th>`;
      for (const { cell: c, i } of items) {
        const quick = this._quick && (p.quick || []).includes(c.key);
        const leds = c.kind === "white" && c.mode && c.mode !== p.modes.white;
        const cls = [c.calibrated ? "done" : "", c.source === "model" ? "model" : "", quick ? "quick" : "", leds ? "leds" : "", i === this._index ? "current" : ""].join(" ");
        const mark = c.calibrated ? "&#10003;" : c.source === "model" ? "M" : c.source === "inferred" ? "~" : "";
        html += `<td><button class="cell ${cls}" data-index="${i}" title="${esc(this._label(c))} (${esc(SOURCE_LABEL[c.source] || c.source)}${leds ? ", colour LEDs" : ""})">${mark}</button></td>`;
      }
      html += "</tr>";
    }
    html += "</table>";
    if (p.cells.some((c) => c.kind === "white" && c.mode && c.mode !== p.modes.white)) {
      html += `<p class="hint"><span class="legend leds"></span> whites shown with the colour LEDs</p>`;
    }
    return html;
  }

  _referenceOptions() {
    const entry = this._entry;
    const twins = this._entries.filter((e) => e.entity_id && e.entry_id !== entry.entry_id);
    const twinIds = new Set(this._entries.map((e) => e.entity_id));
    const lights = Object.keys(this._hass.states)
      .filter((id) => id.startsWith("light.") && !twinIds.has(id) && id !== entry.source)
      .sort();
    const opt = (id, label) =>
      `<option value="${esc(id)}" ${id === this._reference ? "selected" : ""}>${esc(label)}</option>`;
    const name = (id) => this._hass.states[id]?.attributes?.friendly_name || id;
    return (
      `<option value="" ${this._reference ? "" : "selected"}>No reference (compare by eye)</option>` +
      (twins.length ? `<optgroup label="Calibrated lights">${twins.map((e) => opt(e.entity_id, e.title)).join("")}</optgroup>` : "") +
      `<optgroup label="Other lights">${lights.map((id) => opt(id, name(id))).join("")}</optgroup>`
    );
  }

  _modelLine(entry) {
    const m = entry.model_profile;
    const dev = entry.device || {};
    const lamp = [dev.manufacturer, dev.model, dev.model_id].filter(Boolean).join(" ") || "unknown lamp";
    if (!m) return `<p class="meta">${esc(lamp)} · no model profile${entry.profile_choice === "none" ? " (turned off)" : ""}</p>`;
    const how = METHOD_LABEL[m.method] || m.method;
    const rating = m.rating || { up: 0, down: 0 };
    const who = (m.contributors || []).map((c) => c.nickname).filter(Boolean);
    return `<p class="meta">${esc(lamp)} · model profile <b>${esc(m.name)}</b> (${esc(how)}, ${m.verified ? "verified" : "unverified"}${m.cells ? `, ${m.cells} cells` : ""}${m.origin === "online" ? ", online" : ""})</p>
      <p class="meta">&#128077; ${Number(rating.up) || 0} &#128078; ${Number(rating.down) || 0} · ${Number(m.downloads) || 0} downloads${who.length ? ` · by ${esc(who.join(", "))}` : ""}${m.updated ? ` · updated ${esc(m.updated)}` : ""}
      ${m.rate_url ? ` · <a href="${esc(m.rate_url)}" target="_blank" rel="noopener">Rate / report</a>` : ""}</p>${this._importsLine(m)}`;
  }

  _importsLine(m) {
    const seen = new Map();
    for (const i of m.imports || []) {
      const key = i.source + "|" + i.licence;
      const q = seen.get(key) || { src: i, quality: new Set() };
      q.quality.add(i.quality);
      seen.set(key, q);
    }
    if (!seen.size) return "";
    const parts = [...seen.values()].map(({ src, quality }) =>
      `<a href="${esc(src.source_url)}" target="_blank" rel="noopener">${esc(src.source)}</a> (${esc(src.licence)}; ${esc([...quality].join(", "))})`);
    return `<p class="meta">Data from ${parts.join(", ")}. Capabilities and an estimated brightness curve only; calibrate to refine.</p>`;
  }

  _onlineCard() {
    const st = this._settings;
    const res = st.last_result;
    let last = st.checked ? `Last checked ${esc(st.checked.slice(0, 16).replace("T", " "))} UTC` : "Not checked yet";
    if (res && res.error) last += ` · ${esc(res.error)}`;
    else if (res && res.enabled) last += ` · ${res.downloaded.length} downloaded, ${res.rejected.length} rejected`;
    return `<div class="card">
      <label><input type="checkbox" id="online" ${st.online_update ? "checked" : ""}> Get newer lamp profiles from the online database</label>
      <p class="hint">Off by default. When on, Light Match downloads the profile index and newer profiles from GitHub once a day. Nothing about your lamps or your home is sent; your own cells are never changed.</p>
      ${st.online_update ? `<p class="meta">${last} · ${st.online_profiles.length} online profiles · <a href="#" id="checknow">Check now</a></p>` : ""}
    </div>`;
  }

  async _setOnline(on) {
    try {
      this._settings = await this._ws("settings/set", { online_update: on });
      if (on) await this._checkNow();
      else await this._reload();
    } catch (err) {
      this._flash(`Not changed: ${err.message || err}`);
    }
  }

  async _checkNow() {
    try {
      this._settings = await this._ws("profiles/update");
      await this._reload();
    } catch (err) {
      this._flash(`Not checked: ${err.message || err}`);
    }
  }

  async _reload() {
    const res = await this._ws("entries");
    this._entries = res.entries;
    await this._loadLibrary();
    if (this._entryId) await this._loadProfile();
    this._render();
  }

  _render() {
    const root = this.shadowRoot;
    const entry = this._entry;
    let body;
    if (!this._loaded) {
      body = `<p>Loading…</p>`;
    } else if (!entry) {
      body = `<div class="card"><p>No calibrated lights yet. Add one under Settings → Devices &amp; services → Add integration → Light Match.</p>
        <p class="message">${esc(this._message)}</p></div>`;
    } else {
      const cell = this._cell;
      const pct = entry.total ? Math.round((100 * entry.calibrated) / entry.total) : 0;
      body = `
      <div class="card">
        <div class="pickers">
          <label>Lamp to calibrate
            <select id="entry">${this._entries
              .map((e) => `<option value="${esc(e.entry_id)}" ${e.entry_id === entry.entry_id ? "selected" : ""}>${esc(e.title)}</option>`)
              .join("")}</select></label>
          <label>Reference lamp (shows the target look)
            <select id="reference">${this._referenceOptions()}</select></label>
        </div>
        <div class="progress"><div style="width:${pct}%"></div></div>
        ${this._modelLine(entry)}
        <p class="meta">${entry.calibrated} of ${entry.total} cells calibrated · commanded in ${esc(entry.modes.color || "-")} / ${esc(entry.modes.white || "-")} · floor ${entry.floor}${entry.behaviour === "reference" ? " · <b>this lamp is set as the reference; its cells are not used</b>" : ""}</p>
      </div>
      ${
        cell
          ? `<div class="card">
        <h2><span class="dot big" style="background:${this._swatch(cell)}"></span>${esc(this._label(cell))}
          <small>${esc(SOURCE_LABEL[cell.source] || cell.source)} · ${this._order().indexOf(this._index) + 1}/${this._order().length}${this._quick ? " quick" : ""}</small></h2>
        <p class="meta">Target in lm-ref-1: xy ${cell.target ? cell.target.xy.join(", ") : "-"} · ${cell.target ? (cell.target.relative_output * 100).toPrecision(2) : "-"} % of full output</p>
        <div class="target">
          <button class="swatch" id="swatch" title="Show full screen" style="background:${this._targetCss(cell)}"></button>
          <div>
            <p class="hint">Target colour and brightness as your screen can show it: full output is screen white, and the scale follows perceived lightness from the screen floor up, since a lit lamp never looks black, even at 1 %. A rough guide only: screens are not calibrated, so trust the reference lamp first. Tap the circle for full screen.</p>
            <label class="row floor"><span>Screen floor</span><input type="range" id="screenfloor" min="0" max="90" step="5" value="${this._screenFloor}"><span>${this._screenFloor} %</span></label>
          </div>
        </div>
        <p class="hint">Make the lamp under test look like the reference, then Save. Changes are sent to the lamp live.</p>
        ${this._colourToggle(cell)}
        ${this._sliders()}
        <p class="cmd">${esc(JSON.stringify(this._command))}</p>
        <div class="buttons">
          <button id="prev">Previous</button>
          <button id="show" ${this._reference ? "" : "disabled"}>Show target again</button>
          <button id="skip">Skip</button>
          <button id="save" class="primary">Save</button>
        </div>
        <div class="links">
          <label><input type="checkbox" id="auto" ${this._autoShow ? "checked" : ""}> Show target and preview on every step</label>
          <label><input type="checkbox" id="quick" ${this._quick ? "checked" : ""}> Quick calibration (5 cells, the rest inferred)</label>
          <a href="#" id="reset">Reset this cell</a> · <a href="#" id="default">Load default</a>${cell.model_command && cell.source !== "model" ? ` · <a href="#" id="copymodel">Copy from model profile</a>` : ""}
        </div>
        <p class="message">${esc(this._message)}</p>
      </div>`
          : ""
      }
      ${this._shareOpen ? this._shareCard() : ""}
      <div class="card">${this._profile && this._profile.cells.length ? this._grid() : "<p>This lamp has nothing to calibrate.</p>"}</div>
      ${this._filesCard()}
      ${this._browse ? this._libraryCard() : ""}
      ${this._groups && this._browse ? this._groupsCard() : ""}
      <div class="card links">
        <a href="#" id="resetall">Reset all cells</a>${entry.model_profile && entry.model_profile.cells ? ` · <a href="#" id="copymodelall">Copy all model profile cells</a>` : ""}
      </div>
      ${this._settings ? this._onlineCard() : ""}`;
    }
    root.innerHTML = `
      <style>
        :host { display:block; background: var(--primary-background-color); color: var(--primary-text-color); min-height:100vh; font-family: var(--paper-font-body1_-_font-family, Roboto, sans-serif); }
        .toolbar { display:flex; align-items:center; height:56px; padding:0 12px; background: var(--app-header-background-color, var(--primary-color)); color: var(--app-header-text-color, #fff); font-size:20px; }
        .content { max-width: 860px; margin: 0 auto; padding: 16px; box-sizing:border-box; }
        .card { background: var(--card-background-color, #fff); border-radius: var(--ha-card-border-radius, 12px); box-shadow: var(--ha-card-box-shadow, none); border: 1px solid var(--divider-color, #ddd); padding: 16px; margin-bottom: 16px; }
        .pickers { display:grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 12px; }
        label { display:block; font-size: 14px; }
        select { width:100%; margin-top:4px; padding:6px; font-size:14px; background: var(--card-background-color); color: var(--primary-text-color); border:1px solid var(--divider-color); border-radius:6px; }
        .progress { height:6px; background: var(--divider-color, #ddd); border-radius:3px; margin-top:16px; overflow:hidden; }
        .progress div { height:100%; background: var(--primary-color); }
        .meta, .hint { color: var(--secondary-text-color); font-size: 13px; }
        h2 { display:flex; align-items:center; gap:8px; font-size: 20px; margin: 0 0 4px; flex-wrap: wrap; }
        h2 small { font-size: 13px; color: var(--secondary-text-color); font-weight: normal; }
        .row { display:grid; grid-template-columns: 150px 1fr 70px; gap: 8px; align-items:center; margin: 8px 0; }
        .row input[type=number] { width: 64px; padding: 4px; background: var(--card-background-color); color: var(--primary-text-color); border:1px solid var(--divider-color); border-radius:4px; }
        .cmd { font-family: monospace; font-size: 12px; color: var(--secondary-text-color); word-break: break-all; }
        .buttons { display:flex; gap: 8px; flex-wrap: wrap; margin-top: 8px; }
        button { padding: 8px 14px; border-radius: 6px; border: 1px solid var(--divider-color); background: var(--card-background-color); color: var(--primary-text-color); cursor:pointer; font-size:14px; }
        button.primary { background: var(--primary-color); color: var(--text-primary-color, #fff); border-color: var(--primary-color); }
        button:disabled { opacity: .5; cursor: default; }
        .links { font-size: 13px; margin-top: 12px; display:flex; gap: 8px; flex-wrap: wrap; align-items: center; }
        .links label { display:inline; }
        a { color: var(--primary-color); }
        a.button { display:inline-block; padding: 8px 14px; border-radius: 6px; text-decoration: none; }
        a.button.primary { background: var(--primary-color); color: var(--text-primary-color, #fff); }
        input:not([type]) { display:block; width:100%; box-sizing:border-box; margin-top:4px; padding:6px; font-size:14px; background: var(--card-background-color); color: var(--primary-text-color); border:1px solid var(--divider-color); border-radius:6px; }
        .file input { display:none; }
        textarea { display:block; width:100%; box-sizing:border-box; margin-top:8px; padding:6px; font-family: monospace; font-size:12px; background: var(--card-background-color); color: var(--primary-text-color); border:1px solid var(--divider-color); border-radius:6px; }
        .copyfrom { display:flex; gap: 8px; align-items:flex-end; margin-top: 12px; }
        .copyfrom label { flex: 1; }
        h2.sub { font-size: 16px; margin-top: 16px; }
        h3 { font-size: 15px; margin: 16px 0 2px; }
        h3 small { font-size: 12px; color: var(--secondary-text-color); font-weight: normal; }
        .checks { display:flex; flex-wrap: wrap; gap: 4px 16px; }
        label.check { display:inline-flex; align-items:center; gap: 4px; font-size: 14px; margin: 2px 0; }
        .targets { margin: 8px 0; }
        .plist { list-style: none; padding: 0; margin: 4px 0; }
        .plist li { padding: 6px 0; border-bottom: 1px solid var(--divider-color); font-size: 14px; }
        .plist .acts { display:block; font-size: 13px; margin-top: 2px; }
        .results { margin: 4px 0; padding-left: 18px; font-size: 13px; }
        .bad { color: var(--error-color, #db4437); }
        .row.floor { grid-template-columns: 110px 1fr 50px; }
        .file { color: var(--primary-color); text-decoration: underline; cursor: pointer; }
        .message { min-height: 1em; color: var(--primary-color); font-size: 13px; }
        .dot { display:inline-block; width: 12px; height: 12px; border-radius: 50%; margin-right: 6px; border: 1px solid var(--divider-color); vertical-align: middle; }
        .dot.big { width: 20px; height: 20px; }
        .target { display:flex; gap: 16px; align-items:center; margin: 12px 0; }
        .swatch { flex: 0 0 auto; width: 160px; height: 160px; border-radius: 50%; padding: 0; border: 2px solid var(--divider-color); }
        .fullscreen { position: fixed; inset: 0; z-index: 9999; display:flex; align-items:flex-end; justify-content:center; cursor: pointer; }
        .fullscreen span { margin-bottom: 24px; padding: 4px 10px; border-radius: 12px; background: rgba(0,0,0,.35); color: rgba(255,255,255,.6); font-size: 12px; }
        @media (max-width: 600px) { .target { flex-direction: column; } .swatch { width: 200px; height: 200px; } }
        .grid { border-collapse: collapse; width: 100%; font-size: 13px; }
        .grid th { text-align:left; font-weight: normal; color: var(--secondary-text-color); padding: 2px 4px; white-space: nowrap; }
        .grid td { padding: 2px; text-align:center; }
        .cell { width: 100%; min-width: 28px; height: 26px; padding: 0; font-size: 13px; }
        .cell.model { background: var(--secondary-background-color); color: var(--secondary-text-color); font-size: 11px; }
      .cell.done { background: var(--success-color, #43a047); color: #fff; border-color: transparent; }
        .cell.quick { border: 2px dashed var(--primary-color); }
        .cell.current { outline: 2px solid var(--primary-color); outline-offset: 1px; }
        .cell.leds, .legend.leds { background-image: linear-gradient(90deg, #e53935, #43a047, #1e88e5); background-size: 100% 4px; background-repeat: no-repeat; background-position: bottom; }
        .legend { display:inline-block; width: 28px; height: 14px; border: 1px solid var(--divider-color); border-radius: 3px; vertical-align: middle; }
        .toggle { margin: 8px 0; }
        @media (max-width: 600px) { .row { grid-template-columns: 90px 1fr 64px; } .content { padding: 8px 16px; } }
      </style>
      <div class="toolbar"><ha-menu-button></ha-menu-button><span>Light Match</span></div>
      <div class="content">${body}</div>`;
    const menu = root.querySelector("ha-menu-button");
    if (menu) {
      menu.hass = this._hass;
      menu.narrow = this._narrow;
    }
    this._bind();
  }

  _bind() {
    const $ = (sel) => this.shadowRoot.querySelector(sel);
    const on = (sel, ev, fn) => {
      const el = $(sel);
      if (el) el.addEventListener(ev, fn);
    };
    const link = (sel, fn) =>
      on(sel, "click", (e) => {
        e.preventDefault();
        fn();
      });
    on("#entry", "change", async (e) => {
      await this._selectEntry(e.target.value);
      this._render();
    });
    on("#reference", "change", (e) => {
      this._reference = e.target.value;
      this._render();
      if (this._autoShow) this._showTarget();
    });
    this.shadowRoot.querySelectorAll("input[data-id]").forEach((el) =>
      el.addEventListener("input", (e) => this._applyInput(e.target.dataset.id, e.target.value))
    );
    on("#prev", "click", () => this._step(-1));
    on("#skip", "click", () => this._step(1));
    on("#quick", "change", (e) => {
      this._quick = e.target.checked;
      storageSet("light_match_quick", this._quick ? "1" : "");
      const order = this._order();
      if (this._quick && !order.includes(this._index)) {
        const open = order.find((i) => !this._profile.cells[i].calibrated);
        this._goto(open ?? order[0] ?? 0);
      } else this._render();
    });
    link("#copymodel", () => this._copyModel(this._cell.key));
    link("#copymodelall", () => this._copyModel("*"));
    on("#save", "click", () => this._save());
    on("#show", "click", () => this._showTarget());
    on("#swatch", "click", () => this._fullScreen());
    on("#auto", "change", (e) => (this._autoShow = e.target.checked));
    link("#reset", () => this._reset(false));
    link("#resetall", () => this._reset(true));
    on("#online", "change", (e) => this._setOnline(e.target.checked));
    link("#checknow", () => this._checkNow());
    on("#colourmode", "change", (e) => this._setColourMode(e.target.checked));
    link("#default", () => {
      const cell = this._cell;
      this._command = structuredClone((cell.defaults && cell.defaults[this._mode]) || cell.default);
      this._render();
      this._preview(true);
    });
    link("#export", () => this._export());
    link("#copyjson", () => this._copyJson());
    const renameAuto = () => {
      if (this._nameEdited) return;
      this._name = autoName(this._make, this._model);
      const el = $("#pname");
      if (el) el.value = this._name;
    };
    on("#make", "input", (e) => {
      this._make = e.target.value;
      renameAuto();
    });
    on("#model", "input", (e) => {
      this._model = e.target.value;
      renameAuto();
    });
    on("#pname", "input", (e) => {
      this._name = e.target.value;
      this._nameEdited = true;
    });
    link("#savelib", () => this._saveToLibrary());
    on("#merge", "change", (e) => (this._merge = e.target.checked));
    on("#keeplib", "change", (e) => (this._keepLib = e.target.checked));
    on("#screenfloor", "input", (e) => {
      this._screenFloor = Number(e.target.value);
      storageSet("light_match_screen_floor", String(this._screenFloor));
      const label = e.target.nextElementSibling;
      if (label) label.textContent = `${this._screenFloor} %`;
      const sw = $("#swatch");
      if (sw && this._cell) sw.style.background = this._targetCss(this._cell);
    });
    on("#filter", "input", (e) => {
      this._filter = e.target.value;
      const pos = e.target.selectionStart;
      this._render();
      const el = $("#filter");
      if (el) {
        el.focus();
        el.setSelectionRange(pos, pos);
      }
    });
    this.shadowRoot.querySelectorAll("input[data-target]").forEach((el) =>
      el.addEventListener("change", () => {
        if (el.checked) this._targets.add(el.dataset.target);
        else this._targets.delete(el.dataset.target);
        this._render();
      })
    );
    this.shadowRoot.querySelectorAll("input[data-group]").forEach((el) =>
      el.addEventListener("change", () => {
        const g = [...this._groups.auto, ...this._groups.manual].find((x) => x.id === el.dataset.group);
        if (!g) return;
        for (const id of g.entry_ids) {
          if (el.checked) this._targets.add(id);
          else this._targets.delete(id);
        }
        this._render();
      })
    );
    for (const kind of ["apply", "export", "json", "rename", "delete"]) {
      this.shadowRoot.querySelectorAll(`[data-lib-${kind}]`).forEach((el) =>
        el.addEventListener("click", (e) => {
          e.preventDefault();
          this._libAction(kind, el.getAttribute(`data-lib-${kind}`));
        })
      );
    }
    this.shadowRoot.querySelectorAll("[data-db-apply]").forEach((el) =>
      el.addEventListener("click", (e) => {
        e.preventDefault();
        this._libAction("db", el.dataset.dbApply);
      })
    );
    for (const kind of ["set", "rename", "delete"]) {
      this.shadowRoot.querySelectorAll(`[data-grp-${kind}]`).forEach((el) =>
        el.addEventListener("click", (e) => {
          e.preventDefault();
          this._groupAction(kind, el.getAttribute(`data-grp-${kind}`));
        })
      );
    }
    on("#grpadd", "click", () => this._groupAction("add"));
    link("#pastetoggle", () => {
      this._pasteOpen = !this._pasteOpen;
      this._render();
    });
    on("#jsonin", "input", (e) => (this._pasteText = e.target.value));
    on("#pastego", "click", () => this._importText(this._pasteText || "", Boolean(this._merge)));
    on("#copyfrom", "change", (e) => (this._copyFromId = e.target.value));
    on("#copygo", "click", () => this._copyFrom());
    link("#share", () => {
      this._shareOpen = true;
      this._shareResult = null;
      this._render();
    });
    on("#sharego", "click", () => this._share());
    on("#shareclose", "click", () => {
      this._shareOpen = false;
      this._render();
    });
    on("#import", "change", (e) => {
      const file = e.target.files[0];
      if (file) this._import(file, Boolean(this._merge));
    });
    this.shadowRoot.querySelectorAll("button.cell").forEach((el) =>
      el.addEventListener("click", () => this._goto(Number(el.dataset.index)))
    );
  }
}

if (!customElements.get("light-match-panel")) {
  customElements.define("light-match-panel", LightMatchPanel);
}
