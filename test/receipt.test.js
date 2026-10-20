import assert from 'node:assert/strict';
import test from 'node:test';
import { receiptReference } from '../src/receipt.js';

test('Stripe PaymentIntent receiptReference reads latest_charge after 2022-11-15', () => {
  assert.equal(receiptReference({ latest_charge: 'ch_receipt' }), 'receipt:ch_receipt');
});

test('Stripe PaymentIntent receiptReference reads expanded latest_charge after 2022-11-15', () => {
  assert.equal(receiptReference({ latest_charge: { id: 'ch_expanded' } }), 'receipt:ch_expanded');
});

test('Stripe PaymentIntent receiptReference stays pending without latest_charge after 2022-11-15', () => {
  assert.equal(receiptReference({ latest_charge: null }), 'receipt:pending');
});
