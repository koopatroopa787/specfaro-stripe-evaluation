import assert from 'node:assert/strict';
import test from 'node:test';
import { paymentIntentChargeLabel } from '../src/charge-view.js';

test('Stripe 2022-11-15 contract ignores removed legacy charges data', () => {
  const paymentIntent = {
    latest_charge: null,
    charges: { data: [{ id: 'ch_stale' }] },
  };
  assert.equal(paymentIntentChargeLabel(paymentIntent), 'unpaid');
});
