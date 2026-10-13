export function latestChargeId(paymentIntent) {
  const charge = paymentIntent.latest_charge;
  return typeof charge === 'string' ? charge : charge?.id;
}
