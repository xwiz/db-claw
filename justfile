# SemanticSQL — top-level task runner
# Run `just` for a list. Each layer also has its own README.

set windows-shell := ["powershell.exe", "-NoLogo", "-Command"]

default:
    @just --list

# ---- top-level --------------------------------------------------------------

# Build all three stacks
build: build-rust build-py build-ts

# Run all tests across all stacks
test: test-rust test-py test-ts

# Lint everything
lint: lint-rust lint-py lint-ts

# Format everything
fmt: fmt-rust fmt-py fmt-ts

# ---- rust -------------------------------------------------------------------

build-rust:
    cargo build --workspace

test-rust:
    cargo test --workspace --no-fail-fast

lint-rust:
    cargo clippy --workspace --all-targets -- -D warnings

fmt-rust:
    cargo fmt --all

# ---- python -----------------------------------------------------------------

build-py:
    uv sync --all-extras

test-py:
    uv run pytest

lint-py:
    uv run ruff check .
    uv run mypy python

fmt-py:
    uv run ruff format .

# ---- typescript -------------------------------------------------------------

build-ts:
    pnpm -r build

test-ts:
    pnpm -r test

lint-ts:
    pnpm -r lint

fmt-ts:
    pnpm -r format

# ---- protobuf ---------------------------------------------------------------

# Compile schemas/*.proto into Rust + Python + TS bindings
proto:
    cargo build -p semsql-core --features build-protos
    uv run python -m grpc_tools.protoc -I schemas --python_out=python/semsql_py/_proto schemas/*.proto

# ---- semsql cli (developer convenience) -------------------------------------

extract framework path out:
    cargo run -p semsql-cli -- extract --framework {{framework}} {{path}} -o {{out}}

query graph nl:
    cargo run -p semsql-cli -- query --graph {{graph}} "{{nl}}"

doctor graph:
    cargo run -p semsql-cli -- doctor --graph {{graph}}
