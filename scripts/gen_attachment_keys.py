"""gen_attachment_keys — generate/rotate the KEK/DEK pair for chat-attachment
AST encryption (T-CAT.W3, docs/spec/chat_attachments.md §5).

Usage:
    uv run python scripts/gen_attachment_keys.py generate
    uv run python scripts/gen_attachment_keys.py rotate \
        --old-kek "$RAGENT_KEK_BASE64" \
        --old-encrypted-dek "$RAGENT_ENCRYPTED_DEK_BASE64"

`generate` mints a brand-new KEK + DEK pair (first-time bootstrap).
`rotate` re-wraps the *existing* DEK under a freshly generated KEK — the DEK
itself never changes, so no re-encryption of already-stored artifacts is
needed (docs/spec/chat_attachments.md §5 "KEK rotation").

Both subcommands print KEY=VALUE lines to stdout only — never logs or
persists the secret material anywhere else. Pipe directly into `.env` or a
secret manager; do not paste these values into chat/ticket history.
"""

from __future__ import annotations

import argparse
import base64
import os
import sys

from ragent.security.key_manager import KeyManager, KeyManagerError

_KEY_BYTES = 32  # AES-256


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def generate() -> tuple[str, str]:
    """Mint a brand-new KEK + DEK pair. Returns (kek_b64, encrypted_dek_b64)."""
    kek_b64 = _b64(os.urandom(_KEY_BYTES))
    dek = os.urandom(_KEY_BYTES)
    return kek_b64, KeyManager.wrap(kek_b64, dek)


def rotate(old_kek_b64: str, old_encrypted_dek_b64: str) -> tuple[str, str]:
    """Re-wrap the existing DEK under a freshly generated KEK.

    Returns (new_kek_b64, new_encrypted_dek_b64). The DEK itself is
    unchanged, so existing artifacts remain decryptable without
    re-encryption — only the two env vars need updating.
    """
    dek = KeyManager(kek_b64=old_kek_b64, encrypted_dek_b64=old_encrypted_dek_b64).dek
    new_kek_b64 = _b64(os.urandom(_KEY_BYTES))
    return new_kek_b64, KeyManager.wrap(new_kek_b64, dek)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("generate", help="Mint a brand-new KEK + DEK pair (first-time bootstrap).")

    rotate_parser = sub.add_parser(
        "rotate", help="Re-wrap the existing DEK under a new KEK (KEK rotation)."
    )
    rotate_parser.add_argument("--old-kek", required=True, help="Current RAGENT_KEK_BASE64.")
    rotate_parser.add_argument(
        "--old-encrypted-dek", required=True, help="Current RAGENT_ENCRYPTED_DEK_BASE64."
    )

    args = parser.parse_args(argv)

    if args.command == "generate":
        kek_b64, encrypted_dek_b64 = generate()
    else:
        try:
            kek_b64, encrypted_dek_b64 = rotate(args.old_kek, args.old_encrypted_dek)
        except KeyManagerError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

    print(f"RAGENT_KEK_BASE64={kek_b64}")
    print(f"RAGENT_ENCRYPTED_DEK_BASE64={encrypted_dek_b64}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
