import ast
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from dotenv import load_dotenv
from google import genai
from google.genai import types
import requests
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table

load_dotenv()

console = Console()

FLAGSMITH_KEY = os.getenv("FLAGSMITH_API_KEY")
PROJECT_ID = os.getenv("FLAGSMITH_PROJECT_ID", "54836")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")

FS_HEADERS = {
    "Authorization": f"Api-Key {FLAGSMITH_KEY}",
    "Content-Type": "application/json",
}
ENVS = {
    "Dev": os.getenv("FLAGSMITH_DEV_ENV_KEY", "BrTukFPrbc6d5jGFEy2K8w"),
    "Prod": os.getenv("FLAGSMITH_PROD_ENV_KEY", "AFikpqKycTH6CA6n5ZVYce"),
}
FLAG_METHODS = {"is_feature_enabled", "has_feature", "get_feature_value"}

client = genai.Client(api_key=GEMINI_KEY)

JS_FLAG_PATTERN = re.compile(
    r"""\b(?:hasFeature|getValue|useFlags)\s*\(\s*\[?\s*['"]([a-zA-Z0-9_-]+)['"]"""
)


def scan_codebase_with_context(target_dir: str) -> dict[str, list[dict]]:
    """Extracts flag names across Python (AST) and JS/TS/React files with line context."""
    found: dict[str, list[dict]] = {}
    root = Path(target_dir)

    # 1. Python AST scan
    for py_file in root.rglob("*.py"):
        if py_file.name.startswith("test_") or py_file.name.endswith("_test.py"):
            continue
        source = py_file.read_text(encoding="utf-8")
        lines = source.splitlines()
        tree = ast.parse(source, filename=str(py_file))

        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in FLAG_METHODS and node.args:
                    arg = node.args[0]
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        start = max(0, node.lineno - 3)
                        end = min(len(lines), node.lineno + 4)
                        snippet = "\n".join(
                            f"{idx + 1}: {lines[idx]}" for idx in range(start, end)
                        )
                        found.setdefault(arg.value, []).append(
                            {
                                "location": f"{py_file.name}:{node.lineno}",
                                "snippet": snippet,
                            }
                        )

    # 2. JavaScript / TypeScript / React scan
    for ext in ("*.js", "*.ts", "*.jsx", "*.tsx"):
        for js_file in root.rglob(ext):
            lines = js_file.read_text(encoding="utf-8").splitlines()
            for idx, line in enumerate(lines):
                for match in JS_FLAG_PATTERN.finditer(line):
                    flag_name = match.group(1)
                    start = max(0, idx - 2)
                    end = min(len(lines), idx + 3)
                    snippet = "\n".join(
                        f"{i + 1}: {lines[i]}" for i in range(start, end)
                    )
                    found.setdefault(flag_name, []).append(
                        {
                            "location": f"{js_file.name}:{idx + 1}",
                            "snippet": snippet,
                        }
                    )

    return found


def fetch_flagsmith_state() -> dict:
    """Fetches non-archived flags and their Dev/Prod states from Flagsmith."""
    resp = requests.get(
        f"https://api.flagsmith.com/api/v1/projects/{PROJECT_ID}/features/",
        headers=FS_HEADERS,
        timeout=10,
    )
    resp.raise_for_status()
    features = resp.json().get("results", [])

    env_states = {name: {} for name in ENVS}
    env_values = {name: {} for name in ENVS}
    for env_name, env_key in ENVS.items():
        env_resp = requests.get(
            f"https://api.flagsmith.com/api/v1/environments/{env_key}/featurestates/",
            headers=FS_HEADERS,
            timeout=10,
        )
        env_resp.raise_for_status()
        for s in env_resp.json().get("results", []):
            env_states[env_name][s["feature"]] = s["enabled"]
            env_values[env_name][s["feature"]] = s.get("feature_state_value")

    cloud = {}
    for f in features:
        if f.get("is_archived"):
            continue
        fid = f["id"]
        cloud[f["name"]] = {
            "id": fid,
            "created_date": f.get("created_date"),
            "dev": env_states["Dev"].get(fid, False),
            "prod": env_states["Prod"].get(fid, False),
            "dev_value": env_values["Dev"].get(fid),
        }
    return cloud


def get_gemini_models() -> list[str]:
    """Discovers available Gemini Flash models for this API key, prioritizing gemini-3.8-flash."""
    preferred = ["gemini-3.8-flash"]
    try:
        for m in client.models.list():
            name = m.name.replace("models/", "")
            if "flash" in name and "tts" not in name and "image" not in name:
                if name not in preferred:
                    preferred.append(name)
    except Exception:
        pass
    return preferred


