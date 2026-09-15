# CF Fundamental Redo Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rewrite Kahin's Cloudflare solver on the TR reference mechanism (native events, trust contract, jittered retries) and add a real live-watch MJPEG server, verified against TR repo evidence.

**Architecture:** Task 1 rewrites `kahin/tools/cf_clear_mirage.py` to mirror `cf_bypasser/core/bypasser.py` (frame-anchored native click + re-eval verification + retry/backoff), deleting the 19px heuristic and the dead duplicate block. Task 2 adds `kahin/tools/screencast_server_mirage.py` — stdlib-only MJPEG HTTP server fed by the existing screencast pump — so a human watches live via mpv/ffplay/browser. Task 3 verifies (unit + ruff + live attempt) and updates docs/CHANGELOG.

**Tech Stack:** Python 3.13 (repo `.venv`), pytest, ruff, stdlib `http.server`+`threading` only for Task 2 (no new dependencies — `aiohttp`/`curl_cffi` are NOT installed).

## Global Constraints

- Branch `feat/cf-fundamental-redo` in `/home/void0x14/Belgeler/mcp-projelerim/cdp-kahin-mcp` — all work here, never main.
- No pushes (commit only, user explicitly forbids push).
- `cleared:true` ONLY when the interstitial title gate passes — never from cookie presence alone.
- No external browser, no cookie cache, no replay proxy (cf_clear stays embedded; mirror/proxy replay is out of scope).
- Every tool returns bounded JSON strings; follow existing `_json_error(_TOOL, msg, code)` pattern.
- TDD: failing test first, then minimal implementation, then green, then commit per task.
- Run `ruff check` on touched files; fix all findings.
- Turkish user-facing summaries; code/docstrings in English matching repo style.

---

### Task 1: cf_clear rewrite on TR trust contract

**Files:**
- Modify: `kahin/tools/cf_clear_mirage.py` (full rewrite of click path + retry loop; keep tool names `kahin_cf_clear` / `kahin_cf_status` and JSON contract keys)
- Modify: `tests/test_cf_clear.py` (update pinned expectations)
- Read-only reference: `/tmp/cf-tr/cf_bypasser/core/bypasser.py:52-73,105-142,152-167,171-189,200-239` and `cf_bypasser/utils/constants.py:18-33`

**Interfaces:**
- Consumes: `_mirage_engine()` + `engine.call("Page.*", ...)` (see `kahin/tools/screencast_mirage.py:116-120` for call pattern), `_CHALLENGE_STATUS_JS` from `kahin/tools/agent_mirage.py`, `_capture_page_session` + `_json_error` from `kahin/tools/pilot_mirage.py`
- Produces: `cf_clear(url, timeout) -> JSON string {cleared, method, url, cfCookies, clicks?, elapsedMs, ...}`; helpers `_find_checkbox_js` contract, `_is_bypassed`, `_is_blocked` unchanged in behavior
- Task 2 consumes: nothing from this task (independent). Task 3 consumes: final `cf_clear` behavior.

**TR evidence to mirror (exact):**
- `_FIND_CHECKBOX_JS` (`bypasser.py:58-73`): iterate frames filtered by `"challenges.cloudflare" in frame.url` — in Kahin use `kahin_mirage_frame_tree` data via `engine` frame list OR `Page.getFrameTree` through `engine.call`; inside target frame evaluate JS walking `el.fakeShadowRoot || el.shadowRoot` to find `input[type=checkbox]`, return `{x, y (rect center), checked, w}`.
- Click gate (`bypasser.py:175`): skip when `w<=0` or `checked`.
- Frame-element offset (`bypasser.py:177-182`): checkbox page coords = frame element bounding box origin + in-frame checkbox center; click with NATIVE mouse events (`Input.dispatchMouseEvent` / Juggler `Page.dispatchMouseEvent` mousePressed+mouseReleased — check what `pilot_mirage._dispatch_mouse` wraps and reuse it if native, else call `engine.call` directly).
- Verification (`bypasser.py:184-187`): re-evaluate after click; success = checkbox `not found` or `checked`.
- Retry loop (`bypasser.py:237-239`, `constants.py:26,28`): up to 5 attempts, `RETRY_POLL_SECONDS=3` sleep between attempts (jitter it: 3.0s ± 1.0s via existing `kahin/humanize.py::jittered_delay`).
- Settle (`bypasser.py:200-207`, `constants.py:19`): `domcontentloaded` + `CHALLENGE_SETTLE_SECONDS=5` before first probe.
- Clearance gate (`bypasser.py:152-167,309-314`): title `just a moment` → not bypassed; html `please complete the captcha` → not bypassed; `_BLOCK_MARKERS` → blocked; CF-detected requires `cf_clearance` cookie for cache — we have no cache, but REQUIRE `cf_clearance` host-scoped presence as supporting evidence alongside title gate before `cleared:true` with method `click` (keep `auto` only when no click was needed AND cf_clearance present).

