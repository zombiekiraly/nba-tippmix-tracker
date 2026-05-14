/**
 * NBA Tippmix Tracker — app.js
 * Kiegészítő szkript: keresési kiemelés, billentyűparancsok, apró UX javítások.
 * A fő Firebase logika az index.html <script type="module"> blokkjában található.
 */

// ── Keresési kiemelés ─────────────────────────────────────────────────────
function highlightText(text, query) {
  if (!query) return text;
  const escaped = query.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const regex = new RegExp(`(${escaped})`, "gi");
  return text.replace(regex, '<mark>$1</mark>');
}

// ── Billentyűparancsok ────────────────────────────────────────────────────
document.addEventListener("keydown", (e) => {
  // Ctrl/Cmd + K → fókusz a keresőmezőre
  if ((e.ctrlKey || e.metaKey) && e.key === "k") {
    e.preventDefault();
    const activeTab = document.querySelector(".tab-content.active");
    const searchInput = activeTab?.querySelector("input[type='text']");
    searchInput?.focus();
    searchInput?.select();
  }
});

// ── Táblázat exportálás (CSV) ─────────────────────────────────────────────
function tableToCSV(tableId) {
  const table = document.getElementById(tableId);
  if (!table) return "";
  const rows = [...table.querySelectorAll("tr")];
  return rows.map(row => {
    const cells = [...row.querySelectorAll("th, td")];
    return cells.map(c => {
      const text = c.innerText.replace(/\n/g, " ").trim();
      return `"${text.replace(/"/g, '""')}"`;
    }).join(",");
  }).join("\n");
}

function downloadCSV(filename, csv) {
  const blob = new Blob(["﻿" + csv], { type: "text/csv;charset=utf-8;" });
  const url  = URL.createObjectURL(blob);
  const a    = document.createElement("a");
  a.href     = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

// CSV letöltés gombok dinamikus hozzáadása
window.addEventListener("DOMContentLoaded", () => {
  const controlsBars = document.querySelectorAll(".controls-bar");
  const tableIds = ["players-table", "teams-table", "ou-table"];
  const labels   = ["játékosok", "csapatok", "ou-elemzés"];

  controlsBars.forEach((bar, i) => {
    const btn = document.createElement("button");
    btn.textContent = "⬇ CSV";
    btn.title = `${labels[i]} exportálása CSV fájlba`;
    btn.className = "csv-btn";
    btn.style.cssText = `
      background: var(--bg);
      border: 1px solid var(--border);
      border-radius: 4px;
      color: var(--text-muted);
      cursor: pointer;
      font-size: .78rem;
      font-weight: 700;
      padding: 4px 10px;
      align-self: center;
      transition: border-color .2s, color .2s;
    `;
    btn.addEventListener("mouseenter", () => {
      btn.style.borderColor = "var(--accent)";
      btn.style.color = "var(--accent)";
    });
    btn.addEventListener("mouseleave", () => {
      btn.style.borderColor = "var(--border)";
      btn.style.color = "var(--text-muted)";
    });
    btn.addEventListener("click", () => {
      const csv = tableToCSV(tableIds[i]);
      if (csv) {
        const date = new Date().toISOString().slice(0,10);
        downloadCSV(`nba_${labels[i]}_${date}.csv`, csv);
      }
    });
    bar.appendChild(btn);
  });
});

// ── Szám formázó a mark elemek utáni kiemeléshez ──────────────────────────
const markStyle = document.createElement("style");
markStyle.textContent = `
  mark {
    background: rgba(91, 140, 255, 0.35);
    color: inherit;
    border-radius: 2px;
    padding: 0 1px;
  }
`;
document.head.appendChild(markStyle);
