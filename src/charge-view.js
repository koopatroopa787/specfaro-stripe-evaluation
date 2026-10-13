export function paymentIntentChargeLabel(paymentIntent) {
  const legacyCharge = paymentIntent.charges?.data?.[0];
  return legacyCharge?.id ?? 'unpaid';
}
