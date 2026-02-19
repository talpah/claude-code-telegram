import nox

nox.options.default_venv_backend = "uv"
nox.options.sessions = ["lint", "typecheck", "tests"]


@nox.session
def lint(session: nox.Session) -> None:
    """Check code style and imports."""
    session.install("ruff")
    session.run("ruff", "check", "src", "tests")
    session.run("ruff", "format", "--check", "src", "tests")


@nox.session
def format(session: nox.Session) -> None:
    """Auto-format code and fix lint issues."""
    session.install("ruff")
    session.run("ruff", "format", "src", "tests")
    session.run("ruff", "check", "--fix", "src", "tests")


@nox.session
def typecheck(session: nox.Session) -> None:
    """Run type checker."""
    session.install("ty", ".")
    session.run("ty", "check", "src")


@nox.session(python=["3.11", "3.12"])
def tests(session: nox.Session) -> None:
    """Run the test suite."""
    session.install("pytest", "pytest-asyncio", "pytest-cov", "pytest-mock", ".")
    session.run("pytest", *session.posargs)
