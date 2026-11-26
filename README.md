# FlagGuard AI

**Autonomous Feature Flag Lifecycle Agent & Dead-Code Unwrapper**  
Built for **Toggle Wars Hackathon — Track 02 (AI Agents & Feature Flags)**

FlagGuard AI audits repositories against live **Flagsmith** environments, classifies flag hygiene with **Google Gemini**, unwraps dead `if/else` branches at the **Python AST** level, verifies safety with an automated **`pytest` rollback gate**, and posts actionable audits directly on **GitHub Pull Requests**.

---

## Why FlagGuard AI?

Stale feature flags create silent technical debt, configuration drift, and unreachable code branches. Standard linters only delete lines—breaking indentation or removing active `else` fallbacks. FlagGuard AI solves this end-to-end:

1. **Flagsmith SDK + Identity/Segment Targeting:** Evaluates flags per repository tier (`--repo-tier prod` vs `--repo-tier staging`) using Flagsmith Identities and the `staging_repos` Segment so staging-only flags aren't falsely flagged as dead in production.
2. **5-State Lifecycle Classification (Gemini):** Cross-references live Flagsmith states against codebase references to classify every flag into:
   - `ACTIVE` — Enabled in Flagsmith and actively referenced.
   - `DEAD` — Disabled/archived in Flagsmith (or 100% rolled out) with stale branches still in code.
   - `DRIFT` — Flag state diverges across environments/segments.
   - `GHOST` — Referenced in code (`flagsmith.has_feature(...)`) but missing from Flagsmith.
   - `UNUSED` — Defined in Flagsmith with zero references in the codebase.
3. **AST `if/else` Dead-Code Unwrapper + `pytest` Gate:** Uses Python's `ast.NodeTransformer` (`DeadFlagUnwrapper`) to collapse dead conditionals while preserving the surviving branch, validates syntax via `ast.parse()`, runs `pytest`, and **automatically rolls back** any edit if tests fail.
4. **Automated GitHub PR Bot:** Runs on every Pull Request via `.github/workflows/flagguard.yml`, posting a Markdown audit table directly into the PR conversation.

---

## Architecture

```text
┌──────────────────┐     ┌─────────────────────────┐     ┌──────────────────────┐
│  Target Repo     │────▶│  Flagsmith Live SDK     │────▶│  Gemini AI Auditor   │
│  (AST Scanner)   │     │  (--repo-tier Identity) │     │  (Lifecycle Triage)  │
└──────────────────┘     └─────────────────────────┘     └──────────┬───────────┘
                                                                    │
┌──────────────────┐     ┌─────────────────────────┐                ▼
│  GitHub PR Bot   │◀────│  pytest Safety Gate     │◀────┌──────────────────────┐
│  (PR Comment)    │     │  (Auto-Rollback on Fail)│     │  DeadFlagUnwrapper   │
└──────────────────┘     └─────────────────────────┘     │  (AST Transformer)   │
                                                         └──────────────────────┘

```

---

## Quickstart

### 1. Install Dependencies
```bash
git clone [https://github.com/lallucoder65/flagguard-ai.git](https://github.com/lallucoder65/flagguard-ai.git)
cd flagguard-ai
pip install -r requirements.txt
```

### 2. Configure Environment Variables
```bash
export FLAGSMITH_ENV_KEY="your_flagsmith_environment_key"
export GEMINI_API_KEY="your_gemini_api_key"
```

*(On Windows PowerShell, use `$env:FLAGSMITH_ENV_KEY="..."` and `$env:GEMINI_API_KEY="..."`.)*

### 3. Run an Audit
```bash
# Audit sample-repo against production tier
python agent_audit.py --repo ./sample-repo --repo-tier production

# Audit against staging_repos Flagsmith Segment
python agent_audit.py --repo ./sample-repo --repo-tier staging

# Export Markdown report for CI/CD or PR reviews
python agent_audit.py --repo ./sample-repo --repo-tier production --markdown-out audit_report.md
```

---

## GitHub Actions PR Bot Setup

Add these two repository secrets under **Settings → Secrets and variables → Actions**:
- `FLAGSMITH_ENV_KEY` — Your Flagsmith Server-Side Environment Key
- `GEMINI_API_KEY` — Your Google Gemini API Key

Every Pull Request targeting `main` automatically executes `.github/workflows/flagguard.yml`, audits changed code against Flagsmith, and posts/updates the lifecycle report comment on the PR.                                                         