import json

from google import genai
from google.genai import types

from flagguard.config import GEMINI_API_KEY, console

client = genai.Client(api_key=GEMINI_API_KEY)


def get_gemini_models() -> list[str]:
    """Discovers available Gemini Flash models for this API key, prioritizing gemini-3.8-flash."""
    preferred = ["gemini-3.8-flash"]
    try:
        for model in client.models.list():
            name = model.name.replace("models/", "")
            if "flash" in name and "tts" not in name and "image" not in name:
                if name not in preferred:
                    preferred.append(name)
    except Exception:
        pass
    return preferred


AVAILABLE_MODELS = get_gemini_models()
WORKING_MODEL: str | None = None


def fallback_classifier(status: str) -> dict:
    """Deterministic classification when Gemini is unavailable."""
    fallback_actions = {
        "DEAD_ORPHAN": (
            "ARCHIVE_FLAG",
            "Flag is inactive and unreferenced in code.",
            "No code change required.",
            "",
        ),
        "GHOST_FLAG": (
            "CREATE_FLAG",
            "Code references a flag missing in Flagsmith; evaluates to False silently.",
            "Create flag in Flagsmith with default OFF.",
            "",
        ),
        "ENV_DRIFT": (
            "SYNC_OR_REVIEW_DRIFT",
            "Flag is enabled in Dev but disabled in Prod.",
            "Verify rollout readiness before enabling in Prod.",
            "",
        ),
        "DISABLED_EVERYWHERE_IN_CODE": (
            "REMOVE_DEAD_CODE",
            "Flag is OFF in all environments but still clutters codebase.",
            "- if flags.is_feature_enabled(...):\n+ # Removed dead flag branch",
            "",
        ),
    }
    action, summary, patch, replacement = fallback_actions.get(
        status,
        ("SYNC_OR_REVIEW_DRIFT", "Manual review required.", "No code change required.", ""),
    )
    return {
        "risk_summary": f"[Fallback] {summary}",
        "recommended_action": action,
        "code_patch": patch,
        "replacement_code": replacement,
    }


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
    models_to_try = [WORKING_MODEL] if WORKING_MODEL else list(AVAILABLE_MODELS)
    for model_name in models_to_try:
        if not model_name:
            continue
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
        except Exception as exc:
            WORKING_MODEL = None
            if verbose:
                console.print(
                    f"  [dim yellow][WARN] {model_name} unavailable ({type(exc).__name__}), switching...[/]"
                )
            continue

    return fallback_classifier(status)
