---
name: sync-requirements
description: Delete uv.lock, re-sync with dev extras/group against the custom pip registry, and export a hash-free requirements.txt. Use when you need to refresh the lock file or regenerate requirements.txt for CI.
---

# sync-requirements

Refresh the uv lock file and export a hash-free `requirements.txt` for CI pipelines backed by a private pip registry.

---

## Phase 1: Verify Registry Config

Check that the custom registry is reachable before touching the lock file.

The registry URL is read from the environment. Preferred resolution order:
1. `UV_INDEX_URL` — replaces PyPI as the primary index.
2. `UV_EXTRA_INDEX_URL` — supplements PyPI (use when internal packages co-exist with public ones).
3. `[[tool.uv.index]]` block in `pyproject.toml` (already committed config).

Run:
```bash
echo "UV_INDEX_URL=${UV_INDEX_URL:-<not set>}"
echo "UV_EXTRA_INDEX_URL=${UV_EXTRA_INDEX_URL:-<not set>}"
grep -A3 '^\[\[tool.uv.index\]\]' pyproject.toml || echo "(no [[tool.uv.index]] in pyproject.toml)"
```

If no registry is configured and the project has internal packages (e.g. `twp-ai` from a private source), warn the user and stop — do not proceed with a stale or broken resolution.

---

## Phase 2: Remove Lock File

```bash
rm -f uv.lock
```

Removing the lock forces a full re-resolution from the registry on the next sync. This is intentional.

---

## Phase 3: Sync with Dev Extras and Group

```bash
uv sync --extra dev --group dev
```

- `--extra dev` — installs `[project.optional-dependencies] dev`.
- `--group dev` — installs `[dependency-groups] dev` (pytest, ruff, testcontainers, etc.).
- uv automatically picks up `UV_INDEX_URL` / `UV_EXTRA_INDEX_URL` from the environment; no extra flags needed.

This regenerates `uv.lock` and installs into the local venv.

---

## Phase 4: Export Hash-Free requirements.txt

```bash
uv export \
  --no-hashes \
  --extra dev \
  --group dev \
  --output-file requirements.txt
```

Flag rationale:
| Flag | Why |
|------|-----|
| `--no-hashes` | Private registries often serve packages without SHA-256 hash metadata; hashes would cause `pip install` to fail on CI. |
| `--extra dev` / `--group dev` | Mirrors Phase 3 — requirements.txt reflects the full dev environment. |
| `--output-file requirements.txt` | Writes to project root. |

If you only need a production requirements file (no dev tooling), drop `--extra dev --group dev`.

---

## Phase 5: Verify

```bash
head -10 requirements.txt
echo "---"
wc -l requirements.txt
```

Confirm:
- The file is non-empty.
- Key packages from `pyproject.toml` (e.g. `fastapi`, `haystack-ai`) appear in the list.

---

## Notes

- **Re-running on CI**: set `UV_INDEX_URL` (or `UV_EXTRA_INDEX_URL`) as a CI secret, then call this skill or run the four commands above directly in your pipeline step.
- **Workspace package `twp-ai`（注意）**: `uv export` 輸出的 `requirements.txt` 會將 `twp-ai` 寫成 editable 路徑，例如：
  ```
  -e packages/twp-ai
  ```
  這在本機可以正常運作，但 **CI 拿到這份 `requirements.txt` 直接跑 `pip install -r requirements.txt` 時會失敗**，因為 CI runner 上沒有 `packages/twp-ai` 這個本地目錄。

  **解法**：在你的 CI pipeline 中，將 `twp-ai` 先打包成 wheel 上傳到你自己的 pip registry，然後在 `requirements.txt` 裡把那一行手動替換（或用 `sed`）成固定版本：
  ```bash
  sed -i 's|^-e packages/twp-ai$|twp-ai==<version>|' requirements.txt
  ```
  這樣 CI 就能直接從你的 private registry 拉到正確的版本。