AVAILABLE_MODELS = get_gemini_models()
WORKING_MODEL: str | None = None
GOVERNANCE_FLAG = "flagguard_agent_config"


def evaluate_governance_identity(cloud_flags: dict, repo_tier: str) -> dict:
    """Evaluates Flagsmith Identity + repo_tier trait to get Segment-targeted Remote Config."""
    policy = {
        "enabled": True,
        "model_priority": AVAILABLE_MODELS,
        "min_age_days": 7,
        "allow_auto_remediate": False,
        "source": "Environment Default",
    }

    gov = cloud_flags.get(GOVERNANCE_FLAG)
    if gov:
        policy["enabled"] = gov["dev"]
        raw_val = gov.get("dev_value")
    else:
        raw_val = None

    # Evaluate Flagsmith Identity with repo_tier trait for dynamic Segment overrides
    try:
        id_resp = requests.post(
            "https://edge.api.flagsmith.com/api/v1/identities/",
            headers={
                "X-Environment-Key": ENVS["Dev"],
                "Content-Type": "application/json",
            },
            json={
                "identifier": f"flagguard_bot_{repo_tier}",
                "traits": [{"trait_key": "repo_tier", "trait_value": repo_tier}],
            },
            timeout=10,
        )
        if id_resp.ok:
            for item in id_resp.json().get("flags", []):
                if item.get("feature", {}).get("name") == GOVERNANCE_FLAG:
                    policy["enabled"] = bool(item.get("enabled", True))
                    raw_val = item.get("feature_state_value")
                    policy["source"] = f"Identity Trait (repo_tier={repo_tier})"
                    break
    except Exception:
        pass

    if raw_val:
        try:
            remote_cfg = json.loads(raw_val)
            if isinstance(remote_cfg.get("model_priority"), list):
                ordered = remote_cfg["model_priority"] + [
                    m for m in AVAILABLE_MODELS if m not in remote_cfg["model_priority"]
                ]
                policy["model_priority"] = ordered
            policy["min_age_days"] = int(remote_cfg.get("min_age_days", 7))
            policy["allow_auto_remediate"] = bool(remote_cfg.get("allow_auto_remediate", False))
        except json.JSONDecodeError:
            pass

    # Hard safety guardrail: never auto-mutate production tier without explicit CLI override
    if repo_tier == "production":
        policy["allow_auto_remediate"] = False

    return policy


def analyze_anomaly_with_gemini(
    flag_name: str, status: str, details: dict, verbose: bool = False
) -> dict:
    """Uses Gemini with working-model caching and silent fallback retries."""
    global WORKING_MODEL
    prompt = f"""
You are FlagGuard, an autonomous feature-flag reliability and tech-debt agent.
Analyze this flagged anomaly and return ONLY valid JSON with keys:
- "risk_summary": 1-2 concrete sentences on production impact or tech debt.
- "recommended_action": one of ["ARCHIVE_FLAG", "CREATE_FLAG", "SYNC_OR_REVIEW_DRIFT", "REMOVE_DEAD_CODE"]
- "code_patch": If status is DISABLED_EVERYWHERE_IN_CODE, provide a clean unified diff removing the dead flag check while keeping the active fallback logic. Otherwise return "No code change required."
- "replacement_code": If status is DISABLED_EVERYWHERE_IN_CODE, provide the exact cleaned replacement lines for the snippet; otherwise return "".

Flag Name: {flag_name}
Detected Status: {status}
Audit Data: {json.dumps(details, indent=2)}
"""
    models_to_try = [WORKING_MODEL] if WORKING_MODEL else AVAILABLE_MODELS
    for model_name in models_to_try:
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.2,
                    http_options=types.HttpOptions(timeout=15000),
                ),
            )
            WORKING_MODEL = model_name
            return json.loads(response.text)
        except Exception as e:
            WORKING_MODEL = None
            if verbose:
                console.print(
                    f"  [dim yellow][WARN] {model_name} unavailable ({type(e).__name__}), switching...[/]"
                )
            continue

    fallback_actions = {
        "DEAD_ORPHAN": ("ARCHIVE_FLAG", "Flag is inactive and unreferenced in code.", "No code change required.", ""),
        "GHOST_FLAG": ("CREATE_FLAG", "Code references a flag missing in Flagsmith; evaluates to False silently.", "Create flag in Flagsmith with default OFF.", ""),
        "ENV_DRIFT": ("SYNC_OR_REVIEW_DRIFT", "Flag is enabled in Dev but disabled in Prod.", "Verify rollout readiness before enabling in Prod.", ""),
        "DISABLED_EVERYWHERE_IN_CODE": ("REMOVE_DEAD_CODE", "Flag is OFF in all environments but still clutters codebase.", "- if flags.is_feature_enabled(...):\n+ # Removed dead flag branch", ""),
    }
    act, summary, patch, repl = fallback_actions.get(
        status, ("SYNC_OR_REVIEW_DRIFT", "Manual review required.", "No code change required.", "")
    )
    return {
        "risk_summary": f"[Fallback] {summary}",
        "recommended_action": act,
        "code_patch": patch,
        "replacement_code": repl,
    }


