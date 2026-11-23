import mermaid from "mermaid";
import { prepareMermaidSource } from "./mermaid-source.js";

const diagram = document.getElementById("diagram");
let queue = Promise.resolve();
let sequence = 0;
let resourceBlocked = false;

function notify(message) {
  window.parent.postMessage({ type: "flamethrower:mermaid", ...message }, "*");
}

function measure() {
  if (!diagram.querySelector("svg")) return;
  notify({ height: Math.ceil(diagram.getBoundingClientRect().height) + 16 });
}

document.addEventListener("securitypolicyviolation", () => {
  resourceBlocked = true;
  notify({ error: "This diagram tried to load a resource that is not allowed." });
});

window.addEventListener("message", (event) => {
  if (event.source !== window.parent || event.data?.type !== "flamethrower:render-mermaid") return;
  const { source, dark, background, color } = event.data;
  if (typeof source !== "string") return;
  queue = queue.then(async () => {
    resourceBlocked = false;
    document.documentElement.style.colorScheme = dark ? "dark" : "light";
    document.documentElement.style.backgroundColor = background;
    document.body.style.backgroundColor = background;
    document.body.style.color = color;
    mermaid.initialize({
      startOnLoad: false,
      securityLevel: "strict",
      suppressErrorRendering: true,
      theme: dark ? "dark" : "default",
      themeVariables: { background },
      htmlLabels: false,
      secure: ["secure", "securityLevel", "startOnLoad", "suppressErrorRendering", "maxTextSize", "maxEdges", "htmlLabels"],
    });
    try {
      const { svg } = await mermaid.render(`diagram-${++sequence}`, prepareMermaidSource(source));
      if (resourceBlocked) return;
      diagram.innerHTML = svg;
      diagram.querySelector("svg")?.setAttribute("role", "img");
      measure();
    } catch (error) {
      notify({ error: `Could not render Mermaid diagram: ${error.message}` });
    }
  });
});

new ResizeObserver(measure).observe(diagram);
notify({ ready: true });
