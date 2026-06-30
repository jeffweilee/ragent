# Dependency Direction Rules + Quick Index

> Linked from [`docs/00_domain_map.md`](../00_domain_map.md) §三/§五.

## 三、依賴方向規則（AI 操作前必讀）

```
允許的依賴方向（→ 表示「可以 import」）：

Routers     → Services, Schemas, Errors, auth/deps, clients/rate_limiter
Services    → Repositories, Storage, Clients, Errors, Schemas, Utility
Repositories→ Utility, Errors, Schemas
Pipelines   → Clients, Utility, Errors, Schemas
Extractors  → Repositories(注入), Clients(注入), Errors
Clients     → Errors, Utility
Storage     → Errors, Utility
Auth        → Errors, Utility
Middleware  → Errors, Utility
Schemas     → (stdlib + Pydantic only)
Errors      → (stdlib only)
Utility     → (stdlib only)
Security    → stdlib, cryptography（唯一允許的第三方依賴；其餘 domain 不得直接 import cryptography）
Bootstrap   → 全部（唯一可以組裝所有層的地方）
Workers     → Bootstrap(broker/metrics/Container), Pipelines, Repositories, Services, Schemas, Errors, Utility
Reconciler  → Repositories, Bootstrap(Container), Errors, Utility
MCP Hub     → Utility, Errors（完全獨立 subprocess）

Routers → AgentFactory（`twp_ai.agent.Agent` Protocol 的型別別名，由 Bootstrap 注入的 callable）（✅，T-CAv3.DIP）
Bootstrap → Agent/Caller 的具體類別（組裝 factory closure，如 `_build_chatagent_agent_factory()`）（✅，Composition Root 是唯一允許依賴具體實作的層）

❌ 禁止反向依賴：
  Repositories → Services（❌）
  Pipelines    → Services 或 Repositories（❌）
  Extractors   → Pipelines 或 Routers（❌）
  Clients      → Repositories 或 Services（❌）
  Schemas      → 任何 ragent 業務模組（❌）
  Errors       → 任何 ragent 業務模組（❌）
  Utility      → 任何 ragent 業務模組（❌）
  Routers      → Agent/Caller 的具體類別（❌；曾是 `chatagent_v3.py` 的違規模式，已於 T-CAv3.DIP 重構移除）
```

---

## 五、快速查詢索引

> 新功能：查 `docs/00_spec.md` → `docs/00_plan.md` → 定位 Domain（[`docs/00_domain_map.md`](../00_domain_map.md) §二）→ Red test → Green impl → `/simplify` + `/review` + commit。

### 「我改動了 X，可能影響哪些 Domain？」

| 改動 X | 可能影響的 Domain |
|---|---|
| `repositories/document_repository.py` | Services（ingest_service）、Extractors（VectorExtractor）、Reconciler |
| `clients/embedding.py` | Pipelines（ingest）、Extractors（VectorExtractor）|
| `bootstrap/composition.py` | 所有 Domain（DI 變更）|
| `schemas/ingest.py` | Routers（ingest）、Services（ingest_service）|
| `errors/codes.py` | 所有 Domain + `docs/00_spec.md §4.1.2` |
| `pipelines/retrieve/__init__.py` | Routers（chat、retrieve）、integration tests |
| `bootstrap/metrics.py` | 所有有 metric emit 的 Domain |
| `security/key_manager.py` | Bootstrap（建構順序）、`security/ast_cipher.py`、`services/chat_attachment_service.py`、`services/document_artifact_resolver.py` |
| `storage/document_store.py` | Bootstrap、`services/chat_attachment_service.py`、`services/document_artifact_resolver.py`（DIP：兩者只 import Protocol，不 import `minio_document_store.py`）|
