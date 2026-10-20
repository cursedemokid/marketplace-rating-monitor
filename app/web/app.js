"use strict";

const root = document.getElementById("app");
const labels = {
  attention: "Требуют внимания",
  products: "Все артикулы",
  signals: "История сигналов",
  exports: "Выгрузки",
  settings: "Пороги сигналов",
  nomenclature: "Номенклатура",
  ai: "AI и модерация",
  product: "Карточка артикула",
};
const pageHelp = {
  attention: "Последние сигналы по артикулам за выбранный период. Откройте товар, чтобы посмотреть динамику и причины.",
  products: "Все товары под наблюдением. Рейтинг показан на границах периода, а Δ оценок — сколько новых оценок каждой звёздности пришло за этот период.",
  signals: "Записи о сигналах и результате отправки: отправлен, пробный, подавлен, ожидает или ошибка.",
  exports: "Скачайте Excel с теми же датами и фильтрами, которые выбраны в списке товаров.",
  settings: "Пороги задают условия создания сигналов; подавление ограничивает частоту повторов. Изменения применяются при следующем анализе.",
  nomenclature: "Здесь администратор сверяет XLSX и правит внутренние группы товаров. Эти поля помогают фильтровать и собирать отчёты.",
  ai: "Расходы запросов AI, очередь отзывов и группы причин, ожидающие решения модератора.",
  product: "Карточка показывает рейтинг, выкуп, причины негатива и недавние сигналы выбранного товара.",
};
const signalNames = {
  rating_change: "Изменение рейтинга",
  negative_spike: "Рост негатива",
  top_rating_drop: "Снижение доли пятёрок",
  buyout_drop: "Падение выкупа",
};
const productFilterFields = [
  ["account_key", "Кабинет"],
  ["vendor_code", "Артикул", "text"],
  ["wb_code", "Код Wildberries", "number"],
  ["pack", "Пачка"],
  ["line", "Линейка"],
  ["type", "Тип"],
  ["size", "Размер"],
  ["capacity", "Вложимость"],
  ["category", "Категория"],
  ["subject_name", "Предмет Wildberries"],
  ["brand", "Бренд"],
  ["brand2", "Бренд-2"],
  ["nomenclature", "Номенклатура"],
  ["kit", "Комплект"],
  ["kit_qty", "Количество в комплекте", "number-select"],
];
const state = {
  token: null,
  user: null,
  page: "attention",
  productId: null,
  search: "",
  productFilters: {},
  filterOptions: {},
  attention: [],
  productRows: [],
  renderGeneration: 0,
  nomenclatureBytes: null,
  ...defaultDates(),
};

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, character => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[character]);
}
function dayInMoscow() {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: "Europe/Moscow", year: "numeric", month: "2-digit", day: "2-digit",
  }).formatToParts(new Date());
  const byType = Object.fromEntries(parts.map(part => [part.type, part.value]));
  return `${byType.year}-${byType.month}-${byType.day}`;
}
function shiftDay(value, offset) {
  const date = new Date(`${value}T00:00:00Z`);
  date.setUTCDate(date.getUTCDate() + offset);
  return date.toISOString().slice(0, 10);
}
function defaultDates() {
  const end = shiftDay(dayInMoscow(), -1);
  return { start: shiftDay(end, -6), end };
}
const numberFormatters = new Map();
function number(value, digits = 2) {
  if (value == null) return "Нет данных";
  if (!numberFormatters.has(digits)) {
    numberFormatters.set(digits, new Intl.NumberFormat("ru-RU", {
      minimumFractionDigits: digits, maximumFractionDigits: digits,
    }));
  }
  return numberFormatters.get(digits).format(Number(value));
}
function signed(value, digits = 2) {
  if (value == null) return "Нет данных";
  return `${value > 0 ? "+" : ""}${number(value, digits)}`;
}
function percent(value) {
  return value == null ? "Нет данных" : `${number(value * 100, 1)}%`;
}
function comparisonStatus(status) {
  return ({
    ok: "",
    new: "новое",
    zero: "—",
    missing: "Нет данных",
    empty: "Нет оценок",
    correction: "Корректировка WB",
  })[status] ?? "Нет данных";
}
function changeText(change, unit = "%") {
  if (!change || change.status !== "ok" || change.value == null) {
    return comparisonStatus(change?.status || "missing");
  }
  return `${signed(change.value, 1)}${unit}`;
}
function comparisonMetric(value, status, formatter = value => number(value, 0)) {
  if (status === "missing" || value == null) return "Нет данных";
  if (status === "empty") return "Нет оценок";
  if (status === "zero") return "—";
  return `${formatter(value)}${status === "correction" ? " · Корректировка WB" : ""}`;
}
function plural(count, one, few, many) {
  const lastTwo = count % 100;
  if (lastTwo >= 11 && lastTwo <= 14) return many;
  const last = count % 10;
  return last === 1 ? one : last >= 2 && last <= 4 ? few : many;
}
function dateText(value) {
  if (!value) return "—";
  const day = String(value).slice(0, 10).split("-");
  return day.length === 3 ? `${day[2]}.${day[1]}.${day[0]}` : String(value);
}
function accountName(value) {
  return ({ default: "НК Джакс", DemoSeller: "Элалис" })[value] || value || "—";
}
function moscowDateTime(value) {
  if (!value) return "—";
  const parsed = new Date(value);
  return Number.isNaN(parsed.valueOf()) ? String(value) : new Intl.DateTimeFormat("ru-RU", {
    timeZone: "Europe/Moscow", dateStyle: "short", timeStyle: "short",
  }).format(parsed);
}
function moneyUsd(value) {
  return value == null ? "—" : `$${Number(value).toLocaleString("en-US", {
    minimumFractionDigits: 6, maximumFractionDigits: 6,
  })}`;
}
function params(extra = {}, includeFilters = true) {
  const query = new URLSearchParams({ start: state.start, end: state.end });
  if (includeFilters) {
    if (state.search) query.append("q", state.search);
    for (const [key, value] of Object.entries(state.productFilters)) {
      if (value !== "" && value != null) query.append(key, value);
    }
  }
  for (const [key, value] of Object.entries(extra)) if (value != null) query.append(key, value);
  return query.toString();
}

function productFilterControl([key, label, kind]) {
  const selected = String(state.productFilters[key] ?? "");
  if (kind === "text" || kind === "number") {
    return `<label>${label}<input data-product-filter="${key}" type="${kind}"
      ${kind === "number" ? 'min="1" step="1"' : ""} value="${esc(selected)}"></label>`;
  }
  const choices = state.filterOptions[key] || [];
  return `<label>${label}<select data-product-filter="${key}">
    <option value="">Все</option>${choices.map(value => `<option value="${esc(value)}"
      ${selected === String(value) ? "selected" : ""}>${esc(key === "account_key" ? accountName(value) : value)}</option>`).join("")}
  </select></label>`;
}

async function request(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  const response = await fetch(path, { ...options, headers, cache: "no-store" });
  if (response.status === 401 && state.token) {
    state.token = null;
    state.user = null;
    loginScreen("Сеанс закончился. Войдите ещё раз.");
    throw new Error("Сеанс закончился");
  }
  if (!response.ok) {
    let detail = `Ошибка ${response.status}`;
    try { detail = (await response.json()).detail || detail; } catch { /* empty response */ }
    throw new Error(typeof detail === "string" ? detail : "Проверьте введённые данные");
  }
  return response;
}
async function json(path, options = {}) {
  return (await request(path, options)).json();
}

