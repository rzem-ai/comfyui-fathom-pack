// The Fathom section of ComfyUI's settings panel: Settings, Fathom, Pack config, where the config copied from the Backend's Pack config tab in Fathom is pasted (FTH-146.4).
// ComfyUI saves the value in its own settings file, which the pack reads server side; every secret in it is encrypted under a key only Fathom holds.
// The field is the pack's own (FTH-153): a wide monospace text area with Paste and Clear and a line saying what the text is, drawn through ComfyUI's custom setting type,
// a function `(name, setValue, value, attrs) => HTMLElement`. The value is the text as pasted, under the same id, so `pack_config.py` reads it unchanged.
import { app } from "../../scripts/app.js";

const SETTING = "Fathom.PackConfig";
const FIELDS = ["fathom_pack_config", "backend", "fingerprint", "nonce", "ciphertext"];
const STYLE_ID = "fathom-pack-config-style";
const PASTE_BY_HAND = "This browser won't let ComfyUI read the clipboard here: click in the field and paste with Cmd-V or Ctrl-V.";

// Scoped to the field: ComfyUI draws a setting as a row, its name on the left and the input in `div.form-input.flex.justify-end` on the right.
// The row holding this field wraps, so the field takes a line of its own across the panel.
const STYLE = `
div:has(> .form-input > div > .fathom-pack-config) { flex-wrap: wrap; }
.form-input:has(> div > .fathom-pack-config) { flex: 1 1 100%; justify-content: stretch; min-width: 0; }
.form-input:has(> div > .fathom-pack-config) > div { flex: 1 1 auto; min-width: 0; }
.fathom-pack-config { display: flex; flex-direction: column; gap: 8px; width: 100%; padding-bottom: 8px; }
.fathom-pack-config-intro { margin: 0; font-size: 12px; line-height: 1.4; color: var(--descrip-text, #999); }
.fathom-pack-config textarea {
  box-sizing: border-box; width: 100%; min-height: calc(6 * 1.5em + 18px); resize: vertical; margin: 0; padding: 8px;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; line-height: 1.5; word-break: break-all;
  color: var(--input-text, #ddd); background: var(--comfy-input-bg, #222); border: 1px solid var(--border-color, #4e4e4e); border-radius: 6px;
}
.fathom-pack-config textarea:focus { outline: none; border-color: var(--descrip-text, #999); }
.fathom-pack-config textarea::placeholder { color: var(--descrip-text, #999); opacity: 0.7; }
.fathom-pack-config-bar { display: flex; align-items: flex-start; gap: 8px; }
.fathom-pack-config-status { flex: 1 1 auto; min-width: 0; font-size: 12px; line-height: 1.5; color: var(--descrip-text, #999); overflow-wrap: anywhere; }
.fathom-pack-config-status[data-kind="ok"] { color: var(--input-text, #ddd); }
.fathom-pack-config-status[data-kind="bad"] { color: var(--error-text, #ff4444); }
.fathom-pack-config button {
  flex: 0 0 auto; margin: 0; padding: 4px 14px; font: inherit; font-size: 12px; cursor: pointer;
  color: var(--input-text, #ddd); background: var(--comfy-input-bg, #222); border: 1px solid var(--border-color, #4e4e4e); border-radius: 6px;
}
.fathom-pack-config button:hover { border-color: var(--descrip-text, #999); }
.fathom-pack-config button:disabled { cursor: default; opacity: 0.5; }
`;

function present(value) {
  return value !== undefined && value !== null && value !== "";
}

/** What the field's text is, from its clear parts alone: `{kind: "empty" | "ok" | "bad", text}`. It never reads, or says, the ciphertext. */
export function packConfigStatus(text) {
  if (typeof text !== "string" || text.trim() === "") return { kind: "empty", text: "Nothing pasted yet." };
  let config;
  try {
    config = JSON.parse(text);
  } catch {
    return { kind: "bad", text: "This isn't a Fathom Pack config: it isn't JSON." };
  }
  if (config === null || typeof config !== "object" || Array.isArray(config)) {
    return { kind: "bad", text: "This isn't a Fathom Pack config: it isn't a JSON object." };
  }
  const missing = FIELDS.filter((field) => !present(config[field]));
  if (missing.length > 0) return { kind: "bad", text: `This isn't a Fathom Pack config: it has no ${missing.join(", ")}.` };
  return { kind: "ok", text: `Pasted for Backend ${String(config.backend)}, fingerprint ${String(config.fingerprint).slice(0, 12)}.` };
}

function asText(value) {
  if (value === undefined || value === null) return "";
  return typeof value === "string" ? value : JSON.stringify(value);
}

function addStyle() {
  if (document.getElementById(STYLE_ID)) return;
  const style = document.createElement("style");
  style.id = STYLE_ID;
  style.textContent = STYLE;
  document.head.appendChild(style);
}

