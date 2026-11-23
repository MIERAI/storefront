// Flash sale at a constant arrival rate (open loop): PlaceOrder of 1 unit of one of the three hot SKUs
// (M6-FLASH-1..3, stock topped up by seed.js) at RATE orders/s for DURATION. One step of knee.py.
// Plain ASCII on purpose. Needs lib.js (scripts/m6/k6/lib.js) next to it.
//
//   k6 run -e ORDER_URL=http://100.106.140.35:18080 -e RATE=40 -e DURATION=90s -e TAG=11231830 \
//          -e SUMMARY=out/step.json flash.js
import { check } from 'k6';
import { Rate } from 'k6/metrics';
import { ORDER, FLASH, base, call, customerId, pick, summaryWriter } from './lib.js';

const RATE = parseInt(__ENV.RATE || '10', 10);
const okRate = new Rate('m6_order_ok');

export const options = {
  discardResponseBodies: false,
  scenarios: {
    flash: {
      executor: 'constant-arrival-rate',
      rate: RATE,
      timeUnit: '1s',
      duration: __ENV.DURATION || '90s',
      // a slow answer must not slow the arrivals down: enough VUs for RATE x (RTT + a few seconds)
      preAllocatedVUs: Math.max(20, RATE),
      maxVUs: Math.min(5000, Math.max(200, RATE * 12)),
    },
  },
};

export default function () {
  const ord = base('ORDER_URL');
  const p = pick(FLASH);
  const r = call(ord, ORDER, 'PlaceOrder', {
    customer_id: customerId(),
    items: [{ line_no: 1, sku: p.sku, qty: 1, price: p.price }],
  });
  const ok = r.status === 200 && r.body && r.body.indexOf('"orderId"') >= 0;
  okRate.add(ok);
  check(r, { 'order accepted': () => ok });
}

export const handleSummary = summaryWriter((data) => ({
  rate: RATE,
  iterations: data.metrics.iterations ? data.metrics.iterations.values.count : 0,
  vus_max: data.metrics.vus_max ? data.metrics.vus_max.values.max : null,
}));