function loginScreen(message = "") {
  root.innerHTML = `
    <div class="login-shell">
      <section class="login-story">
        <div class="brand"><span class="brand-mark">W</span> WB Монитор</div>
        <div><h1>Рейтинг под наблюдением.</h1>
        <p>Открывайте день с артикулов, где действительно изменилась ситуация. Причина сигнала всегда рядом.</p>
        <div class="story-lines" aria-hidden="true"><i></i><i></i><i></i></div></div>
        <div class="small">Внутренний рабочий инструмент команды</div>
      </section>
      <div class="login-panel"><form class="login-card" id="login-form">
        <h2>Вход</h2><p class="muted">Введите выданные администратором логин и пароль.</p>
        <label class="field">Логин<input name="username" autocomplete="username" required maxlength="64"></label>
        <label class="field">Пароль<input name="REDACTED" type="REDACTED" autocomplete="current-REDACTED" required></label>
        <p class="error" id="login-error" role="alert">${esc(message)}</p>
        <button class="primary" type="submit">Войти</button>
      </form></div>
    </div>`;
  document.getElementById("login-form").addEventListener("submit", async event => {
    event.preventDefault();
    const form = event.currentTarget;
    const button = form.querySelector("button");
    button.disabled = true;
    document.getElementById("login-error").textContent = "";
    try {
      const credentials = Object.fromEntries(new FormData(form));
      const result = await json("/api/login", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(credentials),
      });
      state.token = result.access_token;
      state.user = await json("/api/me");
      state.filterOptions = await json("/api/filters/options");
      renderShell();
      await loadCurrent();
    } catch (error) {
      document.getElementById("login-error").textContent = error.message;
    } finally { button.disabled = false; }
  });
}

function renderShell() {
  const nav = Object.entries(labels).filter(([page]) =>
    page !== "product" && (!["nomenclature", "ai"].includes(page) || state.user.role === "admin"))
    .map(([page, label]) => `<button data-page="${page}" class="${state.page === page ? "active" : ""}">${label}</button>`).join("");
  root.innerHTML = `
    <div class="app-shell">
      <aside class="rail">
        <div class="brand"><span class="brand-mark">W</span> WB Монитор</div>
        <nav class="nav" aria-label="Разделы">${nav}</nav>
        <div class="rail-foot"><strong>${esc(state.user.username)}</strong><br>
          <span class="muted small">${state.user.role === "admin" ? "Администратор" : "Наблюдатель"}</span><br>
          <button id="logout">Выйти</button></div>
      </aside>
      <main class="main">
        <div class="topbar"><div><h1 id="page-title"></h1><p id="page-subtitle"></p></div>
          <div class="meta">Даты по Москве · данные Wildberries</div></div>
        <details class="page-help"><summary>Что здесь показано</summary><p id="page-help-text"></p></details>
        <div class="filters" id="filters">
          <label>С даты<input id="start-date" type="date" value="${state.start}"></label>
          <label>По дату<input id="end-date" type="date" value="${state.end}"></label>
          <label class="search" id="quick-search">Быстрый поиск<input id="search" type="search" placeholder="Название, артикул или код WB" value="${esc(state.search)}"></label>
          <button class="secondary" id="apply-period">Показать</button>
          <details id="advanced-filters" ${Object.values(state.productFilters).some(Boolean) ? "open" : ""}>
            <summary>Фильтры товаров</summary>
            <div class="filter-grid">${productFilterFields.map(productFilterControl).join("")}</div>
            <div class="filter-foot"><span>Поля номенклатуры загружаются администратором из XLSX.</span>
              <button class="secondary" id="clear-filters" type="button">Сбросить фильтры</button></div>
          </details>
        </div>
        <div class="content" id="content" aria-live="polite"></div>
      </main>
    </div>`;
  document.getElementById("filters").hidden = ["settings", "nomenclature", "ai"].includes(state.page);
  document.getElementById("advanced-filters").hidden = state.page === "product";
  document.getElementById("quick-search").hidden = state.page === "product";
  document.querySelectorAll("[data-page]").forEach(button => button.addEventListener("click", async () => {
    state.page = button.dataset.page;
    state.productId = null;
    renderShell();
    await loadCurrent();
  }));
  document.getElementById("logout").addEventListener("click", async () => {
    state.renderGeneration += 1;
    try { await request("/api/logout", { method: "POST" }); } catch { /* already expired */ }
    state.token = null;
    state.user = null;
    loginScreen();
  });
  document.getElementById("apply-period").addEventListener("click", async () => {
    const start = document.getElementById("start-date").value;
    const end = document.getElementById("end-date").value;
    if (!start || !end || end < start) {
      showError("Выберите период: дата окончания должна быть не раньше начала.");
      return;
    }
    state.start = start;
    state.end = end;
    state.search = document.getElementById("search").value.trim();
    state.productFilters = Object.fromEntries(
      productFilterFields.map(([key]) => [key,
        document.querySelector(`[data-product-filter="${key}"]`).value.trim()])
    );
    await loadCurrent();
  });
  document.getElementById("clear-filters").addEventListener("click", async () => {
    state.search = "";
    state.productFilters = {};
    renderShell();
    await loadCurrent();
  });
}

function heading(title, subtitle) {
  document.getElementById("page-title").textContent = title;
  document.getElementById("page-subtitle").textContent = subtitle;
  document.getElementById("page-help-text").textContent = pageHelp[state.page] || "";
  document.querySelectorAll(".nav button").forEach(button => {
    button.classList.toggle("active", button.dataset.page === state.page);
  });
}
function showError(message) {
  document.getElementById("content").innerHTML = `<div class="empty error" role="alert">${esc(message)}</div>`;
}
async function loadCurrent() {
  const content = document.getElementById("content");
  if (!content) return;
  const generation = ++state.renderGeneration;
  const page = state.page;
  const productId = state.productId;
  content.innerHTML = `<div class="empty">Загружаем данные…</div>`;
  try {
    if (page === "attention") await loadAttention(generation);
    else if (page === "products") await loadProducts(generation);
    else if (page === "signals") await loadSignals(generation);
    else if (state.page === "exports") showExports();
    else if (page === "settings") await loadSettings(generation);
    else if (state.page === "nomenclature") showNomenclature();
    else if (page === "ai") await loadAi(generation);
    else if (page === "product") await loadProduct(generation, productId);
  } catch (error) {
    if (state.token && isCurrentRender(generation, page, productId)) showError(error.message);
  }
}
function isCurrentRender(generation, page, productId) {
  return generation === state.renderGeneration && page === state.page &&
    (page !== "product" || productId === state.productId);
}
function productAction(nmId) {
  state.productId = Number(nmId);
  state.page = "product";
  renderShell();
  loadCurrent();
}

