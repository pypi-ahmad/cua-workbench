# CUA Workbench — Execution-Ready Improvement Backlog

> **Status: ALL 31 ITEMS IMPLEMENTED** — See commit history for changes.
> Planning doc created April 2026. All items grounded in actual codebase review.

---

## Full Backlog

| # | Item Title | Problem It Solves | User Type | Current Issue | Recommended Improvement | Expected Impact | Priority | Effort | Risk | Dependency | Notes |
|---|-----------|-------------------|-----------|---------------|------------------------|----------------|----------|--------|------|------------|-------|
| B-01 | Rewrite engine display names | Users see raw internal IDs and don't know which engine to pick | First-time, Non-technical | Engine dropdown shows `playwright_mcp`, `omni_accessibility`, `computer_use` — meaningless to anyone who hasn't read the source. Fallback labels include emoji clutter (`🌳`, `♿`, `🖥️`). | Replace with plain names: "Browser (Semantic)", "Desktop (Accessibility)", "Computer Use (Native)". Keep internal IDs in API payloads only. Apply to both ControlPanel.jsx and Workbench.jsx engine selects plus the backend `/api/engines` response `label` field. | High — eliminates the #1 first-impression confusion point | P0 | Small | Low | None | Also update the fallback `<option>` hardcoded labels in ControlPanel.jsx lines ~221–225 |
| B-02 | Add engine selection help text | Users don't know which engine fits their task | First-time, Non-technical | No description accompanies the engine dropdown. User must guess or read docs. | Add a one-line helper below the engine `<select>` that changes per selection: "Best for web tasks — clicks by element name, not pixel coordinates" / "Best for desktop apps — uses the system accessibility tree" / "Best when the model needs native screen control." | High — answers the immediate "which one?" question | P0 | Small | Low | None | Keep text short; no links needed |
| B-03 | Add sample task chips | First-time users stare at an empty textarea with no idea what to type | First-time | Task `<textarea>` has only a placeholder: "Describe what the agent should do… e.g., Open Chrome and search for 'weather in New York'". Placeholder disappears on focus and isn't clickable. | Add 3–4 clickable chip buttons below the textarea: "Search Google for 'latest AI news'", "Open file manager and list /tmp", "Take a screenshot of the desktop". Clicking fills the textarea. | High — first task achievable in one click | P0 | Small | Low | None | Chips should be visually distinct from action buttons (e.g., outlined, small, secondary color) |
| B-04 | Show "backend unreachable" state | Users see "No Models Loaded" or "Loading models…" with no explanation | First-time, Business | When backend is not running, the model fetch silently fails (catch block is empty: `catch { /* backend not ready */ }`). Start button shows "No Models Loaded" which sounds like a data problem, not a connectivity problem. | When model fetch fails, display an inline warning: "Cannot reach backend — start it with `python -m backend.main`". Change the Start button text to "Backend Offline" with a different disabled state. | High — prevents the #1 support question for new users | P0 | Small | Low | None | ControlPanel.jsx useEffect fetchModelList catch block currently swallows the error |
| B-05 | Rewrite top 5 error messages | Error messages are developer-facing and don't tell users what to do next | All | (a) "API key is required" — doesn't say which key or where to get one. (b) "Task description is required" — robotic. (c) Rate-limit message "max 10 starts per minute" — no recovery hint. (d) AT-SPI failure dumps package names. (e) Step timeout just says seconds elapsed. | Rewrite to: (a) "Enter your {Google/Anthropic} API key, or add {GOOGLE_API_KEY/ANTHROPIC_API_KEY} to your .env file." (b) "Describe what the agent should do." (c) "Too many sessions started — wait a minute and try again." (d) "Accessibility engine needs the Docker container running. Try the Browser engine or start the container." (e) "Step {n} took longer than {timeout}s — the automation may have stalled. Check the screen view." | High — every error becomes a recovery instruction | P0 | Small | Low | None | Frontend messages in ControlPanel.jsx handleStart; backend messages in loop.py, server.py |
| B-06 | Rewrite key-source toggle labels | Toggle buttons use emoji jargon (`✏️ Manual`, `📄 .env`, `💻 System`) | Non-technical, Business | Labels are cryptic for non-developers. ".env" means nothing to a business user. Emoji take space without adding clarity. | Rename to: "Enter key", "From .env file", "Environment variable". Drop all emoji. Keep tooltip on hover showing masked key or availability status. | Medium — removes visual noise and jargon from the most-used form section | P0 | Small | Low | None | Three `<button>` elements in ControlPanel.jsx key-source-row div |
| B-07 | Add loading state to ScreenView | Users see a blank black panel after starting the container while services initialize | First-time, Business | ScreenView shows the monitor SVG with "No screen capture available" until a screenshot arrives. When container is running but agent service is not yet healthy, there's a 5–10 second gap with no feedback. | When `containerRunning=true` and the agent-service health check hasn't passed, show a spinner with "Waiting for agent service to start…". Replace the static monitor icon during this state. | Medium — removes the "is it broken?" anxiety during startup | P0 | Small | Low | None | ScreenView.jsx currently has only two states (VNC or screenshot). Add a third: loading. |
| B-08 | Add tooltips to all form fields | Users must guess what each field does | First-time, Non-technical | Provider, model, engine, execution target, key source, and max steps have no `title` or popover help. The only tooltip is on execution target: "Where to run the engine — Local (host machine) or Docker (Ubuntu container)". | Add `title` attributes to every form control: Provider ("Which AI company's model to use"), Model ("The specific AI model — larger models are slower but more capable"), Engine (reuse help text from B-02), Execution target (already has tooltip — keep it), Max steps ("Maximum number of actions the agent can take before stopping"). | Medium — makes the form self-documenting | P1 | Small | Low | None | title attributes are the simplest approach; upgrade to popovers later if needed |
| B-09 | Show model-engine compatibility | Users pick a model that doesn't support their chosen engine, then get a confusing backend error | First-time | `allowed_models.json` already has `supports_computer_use`, `supports_playwright_mcp`, `supports_accessibility` flags per model. Frontend fetches these but never uses them to filter the engine dropdown. | When a model is selected, grey out or add a "(not supported)" suffix to engines the model doesn't support based on the capability flags already in the fetched model data. | Medium — prevents a class of "unsupported action" errors entirely | P1 | Small | Low | B-01 (engine names should be readable first) | The data is already fetched via `getModels()`; this is a display-only change |
| B-10 | Rename "execution_target" in UI | "execution_target" and "local"/"docker" are developer terms | Non-technical, Business | Execution target dropdown says "🖥️ Run Locally (Host Machine)" and "🐳 Run in Docker (Ubuntu Container)". The concept of "execution target" itself is obscure. The `<select>` has no label — it appears without a heading. | Add a label "Run location" above the dropdown. Rename options to "This machine" and "Docker container". Drop the emoji. | Low-medium — clearer for non-technical users, less visual noise | P1 | Small | Low | None | Both ControlPanel.jsx and Workbench.jsx have this dropdown |
| B-11 | API key pre-validation | Invalid API keys are only discovered when the model call fails mid-session, wasting time and causing confusing errors | All | Backend validates key length (>8 chars) but never makes a test API call. A 100-character garbage key passes validation. User starts a session, waits for container and model call, then gets a cryptic provider error. | Add `POST /api/keys/validate` endpoint that makes a minimal test call to the selected provider (e.g., list-models for Google, a trivial completion for Anthropic). Frontend calls this when key source changes or key is pasted. Show green checkmark or red X inline next to the key field. | High — catches the most frustrating failure before it wastes time | P1 | Medium | Medium | None | Must handle: slow provider responses (5s timeout), rate limits on validation calls, caching results for 5 min |
| B-12 | Pre-flight check before session start | Sessions start with broken config and fail minutes later | All | No pre-submission validation. `/api/agent/start` validates inputs but not environment state (container health, MCP readiness, key validity). Users discover failures one-by-one during the session. | Add `GET /api/preflight?engine=X&provider=Y` returning a checklist: API key valid, container running, agent service healthy, selected engine available. Frontend shows checklist modal if any item fails. Modal should have a "Start anyway" button (never block). | High — eliminates the trial-and-error config loop | P1 | Medium | Medium | B-11 (key validation endpoint) | Pre-flight must be fast (<5s total). Timeout = "could not verify", not "failed". |
| B-13 | Detailed health endpoint | No unified view of system health; header shows only container running/stopped | Business, Technical evaluator | `GET /health` returns `{"status": "ok"}` unconditionally. Container status only checks Docker running state. No visibility into agent service, MCP, AT-SPI, or key availability from one place. | Implement `GET /api/health/detailed` returning: Docker status, agent service latency, Playwright MCP status, API key availability per provider. Frontend header badge: green (all healthy), yellow (degraded), red (critical component down) with hover showing details. | Medium — builds trust; makes status visible at a glance | P1 | Medium | Medium | None | Don't block on any single check; collect all in parallel with 3s timeout each |
| B-14 | Standardize API error response envelope | Frontend parses three different error shapes from different endpoints | Business, Technical evaluator | `/api/agent/start` returns `{"error": "..."}`, container endpoints return `{"success": bool}`, steps use `{"errorCode": "...", "message": "..."}`. Frontend has scattered error parsing logic. | Standardize to `{"success": bool, "data": {...}, "error": {"code": "...", "message": "...", "hint": "..."}}`. Migrate one endpoint at a time. Frontend reads new fields with fallback to old fields during transition. | Medium — consistent error handling; simpler frontend code | P1 | Large | Medium | None | Additive migration: keep old fields during transition. Remove only after all endpoints migrated. |
| B-15 | Single start command | Users must open three terminals and run three commands to start the app | First-time, Non-technical | Startup requires: (1) `docker compose up -d`, (2) `python -m backend.main`, (3) `cd frontend && npm run dev`. No single entrypoint. | Create `start.sh` / `start.bat` that: starts Docker in background, waits for healthy, starts backend in background, starts frontend, opens browser. Trap SIGINT to clean up all processes. Keep manual three-command flow documented as fallback. | High — reduces setup from 3 commands to 1 | P1 | Medium | Low | None | Must test on Windows (PowerShell), macOS (zsh), Linux (bash). Handle port-already-in-use. |
| B-16 | Disk-space check in setup scripts | Docker build fails cryptically when disk is full | First-time | `setup.sh` and `setup.bat` check for Docker, Python, Node.js but not available disk space. Docker build needs ~10 GB. If space is insufficient, build fails with opaque Docker errors. | Before `docker compose build`, check available space. If <10 GB, print "Insufficient disk space ({available} GB free, need 10 GB)" and exit. | Medium — prevents the longest and most confusing failure mode | P1 | Small | Low | None | Use `df` on Linux/macOS, `Get-PSDrive` on Windows |
| B-17 | Docker build progress display | First build takes 30+ minutes with no user-facing progress indication | First-time, Non-technical | `docker compose build` outputs raw Docker layer logs. No step count, no ETA, no summary. User doesn't know if it's hung or progressing. | Pipe build output through a filter that extracts `Step N/M` lines from Docker build output and prints a simplified progress line: "Building… step N of M". Add a final "Build complete" message. | Medium — reduces anxiety during the longest wait | P1 | Small | Low | None | Docker BuildKit output format differs from legacy builder; handle both |
| B-18 | Consolidate env-var defaults | Config defaults are scattered across config.py and docker-compose.yml, causing silent conflicts | Technical evaluator | `AGENT_SERVICE_PORT=9222` appears in both config.py and docker-compose.yml. `PLAYWRIGHT_MCP_PORT=8931` same. If user changes one, the other goes stale. `_NOVNC_HTTP` is hardcoded in server.py with no config override. | Make `backend/config.py` the single source of truth for all defaults. Update `docker-compose.yml` to use `${VAR:-}` (empty default, let backend handle). Document all vars in one place in config.py with comments. | Medium — eliminates a class of silent misconfiguration | P1 | Medium | Medium | None | Test with: no .env, partial .env, full .env. Ensure container startup still works without .env. |
| B-19 | Session timeout for idle sessions | Orphaned sessions consume container resources indefinitely | Business, Technical evaluator | No session expiration. If user starts a session, closes browser, the agent loop continues running until max steps (up to 200). No idle detection. | Auto-stop sessions with no new step for 30 minutes. Backend loop checks last-step timestamp. On timeout, log "Session {id} timed out after 30m idle" and clean up. | Medium — prevents resource waste; improves trust in resource management | P1 | Small | Low | None | Only apply to idle sessions; active sessions running at max steps should complete normally |
| B-20 | Accessibility: aria-labels and roles | Screen readers cannot interpret status indicators, toggles, or icon-only buttons | Non-technical (accessibility) | Status dots (8×8px colored circles) have no aria-label. Key-source toggle buttons lack `role="radiogroup"`. Mode toggle (Browser/Desktop) in Workbench.jsx lacks role. Icon-only Clear/Download buttons have no accessible name. | Add `aria-label` to: all status dots ("Container running"/"Container stopped"), all icon-only buttons ("Clear logs", "Download logs"), all toggle groups (`role="radiogroup"` + `role="radio"` on children). | Medium — required for enterprise accessibility compliance | P2 | Medium | Low | None | Additive-only; no visual changes needed |
| B-21 | Accessibility: color-only indicators | Colorblind users cannot distinguish status badges | Non-technical (accessibility) | Container status relies on green vs red background. WebSocket status uses green/gray/purple dot only. Log levels use color coding with no icon or text prefix. | Add text or icon alongside color: status badges already have text ("Container Running"), so add a small ●/✕ icon prefix for colorblind differentiation. Log levels already show text (INFO/WARN/ERROR) — sufficient as-is. WebSocket dot needs an icon or label. | Low-medium — improves accessibility with minimal visual change | P2 | Small | Low | None | |
| B-22 | Accessibility: contrast audit | Secondary text may fail WCAG AA against dark background | Non-technical (accessibility) | Secondary text color `#9499ad` on background `#0f1117` — contrast ratio ~5.2:1 (passes AA normal text but may fail at smaller sizes). Disabled state (50% opacity) on secondary text drops below 4.5:1. | Audit all text/background combinations. Bump secondary text to `#a8adc0` or similar if any fail AA at 12px. Increase disabled opacity from 0.5 to 0.6 if needed. | Low — compliance improvement | P2 | Small | Low | None | Use browser dev tools or axe-core to measure actual ratios |
| B-23 | Log-level filter | Log panel becomes overwhelming during long sessions | Technical evaluator, Business | All 200 log entries shown regardless of level. Debug logs, info logs, warnings, and errors mixed together. No way to filter. | Add toggle buttons above the log list: Info / Warning / Error / Debug (all on by default). Clicking toggles visibility of that level. Persist filter state during session. | Low-medium — reduces noise for users monitoring specific behavior | P2 | Small | Low | None | LogPanel.jsx in both App.jsx and Workbench.jsx layouts |
| B-24 | Export session as JSON | No way to save or share session results | Business, Technical evaluator | No export functionality exists. Steps, logs, and screenshots are in-memory only. Closing the browser loses everything. | Add a "Download session" button that exports a JSON file containing: config (model, engine, provider), all steps, all logs, and b64 final screenshot. Filename: `session_{id}_{timestamp}.json`. | Low-medium — enables reporting, sharing, audit trails | P2 | Small | Low | None | Available after session completes or is stopped |
| B-25 | Estimated API token cost | Users have no visibility into how much a session costs | Business | No token counting or cost estimation anywhere. Sessions can run 200 steps with no indication of spend. | After session completes, show a "~{N} tokens used · est. ${X.XX}" based on step count, average prompt size, and provider pricing. Mark as estimate. | Low — informational; builds cost awareness and trust | P2 | Medium | Low | B-14 (needs structured response data from session) | Display as read-only info after session ends. Don't block or warn during session. |
| B-26 | Progressive disclosure for advanced settings | Control panel shows too many fields by default | Non-technical, Business | All fields visible at once: provider, model, key source, key input, engine, execution target, max steps. Seven+ controls before the task field. | Collapse execution target, max steps, and key-source toggles under an "Advanced" accordion (closed by default). Show only: provider, model, engine, task, and start button by default. | Medium — cleaner first impression; fewer decisions for simple tasks | P2 | Medium | Medium | B-01, B-02 (engine names and help text should be in place first) | Must not hide any field that blocks starting — only optional/advanced ones |
| B-27 | Unify home page and Workbench shared logic | Two pages duplicate control panel, screen view, and log state management | Technical evaluator (maintenance) | ControlPanel + ScreenView + LogPanel logic is duplicated between App.jsx (home) and Workbench.jsx. State management, API calls, and WebSocket handling repeated. | Extract shared logic into custom hooks: `useAgentSession`, `useContainerStatus`, `useModels`. Both pages import hooks and compose their own layouts. Do not merge pages — keep both routes. | Low — reduces maintenance burden; prevents drift between pages | P2 | Large | Medium | None | Refactor, not rewrite. Keep both routes working throughout. |
| B-28 | Request-ID tracing | No way to correlate frontend errors with backend logs | Business, Technical evaluator | No request ID in API calls. When a user reports an error, there's no way to find the corresponding backend log entry. | Generate UUID per API call in frontend, send as `X-Request-ID` header. Backend logs it. Include in error responses and WebSocket events. Display in error messages for support: "Error ID: {uuid}". | Low — improves supportability | P2 | Medium | Low | B-14 (error envelope should include request_id field) | |
| B-29 | Keyboard shortcut documentation | Ctrl+Enter shortcut exists but is invisible | First-time | ControlPanel.jsx listens for Ctrl+Enter to start the agent. No visual hint anywhere — users must discover it by accident. | Add "(Ctrl+Enter)" as a subtle suffix on the Start Agent button label, or as a tooltip. | Low — small polish item | P2 | Small | Low | None | |
| B-30 | Container log viewer in UI | Users must open a terminal to see container logs | Technical evaluator | No container log endpoint or UI. Debugging container issues requires `docker compose logs` in a separate terminal. | Add `GET /api/container/logs?lines=100` endpoint. Add a "Container Logs" tab or expandable section in the log panel showing recent container stdout. | Low — power-user convenience | P2 | Medium | Low | None | Rate-limit the endpoint; don't stream full history |
| B-31 | VNC password configuration | VNC runs without authentication | Business (security) | x11vnc starts with `-nopw` flag in entrypoint.sh. Logged as warning but continues. No option to set password. | Add optional `VNC_PASSWORD` env var. If set, x11vnc uses it. If unset, continue with `-nopw` but show a small "VNC unprotected" warning badge in the header. Document in .env.example. | Low — security hardening for environments where it matters | P2 | Small | Low | None | Only relevant if VNC is exposed beyond localhost |

