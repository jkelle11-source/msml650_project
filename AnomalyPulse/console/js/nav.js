// Pages: the console shows one part of the story at a time. The address's hash names the
// page (#overview, #incident, ...), so a page can be linked to and the browser's back button
// works. Left and right arrow keys move between pages, which helps when presenting.

export const PAGES = [
  { id: "overview", label: "Overview" },
  { id: "incident", label: "Incident" },
  { id: "diagnosis", label: "AI diagnosis" },
  { id: "verify", label: "Ground truth" },
  { id: "timeline", label: "Timeline" },
];

export function pageFromHash(hash) {
  const id = String(hash ?? "").replace(/^#/, "");
  return PAGES.some((p) => p.id === id) ? id : PAGES[0].id;
}

// The neighbours of a page, for the Previous / Next links.
export function neighbours(id) {
  const i = PAGES.findIndex((p) => p.id === id);
  return { prev: PAGES[i - 1] ?? null, next: PAGES[i + 1] ?? null };
}

// Each tab's dot, from one dashboard reply: what a presenter needs to know before clicking.
export function tabStatus(reply) {
  const incident = reply?.incident ?? null;
  const noData = reply?.health?.status === "NO_DATA";
  const health = noData ? "none" : incident ? "alarm" : "ok";
  if (!incident) return { overview: health, incident: "none", diagnosis: "none", verify: "none", timeline: "none" };
  return {
    overview: health,
    incident: "alarm",
    diagnosis: incident.diagnosis ? "info" : "pending",
    verify: incident.ground_truth ? "info" : "none",
    timeline: "info",
  };
}

const $ = (id) => document.getElementById(id);

export function showPage() {
  const page = pageFromHash(location.hash);
  for (const el of document.querySelectorAll("[data-page]")) el.classList.toggle("off-page", el.dataset.page !== page);
  for (const tab of document.querySelectorAll("[data-tab]")) {
    if (tab.dataset.tab === page) tab.setAttribute("aria-current", "page");
    else tab.removeAttribute("aria-current");
  }
  // A page whose sections the data has hidden (no incident) shows its own empty note instead.
  for (const note of document.querySelectorAll("[data-empty]")) {
    const sections = [...document.querySelectorAll(`[data-page="${note.dataset.empty}"]`)];
    note.hidden = note.dataset.empty !== page || !sections.every((s) => s.hidden);
  }
  const { prev, next } = neighbours(page);
  for (const [link, target, text] of [[$("prev-page"), prev, "Previous: "], [$("next-page"), next, "Next: "]]) {
    link.hidden = !target;
    if (target) { link.href = `#${target.id}`; link.textContent = `${text}${target.label}`; }
  }
}

export function updateTabs(reply) {
  const status = tabStatus(reply);
  for (const { id } of PAGES) $(`dot-${id}`).className = `tab-dot dot-${status[id]}`;
  showPage();
}

export function initNav() {
  window.addEventListener("hashchange", () => { showPage(); window.scrollTo({ top: 0 }); });
  document.addEventListener("keydown", (event) => {
    if (event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
    if (event.target.closest("input, select, textarea, [contenteditable]")) return;
    const { prev, next } = neighbours(pageFromHash(location.hash));
    const target = event.key === "ArrowRight" ? next : event.key === "ArrowLeft" ? prev : null;
    if (target) { event.preventDefault(); location.hash = target.id; }
  });
  showPage();
}
