# JWT Validation

A JSON Web Token (JWT) is a signed, base64url-encoded payload of the form
`header.payload.signature`. Validation is the process of verifying both
the signature and the claims so the server can trust the bearer's
identity.

## Configuring JWT Validation

A complete validation has these steps:

1. Split the token into three parts and base64url-decode each.
2. Verify the signature against the issuer's key. RS256/ES256 use the
   issuer's public key (typically fetched via JWKS at
   `/.well-known/jwks.json`); HS256 uses a shared secret.
3. Check the `alg` header matches the expected algorithm — never trust
   the token's own `alg` field for algorithm selection.
4. Validate standard claims: `iss` (issuer), `aud` (audience), `exp`
   (expiry), `nbf` (not-before), and optionally `iat` (issued-at).
5. Apply application-specific claims, e.g. `scope` or `roles`.

## Best Practices

- Cache JWKS keys and refresh on `kid` mismatch — never on every request.
- Reject tokens with `alg: none` outright.
- Allow only a small whitelist of algorithms; pin RS256 or EdDSA in
  config.
- Use a clock skew tolerance of 30–60 seconds for `exp` and `nbf`.
- Rotate signing keys regularly; advertise both the new and old key in
  JWKS during the rotation window.

## Troubleshooting

- `signature verification failed` usually means `kid` did not resolve to
  a JWKS entry — the issuer rotated keys and the cache is stale.
- Token-expired errors during long requests indicate the access-token
  TTL is shorter than the operation; refresh proactively.
- Audience mismatches are the most common silent failure: check the
  `aud` claim equals exactly the resource server's identifier.