---

## 1. Top 10 Most Important Backlog Items

Ranked by combination of user impact, frequency of the problem, and breadth of users affected.

| Rank | Item | Why |
|------|------|-----|
| 1 | **B-05** Rewrite top 5 error messages | Affects every user who hits any error. Transforms dead-end messages into recovery instructions. Zero architectural risk. |
| 2 | **B-01** Rewrite engine display names | The first thing a new user sees in the form. Eliminates the single biggest comprehension barrier. |
| 3 | **B-03** Add sample task chips | Turns a blank textarea into a one-click first experience. Largest single reduction in time-to-first-task. |
| 4 | **B-04** Show "backend unreachable" state | Prevents the most common support question. Currently a silent failure that looks like a data problem. |
| 5 | **B-02** Add engine selection help text | Completes the engine clarity story. After B-01 gives readable names, B-02 explains when to use each. |
| 6 | **B-11** API key pre-validation | Catches invalid keys before wasting session time. Currently the most frustrating delayed failure. |
| 7 | **B-15** Single start command | Removes the three-terminal manual startup ceremony. Biggest friction-reduction for setup. |
| 8 | **B-12** Pre-flight check before session start | Validates the entire config before committing. Prevents the trial-and-error configuration loop. |
| 9 | **B-07** Add loading state to ScreenView | Removes the 5–10 second "blank screen" gap that makes users think the app is broken. |
| 10 | **B-06** Rewrite key-source toggle labels | Removes emoji jargon from the most-used form section. Quick win with immediate visual improvement. |

