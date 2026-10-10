export function latestChargeId(paymentIntent) {
  return paymentIntent.charges.data[0].id;
}
