"""Unit coverage for the AST status scanner behind gate G4.

The gate itself (``tests/test_openapi_contract_gate.py``) exercises the scanner
end to end against the real application. What it cannot reach are the scanner's
deliberate dead ends -- the shapes it is designed to *stop* at, which is exactly
where a silent over-reach would hide. Each test below pins one of those.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from fastapi import FastAPI

from app.core import openapi_status_scan as scan


def parse_call(source: str) -> ast.Call:
    node = ast.parse(source, mode="eval").body
    assert isinstance(node, ast.Call)
    return node


def parse_raise(source: str) -> ast.Raise:
    node = ast.parse(source).body[0]
    assert isinstance(node, ast.Raise)
    return node


# ── module resolution stops at the repository boundary ───────────────────────


def test_module_path_resolves_a_package_to_its_init() -> None:
    resolved = scan._module_path("app.api")
    assert resolved is not None
    assert resolved.name == "__init__.py"


def test_module_path_returns_none_for_a_missing_in_repo_module() -> None:
    assert scan._module_path("app.does.not.exist") is None


def test_module_path_returns_none_outside_the_app_package() -> None:
    assert scan._module_path("os") is None
    assert scan._module_path("httpx") is None


def test_index_module_returns_none_for_external_modules() -> None:
    assert scan._index_module("sqlalchemy") is None


def test_index_module_reads_imports_and_functions() -> None:
    index = scan._index_module("app.api.v1.deposit")
    assert index is not None
    assert "deposit_accounts" in index.functions
    assert index.imports["require_headers"] == "app.api.deps.require_headers"


def test_index_module_resolves_relative_imports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No module in this repository uses ``from . import x``; the parser still must.

    Covered through a stand-in file rather than by adding a relative import to
    production code: the branch is about the parser, not about the app.
    """
    stand_in = tmp_path / "stand_in.py"
    stand_in.write_text(
        "from . import sibling\nfrom .deeper import helper as aliased\n", encoding="utf-8"
    )
    monkeypatch.setattr(scan, "_module_path", lambda module: stand_in)
    scan._index_module.cache_clear()
    try:
        index = scan._index_module("app.pkg.stand_in")
        assert index is not None
        assert index.imports["sibling"] == "app.pkg.sibling"
        assert index.imports["aliased"] == "app.pkg.deeper.helper"
    finally:
        scan._index_module.cache_clear()


def test_index_module_records_plain_and_aliased_imports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stand_in = tmp_path / "imports.py"
    stand_in.write_text("import app.services.submit as submit\nimport json\n", encoding="utf-8")
    monkeypatch.setattr(scan, "_module_path", lambda module: stand_in)
    scan._index_module.cache_clear()
    try:
        index = scan._index_module("app.pkg.imports")
        assert index is not None
        assert index.imports["submit"] == "app.services.submit"
        assert index.imports["json"] == "json"
    finally:
        scan._index_module.cache_clear()


# ── literal / keyword helpers ────────────────────────────────────────────────


def test_literal_int_rejects_non_integer_nodes() -> None:
    assert scan._literal_int(None) is None
    assert scan._literal_int(ast.parse("'400'", mode="eval").body) is None
    assert scan._literal_int(ast.parse("status", mode="eval").body) is None
    assert scan._literal_int(ast.parse("404", mode="eval").body) == 404


def test_keyword_returns_none_when_absent() -> None:
    call = parse_call("f(detail='x', headers=None)")
    assert scan._keyword(call, "status_code") is None
    assert scan._keyword(call, "detail") is not None


# ── call-target splitting ────────────────────────────────────────────────────


def test_called_name_shapes() -> None:
    assert scan._called_name(parse_call("foo(1)")) == (None, "foo")
    assert scan._called_name(parse_call("mod.foo(1)")) == ("mod", "foo")
    assert scan._called_name(parse_call("a.b.foo(1)")) == (None, "foo")
    # A call whose target is itself a call (`factory()(1)`) is unattributable.
    assert scan._called_name(parse_call("factory()(1)")) == (None, None)


# ── status codes out of call expressions ─────────────────────────────────────


def test_codes_from_call_reads_positional_and_keyword_status() -> None:
    assert list(scan._codes_from_call(parse_call("HTTPException(404, detail='x')"))) == [404]
    assert list(scan._codes_from_call(parse_call("HTTPException(status_code=409)"))) == [409]
    assert list(scan._codes_from_call(parse_call("JSONResponse({}, status_code=503)"))) == [503]


