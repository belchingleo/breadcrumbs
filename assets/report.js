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

async function copyText(value) {
  if (navigator.clipboard && window.isSecureContext) {
    await navigator.clipboard.writeText(value);
    return;
  }
  const field = document.createElement("textarea");
  field.value = value;
  field.setAttribute("readonly", "");
  field.style.position = "fixed";
  field.style.opacity = "0";
  document.body.append(field);
  field.select();
  document.execCommand("copy");
  field.remove();
}

document.querySelectorAll("[data-copy-prompt]").forEach((control) => {
  control.addEventListener("click", async () => {
    const template = document.getElementById(control.dataset.copyPrompt);
    if (!template) return;
    const original = control.textContent;
    try {
      await copyText(template.content?.textContent || template.textContent || "");
      control.dataset.state = "copied";
      control.textContent = "已复制，可回到对话继续";
      window.setTimeout(() => {
        delete control.dataset.state;
        control.textContent = original;
      }, 2500);
    } catch {
      control.textContent = "复制失败，请展开后手动复制";
    }
  });
});

function prepareRouteMap(overview) {
  const scroller = overview?.querySelector(".route-scroll");
  const map = overview?.querySelector(".route-map");
  if (!scroller || !map) return;
  map.style.setProperty("--map-pad", "0px");
}

const lastLocationBySession = new Map();

function setCurrentSection(sessionKey, section) {
  const locationKey = section ? `section:${section.id}` : "orientation";
  if (lastLocationBySession.get(sessionKey) === locationKey) return;
  lastLocationBySession.set(sessionKey, locationKey);

  document.querySelectorAll(
    `[data-route-node][data-session-key="${sessionKey}"]`
  ).forEach((node) => {
    node.classList.remove("is-active");
    node.removeAttribute("aria-current");
  });
  document.querySelectorAll(
    `[data-route-edge][data-session-key="${sessionKey}"]`
  ).forEach((edge) => edge.classList.remove("is-active"));
  document.querySelectorAll(
    `[data-thought][data-session-key="${sessionKey}"]`
  ).forEach((thought) => thought.classList.remove("is-active"));
}

function setActiveThread(sessionKey, thread) {
  const locationKey = `thought:${thread}`;
  lastLocationBySession.set(sessionKey, locationKey);
  document.querySelectorAll(
    `[data-route-node][data-session-key="${sessionKey}"]`
  ).forEach((node) => {
    const isActive = node.dataset.thread === thread;
    node.classList.toggle("is-active", isActive);
    if (isActive) {
      node.setAttribute("aria-current", "location");
    } else {
      node.removeAttribute("aria-current");
    }
  });
  document.querySelectorAll(
    `[data-route-edge][data-session-key="${sessionKey}"]`
  ).forEach((edge) => {
    edge.classList.toggle("is-active", edge.dataset.toThread === thread);
  });
  document.querySelectorAll(
    `[data-thought][data-session-key="${sessionKey}"]`
  ).forEach((thought) => {
    thought.classList.toggle("is-active", thought.dataset.thread === thread);
  });
  // 左侧面包屑负责跟随正文；顶部来路图保持从起点向右展开，不因阅读位置
  // 自动横移。否则用户一边向下读，页面上方的空间参照会悄悄改变。
}

function updateReadingPosition() {
  const probeY = Math.min(window.innerHeight * 0.18, 160);

  document.querySelectorAll(".session").forEach((session) => {
    const sessionKey = session.querySelector("[data-session-key]")?.dataset.sessionKey;
    if (!sessionKey) return;
    const sections = [...session.querySelectorAll("[data-review-section]")];
    if (!sections.length) return;

    const activeSection = sections.find((section) => {
      const box = section.getBoundingClientRect();
      return box.top <= probeY && box.bottom > probeY;
    }) || sections
      .filter((section) => section.getBoundingClientRect().top <= probeY)
      .at(-1);

    if (!activeSection) {
      session.querySelectorAll("[data-section-link]").forEach((link) => {
        link.classList.remove("is-active");
      });
      setCurrentSection(sessionKey, null);
      return;
    }

    session.querySelectorAll("[data-section-link]").forEach((link) => {
      link.classList.toggle(
        "is-active",
        link.getAttribute("href") === `#${activeSection.id}`
      );
    });

    if (activeSection.classList.contains("thoughts-section")) {
      const thoughts = [...activeSection.querySelectorAll("[data-thought]")];
      const activeThought = thoughts.find((thought) => {
        const box = thought.getBoundingClientRect();
        return box.top <= probeY && box.bottom > probeY;
      });
      if (activeThought) {
        setActiveThread(
          activeThought.dataset.sessionKey,
          activeThought.dataset.thread
        );
        return;
      }
    }

    setCurrentSection(sessionKey, activeSection);
  });
}

let readingFrame = 0;
function scheduleReadingPosition() {
  window.cancelAnimationFrame(readingFrame);
  readingFrame = window.requestAnimationFrame(updateReadingPosition);
}

window.addEventListener("scroll", scheduleReadingPosition, { passive: true });
scheduleReadingPosition();

// 来路图的第一眼必须是起点。停在终点会让首屏出现一个被左边缘切断的节点,
// 而且拿不到「这条路有多长」——恰恰是这张图存在的理由。
function updateRouteEdges(overview) {
  const scroller = overview?.querySelector(".route-scroll");
  if (!scroller) return;
  const max = scroller.scrollWidth - scroller.clientWidth;
  scroller.classList.toggle("can-scroll-left", scroller.scrollLeft > 4);
  scroller.classList.toggle("can-scroll-right", scroller.scrollLeft < max - 4);
}

document.querySelectorAll(".route-overview").forEach((overview) => {
  prepareRouteMap(overview);
  const scroller = overview.querySelector(".route-scroll");
  const origin = overview.querySelector("[data-route-node]");
  if (origin) {
    window.requestAnimationFrame(() => {
      scroller.scrollTo({ left: 0, behavior: "auto" });
      updateRouteEdges(overview);
    });
  }
  scroller?.addEventListener(
    "scroll", () => updateRouteEdges(overview), { passive: true }
  );
});

let routeResizeFrame = 0;
window.addEventListener("resize", () => {
  window.cancelAnimationFrame(routeResizeFrame);
  routeResizeFrame = window.requestAnimationFrame(() => {
    document.querySelectorAll(".route-overview").forEach((overview) => {
      prepareRouteMap(overview);
      overview.querySelector(".route-scroll")?.scrollTo({
        left: 0,
        behavior: "auto",
      });
      updateRouteEdges(overview);
    });
    updateReadingPosition();
  });
});
