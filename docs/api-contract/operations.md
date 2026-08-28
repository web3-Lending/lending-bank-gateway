# lending-bank-gateway · operation contract table

Generated from `app/core/openapi_contract.OPERATION_CONTRACTS` — the checked-in
declaration snapshot that `build_openapi` serves at `/openapi.json`. Every status
below is one this build can really return today; see the module docstring for how
each set was derived and what is deliberately excluded.

- Operations: **28**, all declared, each with exactly one success status.
- Error bodies: `application/json`, `err()` envelope (`GatewayErrorEnvelope`).
  **Not** RFC 9457 Problem documents — `x-lending-problem-compliance: false` on
  every operation, tracked by `FU-API-V22-HUB-BLOCKERS-20260828-001`.
- Headers guaranteed on **every** response of every operation: `Cache-Control`, `X-Request-Id`, `X-Trace-Id`.
  The `headers` column lists only the status-specific overlay on top of those.

| Method | Path | Success | Failures | Profile | unknown_query | Error media type | Problem-compliant | Status-specific headers |
|---|---|---|---|---|---|---|---|---|
| `POST` | `/api/v1/admin/outbox/{outbox_id}/replay` | `200` | `401`, `404`, `414`, `422`, `500` | `ADMIN_OPS` | `passthrough` | `application/json` | no | 401: `WWW-Authenticate` |
| `GET` | `/api/v1/admin/platform-accounts` | `200` | `401`, `403`, `414`, `422`, `500` | `ADMIN_CONFIG` | `passthrough` | `application/json` | no | 401: `WWW-Authenticate` |
| `POST` | `/api/v1/admin/platform-accounts` | `200` | `400`, `401`, `403`, `409`, `414`, `422`, `500` | `ADMIN_CONFIG` | `passthrough` | `application/json` | no | 401: `WWW-Authenticate` |
| `PATCH` | `/api/v1/admin/platform-accounts/{row_id}` | `200` | `400`, `401`, `403`, `404`, `414`, `422`, `500` | `ADMIN_CONFIG` | `passthrough` | `application/json` | no | 401: `WWW-Authenticate` |
| `GET` | `/api/v1/admin/stuck-orders` | `200` | `401`, `414`, `500` | `ADMIN_OPS` | `passthrough` | `application/json` | no | 401: `WWW-Authenticate` |
| `GET` | `/api/v1/admin/wedap-import/delivery-report` | `200` | `401`, `414`, `500` | `ADMIN_OPS` | `passthrough` | `application/json` | no | 401: `WWW-Authenticate` |
| `POST` | `/api/v1/bank-funds/collect-from-users` | `200` | `400`, `401`, `403`, `409`, `414`, `422`, `500` | `MONEY_WRITE` | `passthrough` | `application/json` | no | 401: `WWW-Authenticate` |
| `POST` | `/api/v1/bank-funds/distribute-to-users` | `200` | `400`, `401`, `403`, `409`, `414`, `422`, `500` | `MONEY_WRITE` | `passthrough` | `application/json` | no | 401: `WWW-Authenticate` |
| `POST` | `/api/v1/bank-funds/refunds` | `200` | `400`, `401`, `403`, `409`, `414`, `422`, `500` | `MONEY_WRITE` | `passthrough` | `application/json` | no | 401: `WWW-Authenticate` |
| `POST` | `/api/v1/bank-funds/reversals` | `200` | `400`, `401`, `409`, `414`, `422`, `500` | `MONEY_WRITE` | `passthrough` | `application/json` | no | 401: `WWW-Authenticate` |
| `GET` | `/api/v1/bank-funds/status` | `200` | `400`, `401`, `404`, `414`, `422`, `500` | `LEDGER_QUERY` | `passthrough` | `application/json` | no | 401: `WWW-Authenticate` |
| `POST` | `/api/v1/callbacks/wedap/transactions` | `200` | `400`, `401`, `414`, `422`, `500` | `INBOUND_CALLBACK` | `passthrough` | `application/json` | no | 401: `WWW-Authenticate` |
| `GET` | `/api/v1/deposit/account/detail` | `200` | `400`, `401`, `414`, `500`, `502` | `GATEWAY_BFF` | `passthrough` | `application/json` | no | 401: `WWW-Authenticate` |
| `GET` | `/api/v1/deposit/accounts` | `200` | `400`, `401`, `414`, `500`, `502` | `GATEWAY_BFF` | `passthrough` | `application/json` | no | 401: `WWW-Authenticate` |
| `GET` | `/api/v1/deposit/balances/total` | `200` | `400`, `401`, `414`, `500`, `502` | `GATEWAY_BFF` | `passthrough` | `application/json` | no | 401: `WWW-Authenticate` |
| `GET` | `/api/v1/deposit/internal-accounts/info` | `200` | `400`, `401`, `414`, `500`, `502` | `GATEWAY_BFF` | `passthrough` | `application/json` | no | 401: `WWW-Authenticate` |
| `GET` | `/api/v1/deposit/internal-accounts/transactions` | `200` | `400`, `401`, `414`, `500`, `502` | `GATEWAY_BFF` | `passthrough` | `application/json` | no | 401: `WWW-Authenticate` |
| `GET` | `/api/v1/deposit/transactions` | `200` | `400`, `401`, `414`, `500`, `502` | `GATEWAY_BFF` | `passthrough` | `application/json` | no | 401: `WWW-Authenticate` |
| `POST` | `/api/v1/loans/p2p-disbursements` | `200` | `400`, `401`, `409`, `414`, `422`, `500` | `MONEY_WRITE` | `passthrough` | `application/json` | no | 401: `WWW-Authenticate` |
| `POST` | `/api/v1/loans/p2p-repayments` | `200` | `400`, `401`, `409`, `414`, `422`, `500` | `MONEY_WRITE` | `passthrough` | `application/json` | no | 401: `WWW-Authenticate` |
| `GET` | `/api/v1/loans/p2p-repayments/{biz_seq_no}/status` | `200` | `400`, `401`, `404`, `414`, `500` | `LEDGER_QUERY` | `passthrough` | `application/json` | no | 401: `WWW-Authenticate` |
| `POST` | `/api/v1/recon/notify` | `200` | `400`, `401`, `414`, `422`, `500` | `INBOUND_CALLBACK` | `passthrough` | `application/json` | no | 401: `WWW-Authenticate` |
| `GET` | `/api/v1/users/info` | `200` | `400`, `401`, `414`, `500`, `502` | `GATEWAY_BFF` | `passthrough` | `application/json` | no | 401: `WWW-Authenticate` |
| `POST` | `/api/v1/wedap/import/enqueue` | `200` | `400`, `401`, `414`, `422`, `500` | `INTERNAL_WRITE` | `passthrough` | `application/json` | no | 401: `WWW-Authenticate` |
| `GET` | `/api/version` | `200` | `414`, `500` | `PUBLIC_PROBE` | `passthrough` | `application/json` | no | — |
| `GET` | `/build-info` | `200` | `414`, `500` | `PUBLIC_PROBE` | `passthrough` | `application/json` | no | — |
| `GET` | `/healthz` | `200` | `414`, `500` | `PUBLIC_PROBE` | `passthrough` | `application/json` | no | — |
| `GET` | `/readyz` | `200` | `414`, `500`, `503` | `PUBLIC_PROBE` | `passthrough` | `application/json` | no | 503: `Retry-After` |