def test_codes_from_call_yields_nothing_without_a_literal_status() -> None:
    """A computed status is invisible to a static scan -- and must not be guessed."""
    assert list(scan._codes_from_call(parse_call("HTTPException(status_code=code)"))) == []
    assert list(scan._codes_from_call(parse_call("HTTPException(detail='x')"))) == []
    assert list(scan._codes_from_call(parse_call("JSONResponse({}, status_code=code)"))) == []
    assert list(scan._codes_from_call(parse_call("JSONResponse({})"))) == []
    assert list(scan._codes_from_call(parse_call("factory()(1)"))) == []
    assert list(scan._codes_from_call(parse_call("session.execute(stmt)"))) == []


# ── status codes out of raise statements ─────────────────────────────────────


def test_codes_from_raise_maps_known_exception_classes() -> None:
    assert list(scan._codes_from_raise(parse_raise("raise RequestValidationError([])"))) == [422]
    assert list(scan._codes_from_raise(parse_raise("raise ValidationError"))) == [422]


def test_codes_from_raise_falls_back_to_the_catch_all_status() -> None:
    assert list(scan._codes_from_raise(parse_raise("raise WedapError('E', 'm')"))) == [500]
    assert list(scan._codes_from_raise(parse_raise("raise errors.IdempotencyConflict"))) == [500]


def test_codes_from_raise_ignores_unattributable_shapes() -> None:
    assert list(scan._codes_from_raise(parse_raise("raise"))) == []
    assert list(scan._codes_from_raise(parse_raise("raise HTTPException(400)"))) == []
    assert list(scan._codes_from_raise(parse_raise("raise (a if b else c)"))) == []
    assert list(scan._codes_from_raise(parse_raise("raise factory()()"))) == []


# ── call resolution ──────────────────────────────────────────────────────────


def test_resolve_call_shapes() -> None:
    index = scan._index_module("app.api.v1.deposit")
    assert index is not None
    module = "app.api.v1.deposit"

    same_module = scan._resolve_call(module, index, parse_call("_audited_passthrough(r)"))
    assert same_module == (module, "_audited_passthrough")

    imported = scan._resolve_call(module, index, parse_call("require_headers(request)"))
    assert imported == ("app.api.deps", "require_headers")

    assert scan._resolve_call(module, index, parse_call("factory()(1)")) is None
    assert scan._resolve_call(module, index, parse_call("never_heard_of_it()")) is None
    assert scan._resolve_call(module, index, parse_call("unknown_mod.helper()")) is None
    # Resolvable name, but the target module lives outside the repository.
    assert scan._resolve_call(module, index, parse_call("hashlib.sha256(b'')")) is None


# ── walk guards ──────────────────────────────────────────────────────────────


def test_walk_function_stops_at_external_modules() -> None:
    codes: set[int] = set()
    scan._walk_function("os.path", "join", 0, set(), codes)
    assert codes == set()


def test_walk_function_stops_on_unknown_function_names() -> None:
    codes: set[int] = set()
    scan._walk_function("app.api.v1.deposit", "no_such_function", 0, set(), codes)
    assert codes == set()


def test_walk_function_stops_on_depth_and_revisits() -> None:
    deep: set[int] = set()
    scan._walk_function(
        "app.api.v1.deposit", "deposit_accounts", scan.MAX_CALL_DEPTH + 1, set(), deep
    )
    assert deep == set()

    revisited: set[int] = set()
    seen = {("app.api.v1.deposit", "deposit_accounts")}
    scan._walk_function("app.api.v1.deposit", "deposit_accounts", 0, seen, revisited)
    assert revisited == set()


# ── public entry points ──────────────────────────────────────────────────────


def test_scan_route_finds_the_upstream_mapping_of_a_passthrough() -> None:
    target = scan.RouteTarget(
        method="GET",
        path="/api/v1/deposit/accounts",
        module="app.api.v1.deposit",
        function="deposit_accounts",
    )
    assert 502 in scan.scan_route(target)


def test_route_targets_skips_non_api_routes() -> None:
    """`/openapi.json` and the doc routes are Starlette routes, not operations."""
    from app.main import create_app

    targets = scan.route_targets(create_app())
    paths = {target.path for target in targets}
    assert "/openapi.json" not in paths
    assert "/docs" not in paths
    assert len(targets) == 28


def test_route_targets_tolerates_an_app_without_routes() -> None:
    assert scan.route_targets(object()) == []
    assert scan.route_targets(FastAPI()) == []


def test_scan_app_keys_by_method_and_path() -> None:
    from app.main import create_app

    scanned = scan.scan_app(create_app())
    assert ("GET", "/readyz") in scanned
    assert scanned[("GET", "/readyz")] == [503]
