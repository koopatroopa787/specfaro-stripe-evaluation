import assert from 'node:assert/strict';
import test from 'node:test';
import { latestChargeId } from '../src/payment-intent.js';

test('reads the latest charge ID from a Stripe PaymentIntent after the 2022-11-15 change', () => {
  assert.equal(latestChargeId({ latest_charge: 'ch_123' }), 'ch_123');
});

test('reads an expanded latest charge from a Stripe PaymentIntent', () => {
  assert.equal(latestChargeId({ latest_charge: { id: 'ch_456' } }), 'ch_456');
});

test('returns undefined before a Stripe PaymentIntent has a charge', () => {
  assert.equal(latestChargeId({ latest_charge: null }), undefined);
});