## Route-level notes (statuses this service really returns but no operation declares)

**`405 Method Not Allowed`** — Starlette answers it, and `_http_exception_handler`
passes `exc.headers` through, so the mandatory `Allow` header does reach the client
(probed: `POST /healthz` → `405`, `Allow: GET`). It is nonetheless absent from every
operation's `responses`, because it arises only when a *different* method is sent to
a path — never for the `(method, path)` pair the operation itself is. Declaring it
per-operation would assert something untrue of that operation.

**`404` on an unrouted target** and **`307` trailing-slash redirects (with `Location`)**
are excluded for the same reason: they belong to request-targets no operation covers.
The four `404`s that *are* declared are raised by handlers (`GW_404_OUTBOX`,
`GW_404_PLATFORM_ACCOUNT`, `GW_404_ORDER` ×2).

**`429`** is absent because this repository contains no rate limiter at all.

## Operations whose parameters cannot actually produce a `422`

A mechanical "`route.dependant` has parameters ⇒ `422` is reachable" rule would
declare `422` on these two. Both were probed against a live `TestClient` and reach
their handler instead, so `422` is excluded:

- `GET /api/v1/admin/wedap-import/delivery-report` — the only parameter is `import_date: str | None = None` -- an unconstrained optional string query. Every input is a valid `str`, and a repeated query key binds the last value rather than failing. Probed: `?import_date=a&import_date=b` reaches the handler.
- `GET /api/v1/loans/p2p-repayments/{biz_seq_no}/status` — the only parameter is an unconstrained `str` path segment. A segment that fails to bind cannot exist -- an empty segment does not match the route at all (404 on a different request-target). Probed with a whitespace segment: reaches the handler.

