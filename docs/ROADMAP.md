# CUA Workbench — Product Improvement Roadmap

> Planning document. No code changes. Grounded entirely in the current codebase as reviewed April 2026.

---

## 1. Objective

### What we are improving

Make CUA Workbench feel professional, trustworthy, and approachable for users who are not infrastructure engineers. Reduce the time-to-first-successful-session, eliminate confusion in engine/model selection, and remove jargon from every surface the user touches.

### Target users

| User type | What they need |
|-----------|---------------|
| **Technical evaluator** | Wants to compare engines/models quickly. Needs clear status, fast setup, obvious controls. |
| **Non-technical stakeholder** | Wants to see the agent work. Needs plain language, safe defaults, and no Docker troubleshooting. |
| **First-time user** | Has never run the app before. Needs guidance from install through first task. |

### What "business-friendly" means for this app

- Every screen can be understood without reading source code.
- Errors always tell the user what to do next.
- Setup succeeds on the first attempt for >95% of users.
- A new user can run their first agent task in under 15 minutes after install.
- No internal enum values, raw error codes, or implementation details leak into the UI.

---

## 2. Current-State Summary

### The app today

CUA Workbench is a three-tier system (React frontend → FastAPI backend → Dockerized Ubuntu desktop) that lets users run AI agents against a visible sandbox. It supports three automation engines (Playwright MCP, Omni Accessibility, Computer Use), two AI providers (Google Gemini, Anthropic Claude), and exposes a real-time log/screenshot/VNC view. The core agent loop, engine dispatch, and Docker lifecycle all function.

### Biggest UX / business-readiness gaps

| Gap | Evidence |
|-----|----------|
| **No onboarding guidance** | No tooltips on engine or model selects. No sample tasks. No "getting started" flow. Users must invent their first task from scratch. |
| **Jargon-heavy labels** | `playwright_mcp`, `omni_accessibility`, `computer_use`, `execution_target` are exposed directly. Key-source toggle uses emoji + abbreviations (`📄 .env`, `💻 System`). |
| **Error messages are technical** | AT-SPI failure says "Ensure gir1.2-atspi-2.0, python3-gi, and at-spi2-core are installed." Rate-limit says "max 10 starts per minute" with no recovery hint. |
| **No pre-flight validation** | API key is only tested when the model call happens, not on submission. Engine health is checked but failures are non-blocking — session starts and fails later. |
| **Inconsistent API error shapes** | `/api/agent/start` returns `{"error": "..."}`, container endpoints return `{"success": bool}`, steps use `StructuredError`. No standard envelope. |
| **Three-process manual startup** | User must run `docker compose up`, `python -m backend.main`, and `npm run dev` in three terminals. No single command. |
| **Docker build is slow and opaque** | First build can take 30+ minutes. No progress indicator, no ETA, no disk-space check. |
| **Accessibility gaps** | Missing aria-labels on status dots, icon-only buttons, toggle groups. Color-only indicators for status badges. Secondary text may fail WCAG AA contrast. |

### Biggest first-time-user problems

1. Does not know which engine to pick or what the names mean.
2. Pastes an API key but finds out it is invalid only after the session starts and the model call fails.
3. Sees "No models available" because the backend is not running — no message says "start the backend."
4. Docker build fails silently or takes 30+ minutes with no explanation.
5. Stares at a blank ScreenView for 5–10 seconds while the agent service becomes healthy, with no loading indicator.

---

## 3. Guiding Principles

1. **Do not break working features.** Every change must be tested against the existing agent loop, Docker lifecycle, and WebSocket flow.
2. **Simplify before adding.** Remove confusion first. Do not add new features to compensate for unclear existing ones.
3. **Remove jargon.** If a label requires reading source code to understand, rewrite it.
4. **Use better defaults.** The default state of every form should produce a working session with the fewest decisions possible.
5. **Progressive disclosure.** Show engine ID, execution target, and advanced config only when the user opts in. Lead with friendly names.
6. **Make user intent clear.** Every button should say what it will do. Every error should say what to do next.
7. **Improve trust and clarity first.** Accurate status indicators, honest error messages, and visible health checks build more trust than new features.
8. **Prefer reversible changes.** Relabel before restructuring. Add tooltips before redesigning layouts.

---

## 4. Prioritized Roadmap

### Phase 1 — Must-Do Now

**Goal:** Eliminate the most common confusion points and first-time-user failures without changing architecture.

**Problems being solved:**
- Users don't understand engine names or which to pick.
- API key errors surface too late.
- Backend-down state is indistinguishable from "no models."
- Error messages are developer-facing.