**Deletions (exact):**
- `_CHECKBOX_LEFT_PX = 19.0` and `tx = mount["x"] + _CHECKBOX_LEFT_PX` (`cf_clear_mirage.py:114,200`) — replaced by frame-anchored coordinates.
- `_FIND_MOUNT_JS` mount-div search (`cf_clear_mirage.py:92-107`) — replaced by frame-URL + shadow-walk JS.
- Duplicate validation block (`cf_clear_mirage.py:299-308`, identical to `:282-291`) — keep one.
- Bézier travel + 120ms press (`cf_clear_mirage.py:202-212`) — replaced by direct native press/release (TR evidence: no humanization, and it passes). Remove now-unused imports (`bezier_trajectory`, `get_last_mouse_position`) if nothing else in the file uses them.
- `clicks>=2` refused rule (`cf_clear_mirage.py:368-370`) — replaced by: 5 attempts exhausted without verification success → `method:timeout` (keep `refused` ONLY for explicit Ray-rotation-after-click evidence if still detectable, else drop `refused` and document why in the commit message).

- [ ] **Step 1: Write failing tests.** Update `tests/test_cf_clear.py`: (a) test that `_FIND_MOUNT_JS`/`_CHECKBOX_LEFT_PX` no longer exist in module source (grep module file text), (b) test the new checkbox-finder JS string contains `fakeShadowRoot` and `challenges.cloudflare` filter markers, (c) test retry constants `_MAX_ATTEMPTS=5` and settle `5.0` exist, (d) keep existing host-scope cookie tests passing unchanged. Run: `cd /home/void0x14/Belgeler/mcp-projelerim/cdp-kahin-mcp && .venv/bin/python -m pytest tests/test_cf_clear.py -x -q`. Expected: FAIL on (a)/(b)/(c).
- [ ] **Step 2: Rewrite `cf_clear_mirage.py`.** Implement per TR evidence above. Keep: module docstring (update findings paragraph: native events + FakeShadowRoot-style trust contract), `_TOOL`, URL/budget validation (single copy), `_challenge_probe`, `_is_bypassed`, `_is_blocked`, `_cf_cookies` host-scope, `_page_cf_cookie_names`, `_ray_id`, `cf_status` untouched unless imports change. New: `_find_checkbox(session_id)` returning `{x, y}` page coords or `None` (frame filter + shadow walk + `w>0` + not `checked` gate inside JS or Python — put the gate in Python on the returned dict for testability), `_click_at(session_id, x, y)` native press/release, `_verify_checkbox(session_id)` re-eval. Main loop: navigate → sleep 5 → probe fast-path → blocked check → up-to-5 × (verify → click → jittered 3s sleep) → cleared/timeout JSON with same contract keys (`cleared, method, url, cfCookies, pageCfCookies, clicks, elapsedMs, action`).
- [ ] **Step 3: Run tests.** Run: `.venv/bin/python -m pytest tests/test_cf_clear.py -q`. Expected: all PASS.
- [ ] **Step 4: Ruff.** Run: `.venv/bin/python -m ruff check kahin/tools/cf_clear_mirage.py tests/test_cf_clear.py` (if ruff not in venv, `ruff check` via repo config). Expected: All checks passed. Fix all findings.
- [ ] **Step 5: Commit.** Run: `git add kahin/tools/cf_clear_mirage.py tests/test_cf_clear.py && git commit -m "feat(cf): native-event click on TR trust contract (frame filter + shadow walk + re-eval verify)"`. Expected: new commit hash on branch `feat/cf-fundamental-redo`.

---

### Task 2: Live-watch MJPEG server (real canlı yayın)

**Files:**
- Create: `kahin/tools/screencast_server_mirage.py`
- Modify: `kahin/tools/__init__.py` (register module: add `from kahin.tools import screencast_server_mirage` + `"screencast_server_mirage"` in list — mirror existing entries)
- Create: `tests/test_watch_server.py`
- Read-only reference: `kahin/tools/screencast_mirage.py:78-145` (start), `:148-254` (frame+ACK), `:257-287` (stop); engine pump API `wait_for_screencast_frame` / `clear_screencast` / `screencast_generation` in `kahin/the_twins/mirage.py` (grep for `def wait_for_screencast_frame`, `def drain_screencast_frames`, `def screencast_pending`).

**Interfaces:**
- Consumes: `_mirage_engine()`, `_require_mirage()`, `_healer_ref`, `_RO`/`_RW` from `kahin/tools/_common.py`; running screencast started by `kahin_mirage_screencast_start` (this tool does NOT start the stream — it fails closed with `screencast_not_running` if none active)
- Produces: `kahin_mirage_watch_start(port: int = 0) -> {watching, url, port, format: "mjpeg"}`; `kahin_mirage_watch_stop() -> {watching: false}`; serves `GET /` → `multipart/x-mixed-replace; boundary=kahinframe` MJPEG (viewable in `mpv`, `ffplay`, any browser); `GET /snapshot.jpg` → latest JPEG single frame
- Task 3 consumes: watch tools for live verification.