def execute_flagsmith_action(flag_name: str, action: str, cloud_info: dict | None):
    """Executes mutating Flagsmith Management API calls after human approval."""
    if action == "ARCHIVE_FLAG" and cloud_info:
        url = f"https://api.flagsmith.com/api/v1/projects/{PROJECT_ID}/features/{cloud_info['id']}/"
        resp = requests.patch(url, headers=FS_HEADERS, json={"is_archived": True}, timeout=10)
        resp.raise_for_status()
        console.print(
            f"  [bold green]✓ EXECUTED:[/] Archived '[cyan]{flag_name}[/]' (ID {cloud_info['id']}) in Flagsmith."
        )

    elif action == "CREATE_FLAG":
        url = f"https://api.flagsmith.com/api/v1/projects/{PROJECT_ID}/features/"
        payload = {
            "name": flag_name,
            "description": "Auto-scaffolded by FlagGuard after detecting unconfigured code reference.",
            "default_enabled": False,
        }
        resp = requests.post(url, headers=FS_HEADERS, json=payload, timeout=10)
        resp.raise_for_status()
        new_id = resp.json().get("id")
        console.print(
            f"  [bold green]✓ EXECUTED:[/] Created missing flag '[cyan]{flag_name}[/]' (ID {new_id}, default OFF) in Flagsmith."
        )


def get_flag_age_days(created_date: str | None) -> int:
    """Calculates how many days ago a flag was created in Flagsmith."""
    if not created_date:
        return 999
    created_dt = datetime.fromisoformat(created_date.replace("Z", "+00:00"))
    return max(0, (datetime.now(timezone.utc) - created_dt).days)


class DeadFlagUnwrapper(ast.NodeTransformer):
    """AST transformer that removes dead flag branches and unwraps else: fallback logic."""

    def __init__(self, target_flag: str):
        self.target_flag = target_flag
        self.modified = False

    def _is_target_flag_call(self, node: ast.AST) -> bool:
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in FLAG_METHODS and node.args:
                arg = node.args[0]
                if isinstance(arg, ast.Constant) and arg.value == self.target_flag:
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


def apply_verified_dead_code_removal(
    flag_name: str, code_refs: list[dict], target_dir: str
) -> bool:
    """Unwraps dead flag if/else blocks via AST and validates with ast.parse() + pytest."""
    patched_any = False
    seen_files: set[Path] = set()

    for ref in code_refs:
        filename, _ = ref["location"].split(":")
        file_path = next(Path(target_dir).rglob(filename), None)
        if not file_path or file_path.suffix != ".py" or file_path in seen_files:
            continue
        seen_files.add(file_path)

        original = file_path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(original, filename=str(file_path))
            unwrapper = DeadFlagUnwrapper(flag_name)
            new_tree = unwrapper.visit(tree)

            if not unwrapper.modified:
                console.print(
                    f"  [yellow]![/] Could not auto-unwrap complex condition in [bold]{file_path.name}[/]; manual review recommended."
                )
                continue

            ast.fix_missing_locations(new_tree)
            new_source = ast.unparse(new_tree) + "\n"
            ast.parse(new_source)  # Gate 1: Syntax verification

            file_path.write_text(new_source, encoding="utf-8")

            # Gate 2: Test suite verification
            tests_ok, test_msg = run_pytest_gate(target_dir)
            if not tests_ok:
                file_path.write_text(original, encoding="utf-8")
                console.print(
                    f"  [bold red]✗ ROLLBACK:[/] Reverted [bold]{file_path.name}[/] because pytest failed:\n{test_msg}"
                )
                continue

            console.print(
                f"  [bold green]✓ VERIFIED & APPLIED:[/] Unwrapped dead '[cyan]{flag_name}[/]' branch in [bold]{file_path.name}[/] ({test_msg})."
            )
            patched_any = True
        except SyntaxError as e:
            file_path.write_text(original, encoding="utf-8")
            console.print(
                f"  [bold red]✗ ROLLBACK:[/] Patch aborted due to AST syntax check failure: {e}"
            )
    return patched_any


