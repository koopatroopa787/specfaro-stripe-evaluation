# Specfaro Stripe evaluation fixture

Disposable public test repository with a deliberately broken PaymentIntent
integration. Stripe's [2022-11-15 change](https://docs.stripe.com/changelog/2022-11-15/removes-charges-attribute-paymentintent)
removed `charges` from PaymentIntent and directs integrations to
`latest_charge`. The tests cover a charge ID, an expanded charge, and a null
value before confirmation. No Stripe credentials or live API calls are used.

Failed default-branch CI triggers a read-only Specfaro evaluation. It saves a
repair report artifact and cannot publish a PR from this repository.