---

## 2. Top 5 Safest Quick Wins

Highest impact with near-zero risk of breaking anything. All are frontend-only, additive, and touch no backend logic.

| Rank | Item | Effort | Risk | Why safe |
|------|------|--------|------|----------|
| 1 | **B-01** Rewrite engine display names | Small | Low | String replacements in JSX option labels. Internal engine IDs in API payloads stay unchanged. |
| 2 | **B-06** Rewrite key-source toggle labels | Small | Low | Button text changes only. No logic, state, or API impact. |
| 3 | **B-03** Add sample task chips | Small | Low | New JSX elements below textarea. No existing element modified. |
| 4 | **B-02** Add engine selection help text | Small | Low | New `<p>` element below engine `<select>`. Conditional on engine value. No existing element touched. |
| 5 | **B-07** Add loading state to ScreenView | Small | Low | New conditional branch in ScreenView.jsx. Existing screenshot and VNC branches unchanged. |

---

## 3. Top 5 Highest Business-Value Improvements

Changes that most directly affect demos, stakeholder confidence, and professional perception.

| Rank | Item | Business value |
|------|------|----------------|
| 1 | **B-05** Rewrite top 5 error messages | Errors visible during demos become guidance instead of embarrassments. Makes the app look production-ready. |
| 2 | **B-12** Pre-flight check before session start | Eliminates "let me restart, there was a config issue" moments during demos. Validates everything upfront. |
| 3 | **B-15** Single start command | Stakeholder can start the app without developer assistance. Enables self-service evaluation. |
| 4 | **B-13** Detailed health endpoint | Dashboard-quality status visibility. Shows infrastructure maturity. Enterprise evaluators look for this. |
| 5 | **B-25** Estimated API token cost | Answers the #1 business question: "how much does this cost?" Even a rough estimate builds trust. |