def render_summary_table(rows: list[tuple[str, str, str, str, str]]):
    """Renders a polished Rich summary table of all audited flags."""
    table = Table(title="FlagGuard Audit Summary", show_lines=True, header_style="bold cyan")
    table.add_column("Flag Name", style="bold white")
    table.add_column("Dev / Prod", justify="center")
    table.add_column("Age", justify="right")
    table.add_column("Classification", justify="center")
    table.add_column("Action / Status")

    badge_map = {
        "HEALTHY": "[bold green]HEALTHY[/]",
        "NEW_UNRELEASED": "[bold yellow]NEW_UNRELEASED[/]",
        "ENV_DRIFT": "[bold magenta]ENV_DRIFT[/]",
        "DEAD_ORPHAN": "[bold red]DEAD_ORPHAN[/]",
        "GHOST_FLAG": "[bold red]GHOST_FLAG[/]",
        "DISABLED_EVERYWHERE_IN_CODE": "[bold red]DEAD_CODE[/]",
    }

    for name, env_str, age_str, status, note in rows:
        table.add_row(name, env_str, age_str, badge_map.get(status, status), note)

    console.print()
    console.print(table)


def write_markdown_report(
    out_path: str,
    repo: str,
    repo_tier: str,
    rows: list[tuple[str, str, str, str, str]],
    findings: list[dict],
):
    """Exports a GitHub-flavored Markdown report for automated Pull Request comments."""
    lines = [
        "## 🛡️ FlagGuard AI — Feature Flag Audit Report",
        "",
        f"- **Target Directory:** `{repo}`",
        f"- **Evaluated Tier Trait:** `repo_tier={repo_tier}`",
        "",
        "| Flag Name | Dev / Prod | Age | Classification | Recommended Action |",
        "| :--- | :---: | :---: | :---: | :--- |",
    ]
    for name, env_str, age_str, status, note in rows:
        lines.append(f"| `{name}` | {env_str} | {age_str} | **{status}** | {note} |")

    if findings:
        lines.extend(["", "### 🔍 AI Risk Assessments", ""])
        for item in findings:
            lines.append(
                f"- **`{item['name']}` ({item['status']}):** {item['risk_summary']}"
            )

    lines.extend(
        ["", "_Generated automatically by FlagGuard AI (Flagsmith + Gemini + Python AST)._"]
    )
    Path(out_path).write_text("\n".join(lines) + "\n", encoding="utf-8")
    console.print(f"\n[bold green]✓ MARKDOWN REPORT:[/] Exported PR summary to [cyan]{out_path}[/]")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="FlagGuard: Autonomous Feature-Flag Audit & Tech-Debt Agent"
    )
    parser.add_argument("--repo", default="sample-repo", help="Path to target repository directory")
    parser.add_argument(
        "--repo-tier",
        default="staging",
        choices=["staging", "production"],
        help="Repository environment tier sent as a Flagsmith Identity trait",
    )
    parser.add_argument(
        "--auto-approve", action="store_true", help="Auto-execute safe remediations without prompting"
    )
    parser.add_argument(
        "--markdown-out", default=None, help="Optional path to write a Markdown audit report for PR comments"
    )
    parser.add_argument(
        "--verbose", action="store_true", help="Show detailed model fallback and debug logs"
    )
    args = parser.parse_args()

    with console.status("[bold cyan]Scanning repository and evaluating Flagsmith Identity traits..."):
        code_refs = scan_codebase_with_context(args.repo)
        cloud_flags = fetch_flagsmith_state()
        policy = evaluate_governance_identity(cloud_flags, args.repo_tier)

    if not policy["enabled"]:
        console.print(
            Panel(
                "[bold red]KILL-SWITCH ENGAGED:[/] 'flagguard_agent_config' is toggled OFF in Flagsmith.\nAborting audit without modifying any code or flags.",
                title="FlagGuard Governance",
                border_style="red",
            )
        )
        raise SystemExit(0)

    AVAILABLE_MODELS[:] = policy["model_priority"]
    auto_mode = args.auto_approve or policy["allow_auto_remediate"]

    console.print(
        Panel(
            f"[bold]Target Repo:[/] [cyan]{args.repo}[/]   "
            f"[bold]Repo Tier Trait:[/] [blue]{args.repo_tier}[/]\n"
            f"[bold]Policy Source:[/] [green]{policy['source']}[/]   "
            f"[bold]Primary Model:[/] [green]{AVAILABLE_MODELS[0]}[/]\n"
            f"[bold]Age Guard:[/] [yellow]{policy['min_age_days']} days[/]   "
            f"[bold]Auto-Remediate:[/] [magenta]{auto_mode}[/]",
            title="[bold cyan]FlagGuard AI — Autonomous Feature-Flag Governance[/]",
            border_style="cyan",
        )
    )

    all_names = sorted(set(code_refs.keys()) | set(cloud_flags.keys()))
    summary_rows: list[tuple[str, str, str, str, str]] = []
    markdown_findings: list[dict] = []

    for name in all_names:
        if name == GOVERNANCE_FLAG:
            continue
        in_code = code_refs.get(name)
        in_cloud = cloud_flags.get(name)

        env_str = (
            f"{'ON' if in_cloud['dev'] else 'OFF'} / {'ON' if in_cloud['prod'] else 'OFF'}"
            if in_cloud
            else "N/A"
        )
        age_days = get_flag_age_days(in_cloud.get("created_date")) if in_cloud else None
        age_str = f"{age_days}d" if age_days is not None else "—"

        if in_code and not in_cloud:
            status = "GHOST_FLAG"
        elif in_cloud and not in_code:
            status = "DEAD_ORPHAN"
        elif not in_cloud["dev"] and not in_cloud["prod"]:
            if age_days < policy["min_age_days"]:
                summary_rows.append(
                    (
                        name,
                        env_str,
                        age_str,
                        "NEW_UNRELEASED",
                        f"Protected (< {policy['min_age_days']}d threshold)",
                    )
                )
                continue
            status = "DISABLED_EVERYWHERE_IN_CODE"
        elif in_cloud["dev"] != in_cloud["prod"]:
            status = "ENV_DRIFT"
        else:
            summary_rows.append((name, env_str, age_str, "HEALTHY", "Active & verified in code"))
            continue

        with console.status(f"[bold yellow]Analyzing '{name}' ({status}) with Gemini..."):
            analysis = analyze_anomaly_with_gemini(
                name, status, {"cloud": in_cloud, "code_references": in_code}, verbose=args.verbose
            )

        action = analysis["recommended_action"]
        summary_rows.append((name, env_str, age_str, status, action))
        markdown_findings.append(
            {"name": name, "status": status, "risk_summary": analysis["risk_summary"]}
        )

        console.print(
            Panel(
                f"[bold]Risk Assessment:[/] {analysis['risk_summary']}\n"
                f"[bold]Recommended Action:[/] [bold yellow]{action}[/]",
                title=f"[bold red][{status}][/] [bold white]{name}[/]",
                border_style="yellow",
            )
        )

        if analysis.get("code_patch") and analysis["code_patch"] != "No code change required.":
            console.print(Syntax(analysis["code_patch"], "diff", theme="monokai", line_numbers=False))

        if action in {"ARCHIVE_FLAG", "CREATE_FLAG"}:
            choice = (
                "y"
                if auto_mode
                else "n"
                if os.getenv("CI") == "true"
                else console.input(
                    rf"  [bold cyan]>> Execute '{action}' on Flagsmith for '{name}'? \[y/N]: [/]"
                )
                .strip()
                .lower()
            )
            if choice == "y":
                execute_flagsmith_action(name, action, in_cloud)
            else:
                console.print("  [dim][SKIPPED] No changes made to Flagsmith.[/]")

        elif action == "REMOVE_DEAD_CODE" and analysis.get("code_patch") != "No code change required.":
            choice = (
                "y"
                if auto_mode
                else "n"
                if os.getenv("CI") == "true"
                else console.input(
                    rf"  [bold cyan]>> Apply AST-verified if/else unwrapping & export patch for '{name}'? \[y/N]: [/]"
                )
                .strip()
                .lower()
            )
                
            if choice == "y":
                patch_file = Path(f"{name}_cleanup.patch")
                patch_file.write_text(analysis["code_patch"], encoding="utf-8")
                console.print(f"  [dim][SAVED] Unified diff exported to {patch_file.name}[/]")
                apply_verified_dead_code_removal(name, in_code or [], args.repo)

    render_summary_table(summary_rows)

    if args.markdown_out:
        write_markdown_report(
            args.markdown_out, args.repo, args.repo_tier, summary_rows, markdown_findings
        )