async function loadAttention(generation = state.renderGeneration) {
  heading("Требуют внимания", "Последний сигнал по каждому артикулу в выбранном периоде.");
  const items = await json(`/api/attention?${params({ since: state.start, until: state.end, limit: 100 })}`);
  if (!isCurrentRender(generation, "attention", null)) return;
  state.attention = items;
  showAttention(state.attention);
}
function showAttention(items) {
  const shown = items;
  const content = document.getElementById("content");
  if (!shown.length) {
    content.innerHTML = `<div class="empty"><strong>За период сигналов нет.</strong><br>
      Проверьте другой период. Если сбор ещё не запускался, данные появятся после первого полного прогона.</div>`;
    return;
  }
  const countWord = plural(shown.length, "артикул", "артикула", "артикулов");
  content.innerHTML = `<div class="section-head"><h2>Недавние проблемы</h2><span>${shown.length} ${countWord}</span></div>
    <div class="attention-list">${shown.map(item => `
      <button class="attention-item" data-product="${Number(item.nm_id)}">
        <span><span class="sku">${esc(item.vendor_code)}</span><br><span class="subline">WB ${Number(item.nm_id)}</span></span>
        <span class="reason-block"><span class="reason">${esc(signalNames[item.signal_type] || item.signal_type)}</span>
          <span class="subline">${esc(item.ai_summary || "Причина уточняется")} · ${dateText(item.period_end)}</span></span>
        <span class="badge">Сигнал</span>
      </button>`).join("")}</div>`;
  content.querySelectorAll("[data-product]").forEach(button =>
    button.addEventListener("click", () => productAction(button.dataset.product)));
}

async function loadProducts(generation = state.renderGeneration) {
  heading("Все артикулы", "Рейтинг и изменение числа оценок за выбранный период.");
  const report = await json(`/api/reports/ratings?${params({ include_rating_history: false })}`);
  if (!isCurrentRender(generation, "products", null)) return;
  state.productRows = report.rows;
  showProducts(report.rows, report);
}
function showProducts(rows, report = {}) {
  const content = document.getElementById("content");
  const admin = state.user.role === "admin";
  content.innerHTML = `<div class="section-head"><h2>Товары под наблюдением</h2><div class="section-actions"><span>${rows.length} ${plural(rows.length, "артикул", "артикула", "артикулов")}</span>
    ${admin ? '<button class="primary" id="open-catalog" type="button">Добавить товары</button>' : ""}</div></div>
    <div class="period-note"><strong>Текущий период:</strong> ${dateText(state.start)} — ${dateText(state.end)} · <strong>Предыдущий период:</strong> <span id="previous-period">сравнение по данным отчёта</span><br>Относительное изменение = (текущее − предыдущее) / предыдущее × 100%; доля = число оценок звёздности / все оценки; разница долей — в процентных пунктах.</div>
    <details class="metric-help"><summary>Как читать рейтинг и оценки</summary><div class="help-copy">
      <p><strong>Рейтинг в начале</strong> — снимок WB за день до выбранной даты начала. <strong>Рейтинг в конце</strong> — снимок за конечную дату. <strong>Изменение</strong> — конечное значение минус начальное; это пункты рейтинга, не проценты.</p>
      <p>В столбце оценок показано количество новых оценок за период в порядке 5★ / 4★ / 3★ / 2★ / 1★. Например, <strong>+7 / +1 / 0 / 0 / +2</strong> — семь пятёрок, одна четвёрка и две единицы. «Нет данных» означает, что снимок отсутствует, а не нулевое значение.</p>
    </div></details>
    ${rows.length ? `<div class="table-wrap"><table class="comparison-table"><thead><tr><th class="sticky-id" rowspan="2">Кабинет</th><th class="sticky-id" rowspan="2">Название товара</th><th class="sticky-id" rowspan="2">Артикул</th><th rowspan="2">Код WB</th>
    <th colspan="8">Рейтинг и оценки за текущий период</th>${[5,4,3,2,1,"negative"].map(key=>`<th colspan="7">${key === "negative" ? "Негативные" : `${key}★`}</th>`).join("")}<th colspan="3">Выкуп</th></tr><tr>
    <th>Рейтинг в начале</th><th>Рейтинг в конце</th><th>Изменение, пункты</th><th>Рейтинг Δ %</th><th>Оценки 5 / 4 / 3 / 2 / 1, Δ</th><th>Всего оценок, пред.</th><th>Всего, тек.</th><th>Всего Δ %</th>${[5,4,3,2,1,"негативные"].map(key => `<th>пред.</th><th>тек.</th><th>изменение %</th><th>доля пред.</th><th>доля тек.</th><th>доля Δ %</th><th>доля Δ п.п.</th>`).join("")}<th>пред., %</th><th>тек., %</th><th>Δ, п.п.</th></tr></thead><tbody>
    ${rows.map(comparisonRow).join("")}
    </tbody></table></div>` : `<div class="empty">Артикулы не найдены. Проверьте поиск или дождитесь первого сбора WB.</div>`}
    ${admin ? '<dialog id="catalog-dialog" class="catalog-dialog" aria-labelledby="catalog-title"><form method="dialog" class="dialog-close-form"><button class="dialog-x" aria-label="Закрыть">×</button></form><h2 id="catalog-title">Добавить товары в мониторинг</h2><p class="muted">Каталог содержит карточки из подключённых кабинетов. Новые карточки появятся после сбора Wildberries.</p><div class="catalog-controls"><label>Поиск по названию, артикулу или коду WB<input id="catalog-query" type="search" autocomplete="off"></label><label>Кабинет<select id="catalog-account"><option value="">Все кабинеты</option><option value="default">НК Джакс</option><option value="DemoSeller">Элалис</option></select></label></div><div id="catalog-results" class="catalog-results" aria-live="polite"></div><div class="dialog-actions"><span id="catalog-selection-count" class="muted">Ничего не выбрано</span><button class="secondary" id="cancel-catalog" type="button">Отмена</button><button class="primary" id="submit-catalog" type="button" disabled>Добавить выбранные</button></div></dialog>' : ""}`;
  if (report.previous_start && report.previous_end) document.getElementById("previous-period").textContent = `${dateText(report.previous_start)} — ${dateText(report.previous_end)}`;
  content.querySelectorAll("[data-product]").forEach(button =>
    button.addEventListener("click", () => productAction(button.dataset.product)));
  if (admin) wireCatalog();
}

function comparisonRow(row) {
  const comparison = row.comparison;
  const ratingTrend = row.rating_delta == null ? "" : row.rating_delta < 0 ? "trend-down" : "trend-up";
  const legacyDeltas = [5, 4, 3, 2, 1].map(star => signed(row[`ratings_${star}_delta`], 0)).join(" / ");
  const totals = ["previous", "current"].map(period => {
    const metrics = comparison?.[period];
    return `<td class="numeric">${comparisonMetric(metrics?.counts?.total, metrics?.status)}</td>`;
  }).join("");
  const buckets = ["5", "4", "3", "2", "1", "negative"]
    .map(key => comparisonCells(comparison, key)).join("");
  return `<tr><td class="sticky-id">${esc(accountName(row.account_key))}</td>
    <td class="sticky-id">${esc(row.title || "Название не указано")}</td>
    <td class="sticky-id"><button class="link-button" data-product="${Number(row.nm_id)}">${esc(row.vendor_code)}</button></td>
    <td>${Number(row.nm_id)}</td><td class="numeric">${number(row.rating_start)}</td>
    <td class="numeric">${number(row.rating_end)}</td><td class="numeric ${ratingTrend}">${signed(row.rating_delta)}</td>
    <td class="numeric">${changeText(comparison?.rating_change_pct)}</td><td>${legacyDeltas}</td>
    ${totals}<td class="numeric">${changeText(comparison?.count_changes_pct?.total)}</td>${buckets}
    <td class="numeric">${comparisonMetric(comparison?.buyout_previous, "ok", value => number(value,1))}</td>
    <td class="numeric">${comparisonMetric(comparison?.buyout_current, "ok", value => number(value,1))}</td>
    <td class="numeric">${changeText(comparison?.buyout_delta_pp," п.п.")}</td></tr>`;
}

