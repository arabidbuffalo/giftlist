const $ = (s, el = document) => el.querySelector(s);
const state = { items: [], admin: false, editing: null };

async function api(path, opts = {}) {
  const r = await fetch(path, {
    headers: { "Content-Type": "application/json" }, credentials: "same-origin", ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch {}
    throw new Error(msg);
  }
  return r.json();
}

const money = (v, cur = "USD") => v == null ? "" :
  new Intl.NumberFormat(undefined, { style: "currency", currency: cur || "USD" }).format(v);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const ago = iso => {
  if (!iso) return "never";
  const m = Math.round((Date.now() - new Date(iso)) / 60000);
  if (m < 60) return `${m}m ago`;
  if (m < 2880) return `${Math.round(m / 60)}h ago`;
  return `${Math.round(m / 1440)}d ago`;
};

function sparkline(hist) {
  if (!hist || hist.length < 2) return "";
  const ps = hist.map(h => h.p), min = Math.min(...ps), max = Math.max(...ps);
  if (min === max) return "";
  const w = 64, h = 18;
  const d = ps.map((p, i) => `${i ? "L" : "M"}${(i / (ps.length - 1) * w).toFixed(1)},${(h - 2 - (p - min) / (max - min) * (h - 4)).toFixed(1)}`).join("");
  return `<svg class="spark" viewBox="0 0 ${w} ${h}" aria-hidden="true"><path d="${d}"/></svg>`;
}

function card(it) {
  const img = it.image ? `<img src="${esc(it.image)}" alt="" loading="lazy" referrerpolicy="no-referrer" onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'noimg',textContent:'🎁'}))">`
                       : `<div class="noimg">🎁</div>`;
  const sale = it.on_sale;
  const amz = it.amazon_url && it.track_url !== it.amazon_url
    ? ` · <a href="${esc(it.amazon_url)}" target="_blank" rel="noopener">Amazon</a>` : "";
  const stale = it.last_error ? `<div class="warn">⚠ ${esc(it.last_error)}</div>` : "";
  const oos = it.in_stock === 0 ? `<div class="oos">Out of stock</div>` : "";
  const star = it.priority >= 2 ? `<span class="star">★ Top pick</span>` : it.priority == 1 ? `<span class="star">♥</span>` : "";
  return `
  <article class="card ${sale ? "on-sale" : ""} ${state.admin ? "has-edit" : ""}" data-id="${it.id}">
    <a class="img" href="${esc(it.track_url)}" target="_blank" rel="noopener">${img}${star}</a>
    ${sale ? `<span class="ribbon">SALE${it.percent_off ? ` −${it.percent_off}%` : ""}</span>` : ""}
    <div class="body">
      <a class="name" href="${esc(it.track_url)}" target="_blank" rel="noopener" title="${esc(it.name)}">${esc(it.name || it.store)}</a>
      ${it.notes ? `<div class="notes">${esc(it.notes)}</div>` : ""}
      ${oos}${stale}
      <div class="price-row">
        <span class="price">${it.price ? money(it.price, it.currency) : "—"}</span>
        ${sale && it.was_price ? `<span class="was">${money(it.was_price, it.currency)}</span>` : ""}
        ${sale && it.was_price ? `<span class="save">Save ${money(it.was_price - it.price, it.currency)}</span>` : ""}
      </div>
      <div class="meta">
        <span class="store" title="Price checked ${ago(it.last_checked)}">${esc(it.store)}${amz}</span>
        ${sparkline(it.history)}
      </div>
    </div>
    ${state.admin ? `<button class="ghost edit" data-edit="${it.id}" aria-label="Edit" title="Edit">✎</button>` : ""}
  </article>`;
}

function render() {
  const saleOnly = $("#saleOnly").checked, sort = $("#sort").value;
  let active = state.items.filter(i => !i.purchased && (!saleOnly || i.on_sale));
  const bought = state.items.filter(i => i.purchased);
  const by = {
    "price-asc": (a, b) => (a.price ?? 1e9) - (b.price ?? 1e9),
    "price-desc": (a, b) => (b.price ?? -1) - (a.price ?? -1),
    "discount": (a, b) => (b.percent_off || 0) - (a.percent_off || 0),
    "new": (a, b) => b.id - a.id,
  }[sort];
  if (by) active = [...active].sort(by);
  $("#grid").innerHTML = active.map(card).join("");
  $("#empty").hidden = state.items.length > 0;
  const nSale = state.items.filter(i => !i.purchased && i.on_sale).length;
  $("#count").textContent = `${state.items.length - bought.length} items` + (nSale ? ` · ${nSale} on sale` : "");
  $("#purchasedWrap").hidden = !bought.length || !state.admin;
  $("#purchasedCount").textContent = `(${bought.length})`;
  $("#purchasedGrid").innerHTML = bought.map(card).join("");
}

async function load() {
  state.items = await api("/api/items");
  render();
}

function upsert(item) {
  const i = state.items.findIndex(x => x.id === item.id);
  if (i >= 0) state.items[i] = item; else state.items.unshift(item);
  render();
}

function setStatus(msg, kind = "") {
  const el = $("#addStatus");
  el.hidden = !msg; el.className = `status ${kind}`; el.innerHTML = msg || "";
}

// ---- add
$("#addForm").addEventListener("submit", async e => {
  e.preventDefault();
  const url = $("#addUrl").value.trim();
  if (!url) return;
  const btn = $("#addBtn"); btn.disabled = true;
  setStatus(`<span class="spinner"></span>Reading the product page…`);
  try {
    const item = await api("/api/items", { method: "POST", body: { url } });
    upsert(item);
    $("#addUrl").value = "";
    let msg = `Added “${esc(item.name)}”.`;
    if (item.variants?.length > 1) {
      const cur = item.variants.find(v => v.key === item.variant_key);
      msg += ` This page sells ${item.variants.length} versions${cur ? `; tracking <b>${esc(cur.title)}</b>` : ""}. Tap ✎ to pick a different one.`;
    }
    if (item.last_error) msg += ` ${esc(item.last_error)}`;
    setStatus(msg, item.last_error ? "error" : "");
  } catch (err) {
    setStatus(esc(err.message), "error");
  } finally { btn.disabled = false; }
});

// ---- edit dialog
const dlg = $("#editDlg"), form = $("#editForm");
document.addEventListener("click", e => {
  const id = e.target.dataset?.edit;
  if (!id) return;
  const it = state.items.find(x => x.id == id);
  state.editing = it;
  form.name.value = it.name || ""; form.price.value = it.price ?? ""; form.notes.value = it.notes || "";
  form.track_url.value = it.track_url || ""; form.image.value = it.image || ""; form.priority.value = it.priority || 0;
  const vs = it.variants || [];
  $("#variantRow").hidden = vs.length < 2;
  form.variant_key.innerHTML = vs.map(v => `<option value="${esc(v.key)}">${esc(v.title)} — ${money(v.price, it.currency)}</option>`).join("");
  form.variant_key.value = it.variant_key || "";
  dlg.classList.toggle("has-amz", !!it.amazon_url);
  form.querySelector('[data-act="purchased"]').textContent = it.purchased ? "Move back to list" : "Mark purchased";
  form.querySelector('[data-act="use_amazon"]').hidden = it.track_url === it.amazon_url;
  const ids = [it.brand && `Brand: ${it.brand}`, it.mpn && `Model: ${it.mpn}`, it.gtin && `UPC: ${it.gtin}`].filter(Boolean).join(" · ");
  $("#editMeta").textContent = `Price checked ${ago(it.last_checked)}${ids ? " · " + ids : ""}`;
  dlg.showModal();
});

document.querySelectorAll("[data-close]").forEach(b => b.addEventListener("click", () => b.closest("dialog").close()));

form.addEventListener("submit", async e => {
  const it = state.editing, body = {};
  if (form.name.value.trim() !== (it.name || "")) body.name = form.name.value;
  if (form.notes.value !== (it.notes || "")) body.notes = form.notes.value;
  if (form.price.value && Number(form.price.value) !== it.price) body.price = Number(form.price.value);
  if (form.track_url.value.trim() !== it.track_url) body.track_url = form.track_url.value.trim();
  if (form.image.value.trim() !== (it.image || "")) body.image = form.image.value.trim();
  if (Number(form.priority.value) !== (it.priority || 0)) body.priority = Number(form.priority.value);
  const vk = form.variant_key.value;
  if (vk && vk !== (it.variant_key || "")) { body.variant_key = vk; delete body.price; }
  if (!Object.keys(body).length) return;
  try { upsert(await api(`/api/items/${it.id}`, { method: "PATCH", body })); }
  catch (err) { alert(err.message); }
});

form.addEventListener("click", async e => {
  const act = e.target.dataset?.act;
  if (!act) return;
  const it = state.editing, btn = e.target, label = btn.textContent;
  btn.disabled = true; btn.innerHTML = `<span class="spinner"></span>${label}`;
  try {
    if (act === "delete") {
      if (!confirm(`Delete “${it.name}”?`)) return;
      await api(`/api/items/${it.id}`, { method: "DELETE" });
      state.items = state.items.filter(x => x.id !== it.id); render(); dlg.close(); return;
    }
    let item;
    if (act === "refresh") item = await api(`/api/items/${it.id}/refresh`, { method: "POST" });
    if (act === "use_amazon") item = await api(`/api/items/${it.id}`, { method: "PATCH", body: { use_amazon: true } });
    if (act === "purchased") item = await api(`/api/items/${it.id}`, { method: "PATCH", body: { purchased: !it.purchased } });
    upsert(item); dlg.close();
  } catch (err) { alert(err.message); }
  finally { btn.disabled = false; btn.textContent = label; }
});

// ---- auth
$("#loginBtn").addEventListener("click", async () => {
  if (state.admin && state.authEnabled) { await api("/api/logout", { method: "POST" }); return init(); }
  $("#loginErr").hidden = true; $("#loginDlg").showModal();
});
$("#loginForm").addEventListener("submit", async e => {
  e.preventDefault();
  try {
    await api("/api/login", { method: "POST", body: { password: e.target.password.value } });
    $("#loginDlg").close(); e.target.password.value = ""; init();
  } catch (err) { $("#loginErr").hidden = false; $("#loginErr").textContent = err.message; }
});

$("#refreshAll").addEventListener("click", async () => {
  const r = await api("/api/refresh-all", { method: "POST" });
  setStatus(r.message + ". Reload in a few minutes to see updates.");
});
$("#saleOnly").addEventListener("change", render);
$("#sort").addEventListener("change", render);

// ---- seasonal theme: Christmas in Nov–Dec, birthday in March, normal otherwise
function pickTheme(setting) {
  const preview = new URLSearchParams(location.search).get("theme");   // e.g. ?theme=christmas to preview
  const forced = preview || (setting && setting !== "auto" ? setting : null);
  if (forced) return ["christmas", "birthday"].includes(forced) ? forced : "default";
  const m = new Date().getMonth() + 1;
  return m >= 11 ? "christmas" : m === 3 ? "birthday" : "default";
}
function applyTheme(setting, title) {
  const theme = pickTheme(setting), emoji = { christmas: "🎄", birthday: "🎂" }[theme] || "";
  document.documentElement.dataset.theme = theme;
  if (title != null) $("#title").innerHTML = (emoji ? `<span class="theme-emoji">${emoji}</span>` : "") + esc(title);
  document.querySelector("link[rel=icon]").href =
    `data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>${emoji || "🎁"}</text></svg>`;
  let snow = $(".snow");
  if (theme === "christmas" && !snow) {
    snow = Object.assign(document.createElement("div"), { className: "snow" });
    snow.setAttribute("aria-hidden", "true");
    for (let i = 0; i < 18; i++) {
      const f = document.createElement("i"); f.textContent = "❄";
      f.style.left = `${Math.random() * 100}%`; f.style.fontSize = `${8 + Math.random() * 10}px`;
      f.style.animationDuration = `${9 + Math.random() * 12}s`; f.style.animationDelay = `${-Math.random() * 20}s`;
      snow.appendChild(f);
    }
    document.body.appendChild(snow);
  } else if (theme !== "christmas" && snow) snow.remove();
}
applyTheme();   // right away from the date, before the server answers

async function init() {
  const me = await api("/api/me");
  state.admin = me.admin; state.authEnabled = me.auth_enabled;
  document.body.classList.toggle("admin", me.admin);
  applyTheme(me.theme, me.title); document.title = me.title;
  $("#loginBtn").hidden = !me.auth_enabled;
  $("#loginBtn").textContent = me.admin ? "Sign out" : "Sign in";
  await load();
}
init();