---

## 4. Top 5 Improvements for First-Time Non-Technical Users

Changes that most directly reduce confusion for someone who has never used the app and doesn't know Docker, APIs, or model terminology.

| Rank | Item | User impact |
|------|------|-------------|
| 1 | **B-03** Add sample task chips | User doesn't need to know what tasks are possible. One click starts their first experience. |
| 2 | **B-01** + **B-02** Rewrite engine names + help text | User can pick the right engine without reading documentation or understanding acronyms. |
| 3 | **B-04** Show "backend unreachable" state | User knows exactly what's wrong and what to do instead of seeing a mysteriously empty dropdown. |
| 4 | **B-06** Rewrite key-source toggle labels | User can understand "Enter key" / "From .env file" / "Environment variable" without developer context. |
| 5 | **B-26** Progressive disclosure | User sees only 4 fields (provider, model, engine, task) instead of 7+. Fewer decisions = less paralysis. |

---

## 5. Recommended Sprint Order

### Sprint 1 — Copy & Clarity (P0 items, all low-risk)

**Goal:** Make every label and error message understandable without documentation.

| Item | Effort |
|------|--------|
| B-01 Rewrite engine display names | Small |
| B-02 Add engine selection help text | Small |
| B-03 Add sample task chips | Small |
| B-04 Show "backend unreachable" state | Small |
| B-05 Rewrite top 5 error messages | Small |
| B-06 Rewrite key-source toggle labels | Small |
| B-07 Add loading state to ScreenView | Small |