function comparisonCells(comparison, key) {
  const previous = comparison?.previous;
  const current = comparison?.current;
  const count = (period) => comparisonMetric(period?.counts?.[key], period?.status);
  const share = key === "total" ? () => "—" : (period) => period?.status === "ok"
    ? percent(period.shares?.[key])
    : comparisonStatus(period?.status || "missing");
  return `<td class="numeric">${count(previous)}</td><td class="numeric">${count(current)}</td>
    <td class="numeric">${changeText(comparison?.count_changes_pct?.[key])}</td>
    <td class="numeric">${share(previous)}</td><td class="numeric">${share(current)}</td>
    <td class="numeric">${changeText(comparison?.share_changes_pct?.[key])}</td>
    <td class="numeric">${changeText(comparison?.share_changes_pp?.[key], " п.п.")}</td>`;
}

function productComparison(row, report) {
  const comparison = row?.comparison;
  if (!comparison) return `<div class="empty">Нет данных для сравнения периодов.</div>`;
  const dates = `Текущий период: ${dateText(report.start || state.start)} — ${dateText(report.end || state.end)} · Предыдущий период: ${dateText(report.previous_start)} — ${dateText(report.previous_end)}`;
  const rows = ["total", "5", "4", "3", "2", "1", "negative"].map(key => {
    const title = key === "total" ? "Все оценки" : key === "negative" ? "Негативные" : `Оценки ${key}★`;
    return `<tr><th scope="row">${title}</th>${comparisonCells(comparison, key)}</tr>`;
  }).join("");
  return `<section class="panel product-comparison"><h2>Сравнение периодов</h2><p class="muted">${dates}</p>
    <div class="table-wrap"><table><thead><tr><th>Метрика</th><th>Предыдущий, шт.</th><th>Текущий, шт.</th><th>Изменение, %</th><th>Доля пред.</th><th>Доля тек.</th><th>Доля Δ, %</th><th>Доля Δ, п.п.</th></tr></thead><tbody>${rows}</tbody></table></div>
    <div class="comparison-summary"><span>Рейтинг: ${number(row.rating_start)} → ${number(row.rating_end)}; изменение ${signed(row.rating_delta)} пункта; относительное изменение ${changeText(comparison.rating_change_pct)}.</span>
    <span>Выкуп: предыдущий ${comparisonMetric(comparison.buyout_previous,"ok",value=>`${number(value,1)}%`)}, текущий ${comparisonMetric(comparison.buyout_current,"ok",value=>`${number(value,1)}%`)}, изменение ${changeText(comparison.buyout_delta_pp," п.п.")}.</span></div>
  </section>`;
}

function wireCatalog() {
  const dialog = document.getElementById("catalog-dialog");
  const results = document.getElementById("catalog-results");
  const selected = new Set();
  let timer;
  let sequence = 0;
  let submitting = false;
  const selectionCount = () => {
    const count = selected.size;
    document.getElementById("catalog-selection-count").textContent = count
      ? `Выбрано: ${count}${count >= 100 ? " (достигнут лимит)" : ""}` : "Ничего не выбрано";
    document.getElementById("submit-catalog").disabled = submitting || count === 0;
  };
  const search = async () => {
    const current = ++sequence;
    results.innerHTML = '<div class="empty">Загружаем каталог…</div>';
    try {
      const query = new URLSearchParams({ q: document.getElementById("catalog-query").value.trim(),
        account_key: document.getElementById("catalog-account").value, limit: "100" });
      const catalog = await json(`/api/products/catalog?${query}`);
      if (current !== sequence) return;
      results.innerHTML = catalog.products.length ? `<ul class="catalog-list">${catalog.products.map(product => {
        const id = Number(product.nm_id);
        const already = Boolean(product.is_monitored);
        return `<li><label class="catalog-item"><input type="checkbox" data-catalog-id="${id}" ${already ? 'data-monitored="true"' : ""} ${already || submitting ? "disabled" : selected.has(id) ? "checked" : ""}>
          <span><strong>${esc(product.title || "Название не указано")}</strong><small>${esc(accountName(product.account_key))} · ${esc(product.vendor_code || "Артикул не указан")} · WB ${id}</small></span>
          ${already ? '<span class="badge neutral">Уже наблюдается</span>' : ""}</label></li>`;
      }).join("")}</ul><p class="catalog-total">Показано ${catalog.products.length} из ${Number(catalog.total || 0).toLocaleString("ru-RU")}. Уточните поиск, если товара нет в списке.</p>`
        : '<div class="empty">Карточки не найдены. Проверьте запрос или дождитесь сбора карточек из подключённого кабинета.</div>';
      results.querySelectorAll("[data-catalog-id]").forEach(input => input.addEventListener("change", () => {
        const id = Number(input.dataset.catalogId);
        if (input.checked && selected.size >= 100) {
          input.checked = false;
          results.insertAdjacentHTML("afterbegin", '<p class="error" role="alert">За один раз можно добавить не более 100 товаров.</p>');
          return;
        }
        if (input.checked) selected.add(id); else selected.delete(id);
        selectionCount();
      }));
    } catch (error) {
      if (current === sequence) results.innerHTML = `<div class="empty error" role="alert">Не удалось загрузить каталог: ${esc(error.message)}</div>`;
    }
  };
  document.getElementById("open-catalog").addEventListener("click", () => {
    selected.clear(); selectionCount(); dialog.showModal(); search();
  });
  dialog.addEventListener("cancel", event => {
    if (submitting) event.preventDefault();
  });
  document.getElementById("cancel-catalog").addEventListener("click", () => dialog.close());
  document.getElementById("catalog-query").addEventListener("input", () => {
    clearTimeout(timer); timer = setTimeout(search, 250);
  });
  document.getElementById("catalog-account").addEventListener("change", search);
  document.getElementById("submit-catalog").addEventListener("click", async event => {
    const button = event.currentTarget;
    if (submitting || selected.size === 0 || selected.size > 100) return;
    submitting = true;
    dialog.querySelectorAll("#catalog-query, #catalog-account, [data-catalog-id]:not([data-monitored='true']), .dialog-x, #cancel-catalog").forEach(control => { control.disabled = true; });
    button.disabled = true;
    button.textContent = "Добавляем…";
    let result;
    try {
      result = await json("/api/products/monitoring", { method: "POST",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify({ nm_ids: [...selected] }) });
    } catch (error) {
      if (dialog.isConnected) results.insertAdjacentHTML("afterbegin", `<div class="empty error" role="alert">${esc(error.message)}</div>`);
      submitting = false;
      if (dialog.isConnected) {
        dialog.querySelectorAll("#catalog-query, #catalog-account, [data-catalog-id]:not([data-monitored='true']), .dialog-x, #cancel-catalog").forEach(control => { control.disabled = false; });
        button.textContent = "Добавить выбранные";
        selectionCount();
      }
      return;
    }

    const addedMessage = `Добавлено: ${Number(result.added)}. Уже наблюдались: ${Number(result.already_monitored)}.`;
    dialog.close();
    const refreshList = async () => {
      const content = document.getElementById("content");
      content.innerHTML = `<div class="notice" role="status">${esc(addedMessage)}</div><div class="empty">Обновляем список товаров…</div>`;
      try {
        state.filterOptions = await json("/api/filters/options");
        await loadProducts();
        const notice = document.createElement("div");
        notice.className = "notice";
        notice.setAttribute("role", "status");
        notice.textContent = addedMessage;
        document.getElementById("content").prepend(notice);
      } catch (error) {
        const currentContent = document.getElementById("content");
        currentContent.innerHTML = `<div class="notice" role="status">${esc(addedMessage)}</div><div class="empty error" role="alert">Товары добавлены, но список не удалось обновить: ${esc(error.message)}<br><button class="secondary" id="retry-products" type="button">Обновить список</button></div>`;
        document.getElementById("retry-products").addEventListener("click", () => loadCurrent());
      }
    };
    await refreshList();
    submitting = false;
    if (dialog.isConnected) {
      dialog.querySelectorAll("#catalog-query, #catalog-account, [data-catalog-id]:not([data-monitored='true']), .dialog-x, #cancel-catalog").forEach(control => { control.disabled = false; });
      button.textContent = "Добавить выбранные";
      selectionCount();
    }
  });
}

