import { latestChargeId } from './payment-intent.js';

export function paymentIntentChargeLabel(paymentIntent) {
  return latestChargeId(paymentIntent) ?? 'unpaid';
}
