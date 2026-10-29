// 首页和股票页共用：顶部的“历史比较”下拉框决定历史分位按过去几年计算。
// 各个回看年数下的结果在生成页面时已经算好，嵌在 #view-data 里，这里只负责切换显示。
(function () {
  const dataEl = document.getElementById("view-data");
  const view = dataEl ? JSON.parse(dataEl.textContent) : { default: 10, options: [], fields: {} };
  const select = document.getElementById("years-select");
  const listeners = [];
  const STORAGE_KEY = "valuation.years";

  const valid = (v) => (v != null && view.options.map(String).includes(String(v)) ? String(v) : null);
  const stored = () => {
    try { return localStorage.getItem(STORAGE_KEY); } catch (e) { return null; }  // 隐私模式等情况下可能不可用
  };
  const store = (years) => {
    try { localStorage.setItem(STORAGE_KEY, years); } catch (e) { /* 不影响使用 */ }
  };

  // 优先用网址里的 ?years=，其次是上次的选择，最后是默认值
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
    // 页面之间的链接带上所选年数：即使浏览器不允许保存设置，换页后选择也不会丢
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
      history.replaceState(null, "", u.pathname + u.search + u.hash);  // 刷新或分享网址时保持选择
    }
    render(years);
    listeners.forEach((fn) => fn(years));
  }

  if (select) select.addEventListener("change", () => setYears(select.value, true));

  window.ValuationView = {
    view,
    // 登记切换时要执行的函数，并立刻按当前选择执行一次（用于画图）
    onChange(fn) {
      listeners.push(fn);
      fn(current);
    },
  };

  if (select) select.value = current;
  render(current);
})();
