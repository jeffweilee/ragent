# OpenSSL Self-Signed Certificate

A self-signed certificate is an X.509 certificate signed by its own
private key rather than a trusted Certificate Authority. It is the
standard approach for local development, internal testing, and
non-public services where bootstrapping a real CA would be excessive.

## Configuring a Self-Signed Cert

Generate a key and a self-signed cert in one command:

```bash
openssl req -x509 -newkey rsa:4096 -sha256 -days 365 -nodes \
  -keyout server.key -out server.crt \
  -subj "/CN=localhost" \
  -addext "subjectAltName=DNS:localhost,DNS:dev.local,IP:127.0.0.1"
```

Key flags:

- `-newkey rsa:4096` generates a fresh 4096-bit RSA key.
- `-nodes` writes the private key without a passphrase (suitable for
  scripted services).
- `-days 365` sets the validity period.
- `-addext "subjectAltName=..."` is required by modern browsers — the
  CN field alone is no longer trusted.

Inspect the result with `openssl x509 -in server.crt -noout -text`.

## Best Practices

- Always populate `subjectAltName`; without SAN, Chrome/Firefox reject
  the cert even when added to the OS trust store.
- Use a 2048- or 4096-bit RSA key, or an EdDSA key, for current
  security margins.
- Keep `server.key` mode `0600` and never commit it to a repository.
- Rotate the cert before expiry; track expiry with `openssl x509
  -enddate -noout`.

## Troubleshooting

- `unable to get local issuer certificate` from a client means the
  client does not trust the self-signed cert — add `server.crt` to
  the client's trust store, or use `--cacert server.crt`.
- `subjectAltName` mismatches surface as `hostname mismatch` errors.
- Browser-side trust requires importing the cert into the OS root
  store; a per-site exception only lasts the session.
