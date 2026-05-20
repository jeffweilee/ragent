"""T8 — authentication and permission layer (§3.5).

Authentication and permission are deliberately separate layers:
- :mod:`ragent.auth.jwt` decodes an inbound JWT (decode-only per §3.5.1
  accepted risk) and extracts the configured user_id claim.
- A future ``ragent.auth.permission`` module owns the OpenFGA-backed
  ``PermissionClient`` Protocol (T8.3+).
"""
