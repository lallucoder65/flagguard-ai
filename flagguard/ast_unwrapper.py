import ast
from pathlib import Path
import subprocess
import sys

from flagguard.config import FLAG_METHODS, console
from flagguard.scanner import _collect_python_constants


class DeadFlagUnwrapper(ast.NodeTransformer):
    """AST transformer that removes dead flag branches and unwraps else: fallback logic."""

    def __init__(self, target_flag: str, constants: dict[str, str] | None = None):
        self.target_flag = target_flag
        self.constants = constants or {}
        self.modified = False

    def _is_target_flag_call(self, node: ast.AST) -> bool:
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in FLAG_METHODS and node.args:
                arg = node.args[0]
                if isinstance(arg, ast.Constant) and arg.value == self.target_flag:
                    return True
                if isinstance(arg, ast.Name) and self.constants.get(arg.id) == self.target_flag:
                    return True
        return False

    def visit_If(self, node: ast.If):
        self.generic_visit(node)
        if self._is_target_flag_call(node.test):
            self.modified = True
            return node.orelse if node.orelse else None

        if isinstance(node.test, ast.UnaryOp) and isinstance(node.test.op, ast.Not):
            if self._is_target_flag_call(node.test.operand):
                self.modified = True
                return node.body if node.body else None

        return node

    def visit_FunctionDef(self, node: ast.FunctionDef):
        self.generic_visit(node)
        if not node.body:
            node.body = [ast.Pass()]
        return node


def run_pytest_gate(target_dir: str) -> tuple[bool, str]:
    """Runs pytest if test files exist in the target repo to verify patch safety."""
    root = Path(target_dir)
    test_files = list(root.rglob("test_*.py")) + list(root.rglob("*_test.py"))
    if not test_files:
        return True, "No test files found (AST syntax gate passed)"

    proc = subprocess.run(
        [sys.executable, "-m", "pytest", str(root), "-q"],
        capture_output=True,
        text=True,
    )
    if proc.returncode == 0:
        return True, f"pytest passed ({len(test_files)} test file(s))"
    return False, (proc.stdout + "\n" + proc.stderr).strip()


def unwrap_flag_in_file(file_path: Path | str, flag_name: str, repo_path: str | None = None) -> bool:
    """Unwrap a dead flag in one Python file, then verify with ast.parse() and pytest.

    On a syntax or pytest failure the original source is written back.
    """
    path = Path(file_path)
    repo = str(repo_path or path.parent)
    original = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(original, filename=str(path))
        constants = _collect_python_constants(tree)
        unwrapper = DeadFlagUnwrapper(flag_name, constants)
        new_tree = unwrapper.visit(tree)

        if not unwrapper.modified:
            console.print(
                f"  [yellow]![/] Could not auto-unwrap complex condition in [bold]{path.name}[/]; manual review recommended."
            )
            return False

        ast.fix_missing_locations(new_tree)
        new_source = ast.unparse(new_tree) + "\n"
        ast.parse(new_source)

        path.write_text(new_source, encoding="utf-8")

        tests_ok, test_msg = run_pytest_gate(repo)
        if not tests_ok:
            path.write_text(original, encoding="utf-8")
            console.print(
                f"  [bold red]✗ ROLLBACK:[/] Reverted [bold]{path.name}[/] because pytest failed:\n{test_msg}"
            )
            return False

        console.print(
            f"  [bold green]✓ VERIFIED & APPLIED:[/] Unwrapped dead '[cyan]{flag_name}[/]' branch in [bold]{path.name}[/] ({test_msg})."
        )
        return True
    except SyntaxError as exc:
        path.write_text(original, encoding="utf-8")
        console.print(
            f"  [bold red]✗ ROLLBACK:[/] Patch aborted due to AST syntax check failure: {exc}"
        )
        return False


def unwrap_flag_references(flag_name: str, code_refs: list[dict], repo_path: str) -> bool:
    """Unwrap every Python file referenced by an audit hit for ``flag_name``."""
    patched_any = False
    seen_files: set[Path] = set()

    for ref in code_refs:
        filename, _ = ref["location"].split(":")
        file_path = next(Path(repo_path).rglob(filename), None)
        if not file_path or file_path.suffix != ".py" or file_path in seen_files:
            continue
        seen_files.add(file_path)
        if unwrap_flag_in_file(file_path, flag_name, repo_path):
            patched_any = True
    return patched_any