**Total effort:** ~1 week. All frontend-only. Can be reviewed in a single PR.

---

### Sprint 2 — Form Intelligence (P0/P1 items, low risk)

**Goal:** Make the form prevent bad configurations instead of allowing them through.

| Item | Effort |
|------|--------|
| B-08 Add tooltips to all form fields | Small |
| B-09 Show model-engine compatibility | Small |
| B-10 Rename "execution_target" in UI | Small |
| B-16 Disk-space check in setup scripts | Small |
| B-17 Docker build progress display | Small |
| B-29 Keyboard shortcut documentation | Small |

**Total effort:** ~1 week. Mix of frontend and setup scripts. No backend API changes.

---

### Sprint 3 — Validation & Startup (P1 items, medium risk)

**Goal:** Validate configuration before session start. Reduce startup friction.

| Item | Effort |
|------|--------|
| B-11 API key pre-validation | Medium |
| B-15 Single start command | Medium |
| B-19 Session timeout for idle sessions | Small |

**Total effort:** ~1.5 weeks. New backend endpoint for B-11. New scripts for B-15. Backend loop change for B-19.

**Risk mitigation:** B-11 must have a 5-second timeout and never block. B-15 must not replace manual startup (keep it documented). B-19 must only timeout idle sessions, not active ones.

