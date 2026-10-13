import assert from 'node:assert/strict';
import test from 'node:test';
import { paymentIntentChargeLabel } from '../src/charge-view.js';

test('Stripe PaymentIntent paymentIntentChargeLabel reads latest_charge after 2022-11-15', () => {
  assert.equal(paymentIntentChargeLabel({ latest_charge: 'ch_current' }), 'ch_current');
});

test('Stripe PaymentIntent paymentIntentChargeLabel reads expanded latest_charge after 2022-11-15', () => {
  assert.equal(paymentIntentChargeLabel({ latest_charge: { id: 'ch_expanded' } }), 'ch_expanded');
});

test('Stripe PaymentIntent paymentIntentChargeLabel stays unpaid without latest_charge after 2022-11-15', () => {
  assert.equal(paymentIntentChargeLabel({ latest_charge: null }), 'unpaid');
});
