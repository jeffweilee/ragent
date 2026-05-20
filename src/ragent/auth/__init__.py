"""T8 — authentication and permission layer (§3.5).

Authentication and permission are deliberately separate layers:
- :mod:`ragent.auth.jwt` verifies an inbound JWT against the configured OIDC
  JWKS (signature + ``iss`` + ``aud`` + ``exp``) via joserfc and extracts the
  configured user_id claim.
- A future ``ragent.auth.permission`` module owns the OpenFGA-backed
  ``PermissionClient`` Protocol (T8.3+).
"""
