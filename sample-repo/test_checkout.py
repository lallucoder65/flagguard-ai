import ast
import importlib.util
import inspect
import sys
from pathlib import Path
from unittest.mock import MagicMock

SAMPLE_DIR = Path(__file__).parent
CHECKOUT_FILE = SAMPLE_DIR / "checkout.py"


def _load_checkout():
    if importlib.util.find_spec("flagsmith") is None:
        sys.modules["flagsmith"] = MagicMock()
    spec = importlib.util.spec_from_file_location("checkout", CHECKOUT_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_checkout_valid_syntax():
    """Verify checkout.py parses cleanly after AST if/else unwrapping."""
    source = CHECKOUT_FILE.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(CHECKOUT_FILE))
    assert isinstance(tree, ast.Module)


def test_checkout_imports_and_exposes_callables():
    """Verify checkout.py imports cleanly with no broken indentation or missing symbols."""
    checkout = _load_checkout()
    callables = [
        name for name, obj in inspect.getmembers(checkout, inspect.isfunction)
        if obj.__module__ == "checkout"
    ]
    assert len(callables) > 0, "Expected at least one function in checkout.py"


def test_active_flag_preserved():
    """Verify active_checkout remains present in checkout.py."""
    source = CHECKOUT_FILE.read_text(encoding="utf-8")
    assert "active_checkout" in source
# Trigger FlagGuard PR bot check

