"""Interface descriptions shipped with the library (package data)."""

from importlib.resources import files


def load(name: str) -> str:
    """The text of `<name>.varlink`, e.g. load("io.pipanel.App")."""
    return files(__name__).joinpath(f"{name}.varlink").read_text(encoding="utf-8")