**Specific changes:**

| # | Change | Detail |
|---|--------|--------|
| 1.1 | **Rewrite engine display names** | Replace raw IDs with plain names everywhere in the UI. `playwright_mcp` → "Browser (Semantic)" / `omni_accessibility` → "Desktop (Accessibility)" / `computer_use` → "Computer Use (Native)". Keep IDs in API payloads only. |
| 1.2 | **Add engine selection help text** | Below the engine dropdown, show a one-line description: "Best for web tasks — clicks by element name, not coordinates" / "Best for desktop apps — uses system accessibility tree" / "Best for full browser+desktop — uses native model protocol." |
| 1.3 | **Add tooltips to all form fields** | Provider, model, key source, engine, execution target, max steps. Use `title` attributes as a minimum; upgrade to popover tooltips later. |
| 1.4 | **Rewrite key-source toggle labels** | `✏️ Manual` → "Enter key" / `📄 .env` → "From .env file" / `💻 System` → "Environment variable". Drop emoji. |
| 1.5 | **Add "backend unreachable" state** | When `/api/models` fetch fails, show "Backend not running — start it with `python -m backend.main`" instead of "No models available." |
| 1.6 | **Rewrite top-5 error messages** | (a) API key required → "Enter your {provider} API key, or set {env_var} in your .env file." (b) Task required → "Describe what the agent should do." (c) Rate limit → "Too many sessions — wait a minute and try again." (d) AT-SPI failure → "Accessibility engine requires the Docker container. Switch to Browser engine or start the container." (e) Step timeout → "Step {n} took too long ({timeout}s). The automation may have stalled — check the screen view." |
| 1.7 | **Add loading state to ScreenView** | Show a spinner and "Waiting for agent service…" while container is running but agent service health check has not yet passed. |
| 1.8 | **Add sample tasks** | Add 3–4 example task strings as clickable chips below the task textarea: "Search Google for 'latest AI news'", "Open the file manager and list files in /tmp", "Take a screenshot of the desktop." Clicking fills the textarea. |
| 1.9 | **Show model-engine compatibility** | When user selects a model, grey out or mark engines the model does not support (read from `allowed_models.json` capability flags already present). |

**Expected user impact:** First-time users can pick the right engine, get actionable errors, and run a sample task without guessing.

**Business impact:** Dramatically reduces support burden and failed first impressions. Makes demos possible without pre-briefing.

**Risk level:** Low. All changes are UI copy, tooltips, and conditional display logic. No backend logic changes.

**Dependencies:** None.

---

### Phase 2 — Should-Do Next

**Goal:** Improve reliability signals, validate configuration before wasting user time, and standardize backend responses.

**Problems being solved:**
- Invalid API keys only discovered mid-session.
- No unified health view.
- Inconsistent error response formats.
- Three-process startup friction.
- Configuration split across multiple files.

**Specific changes:**

| # | Change | Detail |
|---|--------|--------|
| 2.1 | **Add API key pre-validation** | New endpoint `POST /api/keys/validate` that makes a minimal API call to the selected provider. Frontend calls this when the user changes key source or pastes a key. Show green check or red X inline. |
| 2.2 | **Add pre-flight check before session start** | Before calling `/api/agent/start`, frontend calls a new `GET /api/preflight?engine=X&provider=Y` that returns a checklist: API key valid, container running, agent service healthy, engine available. Display results as a checklist modal if any item fails. |
| 2.3 | **Implement detailed health endpoint** | `GET /api/health/detailed` returning component-level status: Docker, agent service, Playwright MCP, AT-SPI, API keys. Frontend header can show a single green/yellow/red dot with hover detail. |
| 2.4 | **Standardize API error envelope** | All endpoints return `{"success": bool, "data": {...}, "error": {"code": "...", "message": "...", "hint": "..."}}`. Migrate one endpoint at a time; frontend adapts progressively. |
| 2.5 | **Add a single start command** | Create a `start.sh` / `start.bat` that runs `docker compose up -d`, waits for healthy, starts backend, starts frontend, and opens browser. Reduces three-terminal dance to one command. |
| 2.6 | **Add disk-space check to setup scripts** | Before `docker compose build`, verify at least 10 GB free. Print clear message if insufficient. |
| 2.7 | **Show Docker build progress** | Pipe `docker compose build` output through a filter that extracts layer count and prints "Building… step N of M". |
| 2.8 | **Consolidate env-var defaults** | Move all default values to `backend/config.py` as single source of truth. Docker-compose inherits via `${VAR:-default}` syntax. Document in one place. |
| 2.9 | **Add session timeout** | Auto-stop sessions that have been idle (no new step) for 30 minutes. Prevents orphaned loops consuming container resources when user closes browser. |
| 2.10 | **Rename `execution_target` in UI** | Label it "Run location" with options "This machine" and "Docker container" instead of "local" / "docker." |

