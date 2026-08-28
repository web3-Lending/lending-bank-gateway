"""Static discovery of the HTTP status codes each route handler can emit.

Why this module exists
----------------------
The OpenAPI contract snapshot (``OPERATION_CONTRACTS``) is hand-curated: a human
decides which failure codes are genuinely reachable for each operation. That
snapshot rots the moment somebody adds a new ``raise HTTPException(409, ...)``
deep inside a service helper. This scanner is the machine counterpart: it walks
the AST of every route handler and of the in-repo functions it calls, and reports
every status code it can see being produced. The contract gate then asserts
``scanned ⊆ declared`` — a new raise site that nobody declared turns the gate red.

Scope and deliberate limits
---------------------------
* Only modules that live inside this repository's application package are parsed.
  Third-party code under ``.venv`` is never opened; ``site-packages`` call targets
  simply terminate a branch of the walk.
* Call resolution is name-based and therefore conservative in both directions:
  it resolves ``foo(...)`` and ``mod.foo(...)`` through the module's own import
  table, but it cannot resolve method calls on runtime objects
  (``self.x()``, ``client.post()``) or values held in variables. Those branches
  are not followed.
* Recursion is bounded by ``MAX_CALL_DEPTH`` and by a per-route visited set, so a
  cycle in the call graph cannot hang the scan.
* The result is a *superset candidate* list, not a reachability proof. A helper
  may raise 409 on a path this particular route can never take. Excluding such a
  false positive is a human decision recorded in the contract snapshot; this
  module never prunes on its own.

Exception-to-status mapping
---------------------------
``EXCEPTION_STATUS_MAP`` below is read off the exception handlers registered in
``app/main.py`` (``create_app``). It is the whole map -- this service registers
exactly three handlers plus the catch-all, so any exception class that is not an
``HTTPException`` subclass and not ``RequestValidationError`` reaches Starlette's
``ServerErrorMiddleware`` and becomes a 500.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

APP_PACKAGE = "app"
APP_ROOT = Path(__file__).resolve().parent.parent

#: How far the walk follows in-repo calls out of a route handler. Depth 0 is the
#: handler body itself. Six levels covers handler -> api helper -> service ->
#: domain primitive with room to spare; every raise site in this repository sits
#: within three.
MAX_CALL_DEPTH = 6

#: Names that carry an explicit HTTP status as their first positional argument or
#: as ``status_code=``. Both Starlette's and FastAPI's ``HTTPException`` are
#: registered on the same handler in ``create_app``.
_HTTP_EXCEPTION_NAMES = frozenset({"HTTPException", "StarletteHTTPException"})

#: Response classes constructed with an explicit ``status_code=`` keyword.
_RESPONSE_NAMES = frozenset(
    {
        "Response",
        "JSONResponse",
        "ORJSONResponse",
        "UJSONResponse",
        "PlainTextResponse",
        "HTMLResponse",
        "RedirectResponse",
        "StreamingResponse",
        "FileResponse",
    }
)

#: Exception class -> HTTP status, as registered by ``create_app``:
#:   app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
#:   app.add_exception_handler(HTTPException,          _http_exception_handler)
#:   app.add_exception_handler(RequestValidationError, _validation_exception_handler)
#:   app.add_exception_handler(Exception,              _generic_exception_handler)
#: ``HTTPException`` is absent on purpose: its status is read from the call site,
#: not from this table.
EXCEPTION_STATUS_MAP: Mapping[str, int] = {
    "RequestValidationError": 422,
    "ValidationError": 422,
}

#: Status handed to every other ``raise`` of a class this service does not map --
#: the catch-all ``add_exception_handler(Exception, _generic_exception_handler)``.
UNMAPPED_EXCEPTION_STATUS = 500


@dataclass(frozen=True)
class RouteTarget:
    """One OpenAPI operation plus the dotted path of the function implementing it."""

    method: str
    path: str
    module: str
    function: str


@dataclass
class _ModuleIndex:
    """Parsed form of one in-repo module: its functions plus its import table."""

    functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = field(default_factory=dict)
    #: local alias -> dotted target, e.g. ``{"require_headers": "app.api.deps.require_headers"}``
    imports: dict[str, str] = field(default_factory=dict)


def _module_path(module: str) -> Path | None:
    """Map a dotted module name to its file inside this repository, or ``None``.

    Returning ``None`` for anything outside the application package is what keeps
    the walk out of ``.venv``.
    """
    if module != APP_PACKAGE and not module.startswith(APP_PACKAGE + "."):
        return None
    relative = module.split(".")[1:]
    candidate = APP_ROOT.joinpath(*relative).with_suffix(".py")
    if candidate.is_file():
        return candidate
    package_init = APP_ROOT.joinpath(*relative, "__init__.py")
    return package_init if package_init.is_file() else None


@cache
def _index_module(module: str) -> _ModuleIndex | None:
    """Parse one in-repo module into ``{functions, imports}``; ``None`` if external."""
    path = _module_path(module)
    if path is None:
        return None
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    index = _ModuleIndex()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            # Nested and method definitions collapse onto their bare name. Name
            # collisions inside one module are possible in principle but would
            # only widen the candidate set, never narrow it.
            index.functions.setdefault(node.name, node)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                index.imports[alias.asname or alias.name.split(".")[0]] = alias.name
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:  # relative import
                parts = module.split(".")[: -node.level] or [APP_PACKAGE]
                base = ".".join([*parts, base]) if base else ".".join(parts)
            for alias in node.names:
                index.imports[alias.asname or alias.name] = f"{base}.{alias.name}"
    return index


def _literal_int(node: ast.expr | None) -> int | None:
    """Return the value of an integer literal node, else ``None``."""
    if isinstance(node, ast.Constant) and isinstance(node.value, int):
        return node.value
    return None


def _keyword(call: ast.Call, name: str) -> ast.expr | None:
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


def _called_name(call: ast.Call) -> tuple[str | None, str | None]:
    """Split a call target into ``(qualifier, name)``.

    ``foo(...)``     -> ``(None, "foo")``
    ``mod.foo(...)`` -> ``("mod", "foo")``
    Anything deeper (``a.b.c()``, ``obj.attr()`` on a runtime value) yields the
    outermost qualifier we can see; unresolvable qualifiers simply fail to match
    the import table and terminate that branch.
    """
    func = call.func
    if isinstance(func, ast.Name):
        return None, func.id
    if isinstance(func, ast.Attribute):
        value = func.value
        if isinstance(value, ast.Name):
            return value.id, func.attr
        return None, func.attr
    return None, None


def _codes_from_call(call: ast.Call) -> Iterator[int]:
    """Status codes a single call expression produces, if it produces any."""
    _, name = _called_name(call)
    if name is None:
        return
    if name in _HTTP_EXCEPTION_NAMES:
        # HTTPException(400, detail=...) and HTTPException(status_code=400, ...)
        positional = _literal_int(call.args[0]) if call.args else None
        code = positional if positional is not None else _literal_int(_keyword(call, "status_code"))
        if code is not None:
            yield code
        return
    if name in _RESPONSE_NAMES:
        code = _literal_int(_keyword(call, "status_code"))
        if code is not None:
            yield code


def _codes_from_raise(node: ast.Raise) -> Iterator[int]:
    """Status code implied by ``raise <ExcClass>(...)`` / ``raise <ExcClass>``.

    ``HTTPException`` is handled by :func:`_codes_from_call` (the exception object
    carries its own status), so it is skipped here to avoid double counting.
    A bare ``raise`` re-raises whatever is in flight and is not attributable.
    """
    exc = node.exc
    if exc is None:
        return
    if isinstance(exc, ast.Call):
        _, name = _called_name(exc)
    elif isinstance(exc, ast.Name):
        name = exc.id
    elif isinstance(exc, ast.Attribute):
        name = exc.attr
    else:
        return
    if name is None or name in _HTTP_EXCEPTION_NAMES:
        return
    yield EXCEPTION_STATUS_MAP.get(name, UNMAPPED_EXCEPTION_STATUS)


def _resolve_call(module: str, index: _ModuleIndex, call: ast.Call) -> tuple[str, str] | None:
    """Resolve a call to an in-repo ``(module, function)``, or ``None``.

    Three shapes resolve:
      * a function defined in the same module,
      * a name imported via ``from app.x import foo``,
      * ``mod.foo`` where ``mod`` was imported via ``import app.x as mod``.
    Everything else (methods on runtime objects, third-party targets) returns
    ``None`` and ends that branch of the walk.
    """
    qualifier, name = _called_name(call)
    if name is None:
        return None
    if qualifier is None:
        target = index.imports.get(name)
        if target is None:
            return (module, name) if name in index.functions else None
    else:
        base = index.imports.get(qualifier)
        if base is None:
            return None
        target = f"{base}.{name}"
    target_module, _, target_name = target.rpartition(".")
    if _module_path(target_module) is not None:
        return target_module, target_name
    # ``from app.services.submit import submit_order`` where the dotted prefix is
    # itself the module: already handled above. Otherwise the target may be a
    # module attribute of an in-repo package (e.g. ``from app.api.v1 import
    # callbacks`` then ``callbacks.router``) -- not a function, so stop.
    return None


def _walk_function(
    module: str,
    function: str,
    depth: int,
    seen: set[tuple[str, str]],
    codes: set[int],
) -> None:
    """Collect status codes from one function body, recursing into in-repo calls."""
    key = (module, function)
    if key in seen or depth > MAX_CALL_DEPTH:
        return
    seen.add(key)
    index = _index_module(module)
    if index is None:
        return
    node = index.functions.get(function)
    if node is None:
        return
    for child in ast.walk(node):
        if isinstance(child, ast.Raise):
            codes.update(_codes_from_raise(child))
        elif isinstance(child, ast.Call):
            codes.update(_codes_from_call(child))
            target = _resolve_call(module, index, child)
            if target is not None:
                _walk_function(target[0], target[1], depth + 1, seen, codes)


def scan_route(target: RouteTarget) -> set[int]:
    """Status codes statically discoverable for one operation."""
    codes: set[int] = set()
    _walk_function(target.module, target.function, 0, set(), codes)
    return codes


def scan_routes(targets: Iterable[RouteTarget]) -> dict[tuple[str, str], list[int]]:
    """Scan every operation, keyed by ``(METHOD, path)`` with sorted code lists."""
    return {(t.method, t.path): sorted(scan_route(t)) for t in targets}


def route_targets(app: object) -> list[RouteTarget]:
    """Enumerate the app's OpenAPI operations as :class:`RouteTarget` records.

    Uses ``fastapi.routing.iter_route_contexts`` -- the same traversal
    ``fastapi.openapi.utils.get_openapi`` uses -- so this list cannot drift from
    what actually lands in ``/openapi.json``. A plain ``for r in app.routes`` walk
    would miss everything, because ``include_router`` stores an ``_IncludedRouter``
    wrapper rather than the individual ``APIRoute`` objects.
    """
    from fastapi import routing

    targets: list[RouteTarget] = []
    for context in routing.iter_route_contexts(getattr(app, "routes", [])):
        route = getattr(context, "route", context)
        if not isinstance(route, routing.APIRoute):
            continue
        endpoint = route.endpoint
        for method in sorted(route.methods or set()):
            targets.append(
                RouteTarget(
                    method=method,
                    path=route.path_format,
                    module=endpoint.__module__,
                    function=endpoint.__name__,
                )
            )
    return targets


def scan_app(app: object) -> dict[tuple[str, str], list[int]]:
    """Convenience entry point: enumerate operations and scan them all."""
    return scan_routes(route_targets(app))
