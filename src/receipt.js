// Receipt reference a payment webhook stores for a succeeded PaymentIntent.
export function receiptReference(paymentIntent) {
  const chargeId = paymentIntent.charges.data[0]?.id;
  return chargeId ? `receipt:${chargeId}` : 'receipt:pending';
}
