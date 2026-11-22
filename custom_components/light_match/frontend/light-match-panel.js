// Light Match calibration panel. Plain web component, no build step.
// Talks to the integration over the websocket API (light_match/*).

const LEVELS = [1, 5, 20, 50, 100];
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
    this._entryId = entryId;
    const entry = this._entry;
    this._reference = this._defaultReference(entry);
    await this._loadProfile();
    const firstOpen = this._profile.cells.findIndex((c) => !c.calibrated);
    this._index = firstOpen >= 0 ? firstOpen : 0;
    this._command = this._cell ? structuredClone(this._cell.command) : null;
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
    this._command = structuredClone(this._cell.command);
    this._message = "";
    this._render();
    if (show) {
      await this._showTarget();
      await this._preview(true);
    }
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
      await this._ws("cell/set", { entry_id: this._entryId, key: cell.key, command: this._command });
      await this._loadProfile();
      this._flash(`Saved ${this._label(cell)}.`);
      await this._goto(this._index + 1);
    } catch (err) {
      this._flash(`Not saved: ${err.message || err}`);
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

  async _export() {
    const entry = this._entry;
    const model = prompt("Lamp model to put in the profile (optional):", "") || undefined;
    const res = await this._ws("profile/export", { entry_id: this._entryId, ...(model ? { model } : {}) });
    const blob = new Blob([JSON.stringify(res.profile, null, 2)], { type: "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `${(model || entry.title).replace(/[^\w.-]+/g, "_")}.light-match.json`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  }

  async _import(file, merge) {
    try {
      const profile = JSON.parse(await file.text());
      const res = await this._ws("profile/import", { entry_id: this._entryId, profile, merge });
      await this._loadProfile();
      await this._goto(this._index, false);
      this._flash(`Imported ${res.imported} cells, skipped ${res.skipped}.`);
    } catch (err) {
      this._flash(`Import failed: ${err.message || err}`);
    }
  }

  _flash(text) {
    this._message = text;
    const el = this.shadowRoot.querySelector(".message");
    if (el) el.textContent = text;
  }

  _label(cell) {
    const name = cell.kind === "white" ? `White ${cell.kelvin} K` : cell.name[0].toUpperCase() + cell.name.slice(1);
    return `${name} at ${cell.brightness} %`;
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
    const mode = cell.mode;
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
    const mode = this._cell.mode;
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
        const cls = [c.calibrated ? "done" : "", i === this._index ? "current" : ""].join(" ");
        html += `<td><button class="cell ${cls}" data-index="${i}" title="${esc(this._label(c))}">${c.calibrated ? "&#10003;" : ""}</button></td>`;
      }
      html += "</tr>";
    }
    return html + "</table>";
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
        <p class="meta">${entry.calibrated} of ${entry.total} cells calibrated · commanded in ${esc(entry.modes.color || "-")} / ${esc(entry.modes.white || "-")} · floor ${entry.floor}${entry.behaviour === "reference" ? " · <b>this lamp is set as the reference; its cells are not used</b>" : ""}</p>
      </div>
      ${
        cell
          ? `<div class="card">
        <h2><span class="dot big" style="background:${this._swatch(cell)}"></span>${esc(this._label(cell))}
          <small>${cell.calibrated ? "calibrated" : "default"} · ${this._index + 1}/${this._profile.cells.length}</small></h2>
        <p class="hint">Make the lamp under test look like the reference, then Save. Changes are sent to the lamp live.</p>
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
          <a href="#" id="reset">Reset this cell</a> · <a href="#" id="default">Load default</a>
        </div>
        <p class="message">${esc(this._message)}</p>
      </div>`
          : ""
      }
      <div class="card">${this._profile && this._profile.cells.length ? this._grid() : "<p>This lamp has nothing to calibrate.</p>"}</div>
      <div class="card links">
        <a href="#" id="export">Export profile</a> ·
        <label class="file">Import profile <input type="file" id="import" accept=".json,application/json"></label>
        <label><input type="checkbox" id="merge"> merge with existing cells</label> ·
        <a href="#" id="resetall">Reset all cells</a>
      </div>`;
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
        .file input { display:none; }
        .file { color: var(--primary-color); text-decoration: underline; cursor: pointer; }
        .message { min-height: 1em; color: var(--primary-color); font-size: 13px; }
        .dot { display:inline-block; width: 12px; height: 12px; border-radius: 50%; margin-right: 6px; border: 1px solid var(--divider-color); vertical-align: middle; }
        .dot.big { width: 20px; height: 20px; }
        .grid { border-collapse: collapse; width: 100%; font-size: 13px; }
        .grid th { text-align:left; font-weight: normal; color: var(--secondary-text-color); padding: 2px 4px; white-space: nowrap; }
        .grid td { padding: 2px; text-align:center; }
        .cell { width: 100%; min-width: 28px; height: 26px; padding: 0; font-size: 13px; }
        .cell.done { background: var(--success-color, #43a047); color: #fff; border-color: transparent; }
        .cell.current { outline: 2px solid var(--primary-color); outline-offset: 1px; }
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
    on("#prev", "click", () => this._goto(this._index - 1));
    on("#skip", "click", () => this._goto(this._index + 1));
    on("#save", "click", () => this._save());
    on("#show", "click", () => this._showTarget());
    on("#auto", "change", (e) => (this._autoShow = e.target.checked));
    link("#reset", () => this._reset(false));
    link("#resetall", () => this._reset(true));
    link("#default", () => {
      this._command = structuredClone(this._cell.default);
      this._render();
      this._preview(true);
    });
    link("#export", () => this._export());
    on("#import", "change", (e) => {
      const file = e.target.files[0];
      if (file) this._import(file, $("#merge").checked);
    });
    this.shadowRoot.querySelectorAll("button.cell").forEach((el) =>
      el.addEventListener("click", () => this._goto(Number(el.dataset.index)))
    );
  }
}

if (!customElements.get("light-match-panel")) {
  customElements.define("light-match-panel", LightMatchPanel);
}
