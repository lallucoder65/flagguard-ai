import ast
from pathlib import Path
import re

from flagguard.config import FLAG_METHODS

JS_FLAG_PATTERN = re.compile(
    r"""\b(?:hasFeature|getValue|useFlags)\s*\(\s*\[?\s*['"]([a-zA-Z0-9_-]+)['"]"""
)
JS_CONST_PATTERN = re.compile(
    r"""(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*['"]([A-Za-z0-9_-]+)['"]"""
)
JS_IDENT_PATTERN = re.compile(
    r"""\b(?:hasFeature|getValue|useFlags)\s*\(\s*\[?\s*([A-Za-z_$][\w$]*)"""
)
_FLAG_TOKEN = re.compile(r"^[A-Za-z0-9_-]+$")


def _collect_python_constants(tree: ast.AST) -> dict[str, str]:
    """Pass 1: map names to string literals (CHECKOUT_FLAG = \"active_checkout\")."""
    constants: dict[str, str] = {}
    for node in ast.walk(tree):
        targets: list[ast.AST] = []
        value: ast.AST | None = None
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
            value = node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets = [node.target]
            value = node.value
        if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                constants[target.id] = value.value
    return constants


def _resolve_python_flag(arg: ast.AST, constants: dict[str, str], known_flags: set[str]) -> str | None:
    """Resolve a flag SDK argument from a string literal or a collected constant."""
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return arg.value
    if isinstance(arg, ast.Name):
        value = constants.get(arg.id)
        if isinstance(value, str) and (value in known_flags or _FLAG_TOKEN.fullmatch(value)):
            return value
    return None


def _scan_python_file(py_file: Path, known_flags: set[str]) -> dict[str, list[dict]]:
    """Pass 2: record flag SDK calls, resolving constant aliases from pass 1."""
    found: dict[str, list[dict]] = {}
    source = py_file.read_text(encoding="utf-8")
    lines = source.splitlines()
    tree = ast.parse(source, filename=str(py_file))
    constants = _collect_python_constants(tree)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in FLAG_METHODS or not node.args:
            continue
        flag_name = _resolve_python_flag(node.args[0], constants, known_flags)
        if not flag_name:
            continue
        start = max(0, node.lineno - 3)
        end = min(len(lines), node.lineno + 4)
        snippet = "\n".join(f"{idx + 1}: {lines[idx]}" for idx in range(start, end))
        found.setdefault(flag_name, []).append(
            {
                "location": f"{py_file.name}:{node.lineno}",
                "snippet": snippet,
            }
        )
    return found


def _merge_refs(found: dict[str, list[dict]], extra: dict[str, list[dict]]) -> None:
    for flag_name, refs in extra.items():
        found.setdefault(flag_name, []).extend(refs)


def scan_codebase_with_context(repo_path: str, known_flags) -> dict[str, list[dict]]:
    """Extract flag names across Python (AST) and JS/TS/React files with line context.

    ``known_flags`` is the live Flagsmith name set. Direct string literals are always
    recorded. Assignments such as ``CHECKOUT_FLAG = "active_checkout"`` are resolved
    when that name is passed into a flag SDK call.
    """
    known = set(known_flags)
    found: dict[str, list[dict]] = {}
    root = Path(repo_path)

    for py_file in root.rglob("*.py"):
        if py_file.name.startswith("test_") or py_file.name.endswith("_test.py"):
            continue
        _merge_refs(found, _scan_python_file(py_file, known))

    for ext in ("*.js", "*.ts", "*.jsx", "*.tsx"):
        for js_file in root.rglob(ext):
            lines = js_file.read_text(encoding="utf-8").splitlines()
            constants: dict[str, str] = {}
            for line in lines:
                for match in JS_CONST_PATTERN.finditer(line):
                    constants[match.group(1)] = match.group(2)

            for idx, line in enumerate(lines):
                flag_names: list[str] = []
                for match in JS_FLAG_PATTERN.finditer(line):
                    flag_names.append(match.group(1))
                for match in JS_IDENT_PATTERN.finditer(line):
                    value = constants.get(match.group(1))
                    if value and (value in known or _FLAG_TOKEN.fullmatch(value)):
                        flag_names.append(value)
                if not flag_names:
                    continue
                start = max(0, idx - 2)
                end = min(len(lines), idx + 3)
                snippet = "\n".join(f"{i + 1}: {lines[i]}" for i in range(start, end))
                seen_on_line: set[str] = set()
                for flag_name in flag_names:
                    if flag_name in seen_on_line:
                        continue
                    seen_on_line.add(flag_name)
                    found.setdefault(flag_name, []).append(
                        {
                            "location": f"{js_file.name}:{idx + 1}",
                            "snippet": snippet,
                        }
                    )

    return found