function chart(values, dates, label, unit, minValue = null, maxValue = null, statuses = null) {
  const valid = values.map((value, index) => [index, value]).filter(([index, value]) => statuses?.[index] !== "correction" && value != null && Number.isFinite(Number(value)));
  const table=`<div class="table-wrap chart-values"><table><caption>${esc(label)}: значения по дням (${esc(unit)})</caption><thead><tr><th>Дата</th><th>Значение, ${esc(unit)}</th>${statuses?"<th>Состояние</th>":""}</tr></thead><tbody>${values.map((value,index)=>`<tr><td>${dateText(dates[index])}</td><td class="numeric">${value==null?"Нет данных":number(value,1)}</td>${statuses?`<td>${comparisonStatus(statuses[index])}</td>`:""}</tr>`).join("")}</tbody></table></div>`;
  if (!valid.length) return `<div class="empty">За период нет точек для графика.</div>${table}`;
  const low = minValue ?? Math.min(...valid.map(([, value]) => Number(value)));
  const high = maxValue ?? Math.max(...valid.map(([, value]) => Number(value)));
  const span = Math.max(high - low, .01);
  const steps = Math.max(values.length - 1, 1);
  const groups=[]; for (const pair of valid) { if (!groups.length || pair[0] !== groups.at(-1).at(-1)[0]+1) groups.push([]); groups.at(-1).push(pair); }
  const lines=groups.map(group=>group.map(([index,value])=>`${12+index*276/steps},${101-(Number(value)-low)*86/span}`).join(" "));
  return `<svg class="line-chart" viewBox="DemoPhone" role="img" aria-label="${esc(label)}"><text x="4" y="12">${number(high,1)} ${esc(unit)}</text>${lines.map(points=>`<polyline points="${points}"></polyline>`).join("")}</svg>${table}`;
}

function starDistributionChart(daily, dates) {
  const rawSeries = [5,4,3,2,1].map(star => daily.map(point => point?.counts?.[String(star)] ?? null));
  const series = rawSeries.map(values => values.map((value,index)=>daily[index]?.status === "correction" ? null : value));
  const valid = series.flatMap(values => values.filter(value => value != null).map(Number));
  const colors=["#6946b8","#26817b","#ca8b24","#d45c6b","#587990"];
  const table=`<div class="table-wrap chart-values"><table><caption>Оценки 5★–1★: значения по дням (оценок)</caption><thead><tr><th>Дата</th>${[5,4,3,2,1].map(star=>`<th>${star}★</th>`).join("")}<th>Состояние</th></tr></thead><tbody>${dates.map((date,index)=>`<tr><td>${dateText(date)}</td>${rawSeries.map(values=>`<td class="numeric">${values[index]==null?"Нет данных":number(values[index],0)}</td>`).join("")}<td>${comparisonStatus(daily[index]?.status)}</td></tr>`).join("")}</tbody></table></div>`;
  if (!valid.length) return `<div class="empty">За период нет точек для графика.</div>${table}`;
  const low=0, high=Math.max(...valid), span=Math.max(high-low,.01), steps=Math.max(dates.length-1,1);
  const polylines=series.map((values,si)=>{
    const groups=[]; values.forEach((value,index)=>{if(value==null)return;if(!groups.length||index!==groups.at(-1).at(-1)[0]+1)groups.push([]);groups.at(-1).push([index,value]);});
    return groups.map(group=>`<polyline style="stroke:${colors[si]}" points="${group.map(([index,value])=>`${12+index*276/steps},${101-Number(value)*86/span}`).join(" ")}"></polyline>`).join("");
  }).join("");
  return `<svg class="line-chart" viewBox="DemoPhone" role="img" aria-label="Структура новых оценок по звёздам"><text x="4" y="12">${number(high,0)} оценок</text>${polylines}</svg>${table}<p class="chart-legend">${[5,4,3,2,1].map((star,index)=>`<span style="color:${colors[index]}">${star}★</span>`).join(" · ")}</p>`;
}