## Authentication surface (derived from the mounted `S2SMiddleware`)

| 401 challenge | Operations |
|---|---|
| `ApiKey realm="lending-bank-gateway", header="apikey"` | 2: `POST /api/v1/callbacks/wedap/transactions`, `POST /api/v1/recon/notify` |
| `S2S realm="lending-bank-gateway", header="X-S2S-Token"` | 22: `POST /api/v1/admin/outbox/{outbox_id}/replay`, `GET /api/v1/admin/platform-accounts`, `POST /api/v1/admin/platform-accounts`, `PATCH /api/v1/admin/platform-accounts/{row_id}`, `GET /api/v1/admin/stuck-orders`, `GET /api/v1/admin/wedap-import/delivery-report`, `POST /api/v1/bank-funds/collect-from-users`, `POST /api/v1/bank-funds/distribute-to-users`, `POST /api/v1/bank-funds/refunds`, `POST /api/v1/bank-funds/reversals`, `GET /api/v1/bank-funds/status`, `GET /api/v1/deposit/account/detail`, `GET /api/v1/deposit/accounts`, `GET /api/v1/deposit/balances/total`, `GET /api/v1/deposit/internal-accounts/info`, `GET /api/v1/deposit/internal-accounts/transactions`, `GET /api/v1/deposit/transactions`, `POST /api/v1/loans/p2p-disbursements`, `POST /api/v1/loans/p2p-repayments`, `GET /api/v1/loans/p2p-repayments/{biz_seq_no}/status`, `GET /api/v1/users/info`, `POST /api/v1/wedap/import/enqueue` |
| `— (exempt path, no 401)` | 4: `GET /api/version`, `GET /build-info`, `GET /healthz`, `GET /readyz` |

## How this table is kept honest

The declaration snapshot is not trusted on its word. `tests/test_openapi_contract_gate.py`
holds ten gates over it, every one of them mutation-verified (break one thing, watch
that gate go red, put it back):

| Gate | What it refuses to let through |
|---|---|
| G1 | A served route with no declaration, or a declaration with no route. |
| G2 | More than one 2xx on an operation. |
| G3 | `default` / `1XX` / `2XX` / `3XX` / `4XX` / `5XX` anywhere in the document. |
| G4 | A status the code can raise but the snapshot does not declare (AST scan). |
| G5 | An `x-lending-unknown-query-parameters` value the route does not back up. |
| G6 | A required response header that no real response was shown to carry. |
| G7 | An error media type other than the one really served. |
| G8 | Validation answering before authentication, or authentication answering before the request-target budget. |
| G9 | An injected 502/503/500 that lands off-contract, loses a header, or leaves a row behind. |
| G10 | A document that is not valid OpenAPI 3.1, or that has drifted from `openapi.json` / `openapi.sha256`. |

Regenerate `openapi.json` and `openapi.sha256` after any contract or route change:

```bash
.venv/bin/python scripts/gen_openapi_contract.py
```
