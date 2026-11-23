// Speakers: what "Speak on this desk" is, and a place to check a desk name and send a test line.

import { h, busy, copyText } from "./dom.js";
import { sinkStatus, sinkSay, explain, getSink, setSink, SINK_RE } from "./api.js";

export function mountSpeakers(root, ctx) {
  let disposed = false;
  const base = ctx.config?.public_base_url || location.origin;

  const input = h("input", { id: "spk-name", type: "text", value: getSink(), placeholder: "for example my-laptop", autocomplete: "off", autocapitalize: "off", spellcheck: "false", "aria-describedby": "spk-hint" });
  const n = h("span", { class: "listeners__n", "aria-hidden": "true", hidden: true });
  const text = h("p", { class: "muted", "aria-live": "polite" }, "Enter a desk name and press Check.");
  const note = h("p", { class: "small", "aria-live": "polite" });
  const checkBtn = h("button", { class: "btn", type: "button" }, "Check");
  const testBtn = h("button", { class: "btn btn--primary", type: "button" }, "Send a test line");

  function validKey() {
    const key = input.value.trim();
    if (!SINK_RE.test(key)) {
      input.setAttribute("aria-invalid", "true");
      text.textContent = "Desk names use letters, numbers, dots, dashes and underscores, and start with a letter or number.";
      n.hidden = true;
      input.focus();
      return null;
    }
    input.removeAttribute("aria-invalid");
    setSink(key);
    return key;
  }

  async function check() {
    const key = validKey();
    if (!key) return null;
    try {
      const s = await sinkStatus(key);
      if (disposed) return null;
      n.textContent = String(s.listeners);
      n.hidden = false;
      text.textContent = s.listeners > 0
        ? `${s.listeners === 1 ? "1 machine is" : `${s.listeners} machines are`} listening on “${key}”. Speech sent to it plays there.`
        : `Nobody is listening on “${key}” yet. Turn on Speak on this desk on that machine (steps above), then check again.`;
      return s.listeners;
    } catch (err) {
      text.textContent = explain(err);
      return null;
    }
  }

  checkBtn.addEventListener("click", () => busy(checkBtn, "Checking…", check));
  testBtn.addEventListener("click", () => busy(testBtn, "Sending…", async () => {
    const key = validKey();
    if (!key) return;
    note.textContent = "";
    try {
      const res = await sinkSay(key, "Voxxy is speaking on this desk.", "");
      note.textContent = res.delivered > 0
        ? `Sent. ${res.delivered === 1 ? "1 listener" : `${res.delivered} listeners`} on “${key}” should be hearing it now.`
        : `The line was made, but nobody is listening on “${key}”. Turn on Speak on this desk on that machine, then send it again.`;
      check();
    } catch (err) {
      note.textContent = explain(err);
    }
  }));

  const cmd = (label, command) => {
    const b = h("button", { class: "btn btn--small", type: "button", "aria-label": `Copy: ${command}` }, "Copy");
    b.addEventListener("click", () => copyText(command));
    return h("div", { class: "cmd" }, h("span", { class: "small muted" }, label), h("code", {}, command), b);
  };

  root.append(h("div", { class: "page" },
    h("div", { class: "page__head" },
      h("h1", { tabindex: "-1" }, "Speakers"),
      h("p", { class: "lede" }, "Speak on this desk turns a computer into a place Voxxy can talk through. Whatever is sent to its desk name plays on that computer's speakers.")),
    h("div", { class: "studio" },
      h("section", { class: "panel stack", "aria-labelledby": "how-h" },
        h("h2", { id: "how-h" }, "Turn it on for a machine"),
        h("ol", { class: "howto" },
          h("li", {}, h("div", { class: "stack stack--sm" },
            h("p", {}, h("strong", {}, "Install the Voxxy tool on that machine and point it here.")),
            cmd("Terminal", `voxxy setup --url ${base}`))),
          h("li", {}, h("div", { class: "stack stack--sm" },
            h("p", {}, h("strong", {}, "Switch on Speak on this desk.")),
            h("p", { class: "muted small" }, "In the HeyMa tray menu, choose Speak on this desk. Or in a terminal:"),
            cmd("Terminal", "voxxy sink on"))),
          h("li", {}, h("div", { class: "stack stack--sm" },
            h("p", {}, h("strong", {}, "Check it here.")),
            h("p", { class: "muted small" }, "The desk name is the machine's host name unless you chose another. Enter it on the right and press Check.")))),
        h("p", { class: "muted small" }, "Turn it off the same way, with the tray item or ", h("span", { class: "mono" }, "voxxy sink off"), ".")),
      h("section", { class: "panel stack", "aria-labelledby": "chk-h" },
        h("h2", { id: "chk-h" }, "Check a desk"),
        h("div", { class: "field" },
          h("label", { for: "spk-name" }, "Desk name"),
          input,
          h("p", { class: "hint", id: "spk-hint" }, "Saved in this browser, and shared with the Say page.")),
        h("div", { class: "listeners" }, n, text),
        h("div", { class: "row" }, checkBtn, testBtn),
        note))));

  if (input.value.trim() && SINK_RE.test(input.value.trim())) check();
  return () => { disposed = true; };
}
