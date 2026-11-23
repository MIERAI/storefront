// Users of the v2 stack during an experiment: browsing (catalog ListProducts / GetProduct) and ordering (order
// PlaceOrder), each at a constant arrival rate (open loop: a slow service does not slow the load down).
//
//   k6 run -e CATALOG_URL=... -e ORDER_URL=... -e TAG=<MMDDHHMM> [-e BROWSE_RPS=20] [-e ORDER_RPS=2]
//          [-e DURATION=16m] [-e PRICY_RATE=0.02] [-e SUMMARY=path.json] --out csv=<file.csv.gz> load.js
//
// Custom metrics (what check.py reads):
//   m6_browse_ok     Rate    1 = a browse call answered 2xx (anything else, timeouts included, is 0)
//   m6_order_ok      Rate    1 = PlaceOrder accepted the order (2xx with an order_id)
//   m6_orders_accepted Counter  orders the service said it committed: the checks find each one in order's database
// The CSV output (one row per sample with its timestamp) lets check.py compute the browse success ratio inside the
// fault window alone (the timeline from watch.py), not over the whole run.
// PRICY_RATE: share of orders with a line of M6-PRICY (above payment's limit): they take the compensation path
// (payment.failed -> inventory.release -> CANCELLED), so a run exercises both endings.
import { Counter, Rate } from 'k6/metrics';
import { CATALOG, ORDER, CATEGORY, ITEMS, PRICY, base, call, customerId, lines, pick, summaryWriter } from './lib.js';

const browseOk = new Rate('m6_browse_ok');
const orderOk = new Rate('m6_order_ok');
const accepted = new Counter('m6_orders_accepted');

const DURATION = __ENV.DURATION || '16m';
const BROWSE_RPS = parseInt(__ENV.BROWSE_RPS || '20', 10);
const ORDER_RPS = parseInt(__ENV.ORDER_RPS || '2', 10);
const PRICY_RATE = parseFloat(__ENV.PRICY_RATE || '0.02');

function scenario(rate, fn) {
  return {
    executor: 'constant-arrival-rate', exec: fn, rate: rate, timeUnit: '1s', duration: DURATION,
    preAllocatedVUs: Math.max(5, rate * 2), maxVUs: Math.max(20, rate * 20),
  };
}

const scenarios = {};
if (BROWSE_RPS > 0) scenarios.browse = scenario(BROWSE_RPS, 'browse');
if (ORDER_RPS > 0) scenarios.orders = scenario(ORDER_RPS, 'orders');

export const options = {
  discardResponseBodies: false,
  scenarios: scenarios,
  summaryTrendStats: ['avg', 'p(50)', 'p(95)', 'p(99)', 'max'],
  systemTags: ['status', 'name', 'scenario', 'error_code'],   // keeps the CSV small
};

const CAT = __ENV.CATALOG_URL ? base('CATALOG_URL') : null;
const ORD = __ENV.ORDER_URL ? base('ORDER_URL') : null;

export function browse() {
  let r;
  if (Math.random() < 0.5) {
    r = call(CAT, CATALOG, 'ListProducts', { category: CATEGORY, page_size: 20 });
  } else {
    r = call(CAT, CATALOG, 'GetProduct', { sku: pick(ITEMS).sku });
  }
  browseOk.add(r.status >= 200 && r.status < 300);
}

export function orders() {
  const items = lines(ITEMS, 1 + Math.floor(Math.random() * 3), 2);
  if (Math.random() < PRICY_RATE) items.push({ line_no: items.length + 1, sku: PRICY.sku, qty: 1, price: PRICY.price });
  const r = call(ORD, ORDER, 'PlaceOrder', { customer_id: customerId(), items: items });
  let ok = false;
  if (r.status >= 200 && r.status < 300) {
    try {
      ok = !!JSON.parse(r.body).orderId;
    } catch (e) {
      ok = false;
    }
  }
  orderOk.add(ok);
  if (ok) accepted.add(1);
}

export const handleSummary = summaryWriter();