**Expected user impact:** Users know their config is valid before starting. Failed sessions drop significantly. Setup is one command.

**Business impact:** Session start success rate increases from ~80% to >95%. Setup failure rate drops below 5%.

**Risk level:** Medium. Key validation and preflight endpoints are new backend code. Error envelope migration touches all endpoints. Single start script introduces a new coordination layer.

**Dependencies:** Phase 1 label/copy changes should land first so the new preflight UI uses the improved terminology.

---

### Phase 3 — Nice-to-Have Later

**Goal:** Polish, accessibility compliance, and quality-of-life features.

**Problems being solved:**
- Accessibility gaps (WCAG compliance).
- No exportable session history.
- Log panel overwhelm.
- No cost visibility.
- Workbench and home page have duplicated logic.

**Specific changes:**

| # | Change | Detail |
|---|--------|--------|
| 3.1 | **Accessibility audit and fix** | Add `aria-label` to all status dots, icon-only buttons, toggle groups. Add `role="radiogroup"` to key-source and mode toggles. Verify WCAG AA contrast for secondary text (#9499ad on #0f1117). |
| 3.2 | **Add log-level filter to Log Panel** | Dropdown or toggle buttons (Info / Warning / Error / Debug) so users can mute noise. |
| 3.3 | **Export session as JSON** | "Download session" button that exports steps, logs, config, and final screenshot as a JSON bundle. |
| 3.4 | **Add estimated API cost** | After session completes, show approximate token usage and estimated cost based on provider pricing (read-only, informational). |
| 3.5 | **Unify home page and Workbench** | Both pages duplicate control panel, screen view, and log logic. Consolidate into one configurable layout, removing the separate `/workbench` route or making it a layout toggle. |
| 3.6 | **Add request-ID tracing** | Generate a UUID per API call, propagate through backend logs and WebSocket events, display in error details for support correlation. |
| 3.7 | **Keyboard shortcut documentation** | Show `Ctrl+Enter` hint on the Start button. Add a `?` keyboard shortcut overlay. |
| 3.8 | **Add container log viewer** | New endpoint `GET /api/container/logs?lines=100` and a UI tab to view container stdout without opening a terminal. |
| 3.9 | **Progressive disclosure for advanced settings** | Collapse execution target, max steps, and debug toggles under an "Advanced" accordion. Show provider, model, engine, and task by default. |
| 3.10 | **VNC password configuration** | x11vnc currently runs with `-nopw`. Add optional `VNC_PASSWORD` env var, document it, and show warning in UI if VNC is unprotected. |

**Expected user impact:** App meets accessibility standards. Power users get export and filtering. Casual users see a cleaner form.

**Business impact:** Accessibility compliance unlocks enterprise procurement. Session export supports audit trails. Cost visibility builds trust.

**Risk level:** Low to medium. Accessibility fixes are additive. Layout unification (3.5) is the riskiest item — it touches both pages and routing.

**Dependencies:** Phase 2 error envelope (2.4) should be in place before request-ID tracing (3.6). Preflight checks (2.2) should exist before progressive disclosure (3.9) hides fields.

---

## 5. Workstream Breakdown

### A. Copy / Labels / Microcopy

**Why it matters:** Every piece of text the user reads shapes their understanding. Current labels expose internal IDs and jargon.

| Improvement | Priority | Risk |
|-------------|----------|------|
| Rewrite engine display names (1.1) | P1 | Very low |
| Rewrite key-source toggle labels (1.4) | P1 | Very low |
| Rewrite top-5 error messages (1.6) | P1 | Low |
| Rename "execution_target" → "Run location" (2.10) | P2 | Very low |
| Add `Ctrl+Enter` hint to Start button (3.7) | P3 | Very low |

### B. Onboarding / Guidance

**Why it matters:** First-time users currently have no help choosing an engine, no sample tasks, and no explanation of what a "step" is.

| Improvement | Priority | Risk |
|-------------|----------|------|
| Add engine selection help text (1.2) | P1 | Very low |
| Add tooltips to all form fields (1.3) | P1 | Very low |
| Add sample tasks (1.8) | P1 | Very low |
| Show model-engine compatibility (1.9) | P1 | Low |
| Add pre-flight checklist (2.2) | P2 | Medium |

### C. Forms / Validation

**Why it matters:** Users submit invalid config and only find out when the session fails minutes later.

| Improvement | Priority | Risk |
|-------------|----------|------|
| Add "backend unreachable" state (1.5) | P1 | Low |
| Add API key pre-validation (2.1) | P2 | Medium |
| Add disk-space check to setup (2.6) | P2 | Low |
| Session timeout for idle sessions (2.9) | P2 | Low |

### D. Trust / Professionalism / Polish

**Why it matters:** Status indicators, loading states, and consistent responses are what make an app feel reliable.

| Improvement | Priority | Risk |
|-------------|----------|------|
| Add loading state to ScreenView (1.7) | P1 | Very low |
| Implement detailed health endpoint (2.3) | P2 | Medium |
| Standardize API error envelope (2.4) | P2 | Medium |
| Show Docker build progress (2.7) | P2 | Low |
| Add estimated API cost display (3.4) | P3 | Low |

### E. Settings / Configuration Simplification

**Why it matters:** Config is split across `config.py`, `docker-compose.yml`, and `.env` with duplicated defaults. Users change one and the other breaks.

| Improvement | Priority | Risk |
|-------------|----------|------|
| Consolidate env-var defaults (2.8) | P2 | Medium |
| Single start command (2.5) | P2 | Low |
| Progressive disclosure for advanced settings (3.9) | P3 | Medium |
| VNC password configuration (3.10) | P3 | Low |

### F. Accessibility / Readability

**Why it matters:** Missing aria-labels, color-only indicators, and potential contrast failures exclude users and block enterprise adoption.

| Improvement | Priority | Risk |
|-------------|----------|------|
| Full accessibility audit and fix (3.1) | P3 | Low |
| Add log-level filter (3.2) | P3 | Very low |

### G. UX / Navigation

**Why it matters:** Two separate pages (home and `/workbench`) duplicate control panel and screen logic, creating maintenance burden and inconsistent experience.

| Improvement | Priority | Risk |
|-------------|----------|------|
| Unify home page and Workbench (3.5) | P3 | Medium |
| Add container log viewer (3.8) | P3 | Low |

---

## 6. Quick Wins

Highest impact, lowest risk. Can be shipped individually in any order.

| # | Change | Effort | Risk | Impact |
|---|--------|--------|------|--------|
| 1.1 | Rewrite engine display names | ~1 hour | Very low | High — instantly clearer |
| 1.4 | Rewrite key-source toggle labels | ~30 min | Very low | Medium — reduces confusion |
| 1.2 | Add engine help text | ~1 hour | Very low | High — answers "which engine?" |
| 1.8 | Add sample tasks | ~1 hour | Very low | High — first task in seconds |
| 1.7 | Add loading state to ScreenView | ~1 hour | Very low | Medium — removes blank-screen anxiety |
| 1.3 | Add tooltips to form fields | ~2 hours | Very low | Medium — self-documenting UI |
| 1.5 | Show "backend unreachable" state | ~1 hour | Low | High — prevents most common support question |
| 1.6 | Rewrite top-5 error messages | ~2 hours | Low | High — actionable errors |

---

## 7. Risky or Sensitive Areas

### 7.1 API Error Envelope Standardization (2.4)

**Why risky:** Every frontend API call parses the response shape. Changing the envelope format can break the running app if frontend and backend are updated out of sync.

**Safe approach:** Introduce the new envelope alongside existing fields (additive, not replacing). Frontend reads new fields first with fallback to old fields. Migrate one endpoint per release. Remove old shape only after all endpoints are migrated and frontend no longer references old fields.

### 7.2 Pre-flight Validation Endpoint (2.2)

**Why risky:** The pre-flight check calls external APIs (key validation) and internal services (container, MCP). If the check itself is slow or flaky, it adds friction rather than removing it. A false negative ("key invalid" when it's actually valid) would block users.

**Safe approach:** Make the pre-flight optional (skippable modal). Set a strict 5-second timeout on each check. If a check times out, report "could not verify" rather than "failed." Never block session start — only warn.

### 7.3 Single Start Command (2.5)

**Why risky:** Coordinating three processes (Docker, Python backend, Node frontend) in one script is fragile. If any process crashes, the script must handle cleanup. Different OSes behave differently with background processes.

**Safe approach:** Use the script as a convenience wrapper, not a replacement. Keep the three-command manual flow documented. Script should trap SIGINT and stop all processes cleanly. Test on Windows (PowerShell), macOS (zsh), and Linux (bash).

### 7.4 Unify Home and Workbench Pages (3.5)

**Why risky:** Both pages work today. Merging them touches routing, layout, component props, and state management. Regressions could break both views.

**Safe approach:** Do not merge. Instead, extract shared logic into hooks and shared components. Keep both routes but have them compose from the same building blocks. Only remove the second route if user research shows nobody uses it.

### 7.5 Consolidating Env-Var Defaults (2.8)

**Why risky:** Defaults currently live in `config.py` *and* `docker-compose.yml`. Removing them from one source could break setups that rely on the current behavior (e.g., users who haven't created a `.env` file).

**Safe approach:** Keep defaults in `config.py` as canonical. Update `docker-compose.yml` to use `${VAR:-}` (empty default) and let backend fill in defaults. Test with no `.env` file, with partial `.env`, and with full `.env`.

---

## 8. Suggested Rollout Order

The safest implementation sequence, accounting for dependencies and risk:

```
Week 1 ──────────────────────────────────────────────────
  1.1  Rewrite engine display names
  1.4  Rewrite key-source toggle labels
  1.2  Add engine selection help text
  1.8  Add sample tasks

Week 2 ──────────────────────────────────────────────────
  1.3  Add tooltips to all form fields
  1.5  Add "backend unreachable" state
  1.6  Rewrite top-5 error messages
  1.7  Add loading state to ScreenView

Week 3 ──────────────────────────────────────────────────
  1.9  Show model-engine compatibility
  2.10 Rename "execution_target" in UI
  2.5  Single start command (script only)
  2.6  Disk-space check in setup scripts

Week 4 ──────────────────────────────────────────────────
  2.1  API key pre-validation endpoint
  2.3  Detailed health endpoint
  2.7  Docker build progress display

Week 5–6 ────────────────────────────────────────────────
  2.2  Pre-flight check before session start
  2.4  Standardize API error envelope (additive migration)
  2.8  Consolidate env-var defaults
  2.9  Session timeout for idle sessions

Week 7+ (ongoing) ──────────────────────────────────────
  3.1  Accessibility audit and fixes
  3.2  Log-level filter
  3.3  Session export
  3.9  Progressive disclosure
  Remaining Phase 3 items as capacity allows
```

---

## 9. Success Criteria

### Quantitative

| Metric | Current estimate | Target | How to measure |
|--------|-----------------|--------|----------------|
| Time from install to first successful task | 45+ min | < 15 min | Timed user test |
| First-time setup success rate | ~70% | > 95% | Count of users who complete setup without manual intervention |
| Session-start success rate | ~80% | > 95% | Ratio of `/api/agent/start` calls that reach step 1 |
| Error message actionability | 0% include recovery hints | 100% of top-10 errors include a next-step hint | Audit of error strings |
| Jargon-free UI labels | ~40% of labels are plain English | 100% | Label audit |
| WCAG AA compliance | Partial | Full for all interactive elements | Automated + manual accessibility test |

### Qualitative

- A user who has never seen the app can select the right engine for a web-search task without asking for help.
- A user who enters an invalid API key is told it's invalid *before* a session starts.
- A user who sees an error message knows what to do next without reading documentation.
- A stakeholder watching a demo sees clean status indicators and professional copy, not raw enum values.

---

## 10. Final Recommendation

### Top 5 Actions

| Rank | Action | Why |
|------|--------|-----|
| 1 | **Rewrite engine names and add help text** (1.1 + 1.2) | Eliminates the #1 first-time confusion point with zero risk. |
| 2 | **Add sample tasks** (1.8) | Turns "what do I type?" into one click. Fastest path to a working demo. |
| 3 | **Rewrite error messages** (1.6) | Every error becomes a recovery instruction instead of a dead end. |
| 4 | **Show "backend unreachable" state** (1.5) | Prevents the most common support question ("no models available"). |
| 5 | **Add API key pre-validation** (2.1) | Catches the most frustrating failure (bad key discovered mid-session) before it wastes time. |

### Safest First Step

**1.1 — Rewrite engine display names.** It is a string change in the frontend engine list and the backend `/api/engines` response. It cannot break any logic because the internal engine ID stays the same. It is immediately visible and immediately improves comprehension.

### Highest Business-Impact Step

**1.8 — Add sample tasks.** A new user who clicks a sample task and sees the agent work within 60 seconds of opening the UI will form a positive first impression. Every other improvement reduces friction; this one creates a moment of success.
