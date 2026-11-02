// Shared by the home page and the stock pages: the "History" drop-down at the top decides how many past years the percentile is based on.
// The results for every window are computed when the page is generated and embedded in #view-data; this script only switches what is shown.
(function () {
  const dataEl = document.getElementById("view-data");
  const view = dataEl ? JSON.parse(dataEl.textContent) : { default: 10, options: [], fields: {} };
  const select = document.getElementById("years-select");
  const listeners = [];
  const STORAGE_KEY = "valuation.years";

  const valid = (v) => (v != null && view.options.map(String).includes(String(v)) ? String(v) : null);
  const stored = () => {
    try { return localStorage.getItem(STORAGE_KEY); } catch (e) { return null; }  // may be unavailable, e.g. in private browsing
  };
  const store = (years) => {
    try { localStorage.setItem(STORAGE_KEY, years); } catch (e) { /* does not affect usage */ }
  };

  // Prefer ?years= in the URL, then the last choice, then the default
  const initialYears = () =>
    valid(new URLSearchParams(location.search).get("years")) || valid(stored()) || String(view.default);

  let current = initialYears();

  function render(years) {
    document.querySelectorAll("[data-ref]").forEach((el) => {
      const f = (view.fields[el.dataset.ref] || {})[years];
      if (!f) return;
      el.querySelectorAll("[data-f]").forEach((t) => { t.textContent = f[t.dataset.f]; });
      el.querySelectorAll("[data-f-color]").forEach((t) => t.style.setProperty("--c", f.color));
      el.querySelectorAll("[data-f-left]").forEach((t) => { t.style.left = f.percentile + "%"; });
    });
    document.querySelectorAll("[data-years-label]").forEach((t) => { t.textContent = years; });
    // Links between pages carry the selected years, so the choice survives page changes even if the browser refuses to store settings
    document.querySelectorAll("a[data-keep-years]").forEach((a) => {
      const u = new URL(a.getAttribute("href"), location.href);
      u.searchParams.set("years", years);
      a.setAttribute("href", u.pathname + u.search + u.hash);
    });
  }

  function setYears(years, remember) {
    current = years;
    if (select) select.value = years;
    if (remember) {
      store(years);
      const u = new URL(location.href);
      u.searchParams.set("years", years);
      history.replaceState(null, "", u.pathname + u.search + u.hash);  // keeps the choice on reload or when the URL is shared
    }
    render(years);
    listeners.forEach((fn) => fn(years));
  }

  if (select) select.addEventListener("change", () => setYears(select.value, true));

  window.ValuationView = {
    view,
    // Register a function to run when the choice changes, and run it once right away with the current choice (used to draw charts)
    onChange(fn) {
      listeners.push(fn);
      fn(current);
    },
  };

  if (select) select.value = current;
  render(current);
})();