**Design (fixed, do not redesign):**
- stdlib only: `http.server.ThreadingHTTPServer` in a daemon thread + `asyncio` pump task created via `asyncio.get_running_loop().create_task` inside the tool call.
- Pump loop: `frame = await engine.wait_for_screencast_frame(timeout=5.0, generation=...)`; on frame: store raw JPEG bytes in a `threading.Lock`-guarded slot, then ACK via `engine.call("Page.screencastFrameAck", {"screencastId": sid}, session_id=owner_session)` (same pattern as `screencast_mirage.py:225-236`). On timeout/None: loop again (check stop flag). On generation change/stop: exit task.
- Bind `127.0.0.1`, port `0` = OS-assigned free port; return actual port + `http://127.0.0.1:{port}/` URL. No auth token (localhost-only is the access control; document it).
- `watch_start` when already watching → return existing URL (idempotent). `watch_stop` → set stop event, join thread (timeout 5s), cancel pump task, return `{watching:false}`. Never touches `Page.stopScreencast` (stream lifecycle stays with screencast_* tools).
- Conflict rule: while watching, `kahin_mirage_screencast_frame` manual calls race the pump for frames — document in `watch_start` description string; do not add locking against it (YAGNI).
- Pure helper for tests: `def _mjpeg_part(jpeg: bytes, boundary: bytes = b"kahinframe") -> bytes` returning full multipart chunk (headers + bytes + CRLF). All HTTP formatting goes through it.

- [ ] **Step 1: Write failing tests** in `tests/test_watch_server.py`: (a) `_mjpeg_part(b"\xff\xd8fake")` starts with `b"--kahinframe\r\nContent-Type: image/jpeg\r\nContent-Length: 5\r\n\r\n"` and ends with `b"\r\n"`, (b) importing module registers two tools (assert `"kahin_mirage_watch_start" in <registered names>` — check how other test files assert registration, e.g. grep tests/ for `mcp` tool-list pattern; if none exists, assert module has `mirage_watch_start` and `mirage_watch_stop` coroutine functions), (c) `watch_start` with no engine returns JSON with `engine_unavailable`/`engine_unavailable`-style error (call the underlying coroutine directly — no browser in unit tests). Run: `.venv/bin/python -m pytest tests/test_watch_server.py -q`. Expected: FAIL (module missing).
- [ ] **Step 2: Implement `screencast_server_mirage.py`** per design above. Docstring must state: localhost-only, requires active screencast, races manual `frame()` calls.
- [ ] **Step 3: Register in `kahin/tools/__init__.py`.** Mirror the `screencast_mirage` lines exactly.
- [ ] **Step 4: Run tests + ruff.** Run: `.venv/bin/python -m pytest tests/test_watch_server.py tests/test_cf_clear.py -q` → all PASS. Run ruff on all touched files → clean.
- [ ] **Step 5: Commit.** `git add kahin/tools/screencast_server_mirage.py kahin/tools/__init__.py tests/test_watch_server.py && git commit -m "feat(watch): localhost MJPEG live-watch server fed by screencast pump"`.

---

### Task 3: Verification + docs + changelog

**Files:**
- Modify: `docs/juggler-ai-native.md` (extend the `kahin_cf_clear` paragraph added in commit 6184404 + add watch tools to the Screencast table), `CHANGELOG.md` (Unreleased entries)
- Read-only: `tests/test_e2e_cf_clear.py` (live contract test)

**Interfaces:**
- Consumes: Task 1 + Task 2 commits.
- Produces: docs entries, CHANGELOG entries, live-run evidence.

- [ ] **Step 1: Re-run full relevant suite.** Run: `.venv/bin/python -m pytest tests/test_cf_clear.py tests/test_watch_server.py -q` → PASS. Run ruff over the three touched source files → clean. Record outputs.
- [ ] **Step 2: Live attempt (evidence, not gate).** Run with the MCP server live (browser + network required): `timeout 150 .venv/bin/python -m pytest tests/test_e2e_cf_clear.py -q`. Record PASS/FAIL + returned `method`/`clicks`. PASS required only on contract shape (`cleared`+`method`/`action` keys), NOT on `cleared:true` — CF may still refuse; a refused/timeout with evidence is an honest PASS of the contract. If the e2e file asserts `cleared:true`, loosen it to contract-shape (edit + note in commit).
- [ ] **Step 3: Live-watch smoke.** Start browser → navigate `https://nopecha.com/demo/cloudflare` → `screencast_start` → `watch_start` → `curl -s http://127.0.0.1:{port}/snapshot.jpg | head -c 3 | xxd` must show `ffd8ff` (JPEG magic). Stop watch + browser. Record result.
- [ ] **Step 4: Docs + CHANGELOG.** `docs/juggler-ai-native.md`: update cf paragraph (native-event click, frame-anchored coords, FakeShadowRoot-style shadow walk, 5×3s jittered retries, cf_clearance-required evidence) + Screencast table rows for `kahin_mirage_watch_start/stop`. `CHANGELOG.md` Unreleased: `feat(cf)` rewrite line + `feat(watch)` line.
- [ ] **Step 5: Commit.** `git add docs/juggler-ai-native.md CHANGELOG.md <any e2e edit> && git commit -m "docs+verify: TR-contract rewrite evidence, watch tools, changelog"`. NO PUSH.
