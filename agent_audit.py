import ast
import argparse
import json
import os
from pathlib import Path
import re
import requests
from dotenv import load_dotenv
from google import genai
from google.genai import errors, types
import ast
from datetime import datetime, timezone




load_dotenv()

FLAGSMITH_KEY = os.getenv("FLAGSMITH_API_KEY")
PROJECT_ID = os.getenv("FLAGSMITH_PROJECT_ID", "54836")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")

FS_HEADERS = {
    "Authorization": f"Api-Key {FLAGSMITH_KEY}",
    "Content-Type": "application/json",
}
ENVS = {
    "Dev": "BrTukFPrbc6d5jGFEy2K8w",
    "Prod": "AFikpqKycTH6CA6n5ZVYce",
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


def load_agent_governance(cloud_flags: dict) -> dict:
    """Reads live agent behavior rules from Flagsmith's own Remote Config."""
    policy = {
        "enabled": True,
        "model_priority": AVAILABLE_MODELS,
        "min_age_days": 7,
        "allow_auto_remediate": False,
    }
    gov = cloud_flags.get(GOVERNANCE_FLAG)
    if not gov:
        return policy

    policy["enabled"] = gov["dev"]
    raw_val = gov.get("dev_value")
    if raw_val:
        try:
            remote_cfg = json.loads(raw_val)
            if isinstance(remote_cfg.get("model_priority"), list):
                # Keep remote priority first, followed by auto-discovered fallbacks
                ordered = remote_cfg["model_priority"] + [
                    m for m in AVAILABLE_MODELS if m not in remote_cfg["model_priority"]
                ]
                policy["model_priority"] = ordered
            policy["min_age_days"] = int(remote_cfg.get("min_age_days", 7))
            policy["allow_auto_remediate"] = bool(remote_cfg.get("allow_auto_remediate", False))
        except json.JSONDecodeError:
            pass
    return policy


def analyze_anomaly_with_gemini(flag_name: str, status: str, details: dict) -> dict:
    """Uses Gemini with working-model caching and network-drop resilience."""
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
            print(f"  [WARN] {model_name} unavailable ({type(e).__name__}), switching...")
            continue

    fallback_actions = {
        "DEAD_ORPHAN": ("ARCHIVE_FLAG", "Flag is inactive and unreferenced in code.", "No code change required.", ""),
        "GHOST_FLAG": ("CREATE_FLAG", "Code references a flag missing in Flagsmith; evaluates to False silently.", "Create flag in Flagsmith with default OFF.", ""),
        "ENV_DRIFT": ("SYNC_OR_REVIEW_DRIFT", "Flag is enabled in Dev but disabled in Prod.", "Verify rollout readiness before enabling in Prod.", ""),
        "DISABLED_EVERYWHERE_IN_CODE": ("REMOVE_DEAD_CODE", "Flag is OFF in all environments but still clutters codebase.", "- if flags.is_feature_enabled(...):\n+ # Removed dead flag branch", ""),
    }
    act, summary, patch, repl = fallback_actions.get(status, ("SYNC_OR_REVIEW_DRIFT", "Manual review required.", "No code change required.", ""))
    return {"risk_summary": f"[Fallback] {summary}", "recommended_action": act, "code_patch": patch, "replacement_code": repl}
def execute_flagsmith_action(flag_name: str, action: str, cloud_info: dict | None):
    """Executes mutating Flagsmith Management API calls after human approval."""
    if action == "ARCHIVE_FLAG" and cloud_info:
        url = f"https://api.flagsmith.com/api/v1/projects/{PROJECT_ID}/features/{cloud_info['id']}/"
        resp = requests.patch(url, headers=FS_HEADERS, json={"is_archived": True}, timeout=10)
        resp.raise_for_status()
        print(f"  [EXECUTED] Archived '{flag_name}' (ID {cloud_info['id']}) in Flagsmith.")

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
        print(f"  [EXECUTED] Created missing flag '{flag_name}' (ID {new_id}, default OFF) in Flagsmith.")

def get_flag_age_days(created_date: str | None) -> int:
    """Calculates how many days ago a flag was created in Flagsmith."""
    if not created_date:
        return 999
    created_dt = datetime.fromisoformat(created_date.replace("Z", "+00:00"))
    return max(0, (datetime.now(timezone.utc) - created_dt).days)

def apply_verified_dead_code_removal(flag_name: str, code_refs: list[dict], target_dir: str) -> bool:
    """Removes dead flag if-block from Python source and verifies AST syntax validity."""
    patched_any = False
    for ref in code_refs:
        filename, line_str = ref["location"].split(":")
        file_path = next(Path(target_dir).rglob(filename), None)
        if not file_path or file_path.suffix != ".py":
            continue

        original = file_path.read_text(encoding="utf-8")
        lines = original.splitlines()
        target_idx = int(line_str) - 1

        if 0 <= target_idx < len(lines) and flag_name in lines[target_idx] and lines[target_idx].lstrip().startswith("if "):
            base_indent = len(lines[target_idx]) - len(lines[target_idx].lstrip())
            end_idx = target_idx + 1
            while end_idx < len(lines):
                stripped = lines[end_idx].strip()
                if stripped == "":
                    end_idx += 1
                    continue
                curr_indent = len(lines[end_idx]) - len(lines[end_idx].lstrip())
                if curr_indent > base_indent:
                    end_idx += 1
                else:
                    break

            new_source = "\n".join(lines[:target_idx] + lines[end_idx:]) + "\n"
            try:
                ast.parse(new_source)  # Self-verification gate
                file_path.write_text(new_source, encoding="utf-8")
                print(f"  [VERIFIED & APPLIED] Removed dead '{flag_name}' block from {file_path.name} (AST syntax check passed).")
                patched_any = True
            except SyntaxError as e:
                print(f"  [ROLLBACK] Patch aborted due to syntax check failure: {e}")
    return patched_any    

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="FlagGuard: Autonomous Feature-Flag Audit & Tech-Debt Agent")
    parser.add_argument("--repo", default="sample-repo", help="Path to target repository directory")
    parser.add_argument("--auto-approve", action="store_true", help="Auto-execute safe remediations without prompting")
    args = parser.parse_args()

    code_refs = scan_codebase_with_context(args.repo)
    cloud_flags = fetch_flagsmith_state()
    policy = load_agent_governance(cloud_flags)
    if not policy["enabled"]:
        print("[KILL-SWITCH] 'flagguard_agent_config' is OFF in Flagsmith. Aborting audit.")
        raise SystemExit(0)

    AVAILABLE_MODELS[:] = policy["model_priority"]
    auto_mode = args.auto_approve or policy["allow_auto_remediate"]
    print(f"[GOVERNANCE] Flagsmith policy loaded -> primary_model={AVAILABLE_MODELS[0]}, min_age_days={policy['min_age_days']}, auto_remediate={auto_mode}\n")
    all_names = sorted(set(code_refs.keys()) | set(cloud_flags.keys()))

    print(f"=== FlagGuard AI Audit & Remediation Agent (Target: {args.repo}) ===\n")

    for name in all_names:
        if name == GOVERNANCE_FLAG:
            continue
        in_code = code_refs.get(name)
        in_cloud = cloud_flags.get(name)

        if in_code and not in_cloud:
            status = "GHOST_FLAG"
        elif in_cloud and not in_code:
            status = "DEAD_ORPHAN"
        elif not in_cloud["dev"] and not in_cloud["prod"]:
            age_days = get_flag_age_days(in_cloud.get("created_date"))
            if age_days < policy["min_age_days"]:
                print(f"[NEW_UNRELEASED] {name} -> Created {age_days}d ago (< {policy['min_age_days']}d threshold). Safe to keep.\n")
                continue
            status = "DISABLED_EVERYWHERE_IN_CODE"
        elif in_cloud["dev"] != in_cloud["prod"]:
            status = "ENV_DRIFT"
        else:
            print(f"[HEALTHY] {name} -> Active in Dev & Prod, verified in code.\n")
            continue

        print(f"[{status}] {name}")
        analysis = analyze_anomaly_with_gemini(
            name, status, {"cloud": in_cloud, "code_references": in_code}
        )
        print(f"  AI Risk Assessment : {analysis['risk_summary']}")
        print(f"  Suggested Action   : {analysis['recommended_action']}")
        print(f"  Suggested Patch    :\n{analysis['code_patch']}\n")

        action = analysis["recommended_action"]
        if action in {"ARCHIVE_FLAG", "CREATE_FLAG"}:
            choice = "y" if auto_mode else input(f"  >> Execute '{action}' on Flagsmith for '{name}'? [y/N]: ").strip().lower()
            if choice == "y":
                execute_flagsmith_action(name, action, in_cloud)
            else:
                print("  [SKIPPED] No changes made to Flagsmith.")
        elif action == "REMOVE_DEAD_CODE" and analysis.get("code_patch") != "No code change required.":
            choice = "y" if auto_mode else input(f"  >> Apply AST-verified dead-code removal & export patch for '{name}'? [y/N]: ").strip().lower()
            if choice == "y":
                patch_file = Path(f"{name}_cleanup.patch")
                patch_file.write_text(analysis["code_patch"], encoding="utf-8")
                print(f"  [SAVED] Unified diff exported to {patch_file.name}")
                apply_verified_dead_code_removal(name, in_code or [], args.repo)