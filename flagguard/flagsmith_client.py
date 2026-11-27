from datetime import datetime, timezone
import json

import requests

from flagguard.ai_auditor import AVAILABLE_MODELS
from flagguard.config import ENVS, FS_HEADERS, PROJECT_ID, console

GOVERNANCE_FLAG = "flagguard_agent_config"


def _fetch_all_pages(initial_url: str) -> list[dict]:
    """Follows Flagsmith API 'next' pagination links so large projects never truncate."""
    results = []
    url = initial_url
    while url:
        resp = requests.get(url, headers=FS_HEADERS, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        results.extend(data.get("results", []))
        url = data.get("next")
    return results


def fetch_flagsmith_state() -> dict:
    """Fetches all non-archived flags and their Dev/Prod states from Flagsmith."""
    try:
        features = _fetch_all_pages(
            f"https://api.flagsmith.com/api/v1/projects/{PROJECT_ID}/features/"
        )

        env_states = {name: {} for name in ENVS}
        env_values = {name: {} for name in ENVS}
        for env_name, env_key in ENVS.items():
            states = _fetch_all_pages(
                f"https://api.flagsmith.com/api/v1/environments/{env_key}/featurestates/"
            )
            for state in states:
                env_states[env_name][state["feature"]] = state["enabled"]
                env_values[env_name][state["feature"]] = state.get("feature_state_value")
    except requests.exceptions.RequestException as exc:
        console.print(
            f"[yellow]Warning: Flagsmith API unreachable ({exc}); continuing with empty cloud state.[/]"
        )
        return {}

    cloud = {}
    for feature in features:
        if feature.get("is_archived"):
            continue
        feature_id = feature["id"]
        cloud[feature["name"]] = {
            "id": feature_id,
            "created_date": feature.get("created_date"),
            "dev": env_states["Dev"].get(feature_id, False),
            "prod": env_states["Prod"].get(feature_id, False),
            "dev_value": env_values["Dev"].get(feature_id),
        }
    return cloud


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
            timeout=30,
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
                    model
                    for model in AVAILABLE_MODELS
                    if model not in remote_cfg["model_priority"]
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


def get_flag_age_days(created_date: str | None) -> int:
    """Calculates how many days ago a flag was created in Flagsmith."""
    if not created_date:
        return 999
    created_dt = datetime.fromisoformat(created_date.replace("Z", "+00:00"))
    return max(0, (datetime.now(timezone.utc) - created_dt).days)


def archive_flag(flag_name: str, cloud_info: dict) -> None:
    """PATCH the Flagsmith feature so a confirmed dead flag is archived."""
    url = (
        f"https://api.flagsmith.com/api/v1/projects/{PROJECT_ID}/features/{cloud_info['id']}/"
    )
    resp = requests.patch(url, headers=FS_HEADERS, json={"is_archived": True}, timeout=30)
    resp.raise_for_status()
    console.print(
        f"  [bold green]✓ EXECUTED:[/] Archived '[cyan]{flag_name}[/]' "
        f"(ID {cloud_info['id']}) in Flagsmith."
    )


def create_flag(flag_name: str) -> None:
    """POST a missing Flagsmith feature scaffolded from a ghost code reference."""
    url = f"https://api.flagsmith.com/api/v1/projects/{PROJECT_ID}/features/"
    payload = {
        "name": flag_name,
        "description": "Auto-scaffolded by FlagGuard after detecting unconfigured code reference.",
        "default_enabled": False,
    }
    resp = requests.post(url, headers=FS_HEADERS, json=payload, timeout=30)
    resp.raise_for_status()
    new_id = resp.json().get("id")
    console.print(
        f"  [bold green]✓ EXECUTED:[/] Created missing flag '[cyan]{flag_name}[/]' "
        f"(ID {new_id}, default OFF) in Flagsmith."
    )


def execute_flagsmith_action(flag_name: str, action: str, cloud_info: dict | None) -> None:
    """Executes mutating Flagsmith Management API calls after human approval."""
    if action == "ARCHIVE_FLAG" and cloud_info:
        archive_flag(flag_name, cloud_info)
    elif action == "CREATE_FLAG":
        create_flag(flag_name)
