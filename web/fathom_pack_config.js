// The Fathom section of ComfyUI's settings panel: Settings, Fathom, Pack config, where the config copied from the Backend's Pack config tab in Fathom is pasted (FTH-146.4).
// ComfyUI saves the value in its own settings file, which the pack reads server side; every secret in it is encrypted under a key only Fathom holds.
import { app } from "../../scripts/app.js";

app.registerExtension({
  name: "Fathom.PackConfig",
  settings: [
    {
      id: "Fathom.PackConfig",
      category: ["Fathom", "Pack config", "Pack config"],
      name: "Pack config",
      type: "text",
      defaultValue: "",
      tooltip: "Paste the config from this Backend's Pack config tab in Fathom. Its secrets are encrypted; Fathom sends the key with each call.",
    },
  ],
});