---

### Sprint 4 — Health & Reliability (P1 items, medium risk)

**Goal:** Give users and operators a clear picture of system health.

| Item | Effort |
|------|--------|
| B-13 Detailed health endpoint | Medium |
| B-12 Pre-flight check before session start | Medium |
| B-18 Consolidate env-var defaults | Medium |

**Total effort:** ~2 weeks. New backend endpoints. Config refactor requires careful testing.

**Risk mitigation:** B-13 must not block if any health check is slow. B-12 must be skippable. B-18 must be tested with no .env, partial .env, and full .env.

---

### Sprint 5 — Error Standardization (P1 item, highest-risk single item)

**Goal:** Standardize all API responses to a single envelope format.

| Item | Effort |
|------|--------|
| B-14 Standardize API error response envelope | Large |

**Total effort:** ~2 weeks. Touches every endpoint and every frontend API parsing path.

**Risk mitigation:** Additive migration — keep old fields during transition. Frontend reads new fields first with fallback. Migrate one endpoint per sub-PR. Remove old format only when all endpoints are migrated and verified.

---

### Sprint 6 — Accessibility & Polish (P2 items, low risk)

**Goal:** Meet WCAG AA. Reduce visual noise for non-technical users.

| Item | Effort |
|------|--------|
| B-20 Accessibility: aria-labels and roles | Medium |
| B-21 Accessibility: color-only indicators | Small |
| B-22 Accessibility: contrast audit | Small |
| B-23 Log-level filter | Small |
| B-26 Progressive disclosure for advanced settings | Medium |

**Total effort:** ~1.5 weeks. All frontend. Additive — no existing features changed.

---

### Sprint 7 — Power-User & Maintenance (P2 items, mixed risk)

**Goal:** Export, tracing, and architectural housekeeping.

| Item | Effort |
|------|--------|
| B-24 Export session as JSON | Small |
| B-25 Estimated API token cost | Medium |
| B-27 Unify home/Workbench shared logic | Large |
| B-28 Request-ID tracing | Medium |
| B-30 Container log viewer in UI | Medium |
| B-31 VNC password configuration | Small |

**Total effort:** ~3 weeks. B-27 is the riskiest (refactor touching both pages). B-25 depends on B-14.

**Risk mitigation:** B-27 should extract hooks first, then migrate one page at a time. Both routes must keep working at every commit.
