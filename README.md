# FlagGuard: Autonomous Feature-Flag Audit & Tech-Debt Agent

Built for **Toggle Wars Hackathon — Track 02 (AI Agents and Feature Flags)**.

FlagGuard bridges static source code analysis with Flagsmith's Management API and Google Gemini to detect, explain, and safely remediate feature-flag technical debt—while using **Flagsmith Remote Config itself** to govern the AI agent's model routing, age thresholds, and kill-switch permissions in real time.

---

## Architecture

```text
+-------------------+       +-------------------------------+
|  Target Codebase  |       |       Flagsmith Cloud         |
| (.py AST + .tsx/  |       | - Dev & Prod Feature States   |
|  .js/.ts Regex)   |       | - Created Dates (Age Guard)   |
+---------+---------+       | - flagguard_agent_config (RC) |
          |                 +---------------+---------------+
          +----------------+----------------+
                           v
           +-------------------------------+
           |      FlagGuard Core Engine    |
           |  1. Cross-State Classification|
           |  2. Gemini Risk & Diff Synth  |
           |  3. AST-Verified Remediation  |
           +-------------------------------+
```

---

## Detected Flag States & Remediations

| Classification | Condition | Agent Action |
| :--- | :--- | :--- |
| **`HEALTHY`** | Enabled in Dev & Prod and referenced in code | Verified safe; no action taken |
| **`NEW_UNRELEASED`** | Disabled everywhere, created `< min_age_days` ago | Protected from premature deletion |
| **`DEAD_ORPHAN`** | Exists in Flagsmith, zero references in codebase | Archives flag via Flagsmith `PATCH` API |
| **`GHOST_FLAG`** | Referenced in code, missing from Flagsmith | Auto-scaffolds flag (default `OFF`) via `POST` API |
| **`ENV_DRIFT`** | Enabled in Dev but disabled in Prod (or vice versa) | Flags rollout mismatch with risk summary |
| **`DISABLED_EVERYWHERE_IN_CODE`** | Disabled everywhere (`>= min_age_days`) + still in code | Generates unified diff, strips dead `if` block, and verifies syntax via `ast.parse()` |

---

## Live Agent Governance via Flagsmith

FlagGuard dogfoods Flagsmith through a **`flagguard_agent_config`** feature flag:
- **Toggle `OFF`**: Acts as an instant emergency kill-switch, halting all agent mutations.
- **Remote Config JSON**: Dynamically controls `model_priority`, `min_age_days` (false-positive age guard), and `allow_auto_remediate` without redeploying code.

---

## Quickstart

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Configure environment variables
cp .env.example .env
# Add your FLAGSMITH_API_KEY, FLAGSMITH_PROJECT_ID, and GEMINI_API_KEY

# 3. Run interactive audit
python agent_audit.py --repo sample-repo

# 4. Run non-interactive CI mode
python agent_audit.py --repo sample-repo --auto-approve
```