async function askToClear() {
  const dialog = app.extensionManager?.dialog;
  const message = "The pack can't fetch through a proxy or use a source key until the config is pasted again from the Backend's Pack config tab in Fathom.";
  if (typeof dialog?.confirm === "function") {
    return (await dialog.confirm({ title: "Clear the Pack config?", message, type: "default", key: "fathom-pack-config-clear" })) === true;
  }
  return window.confirm(`Clear the Pack config? ${message}`);
}

// ComfyUI calls the render function afresh, and empties its container, each time the setting's value changes, so one field is kept and handed back each time.
// A save made while typing would otherwise drop the field's focus and caret, so they are put back as soon as ComfyUI has added the field again.
let view = null;

function build() {
  const root = document.createElement("div");
  root.className = "fathom-pack-config";

  const intro = document.createElement("p");
  intro.className = "fathom-pack-config-intro";
  intro.textContent = "Copy the config from this Backend's Pack config tab in Fathom and paste it here. Its secrets stay encrypted; Fathom sends the key with each call.";

  const textarea = document.createElement("textarea");
  textarea.rows = 8;
  textarea.spellcheck = false;
  textarea.autocomplete = "off";
  textarea.setAttribute("autocapitalize", "off");
  textarea.placeholder = '{"fathom_pack_config": 1, "backend": "...", "fingerprint": "...", "nonce": "...", "ciphertext": "..."}';

  const bar = document.createElement("div");
  bar.className = "fathom-pack-config-bar";
  const status = document.createElement("div");
  status.className = "fathom-pack-config-status";
  status.setAttribute("role", "status");
  status.setAttribute("aria-live", "polite");
  const paste = document.createElement("button");
  paste.type = "button";
  paste.textContent = "Paste";
  const clear = document.createElement("button");
  clear.type = "button";
  clear.textContent = "Clear";
  bar.append(status, paste, clear);
  root.append(intro, textarea, bar);

  const v = { root, textarea, status, clear, setValue: null, value: "", restore: null, composing: false };

  v.show = (said) => {
    const shown = said ?? packConfigStatus(textarea.value);
    status.textContent = shown.text;
    status.dataset.kind = shown.kind;
    clear.disabled = textarea.value === "";
  };

  v.save = () => {
    v.show();
    const text = textarea.value;
    if (text === v.value || typeof v.setValue !== "function") return;
    v.value = text;
    v.restore =
      document.activeElement === textarea
        ? { focus: true, start: textarea.selectionStart, end: textarea.selectionEnd, direction: textarea.selectionDirection, scroll: textarea.scrollTop }
        : { focus: false, scroll: textarea.scrollTop };
    v.setValue(text);
    // ComfyUI draws the field again within this turn; a restore it never took is dropped, so a later draw takes the stored value.
    setTimeout(() => {
      v.restore = null;
    }, 0);
  };

  textarea.addEventListener("compositionstart", () => {
    v.composing = true;
  });
  textarea.addEventListener("compositionend", () => {
    v.composing = false;
    v.save();
  });
  textarea.addEventListener("input", () => {
    if (!v.composing) v.save();
  });

  paste.addEventListener("click", async () => {
    let text;
    try {
      if (typeof navigator.clipboard?.readText !== "function") throw new Error("no clipboard");
      text = await navigator.clipboard.readText();
    } catch {
      textarea.focus();
      v.show({ kind: "bad", text: PASTE_BY_HAND });
      return;
    }
    if (text.trim() === "") {
      v.show({ kind: "bad", text: "The clipboard is empty: copy the config from the Backend's Pack config tab in Fathom first." });
      return;
    }
    textarea.value = text.trim();
    textarea.scrollTop = 0;
    v.save();
  });

  clear.addEventListener("click", async () => {
    if (textarea.value === "" || !(await askToClear())) return;
    textarea.value = "";
    v.save();
  });

  return v;
}

function render(_name, setValue, value) {
  addStyle();
  if (view === null) view = build();
  view.setValue = setValue;
  const { textarea, restore } = view;
  if (restore === null) {
    // Drawn with the stored value: on opening the settings, or after a change made elsewhere.
    view.value = asText(value);
    if (textarea.value !== view.value) textarea.value = view.value;
  } else {
    view.restore = null;
    queueMicrotask(() => {
      if (!textarea.isConnected) return;
      if (restore.focus) {
        textarea.focus({ preventScroll: true });
        textarea.setSelectionRange(restore.start, restore.end, restore.direction);
      }
      textarea.scrollTop = restore.scroll;
    });
  }
  view.show();
  return view.root;
}

app.registerExtension({
  name: "Fathom.PackConfig",
  settings: [
    {
      id: SETTING,
      category: ["Fathom", "Pack config", "Pack config"],
      name: "Pack config",
      type: render,
      defaultValue: "",
      tooltip: "Paste the config from this Backend's Pack config tab in Fathom. Its secrets are encrypted; Fathom sends the key with each call.",
    },
  ],
});
