// Seed the M6 data through the services' own APIs (no SQL): the products in catalog (CreateProduct ->
// catalog.product_created -> inventory's InitStock creates the stock row), then stock in inventory (AdjustStock) up
// to STOCK per SKU (default 100000). Idempotent: an existing product answers already_exists, stock is only topped up.
// M6-SCARCE is left alone here (flash-oversell.js sets its stock exactly).
//
//   k6 run -e CATALOG_URL=... -e INVENTORY_URL=... [-e STOCK=100000] seed.js
import { check, sleep } from 'k6';
import { ALL, SCARCE, CATALOG, INVENTORY, base, callRetry as call } from './lib.js';

export const options = { vus: 1, iterations: 1, thresholds: { checks: ['rate==1'] } };

export function stockOf(inv, sku) {
  const r = call(inv, INVENTORY, 'GetStock', { sku: sku });
  if (r.status !== 200) throw new Error('GetStock ' + sku + ': HTTP ' + r.status + ' ' + r.body);
  const b = JSON.parse(r.body);   // protojson leaves zero values out: found=false, available=0 are absent
  return { found: !!b.found, available: b.available || 0, reserved: b.reserved || 0 };
}

export function waitStock(inv, sku, seconds) {
  for (let i = 0; i < seconds; i++) {
    const s = stockOf(inv, sku);
    if (s.found) return s;
    sleep(1);
  }
  throw new Error('no stock row for ' + sku + ' after ' + seconds + 's (is inventory consuming catalog.product_created?)');
}

export function adjust(inv, sku, delta) {
  // AdjustStock takes |delta| <= 100000 per call
  while (delta !== 0) {
    const d = Math.max(-100000, Math.min(100000, delta));
    const r = call(inv, INVENTORY, 'AdjustStock', {
      sku: sku, delta: d, reason: d > 0 ? 'ADJUSTMENT_REASON_RECEIVED' : 'ADJUSTMENT_REASON_STOCKTAKE',
    });
    if (r.status !== 200) throw new Error('AdjustStock ' + sku + ' ' + d + ': HTTP ' + r.status + ' ' + r.body);
    delta -= d;
  }
}

export default function () {
  let ok = false;
  try {
    seed();
    ok = true;
  } finally {
    check(null, { 'seeded': () => ok });   // the threshold turns a failed seed into a non-zero exit
  }
}

function seed() {
  const cat = base('CATALOG_URL');
  const inv = base('INVENTORY_URL');
  const target = parseInt(__ENV.STOCK || '100000', 10);
  for (const p of ALL) {
    const r = call(cat, CATALOG, 'CreateProduct', { sku: p.sku, name: 'M6 ' + p.sku, category: p.category, price: p.price });
    if (r.status !== 200 && r.status !== 409) throw new Error('CreateProduct ' + p.sku + ': HTTP ' + r.status + ' ' + r.body);
    console.log(p.sku + ': ' + (r.status === 200 ? 'created' : 'exists'));
  }
  for (const p of ALL) {
    const s = waitStock(inv, p.sku, 60);
    if (p.sku === SCARCE.sku) continue;
    if (s.available < target) adjust(inv, p.sku, target - s.available);
    console.log(p.sku + ': available ' + Math.max(s.available, target) + ', reserved ' + s.reserved);
  }
}