async function loadProduct(generation = state.renderGeneration, nmId = state.productId) {
  const [card, rating, causes, signals] = await Promise.all([
    json(`/api/products/${nmId}/snapshots?${params({}, false)}`),
    json(`/api/reports/ratings?${params({ nm_id: nmId }, false)}`),
    json(`/api/reports/reasons?${params({ nm_id: nmId }, false)}`),
    json(`/api/signals?nm_id=${nmId}&limit=30`),
  ]);
  if (!isCurrentRender(generation, "product", nmId)) return;
  heading(card.title || "Название не указано", `${accountName(card.account_key)} · ${card.vendor_code} · Код WB ${Number(nmId)}${card.brand ? ` · ${card.brand}` : ""}${card.size ? ` · ${card.size}` : ""}`);
  const row = rating.rows[0];
  const current = card.snapshots.at(-1);
  const daily = card.daily || [];
  const dates = daily.map(point => point.snapshot_date);
  const buyoutMap = new Map(card.snapshots.map(point => [String(point.snapshot_date).slice(0,10), point.buyout_percent == null ? null : Number(point.buyout_percent)]));
  const buyout = dates.map(day => buyoutMap.has(day) ? buyoutMap.get(day) : null);
  const dayMap = key => daily.map(point => point[key] || {});
  const dailyCounts=dayMap("counts"), dailyShares=dayMap("shares");
  const dailyStatuses=daily.map(point=>point.status);
  const ratingSeries = rating.ratings_by_day[String(nmId)] || [];
  const ratingMap = new Map((rating.dates || []).map((day,index) => [String(day).slice(0,10), ratingSeries[index] == null ? null : Number(ratingSeries[index])]));
  const ratingValues = dates.map(day => ratingMap.has(day) ? ratingMap.get(day) : null);
  const dayIndex = causes.negative_counts.findLastIndex(count => count > 0);
  const topCauses = causes.rows.filter(reason => !reason.is_external)
    .map(reason => ({ name: reason.name, share: reason.shares[dayIndex] }))
    .filter(reason => reason.share > 0 && reason.name !== "причина не указана")
    .sort((a, b) => b.share - a.share).slice(0, 5);
  const content = document.getElementById("content");
  const chartPanel = (title, values, unit, min, max, statuses=null) => `<section class="panel"><h2>${title}</h2>${chart(values,dates,title,unit,min,max,statuses)}<div class="chart-note"><span>${dateText(dates[0])}</span><span>${dateText(dates.at(-1))}</span></div></section>`;
  content.innerHTML = `<div class="stat-grid">
    <div class="stat"><span class="muted small">Рейтинг на конец</span><strong>${number(row?.rating_end)}</strong></div>
    <div class="stat"><span class="muted small">Изменение рейтинга</span><strong class="${row?.rating_delta < 0 ? "trend-down" : ""}">${signed(row?.rating_delta)}</strong></div>
    <div class="stat"><span class="muted small">Выкуп, последняя точка</span><strong>${current?.buyout_percent == null ? "Нет данных" : `${number(current.buyout_percent, 1)}%`}</strong></div>
  </div>${productComparison(row,rating)}<div class="product-grid">
    ${chartPanel("Динамика рейтинга",ratingValues,"балла",4,5)}
    ${chartPanel("Динамика выкупа",buyout,"%",0,100)}
    ${chartPanel("Новые оценки всего",dailyCounts.map(v=>v.total),"оценок",0,null,dailyStatuses)}
    ${chartPanel("Новые негативные оценки",dailyCounts.map(v=>v.negative),"оценок",0,null,dailyStatuses)}
    ${chartPanel("Доля негатива",dailyShares.map(v=>v.negative == null ? null : Number(v.negative)*100),"%",0,100,dailyStatuses)}
    <section class="panel"><h2>Структура новых оценок по звёздам</h2>${starDistributionChart(daily,dates)}<div class="chart-note"><span>${dateText(dates[0])}</span><span>${dateText(dates.at(-1))}</span></div></section>
  </div><div class="product-grid"><section class="panel"><h2>Причины негатива ${dayIndex >= 0 ? `за ${dateText(causes.dates[dayIndex])}` : ""}</h2>
    ${topCauses.length ? `<div class="table-wrap"><table><tbody>${topCauses.map(cause =>
      `<tr><td>${esc(cause.name)}</td><td class="numeric">${percent(cause.share)}</td></tr>`).join("")}</tbody></table></div>` :
      `<div class="empty">Выявленных причин за выбранный период нет.</div>`}</section>
    <section class="panel"><h2>Структура новых оценок</h2>
      ${row ? `<div class="table-wrap"><table><tbody>${[5,4,3,2,1].map(star =>
        `<tr><td>Оценки ${star}★</td><td class="numeric">${signed(row[`ratings_${star}_delta`], 0)}</td></tr>`).join("")}</tbody></table></div>` :
        `<div class="empty">Нет снимков на границах периода.</div>`}</section></div>
    <section class="panel"><h2>Последние сигналы</h2>${signalTable(signals)}</section>`;
}

function signalStatus(signal) {
  if (signal.is_suppressed) return "Подавлен";
  if (signal.dry_run_at) return "Пробный";
  if (signal.sent_at) return "Отправлен";
  if (signal.send_error) return "Ошибка отправки";
  return "Ожидает отправки";
}
function aiRequestStatus(status) {
  return ({ reserved: "Зарезервировано", settled: "Оплачено по факту", uncertain: "Расход неизвестен" })[status] || status || "—";
}
function signalTable(signals) {
  if (!signals.length) return `<div class="empty">Автоматических сигналов пока нет.</div>`;
  return `<div class="table-wrap"><table><thead><tr><th>Дата</th><th>Артикул</th><th>Сигнал</th><th>Причина</th><th>Статус</th></tr></thead><tbody>
    ${signals.map(signal => `<tr><td>${dateText(signal.period_end)}</td>
      <td>${esc(signal.vendor_code)}</td><td>${esc(signalNames[signal.signal_type] || signal.signal_type)}</td>
      <td>${esc(signal.ai_summary || "—")}</td><td><span class="badge neutral">${signalStatus(signal)}</span></td></tr>`).join("")}
    </tbody></table></div>`;
}
async function loadSignals(generation = state.renderGeneration) {
  heading("История сигналов", "Что монитор заметил и что произошло с уведомлением.");
  const items = await json(`/api/signals?${params({ since: state.start, until: state.end, limit: 100 })}`);
  if (!isCurrentRender(generation, "signals", null)) return;
  document.getElementById("content").innerHTML = `<div class="section-head"><h2>Последние записи</h2>
    <span>До 100 сигналов · включая пробные и подавленные</span></div>${signalTable(items)}`;
}

function showExports() {
  heading("Выгрузки", "Файлы Excel за выбранный период и с выбранными фильтрами.");
  document.getElementById("content").innerHTML = `
    <div class="notice">К выгрузкам применяются те же фильтры, что к списку артикулов. Если все поля пустые, файл включает все отслеживаемые артикулы.</div>
    <div class="export-row"><div><h2>Динамика рейтингов и оценок</h2><p>Рейтинг на границах периода, изменение числа оценок и график.</p></div>
      <button class="primary" data-export="ratings">Скачать отчёт 1</button></div>
    <div class="export-row"><div><h2>Причины негатива</h2><p>Доли причин по дням, уникальный итог и объёмы отзывов.</p></div>
      <button class="primary" data-export="reasons">Скачать отчёт 2</button></div>
    <p id="export-message" role="status"></p>`;
  document.querySelectorAll("[data-export]").forEach(button => button.addEventListener("click", async () => {
    button.disabled = true;
    const message = document.getElementById("export-message");
    message.textContent = "Готовим файл…";
    try {
      const response = await request(`/api/exports/${button.dataset.export}?${params()}`);
      const url = URL.createObjectURL(await response.blob());
      const link = document.createElement("a");
      link.href = url;
      link.download = `${button.dataset.export}_${state.start}_${state.end}.xlsx`;
      link.click();
      URL.revokeObjectURL(url);
      message.textContent = "Файл скачан.";
    } catch (error) { message.textContent = error.message; }
    finally { button.disabled = false; }
  }));
}

const nomenclatureFields = [
  ["pack", "Пачка"], ["line", "Линейка"], ["type", "Тип"],
  ["capacity", "Вложимость"], ["category", "Категория"],
  ["brand2", "Бренд-2"], ["kit", "Комплект"],
  ["kit_qty", "Количество в комплекте"],
];

