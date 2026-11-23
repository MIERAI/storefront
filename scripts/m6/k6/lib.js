// Shared by the M6 k6 scripts (docs/m6-verification.md). Plain ASCII on purpose: the scripts are copied to winpc.
//
// The v2 services speak Connect (connect-go): a unary call is POST <base>/<package>.<Service>/<Method> with a JSON
// body and Content-Type application/json. int64 fields come back as strings (protojson), enums as their names.
// Environment: CATALOG_URL, ORDER_URL, INVENTORY_URL (base URLs, e.g. http://100.76.236.114:31234), TOKEN (optional
// Bearer token when the services run with AUTH_MODE=token).
import http from 'k6/http';

export const CATALOG = 'catalog.catalog.v1.CatalogService';
export const ORDER = 'order.order.v1.OrderService';
export const INVENTORY = 'inventory.inventory.v1.InventoryService';

// The data the scripts own: every SKU starts with M6- so the checks can find (and only touch) them.
export const CATEGORY = 'm6';
export const ITEMS = [];
for (let i = 1; i <= 20; i++) {
  ITEMS.push({ sku: 'M6-ITEM-' + (i < 10 ? '0' : '') + i, price: (5 + i * 1.25).toFixed(2), category: CATEGORY });
}
export const FLASH = [1, 2, 3].map((i) => ({ sku: 'M6-FLASH-' + i, price: '9.99', category: 'm6-flash' }));
export const SCARCE = { sku: 'M6-SCARCE', price: '19.99', category: 'm6-flash' };
export const PRICY = { sku: 'M6-PRICY', price: '100001', category: CATEGORY };   // above payment's limit: refused
export const ALL = ITEMS.concat(FLASH, [SCARCE, PRICY]);

export function base(name) {
  const v = __ENV[name];
  if (!v) throw new Error(name + ' is not set (base URL of the service, see docs/m6-verification.md)');
  return v.replace(/\/+$/, '');
}

export function call(url, service, method, body, tags) {
  const headers = { 'Content-Type': 'application/json' };
  if (__ENV.TOKEN) headers.Authorization = 'Bearer ' + __ENV.TOKEN;
  return http.post(url + '/' + service + '/' + method, JSON.stringify(body || {}), {
    headers: headers,
    timeout: __ENV.TIMEOUT || '10s',
    tags: Object.assign({ name: service.split('.').pop() + '/' + method }, tags || {}),
  });
}

// Customer ids of one run: TAG * 1e6 + n. TAG (an integer such as the run's MMDDHHMM) lets the checks find exactly
// the orders this run placed (check.py --tag), without touching anything else in the database.
export function customerId() {
  const tag = parseInt(__ENV.TAG || '0', 10);
  if (!(tag > 0)) throw new Error('TAG must be a positive integer (e.g. the run start as MMDDHHMM)');
  return String(tag * 1000000 + Math.floor(Math.random() * 1000000));
}

export function pick(list) {
  return list[Math.floor(Math.random() * list.length)];
}

// Lines of an order: n distinct SKUs from list, qty 1..maxQty.
export function lines(list, n, maxQty) {
  const pool = list.slice();
  const out = [];
  for (let i = 0; i < n && pool.length; i++) {
    const p = pool.splice(Math.floor(Math.random() * pool.length), 1)[0];
    out.push({ line_no: i + 1, sku: p.sku, qty: 1 + Math.floor(Math.random() * maxQty), price: p.price });
  }
  return out;
}

// call() again while the network (not the service) failed: status 0 = no answer (timeout, reset). For setup / seeding
// only: the load scripts count every failure, since users see them too.
export function callRetry(url, service, method, body, tries) {
  let r;
  for (let i = 0; i < (tries || 5); i++) {
    r = call(url, service, method, body);
    if (r.status !== 0) return r;
  }
  return r;
}

export function summaryWriter(extra) {
  // handleSummary: the end-of-test summary as JSON to SUMMARY (a path), plus the usual text on stdout.
  return function (data) {
    const out = {};
    const m = (k, f) => (data.metrics[k] && data.metrics[k].values[f] !== undefined ? data.metrics[k].values[f] : null);
    const s = {
      started: __ENV.STARTED || null, tag: __ENV.TAG || null,
      http_reqs: m('http_reqs', 'count'), http_failed_rate: m('http_req_failed', 'rate'),
      dropped_iterations: m('dropped_iterations', 'count'),
      p95_ms: m('http_req_duration', 'p(95)'), p99_ms: m('http_req_duration', 'p(99)'),
      metrics: {},
    };
    for (const k of Object.keys(data.metrics)) {
      if (k.indexOf('m6_') === 0) s.metrics[k] = data.metrics[k].values;
    }
    Object.assign(s, extra ? extra(data) : {});
    if (__ENV.SUMMARY) out[__ENV.SUMMARY] = JSON.stringify(s, null, 2);
    out.stdout = '\nM6SUMMARY ' + JSON.stringify(s) + '\n';
    return out;
  };
}
