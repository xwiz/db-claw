"""SemanticSQL — Python SDK.

Thin wrapper over ``semsql-ffi`` (the Rust C-ABI binding compiled via
maturin). Surface mirrors the planned Node and PHP SDKs: embeddable-library
first, HTTP server second.

Planned surface (lands in v0.2 once the cascade is wired):

    from semsql import SemSQL

    client = SemSQL("graph.semsql", model="model.onnx")
    result = client.query("active students who joined last month", tenant_id=42)
    result.sql              # str — final dialect-rendered SQL
    result.bindings         # dict[str, Any] — bound parameters
    result.confidence       # float — per-stage confidence score
    result.injected_filters # list[tuple[str, str]] — audit log for the call

This v0.1 cut ships an empty namespace package so dependents can already
declare ``semsql`` as a dependency.
"""

__version__ = "0.1.0.dev0"
