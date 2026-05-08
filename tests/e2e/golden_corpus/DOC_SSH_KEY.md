# SSH Key Authentication

SSH key authentication replaces password login with a public/private
keypair. The client proves possession of the private key during the
handshake; the server only stores the public half. This is both more
secure than passwords (no shared secret on the server) and easier to
automate (no interactive prompt).

## Configuring Key Authentication

Generate a modern Ed25519 keypair:

```bash
ssh-keygen -t ed25519 -C "alice@example.com" -f ~/.ssh/id_ed25519
```

Install the public key on the remote host:

```bash
ssh-copy-id -i ~/.ssh/id_ed25519.pub user@host
# or, manually: append the public key to ~/.ssh/authorized_keys
```

Recommended `~/.ssh/config` block per host:

```text
Host bastion
  HostName bastion.example.com
  User alice
  IdentityFile ~/.ssh/id_ed25519
  IdentitiesOnly yes
  ForwardAgent no
```

Server side (`/etc/ssh/sshd_config`):

```text
PubkeyAuthentication yes
PasswordAuthentication no
PermitRootLogin prohibit-password
```

Reload with `systemctl reload sshd` after editing.

## Best Practices

- Prefer `ed25519` over RSA for new keys; if RSA is required for
  compatibility, use at least 4096 bits.
- Protect the private key with a passphrase and load it via
  `ssh-agent` so the passphrase is entered only once per session.
- Set `IdentitiesOnly yes` to stop the client from offering every key
  in the agent — this prevents lockouts from the server's `MaxAuthTries`.
- Disable password authentication on production servers once keys are
  in place.
- Rotate keys when team members leave; remove their entries from
  `authorized_keys`.

## Troubleshooting

- `Permission denied (publickey)` after a fresh setup is almost always
  wrong file permissions: `~/.ssh` must be `0700`,
  `authorized_keys` `0600`, owned by the connecting user.
- `Too many authentication failures` means the agent offered too many
  keys; set `IdentitiesOnly yes` or remove unused keys.
- Verify the server actually accepts the key with
  `ssh -vvv user@host` and read the `Authentications that can continue`
  line.