function showNomenclature() {
  heading("Номенклатура", "Сверка XLSX и точечная правка внутренних полей товара.");
  document.getElementById("content").innerHTML = `
    <section class="panel"><h2>Импорт XLSX</h2>
      <p class="muted">Сначала проверьте строки и расхождения, затем подтвердите загрузку.</p>
      <input id="nomenclature-file" type="file" accept=".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet">
      <button class="secondary" id="preview-nomenclature" type="button">Сверить файл</button>
      <div id="nomenclature-preview" role="status"></div>
    </section>
    <section class="panel"><h2>Правка товара</h2>
      <form id="nomenclature-lookup" class="nomenclature-lookup">
        <label>Кабинет<select name="account_key"><option value="default">НК Джакс</option><option value="DemoSeller">Элалис</option></select></label>
        <label>Артикул продавца<input name="vendor_code" required></label>
        <button class="secondary" type="submit">Открыть</button>
      </form>
      <div id="nomenclature-editor" role="status"></div>
    </section>`;
  document.getElementById("preview-nomenclature").addEventListener("click", async () => {
    const file = document.getElementById("nomenclature-file").files[0];
    const box = document.getElementById("nomenclature-preview");
    if (!file) { box.textContent = "Выберите XLSX."; return; }
    box.textContent = "Сверяем файл…";
    try {
      const bytes = await file.arrayBuffer();
      const preview = await json("/api/nomenclature/preview", {
        method: "POST", headers: { "Content-Type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" },
        body: bytes,
      });
      state.nomenclatureBytes = preview.can_import ? bytes : null;
      const problems = [
        ["Ошибки строк", preview.row_errors], ["Повторы", preview.duplicates],
        ["Нет в базе WB", preview.unknown_products], ["Расхождения кодов", preview.discrepancies],
        ["Нет в файле", preview.missing_in_file],
      ];
      box.innerHTML = `<p>Строк: ${preview.rows.length}; готовы к импорту: ${preview.valid_count}.</p>
        ${problems.map(([label, items]) => `<details ${items.length ? "open" : ""}><summary>${label}: ${items.length}</summary>
          <ul>${items.map(item => `<li>${item.row_number ? `Строка ${item.row_number}: ` : ""}${esc(item.account_key || "")} ${esc(item.vendor_code || "")} ${esc(item.message)}</li>`).join("")}</ul></details>`).join("")}
        <details><summary>Посмотреть ${preview.rows.length} строк</summary><div class="table-wrap"><table><thead><tr>
          <th>Строка</th><th>Кабинет</th><th>Артикул</th><th>Код WB</th>${nomenclatureFields.map(([, label]) => `<th>${label}</th>`).join("")}
          </tr></thead><tbody>${preview.rows.map(row => `<tr><td>${row.row_number}</td><td>${esc(row.account_key)}</td>
          <td>${esc(row.vendor_code)}</td><td>${row.nm_id}</td>${nomenclatureFields.map(([key]) => `<td>${esc(row[key] ?? "")}</td>`).join("")}</tr>`).join("")}</tbody></table></div></details>
        ${preview.can_import ? '<button class="primary" id="confirm-nomenclature" type="button">Подтвердить импорт</button>' : '<p class="error">Исправьте ошибки перед импортом.</p>'}`;
      document.getElementById("confirm-nomenclature")?.addEventListener("click", async event => {
        const button = event.currentTarget;
        button.disabled = true;
        try {
          const result = await json("/api/nomenclature/import", {
            method: "POST", headers: { "Content-Type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" },
            body: state.nomenclatureBytes,
          });
          state.filterOptions = await json("/api/filters/options");
          state.nomenclatureBytes = null;
          box.innerHTML = `<p>Загружено товаров: ${result.imported}.</p>`;
        } catch (error) { box.textContent = error.message; button.disabled = false; }
      });
    } catch (error) { state.nomenclatureBytes = null; box.textContent = error.message; }
  });
  document.getElementById("nomenclature-lookup").addEventListener("submit", async event => {
    event.preventDefault();
    const values = Object.fromEntries(new FormData(event.currentTarget));
    const box = document.getElementById("nomenclature-editor");
    const path = `/api/nomenclature/${encodeURIComponent(values.account_key)}/${encodeURIComponent(values.vendor_code.trim())}`;
    try {
      const record = await json(path);
      box.innerHTML = `<form id="nomenclature-edit"><div class="filter-grid">${nomenclatureFields.map(([key, label]) =>
        `<label>${label}<input name="${key}" value="${esc(record[key] ?? "")}" ${key === "kit_qty" ? 'type="number" min="0" step="1"' : ""}></label>`).join("")}</div>
        <button class="primary" type="submit">Сохранить</button></form>`;
      document.getElementById("nomenclature-edit").addEventListener("submit", async editEvent => {
        editEvent.preventDefault();
        const payload = Object.fromEntries([...new FormData(editEvent.currentTarget)].map(([key, value]) =>
          [key, key === "kit_qty" ? (value === "" ? null : Number(value)) : (value.trim() || null)]));
        try {
          await json(path, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
          state.filterOptions = await json("/api/filters/options");
          box.insertAdjacentHTML("beforeend", '<p role="status">Сохранено.</p>');
        } catch (error) { box.insertAdjacentHTML("beforeend", `<p class="error">${esc(error.message)}</p>`); }
      });
    } catch (error) { box.textContent = error.message; }
  });
}

async function loadAi(generation = state.renderGeneration) {
  heading("AI и модерация", "Расходы проекта и новые причины до утверждения администратором.");
  const [costs, candidates] = await Promise.all([
    json("/api/ai/costs"), json("/api/moderation/candidates?candidate_status=pending"),
  ]);
  if (!isCurrentRender(generation, "ai", null)) return;
  const used = Number(costs.committed_usd);
  document.getElementById("content").innerHTML = `
    <section class="panel"><h2>Расходы AI за ${esc(costs.month.slice(0, 7))}</h2>
      <p><strong>${moneyUsd(used)}</strong> из ${moneyUsd(costs.limit_usd)} зарезервировано или оплачено.</p>
      <p>Остаток бюджета: <strong>${moneyUsd(costs.remaining_usd)}</strong>.</p>
      <p>Ждут AI: <strong>${esc(costs.pending_text)}</strong> с текстом; пустых отзывов: <strong>${esc(costs.pending_blank)}</strong>.
      Закреплены за запросами без меток: <strong>${esc(costs.claimed_without_labels)}</strong>.</p>
      <p class="muted">Токены входа включают текст отзыва, инструкции к товару и служебный запрос; токены выхода — ответ модели. Для завершённого запроса показана фактическая стоимость, для остальных — максимальный резерв. Неизвестный расход учитывается по полному резерву и может занимать лимит проекта. Отзывы без ответа видны администратору для проверки и отдельного повтора.</p>
      <details class="metric-help"><summary>Что означают столбцы и статусы</summary><div class="help-copy">
        <p><strong>Вход, токены</strong> — части текста запроса, которые модель обработала: системные и модельные инструкции, словарь/данные товара и все отзывы в одной пачке. Это не число отзывов.</p>
        <p><strong>Выход, токены</strong> — части текста, созданные моделью в ответе.</p>
        <p><strong>Стоимость, $</strong> — фактическая стоимость для завершённого запроса; для незавершённого или неопределённого показан максимальный резерв.</p>
        <p><strong>Статус</strong>: Зарезервировано (reserved) — ждём результат; Оплачено по факту (settled) — фактическая стоимость известна; Расход неизвестен (uncertain) — результат или стоимость неизвестны, в лимите учитывается весь резерв.</p>
      </div></details>
      <div class="table-wrap"><table><thead><tr><th>Время</th><th>Модель</th><th>Статус</th><th>Вход, токены</th><th>Выход, токены</th><th>Стоимость, $</th></tr></thead><tbody>
        ${costs.requests.map(item => `<tr><td>${esc(moscowDateTime(item.reserved_at))}</td><td>${esc(item.model)}</td>
          <td title="${esc(item.status)}">${esc(aiRequestStatus(item.status))} (${esc(item.status)})</td><td class="numeric">${item.input_tokens == null ? "—" : Number(item.input_tokens).toLocaleString("ru-RU")}</td><td class="numeric">${item.output_tokens == null ? "—" : Number(item.output_tokens).toLocaleString("ru-RU")}</td>
          <td class="numeric" title="${item.status === "settled" ? "Фактическая стоимость" : "Максимальный резерв"}">${moneyUsd(item.status === "settled" ? item.actual_cost_usd : item.estimated_cost_usd)}</td></tr>`).join("")}</tbody></table></div>
    </section>
    <section class="panel"><h2>Отзывы без результата AI</h2>
      ${costs.unlabeled_claims.length ? `<div class="table-wrap"><table><thead><tr><th>ID отзыва</th><th>Первый запрос</th><th>Повтор</th></tr></thead><tbody>
        ${costs.unlabeled_claims.map(item => `<tr><td>${esc(item.feedback_id)}</td>
          <td>${esc(item.original_request_id)} · ${esc(item.original_status)}</td>
          <td>${item.retry_request_id ? `${esc(item.retry_request_id)} · ${esc(item.retry_status)}` : "—"}</td></tr>`).join("")}
      </tbody></table></div>` : '<p class="muted">Нет закреплённых отзывов без меток.</p>'}
    </section>
    <section class="panel"><h2>Ежедневная классификация</h2>
      ${costs.classify_runs.length ? `<div class="table-wrap"><table><thead><tr><th>Дата</th><th>Статус</th><th>Причина</th></tr></thead><tbody>
        ${costs.classify_runs.map(item => `<tr><td>${esc(item.run_date)}</td><td>${esc(item.classify_status)}</td><td>${esc(item.classify_detail ?? "—")}</td></tr>`).join("")}
      </tbody></table></div>` : '<p class="muted">Прогонов пока нет.</p>'}
    </section>
    <section class="panel"><h2>Ожидают решения: ${candidates.length}</h2>
      ${candidates.length ? candidates.map(item => `<div class="settings-row" data-candidate="${item.id}">
        <div><strong>Группа ${item.id}</strong> · ${item.feedback_ids.length} отзывов
          <ul>${item.examples.map(example => `<li>${esc(example)}</li>`).join("")}</ul>
          <label>Название причины<input data-candidate-name value="${esc(item.name ?? "")}" maxlength="200"></label>
          <label>Объединить с группой №<input data-merge-target type="number" min="1" step="1"></label>
        </div><div class="settings-actions">
          <button class="secondary" data-action="rename">Сохранить название</button>
          <button class="primary" data-action="approve">Утвердить</button>
          <button class="secondary" data-action="merge">Объединить</button>
          <button class="secondary" data-action="reject">Отклонить</button>
        </div></div>`).join("") : '<p class="muted">Новых групп пока нет.</p>'}
    </section>`;
  document.querySelectorAll("[data-candidate] [data-action]").forEach(button => button.addEventListener("click", async () => {
    const row = button.closest("[data-candidate]");
    const id = row.dataset.candidate;
    const action = button.dataset.action;
    const path = `/api/moderation/candidates/${id}/${action === "rename" ? "name" : action}`;
    const method = action === "rename" ? "PATCH" : "POST";
    let payload = null;
    if (action === "rename") payload = { name: row.querySelector("[data-candidate-name]").value.trim() };
    if (action === "merge") payload = { target_id: Number(row.querySelector("[data-merge-target]").value) };
    if (action === "approve" && !row.querySelector("[data-candidate-name]").value.trim()) {
      row.insertAdjacentHTML("beforeend", '<p class="error">Сначала задайте и сохраните название.</p>');
      return;
    }
    button.disabled = true;
    try {
      if (action === "approve") {
        await json(`/api/moderation/candidates/${id}/name`, {
          method: "PATCH", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name: row.querySelector("[data-candidate-name]").value.trim() }),
        });
      }
      await json(path, { method, headers: { "Content-Type": "application/json" },
        body: payload ? JSON.stringify(payload) : undefined });
      await loadAi();
    } catch (error) { row.insertAdjacentHTML("beforeend", `<p class="error">${esc(error.message)}</p>`); button.disabled = false; }
  }));
}

