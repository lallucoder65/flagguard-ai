import argparse
import os
from pathlib import Path

from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table

from flagguard.ai_auditor import AVAILABLE_MODELS, analyze_anomaly_with_gemini
from flagguard.ast_unwrapper import unwrap_flag_references
from flagguard.config import console
from flagguard.flagsmith_client import (
    GOVERNANCE_FLAG,
    evaluate_governance_identity,
    execute_flagsmith_action,
    fetch_flagsmith_state,
    get_flag_age_days,
)
from flagguard.scanner import scan_codebase_with_context


def render_summary_table(rows: list[tuple[str, str, str, str, str]]) -> None:
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
) -> None:
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


def _confirm(auto_mode: bool, prompt: str) -> str:
    if auto_mode:
        return "y"
    if os.getenv("CI") == "true":
        return "n"
    return console.input(prompt).strip().lower()


def _export_patch(flag_name: str, code_patch: str | None) -> None:
    if not code_patch or code_patch == "No code change required.":
        return
    patch_file = Path(f"{flag_name}_cleanup.patch")
    patch_file.write_text(code_patch, encoding="utf-8")
    console.print(f"  [dim][SAVED] Unified diff exported to {patch_file.name}[/]")


def _apply_dead_code_removal(flag_name: str, code_refs: list[dict] | None, repo: str, code_patch: str | None) -> None:
    _export_patch(flag_name, code_patch)
    unwrap_flag_references(flag_name, code_refs or [], repo)


def build_parser() -> argparse.ArgumentParser:
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
        "--auto-approve",
        action="store_true",
        help="Auto-execute safe remediations without prompting",
    )
    parser.add_argument(
        "--auto-unwrap",
        action="store_true",
        help="Non-interactively unwrap eligible dead flags and verify with pytest",
    )
    parser.add_argument(
        "--markdown-out",
        default=None,
        help="Optional path to write a Markdown audit report for PR comments",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show detailed model fallback and debug logs",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()

    with console.status("[bold cyan]Scanning repository and evaluating Flagsmith Identity traits..."):
        cloud_flags = fetch_flagsmith_state()
        code_refs = scan_codebase_with_context(args.repo, set(cloud_flags))
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
                name,
                status,
                {"cloud": in_cloud, "code_references": in_code},
                verbose=args.verbose,
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
            choice = _confirm(
                auto_mode,
                rf"  [bold cyan]>> Execute '{action}' on Flagsmith for '{name}'? \[y/N]: [/]",
            )
            if choice == "y":
                execute_flagsmith_action(name, action, in_cloud)
            else:
                console.print("  [dim][SKIPPED] No changes made to Flagsmith.[/]")

        elif args.auto_unwrap and status == "DISABLED_EVERYWHERE_IN_CODE":
            console.print(
                f"  [dim]--auto-unwrap: unwrapping '[cyan]{name}[/]' and verifying with pytest.[/]"
            )
            _apply_dead_code_removal(name, in_code, args.repo, analysis.get("code_patch"))

        elif action == "REMOVE_DEAD_CODE" and analysis.get("code_patch") != "No code change required.":
            choice = _confirm(
                auto_mode,
                rf"  [bold cyan]>> Apply AST-verified if/else unwrapping & export patch for '{name}'? \[y/N]: [/]",
            )
            if choice == "y":
                _apply_dead_code_removal(name, in_code, args.repo, analysis.get("code_patch"))

    render_summary_table(summary_rows)

    if args.markdown_out:
        write_markdown_report(
            args.markdown_out, args.repo, args.repo_tier, summary_rows, markdown_findings
        )
