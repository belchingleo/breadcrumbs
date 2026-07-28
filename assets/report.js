const sessions = window.__DATA__ || [];
const sourceIndex = new Map();

for (const session of sessions) {
  for (const source of session.sources || []) {
    sourceIndex.set(source.anchor, { ...source, session: session.name });
  }
}

const dialog = document.querySelector("#source-dialog");
const closeButton = dialog?.querySelector(".dialog-close");
const label = dialog?.querySelector(".dialog-label");
const title = dialog?.querySelector(".source-title");
const text = dialog?.querySelector(".source-text");
const tech = dialog?.querySelector(".source-tech-body");

function openSource(anchor, updateLocation = true) {
  const source = sourceIndex.get(anchor);
  if (!source || !dialog) return;
  label.textContent = `${source.session} · 当时的用户输入`;
  title.textContent = source.topic || "回到当时的问题";
  text.textContent = source.text;
  tech.textContent = [
    `时间：${source.time || "未记录"}`,
    `技术定位：记录 ${source.turn}`,
    `结构主题：${source.thread}`,
    `稳定锚点：${source.anchor}`,
  ].join("\n");
  if (updateLocation) history.replaceState(null, "", `#${source.anchor}`);
  if (!dialog.open) dialog.showModal();
  closeButton?.focus();
}

document.querySelectorAll("[data-source]").forEach((control) => {
  control.addEventListener("click", (event) => {
    event.preventDefault();
    openSource(control.dataset.source);
  });
});

closeButton?.addEventListener("click", () => dialog.close());
dialog?.addEventListener("click", (event) => {
  if (event.target === dialog) dialog.close();
});
dialog?.addEventListener("close", () => {
  if (location.hash.startsWith("#prompt-")) {
    history.replaceState(null, "", `${location.pathname}${location.search}`);
  }
});

const initialAnchor = location.hash.slice(1);
if (initialAnchor.startsWith("prompt-")) openSource(initialAnchor, false);