async function loadSettings(generation = state.renderGeneration) {
  heading("Пороги сигналов", "Изменения вступают в силу со следующего анализа.");
  const settings = await json("/api/signal-settings");
  if (!isCurrentRender(generation, "settings", null)) return;
  const admin = state.user.role === "admin";
  document.getElementById("content").innerHTML = `
    ${admin ? "" : `<div class="notice">У вас доступ для просмотра. Пороги меняет администратор.</div>`}
    <div class="panel"><div class="section-head"><h2>Правила мониторинга</h2><span>Порог: пункты рейтинга или процентные пункты (п.п.); подавление — часы</span></div>
    ${settings.map(setting => `<div class="settings-row" data-rule="${esc(setting.signal_type)}">
      <strong>${esc(signalNames[setting.signal_type] || setting.signal_type)}</strong>
      <label>Порог, ${setting.signal_type === "rating_change" ? "пункты рейтинга" : "п.п."}<input class="control" data-field="threshold" type="number" step="0.01" min="0" value="${Number(setting.threshold)}" ${admin ? "" : "disabled"}></label>
      <label>Подавление, ч<input class="control" data-field="suppress_hours" type="number" step="1" min="0" value="${Number(setting.suppress_hours)}" ${admin ? "" : "disabled"}></label>
      <label>Включено<input data-field="is_enabled" type="checkbox" ${setting.is_enabled ? "checked" : ""} ${admin ? "" : "disabled"}></label>
      ${admin ? `<button class="secondary save-rule">Сохранить</button>` : ""}
      <span class="small muted message" role="status"></span></div>`).join("")}</div>`;
  document.querySelectorAll(".save-rule").forEach(button => button.addEventListener("click", async () => {
    const row = button.closest("[data-rule]");
    const message = row.querySelector(".message");
    button.disabled = true;
    try {
      const thresholdInput = row.querySelector('[data-field="threshold"]');
      const suppressInput = row.querySelector('[data-field="suppress_hours"]');
      if (thresholdInput.value === "" || suppressInput.value === "") {
        throw new Error("Заполните порог и часы подавления");
      }
      await json(`/api/signal-settings/${encodeURIComponent(row.dataset.rule)}`, {
        method: "PATCH", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          threshold: Number(thresholdInput.value),
          suppress_hours: Number(suppressInput.value),
          is_enabled: row.querySelector('[data-field="is_enabled"]').checked,
        }),
      });
      message.textContent = "Сохранено";
    } catch (error) { message.textContent = error.message; }
    finally { button.disabled = false; }
  }));
}

loginScreen();
