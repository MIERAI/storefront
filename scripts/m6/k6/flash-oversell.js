// The oversell test: far more buyers than stock on one SKU. setup() sets M6-SCARCE's available stock to exactly
// SCARCE_STOCK (default 200; AdjustStock), then ORDER_RPS orders of 1 x M6-SCARCE per second for DURATION
// (default 50/s for 30s = 1500 orders for 200 units). Afterwards (check.py stock): the units committed to orders of
// this run are at most SCARCE_STOCK, available never went negative, and the stock row's books balance.
//
//   k6 run -e ORDER_URL=... -e INVENTORY_URL=... -e TAG=<MMDDHHMM> [-e SCARCE_STOCK=200] [-e ORDER_RPS=50]
//          [-e DURATION=30s] [-e SUMMARY=path.json] flash-oversell.js
import { Counter, Rate } from 'k6/metrics';
import { ORDER, SCARCE, base, call, customerId, summaryWriter } from './lib.js';
import { adjust, waitStock } from './seed.js';

const orderOk = new Rate('m6_order_ok');
const accepted = new Counter('m6_orders_accepted');
const RPS = parseInt(__ENV.ORDER_RPS || '50', 10);

export const options = {
  scenarios: {
    rush: {
      executor: 'constant-arrival-rate', rate: RPS, timeUnit: '1s', duration: __ENV.DURATION || '30s',
      preAllocatedVUs: RPS * 2, maxVUs: RPS * 20,
    },
  },
};

export function setup() {
  const inv = base('INVENTORY_URL');
  const want = parseInt(__ENV.SCARCE_STOCK || '200', 10);
  const s = waitStock(inv, SCARCE.sku, 60);
  if (s.available !== want) adjust(inv, SCARCE.sku, want - s.available);
  const after = waitStock(inv, SCARCE.sku, 5);
  console.log(SCARCE.sku + ': available ' + after.available + ' (reserved ' + after.reserved + ' from earlier runs)');
  return { stock: after.available, reservedBefore: after.reserved };
}

const ORD = __ENV.ORDER_URL ? base('ORDER_URL') : null;

export default function () {
  const r = call(ORD, ORDER, 'PlaceOrder', {
    customer_id: customerId(), items: [{ line_no: 1, sku: SCARCE.sku, qty: 1, price: SCARCE.price }],
  });
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

export const handleSummary = summaryWriter((data) => ({ scarce_stock: parseInt(__ENV.SCARCE_STOCK || '200', 10) }));
