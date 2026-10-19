# Run the same non-mutating checks locally and in CI; stop on the first failure.
check: lint test

# Pin the linter so checks do not change with new Ruff releases.
lint:
    uv tool run --from ruff==0.17.1 ruff check src tests

# Run the test suite; the real SSH smoke test requires explicit opt-in.
test:
    PYTHONPATH=src python3 -m unittest discover -v
