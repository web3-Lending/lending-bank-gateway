"""The query location, isolated: parsed once, validated once, never merged.

Why this module exists
----------------------
v2.2 API-HTTP-021 clause 3 and API-HTTP-022 require query, form body, header and
cookie to be validated as **separate OpenAPI locations**, and §7.2.1 clause 7 makes
this service -- the first controlled boundary in front of the bank -- the one that
does the validating. The 2026-08-27 inventory measured two ways this service failed
that (report §241 and §300):

* ``dict(request.query_params)`` and FastAPI's scalar binding are both *last-wins*:
  ``?bizSeqNo=AAA&bizSeqNo=BBB`` silently became ``BBB`` and went on to hit the
  ledger. A repeated scalar is not a value, it is a malformed request, and the
  caller has to be told which name it repeated.
* The seven wedap passthroughs forwarded that collapsed dict to the bank verbatim.
  A caller could therefore put ``X-Tenant-Id=OTHER`` in the **query** location and
  have it arrive in the upstream **query** location, sitting next to the gateway's
  own ``X-Tenant-Id`` **header** -- two locations carrying two different tenants,
  with the bank picking. That is the header/query confusion API-HTTP-022 exists to
  prevent, and it is why the guard below is about names, not values.

Both checks run for every operation, mounted once as an application-level
dependency in ``create_app``. Application-level dependencies are solved before the
route's own parameters and body, so the rejection lands before any handler, any
database access and any upstream call -- §7.2.1 clause 6's "no business side effect"
requirement. They run *after* authentication for free: authentication in this
service is :class:`~app.core.s2s.S2SMiddleware`, and middleware always precedes
routing, so the §7.2.1 step-2B ordering ("authenticate, then reject undeclared
input") cannot be inverted here the way it can in a repository that mounts auth as
a router dependency.

Deliberately **not** in scope
----------------------------
Rejecting query names an operation does not declare (API-HTTP-021, report §C1) and
the byte-level ``%ZZ`` / overlong-UTF-8 / empty-field vectors (report §C4) are
separate, breaking changes: the first needs a per-operation declared allow-list for
the seven passthroughs, which the bank contract does not yet pin down. This module
does not pretend otherwise -- it isolates the location and rejects what is
unambiguously malformed within it.
"""

from __future__ import annotations

from fastapi import HTTPException, Request

#: Field names this service binds from the **header** location. A query parameter
#: carrying one of these names is a location confusion by construction: there is no
#: reading of the request under which the caller meant it, and forwarding it to the
#: bank puts a second, caller-controlled answer next to the header the gateway sets
#: itself. Compared case-insensitively, because HTTP field names are.
#:
#: Sources, all read from ``request.headers`` and nowhere else:
#: ``app/core/context.py`` (X-Request-Id / X-Trace-Id / X-Tenant-Id / X-Biz-Seq-No),
#: ``app/core/s2s.py`` (X-Caller-Service / X-S2S-Token / apikey),
#: ``app/api/deps.py`` (Idempotency-Key).
HEADER_LOCATION_NAMES: frozenset[str] = frozenset(
    {
        "apikey",
        "idempotency-key",
        "x-biz-seq-no",
        "x-caller-service",
        "x-request-id",
        "x-s2s-token",
        "x-tenant-id",
        "x-trace-id",
    }
)


def _reject(code: str, message: str, parameters: list[str]) -> HTTPException:
    """422 carrying the offending names, so the caller can fix the request itself.

    Only the *names* are echoed, never the values: the inventory found a rejected
    query value (`%C0%AF`, decoded to U+FFFD) reflected back inside an error
    message on ``/api/v1/bank-funds/status``.
    """
    return HTTPException(422, detail={"code": code, "message": message, "parameters": parameters})


def enforce_query_location(request: Request) -> dict[str, str]:
    """Validate the query location on its own terms and return only its values.

    Raises:
        HTTPException: ``422 GW_422_DUPLICATE_QUERY_PARAMETER`` when a name appears
            more than once (equal values included -- ``?a=1&a=1`` is two fields, and
            treating it as one is the same silent merge in a friendlier costume),
            ``422 GW_422_QUERY_LOCATION_CONFLICT`` when a name belongs to the header
            location.

    The result is stashed on ``request.state.query_location`` so handlers can consume
    the validated mapping instead of reaching back into ``request.query_params`` --
    reaching back would re-open the last-wins collapse this function exists to close.
    """
    seen: dict[str, str] = {}
    duplicates: list[str] = []
    conflicts: list[str] = []
    for name, value in request.query_params.multi_items():
        if name in seen and name not in duplicates:
            duplicates.append(name)
        elif name not in seen:
            seen[name] = value
        if name.lower() in HEADER_LOCATION_NAMES and name not in conflicts:
            conflicts.append(name)
    if duplicates:
        raise _reject(
            "GW_422_DUPLICATE_QUERY_PARAMETER",
            "query parameter repeated; a scalar parameter must be sent once",
            duplicates,
        )
    if conflicts:
        raise _reject(
            "GW_422_QUERY_LOCATION_CONFLICT",
            "query parameter uses a name this service reads from the header location",
            conflicts,
        )
    request.state.query_location = seen
    return seen


def query_location_params(request: Request) -> dict[str, str]:
    """The validated query-location mapping for the current request.

    Falls back to running the validation when the state slot is unset, which happens
    only when a handler is exercised outside the application (a direct unit call).
    Handlers must use this rather than ``dict(request.query_params)``.
    """
    params: dict[str, str] | None = getattr(request.state, "query_location", None)
    if params is None:
        return enforce_query_location(request)
    return params
