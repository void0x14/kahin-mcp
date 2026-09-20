//! Kahin sidecar: Juggler-native method-based JSON-over-stdio IPC in front
//! of the Juggler pipe driver (MASTER-PLAN Faz 5, Faz 9 Task 2).
//! `kahin/the_twins/mirage.py` spawns this binary and speaks
//! newline-delimited JSON over stdin/stdout; this process owns the real
//! Camoufox browser via the driver's Juggler pipe.
//!
//! Build (from camoufox-harness/core):
//!   zig build-exe --dep driver -Mroot=ipc_main.zig -Mdriver=driver.zig \
//!     -O ReleaseSafe -femit-bin=zig-out/bin/kahin-sidecar
//! Usage: kahin-sidecar <firefox-binary> [profile-dir]
//!
//! Wire protocol (one JSON object per line) — Juggler-native method wire;
//! the CDP-shaped {domain,command} request layer is gone:
//!   req  -> {"id":N,"method":"<Domain.method>","params":{...},"sessionId?":"..."}
//!   resp -> {"id":N,"result":{...}} | {"id":N,"error":{"code":N,"message":"..."}}
//!   evt  -> {"method":"<M>","params":{...},"sessionId":"..."}
//!           (Juggler event names forwarded VERBATIM — no CDP translation)
//!
//! Routing:
//!   Browser.health          -> answered locally (no wire; process health)
//!   Browser.close           -> Browser.close + sidecar shutdown
//!   Browser.newPage         -> driver newPage (registers + tracks current target)
//!   Page.navigate           -> driver navigate (frameId/loaderId shape)
//!   Page.captureScreenshot  -> Page.screenshot translation (mimeType/clip)
//!   Page.getFrameTree       -> derived from driver state (CDP name)
//!   Runtime.evaluate        -> driver evaluate with evaluateWithRetry (context race)
//!   Browser.*               -> root-session passthrough
//!   Page.*/Runtime.*/Network.*/Heap.* -> page-session passthrough
//!        (request sessionId ?? current page ?? error -32600)
//!   anything else           -> root-session passthrough so the real Juggler
//!        -32601 "Method not found" propagates back unchanged.
//!
//! No-op Network.enable / Console.enable handlers were REMOVED (Faz 9): the
//! calls now forward to the real Juggler domain (Juggler has Network.enable;
//! unknown methods like Console.enable surface the genuine -32601 error).
//!
//! Events are forwarded by an idle drain: browser bytes are copied into the
//! driver's read buffer as they are read; complete messages are replayed
//! into the driver state AND emitted upward. The driver's pump also tees
//! every event it dispatches while a call is in flight (response pending)
//! into a private buffer, flushed upward once the call completes — so
//! console/network events are not lost to Python during Runtime.evaluate
//! and friends (Faz 9 Task 3 console collection).

const std = @import("std");
const linux = std.os.linux;
const Allocator = std.mem.Allocator;

const driver_mod = @import("driver.zig");
const pipe = @import("src/transport/pipe.zig");
const process_manager = @import("process-manager/lifecycle.zig");

const max_line: usize = 16 * 1024 * 1024;
const request_timeout_ms: i32 = 30_000;
const chunk_size: usize = 64 * 1024;
const max_sink_bytes: usize = 8 * 1024 * 1024;
// Keep JSON responses below the browser/sidecar serialization cliff. A
// Runtime.evaluate value or a diagnostic tree can be much smaller than the
// stdin line limit and still make Camoufox/Juggler die while serializing it.
// Screenshots use a separate, larger budget in formatTaskOk.
const max_json_response_bytes: usize = 512 * 1024;
const max_evaluate_value_bytes: usize = 512 * 1024;
// Accessibility.getFullAXTree has no wire-level maxNodes parameter. Allow
// its bounded Python adapter to receive a larger raw tree so it can trim the
// real AX nodes before returning them to the agent; every other passthrough
// remains on the strict 512 KiB cap.
const max_accessibility_response_bytes: usize = 8 * 1024 * 1024;
const max_screenshot_response_bytes: usize = 48 * 1024 * 1024;

var line_buf: std.array_list.Aligned(u8, null) = .empty;
/// Set by Browser.close; the sidecar shuts down with the browser.
var running: bool = true;
/// Set when stdin hit clean EOF (read 0). The sidecar keeps servicing
/// in-flight requests after EOF (the perf probe pipelines its batch and
/// closes stdin) and exits once none remain.
var stdin_eof: bool = false;
/// Raw events the driver's pump dispatched while a call was in flight,
/// appended \x00-framed by the event sink (non-allocating call path) and
/// flushed upward by flushSinkEvents once the call completes. Without this
/// the pump consumes such events into driver state and they never reach
/// Python (Task 3 console collection data loss).
var sink_buf: std.array_list.Aligned(u8, null) = .empty;
var sink_dropped_events: usize = 0;
/// targetId of the page the caller last created; page-scoped commands run on it.
var current_target: ?[]u8 = null;

// ---- Faz 4 Task 2: async request registry ------------------------------

/// Cap on requests in flight. Bounds the browser pipe's write pressure
/// (each wire request is a few hundred bytes; 128 stays far below the 64 KiB
/// pipe capacity, so writes never block) and the pending map size.
const max_inflight: usize = 128;

const TaskKind = enum {
    passthrough,
    evaluate,
    navigate,
    new_page,
    screenshot,
    close,
};

/// The flow a task is currently driving. `none` = staging (screenshot
/// waiting for the main frame) or an immediately-failed task.
const Flow = union(enum) {
    none,
    wire: *driver_mod.WireCall,
    eval: *driver_mod.Driver.EvalFlow,
    nav: *driver_mod.Driver.NavFlow,
    page: *driver_mod.Driver.NewPageFlow,
};

/// One client request from the moment it is read off stdin until its
/// response is written. Heap-owned; freed on completion, on failure, or at
/// shutdown. All pointer fields are owned copies (the per-turn parse arena
/// is reset every loop iteration).
const Task = struct {
    ipc_id: u32,
    kind: TaskKind,
    deadline_ms: i64,
    flow: Flow = .none,
    /// Page target/session for response formatting and staging (owned).
    target_id: ?[]u8 = null,
    session_id: ?[]u8 = null,
    accessibility_tree: bool = false,
    /// Screenshot staging.
    shot_stage: u8 = 0, // 0 = ensure main frame, 1 = measure, 2 = shoot
    full_page: bool = false,
    mime: []const u8 = "image/png",
    quality: ?i64 = null,
    omit_dsf: ?bool = null,
    clip_payload: ?[]u8 = null, // owned; user-supplied clip JSON
    size: ?driver_mod.Size = null,
    done: bool = false,
    failed: bool = false,
    err_msg: []const u8 = "",

    fn fail(self: *Task, message: []const u8) void {
        self.failed = true;
        self.err_msg = message;
    }
};

pub fn main(args: std.process.Init.Minimal) u8 {
    run(args) catch |err| {
        std.debug.print("error: {s}\n", .{@errorName(err)});
        return 1;
    };
    return 0;
}

/// Own the sidecar shutdown sequence. The browser must be stopped before the
/// Driver is deinitialized, and all process-global buffers must be released
/// even when the loop exits through an I/O error (not only clean EOF).
fn cleanupRun(d: *driver_mod.Driver, a: Allocator) !void {
    var stop_error: ?anyerror = null;
    if (d.stop()) |_| {} else |err| switch (err) {
        // A second cleanup pass is safe after the first pass reaped the
        // child.  All other stop errors remain visible to the caller.
        error.AlreadyStopped => {},
        else => stop_error = err,
    }

    if (current_target) |t| a.free(t);
    current_target = null;

    sink_buf.deinit(a);
    sink_buf = .empty;
    sink_dropped_events = 0;
    line_buf.deinit(a);
    line_buf = .empty;
    stdin_eof = false;
    running = false;

    if (stop_error) |err| return err;
}

fn run(args: std.process.Init.Minimal) !void {
    const argv = args.args.vector;
    if (argv.len < 2) {
        std.debug.print("usage: kahin-sidecar <firefox-binary> [profile-dir]\n", .{});
        return error.InvalidArgs;
    }
    const exe = std.mem.sliceTo(argv[1], 0);
    var visible = false;
    var verbose = false;
    const prof_arg: ?[]const u8 = blk: {
        var i: usize = 2;
        while (i < argv.len) : (i += 1) {
            const a = std.mem.sliceTo(argv[i], 0);
            if (std.mem.eql(u8, a, "--visible")) {
                visible = true;
                continue;
            }
            if (std.mem.eql(u8, a, "--verbose")) {
                verbose = true;
                continue;
            }
            break :blk a;
        }
        break :blk null;
    };
    var prof_buf: [64]u8 = undefined;
    const profile: ?[]const u8 = prof_arg orelse
        std.fmt.bufPrint(&prof_buf, "/tmp/kahin-sidecar-{d}", .{linux.getpid()}) catch "kahin-sidecar-default";

    ignoreSigpipe();
    setNonblockingStdin();
    running = true;

    var gpa = std.heap.DebugAllocator(.{}).init;
    defer _ = gpa.deinit();
    const a = gpa.allocator();

    var d = try driver_mod.Driver.start(a, exe, profile, verbose, visible);
    var cleaned = false;
    // Register Driver.deinit before the fallback cleanup: defers run in
    // reverse order, so the process is stopped before Driver frees the
    // Instance that owns it.  An unrecoverable cleanup failure is fatal;
    // continuing would leave a browser process orphaned behind the sidecar.
    defer d.deinit();
    defer {
        if (!cleaned) {
            cleanupRun(&d, a) catch |err| switch (err) {
                error.ChildSignaled, error.AlreadyStopped => {},
                else => {
                    std.debug.print("fatal: sidecar cleanup failed: {s}\n", .{@errorName(err)});
                    @panic("sidecar cleanup failed");
                },
            };
        }
    }

    // Tee every event the driver's pump dispatches (calls in flight) into
    // sink_buf; flushed upward after each request completes.
    var sink_alloc = a;
    d.event_sink = &eventSink;
    d.event_sink_ctx = @ptrCast(&sink_alloc);

    var arena = std.heap.ArenaAllocator.init(a);
    defer arena.deinit();

    // Faz 4 Task 2: every request becomes a Task; the loop owns polling on
    // both fds and advances all flows once per turn. send() is never
    // called here, so stdin processing never blocks on a browser reply and
    // multiple wire calls are in flight at once.
    var inflight: std.array_list.Aligned(*Task, null) = .empty;
    defer {
        for (inflight.items) |t| freeTask(&d, a, t);
        inflight.deinit(a);
    }

    var pollfds = [_]linux.pollfd{
        .{ .fd = 0, .events = linux.POLL.IN, .revents = 0 },
        .{ .fd = d.reader.fd, .events = linux.POLL.IN, .revents = 0 },
    };
    var browser_gone = false;
    while (running) {
        _ = arena.reset(.retain_capacity);

        // Poll timeout = the nearest task deadline/settle, so timeouts fire
        // even when both fds are silent; -1 blocks (no tasks in flight).
        const poll_ms = nextPollTimeoutMs(&inflight);
        const prc = linux.poll(&pollfds, pollfds.len, poll_ms);
        switch (linux.errno(prc)) {
            .SUCCESS => {},
            .INTR => continue,
            else => return error.PollFailed,
        }

        // Responses/events accumulate in `out`; flushed once per iteration
        // (single write sequence per turn).
        var out: std.array_list.Aligned(u8, null) = .empty;
        defer out.deinit(a);

        // 1. Browser bytes: read + replay into driver state. dispatchRaw
        // resolves in-flight wire calls by id and tees events into the sink.
        if (pollfds[1].revents & (linux.POLL.IN | linux.POLL.HUP | linux.POLL.ERR | linux.POLL.NVAL) != 0) {
            readChunk(&d, a) catch |err| switch (err) {
                error.ReadFailed => browser_gone = true, // pipe unusable
                else => return err,
            };
            if (pollfds[1].revents & (linux.POLL.HUP | linux.POLL.ERR | linux.POLL.NVAL) != 0) {
                browser_gone = true;
            }
        }
        try forwardFromBuf(&d);

        // 2. Stdin: start as many requests as the in-flight cap allows.
        // Poll said readable, so the reads here never block (O_NONBLOCK).
        if (!stdin_eof and inflight.items.len < max_inflight and
            (pollfds[0].revents & (linux.POLL.IN | linux.POLL.HUP | linux.POLL.NVAL)) != 0)
        {
            var live = true;
            while (live and !stdin_eof and inflight.items.len < max_inflight) {
                const line = nextStdinLine(a) catch |err| {
                    running = false;
                    return err;
                };
                if (line) |l| {
                    defer a.free(l);
                    try startTask(&d, a, arena.allocator(), l, &out, &inflight);
                } else {
                    live = false;
                }
            }
        }

        // 3. Service in-flight tasks. Browser death fails everything with
        // the same per-kind messages the synchronous handlers produced.
        if (browser_gone) {
            for (inflight.items) |t| {
                try respondErr(a, &out, t.ipc_id, -32000, failureMessage(t));
                freeTask(&d, a, t);
            }
            inflight.clearRetainingCapacity();
            running = false;
        } else {
            // Advance until no task makes progress: a stage transition
            // (screenshot measure -> shoot) must drive its next wire call
            // in the same turn, not after the next poll wakeup.
            var progressed = true;
            while (progressed) {
                progressed = false;
                var i: usize = 0;
                while (i < inflight.items.len) {
                    const t = inflight.items[i];
                    const before = taskFingerprint(t);
                    advanceTask(&d, t);
                    if (t.done) {
                        try formatTaskOk(&d, a, t, &out);
                        freeTask(&d, a, t);
                        _ = inflight.swapRemove(i);
                        progressed = true;
                        continue;
                    }
                    if (t.failed) {
                        try respondErr(a, &out, t.ipc_id, -32000, t.err_msg);
                        freeTask(&d, a, t);
                        _ = inflight.swapRemove(i);
                        progressed = true;
                        continue;
                    }
                    if (taskFingerprint(t) != before) progressed = true;
                    i += 1;
                }
            }
        }

        // 4. Emit events the driver dispatched this turn (sink path), keep
        // the current-target bookkeeping fresh, then flush everything once.
        try flushSinkEvents(a, &out);
        refreshCurrentTarget(&d, a);
        writeAllStdout(out.items) catch |err| switch (err) {
            error.BrokenPipe => return, // client went away: shut down cleanly
            else => return err,
        };

        // Stdin EOF with nothing left in flight: take the browser down
        // (the perf probe closes stdin after its pipelined batch and
        // expects the sidecar to drain, answer, and exit 0).
        if (stdin_eof and inflight.items.len == 0) break;
    }

    // Surface a failed reap/kill to main() after all process-global buffers
    // have been released.  `cleaned` prevents the fallback defer from
    // attempting to free those buffers a second time.
    const cleanup_result = cleanupRun(&d, a);
    cleaned = true;
    try cleanup_result;
}

/// Route one method-based request. Sidecar-local methods (health, close,
/// frame tree) answer synchronously; everything else becomes an in-flight
/// Task whose response is produced by the main loop once its flow resolves.
fn startTask(d: *driver_mod.Driver, a: Allocator, aa: Allocator, line: []const u8, out: *std.array_list.Aligned(u8, null), inflight: *std.array_list.Aligned(*Task, null)) !void {
    const parsed = std.json.parseFromSlice(std.json.Value, aa, line, .{}) catch {
        try respondErr(a, out, 0, -32700, "parse error");
        return;
    };
    const root = parsed.value;
    if (root != .object) {
        try respondErr(a, out, 0, -32600, "request must be an object");
        return;
    }
    const obj = root.object;
    const id_v = obj.get("id") orelse {
        try respondErr(a, out, 0, -32600, "missing id");
        return;
    };
    if (id_v != .integer) {
        try respondErr(a, out, 0, -32600, "id must be an integer");
        return;
    }
    // Range-check before the cast: @intCast traps on negative or >u32 ids
    // in ReleaseSafe, taking the whole sidecar down.
    const id_raw: i64 = id_v.integer;
    if (id_raw < 0 or id_raw > 0xFFFF_FFFF) {
        try respondErr(a, out, 0, -32600, "id out of range");
        return;
    }
    const id: u32 = @intCast(id_raw);
    const method_v = obj.get("method") orelse {
        try respondErr(a, out, id, -32600, "missing method");
        return;
    };
    if (method_v != .string) {
        try respondErr(a, out, id, -32600, "method must be a string");
        return;
    }
    const method = method_v.string;
    const params = if (obj.get("params")) |p| p else std.json.Value{ .object = .empty };
    if (params != .object) {
        try respondErr(a, out, id, -32602, "params must be an object");
        return;
    }
    const session_v = obj.get("sessionId");
    const session_id: ?[]const u8 = if (session_v) |sv| blk: {
        if (sv != .string or sv.string.len == 0) {
            try respondErr(a, out, id, -32600, "sessionId must be a non-empty string");
            return;
        }
        break :blk sv.string;
    } else null;

    // === Sidecar-local / driver-specialized methods (no raw wire) ===
    if (std.mem.eql(u8, method, "Browser.health")) return handleHealth(d, a, out, id);
    if (std.mem.eql(u8, method, "Browser.close")) return handleClose(d, a, out, id);
    if (std.mem.eql(u8, method, "Browser.newPage")) return startNewPage(d, a, inflight, id, params);
    if (std.mem.eql(u8, method, "Page.navigate")) return startNavigate(d, a, out, inflight, id, params, session_id);
    if (std.mem.eql(u8, method, "Page.captureScreenshot")) return startScreenshot(d, a, out, inflight, id, params, session_id);
    if (std.mem.eql(u8, method, "Page.getFrameTree")) return handleFrameTree(d, a, out, id, session_id);
    if (std.mem.eql(u8, method, "Runtime.evaluate")) return startEvaluate(d, a, out, inflight, id, params, session_id);

    // === Default routing ===
    // Browser.* and unknown methods -> root session; page domains ->
    // request sessionId, else the current page, else -32600.
    return startPassthrough(d, a, out, inflight, id, session_id, method, params);
}

/// Page-oriented Juggler method families: forwarded on the page session.
/// Accessibility (Camoufox fork, targets ['page'] — vendored Protocol.js)
/// joins the page families; on the root session its handler is absent
/// ("Handler for does not implement method Accessibility.getFullAXTree").
fn isPageDomain(method: []const u8) bool {
    return std.mem.startsWith(u8, method, "Page.") or
        std.mem.startsWith(u8, method, "Runtime.") or
        std.mem.startsWith(u8, method, "Network.") or
        std.mem.startsWith(u8, method, "Heap.") or
        std.mem.startsWith(u8, method, "Accessibility.");
}

/// Raw forward to the browser: one async wire call; the Juggler response
/// (result OR its genuine error, e.g. -32601 "Method not found") is
/// re-wrapped in the IPC envelope when the main loop resolves the task.
fn startPassthrough(d: *driver_mod.Driver, a: Allocator, out: *std.array_list.Aligned(u8, null), inflight: *std.array_list.Aligned(*Task, null), id: u32, session_id: ?[]const u8, method: []const u8, params: std.json.Value) !void {
    const target_session: ?[]const u8 = if (isPageDomain(method))
        session_id orelse blk: {
            const p = currentPage(d) orelse {
                try respondErr(a, out, id, -32600, "no page session");
                return;
            };
            break :blk p.session_id;
        }
    else
        null;

    const params_json = try std.json.Stringify.valueAlloc(a, params, .{});
    defer a.free(params_json);
    const t = try allocTask(a, id, .passthrough);
    t.accessibility_tree = std.mem.eql(u8, method, "Accessibility.getFullAXTree");
    t.flow = .{ .wire = d.sendAsync(target_session, method, params_json, request_timeout_ms) catch {
        a.destroy(t);
        try respondErr(a, out, id, -32000, "Juggler call failed");
        return;
    } };
    try inflight.append(a, t);
}

// === Sidecar-local handlers ===

/// Browser.health — process-manager status, no Juggler wire. Works even
/// when the browser is gone (alive=false).
fn handleHealth(d: *driver_mod.Driver, a: Allocator, out: *std.array_list.Aligned(u8, null), id: u32) !void {
    const alive = if (d.instance) |inst| inst.health() == .healthy else false;
    const pid: i32 = if (d.instance) |inst| inst.child.pid else -1;
    const state: []const u8 = if (alive) "running" else "dead";
    const json = try std.json.Stringify.valueAlloc(a, .{ .alive = alive, .pid = pid, .state = state }, .{});
    defer a.free(json);
    return respondOk(a, out, id, json);
}

/// Browser.close — browser exits before replying; the sidecar shuts down
/// with it.
fn handleClose(d: *driver_mod.Driver, a: Allocator, out: *std.array_list.Aligned(u8, null), id: u32) !void {
    // Browser.close is terminal: Juggler may tear down its pipe without
    // sending a response. The cleanup defer closes/reaps the child, so do
    // not hold the IPC loop for the normal request timeout here.
    d.close(0) catch |err| switch (err) {
        // Browser.close is terminal: a normal Juggler implementation may
        // close the pipe before its response reaches the sidecar. These are
        // successful shutdown outcomes, not a reason to report a false MCP
        // failure. Other transport errors remain visible.
        error.WaitTimeout, error.BrowserClosed => {},
        else => {
            try respondErr(a, out, id, -32000, @errorName(err));
            running = false;
            return;
        },
    };
    try respondOk(a, out, id, "{}");
    running = false;
}

/// Browser.newPage — async flow: wire call, attachedToTarget wait,
/// optional navigate; the response records the page as the current target.
fn startNewPage(d: *driver_mod.Driver, a: Allocator, inflight: *std.array_list.Aligned(*Task, null), id: u32, params: std.json.Value) !void {
    const ctx = getStringParam(params, "browserContextId");
    const url = getStringParam(params, "url");
    const t = try allocTask(a, id, .new_page);
    t.flow = .{ .page = try driver_mod.Driver.NewPageFlow.init(d, ctx, url, request_timeout_ms) };
    try inflight.append(a, t);
}

// === Page / Runtime specialized handlers ===

/// The page a session-scoped call operates on: the request's explicit
/// sessionId, else the current (last created) page.
fn resolvePageFor(d: *driver_mod.Driver, session_id: ?[]const u8) ?*driver_mod.Page {
    if (session_id) |sid| return d.by_session.get(sid);
    return currentPage(d);
}

/// Page.navigate — async NavFlow (lifecycle + load gate); the response
/// carries the CDP-compatible {frameId, loaderId} shape.
fn startNavigate(d: *driver_mod.Driver, a: Allocator, out: *std.array_list.Aligned(u8, null), inflight: *std.array_list.Aligned(*Task, null), id: u32, params: std.json.Value, session_id: ?[]const u8) !void {
    const url = getStringParam(params, "url") orelse {
        try respondErr(a, out, id, -32602, "missing url");
        return;
    };
    const p = resolvePageFor(d, session_id) orelse {
        try respondErr(a, out, id, -32600, "no page session");
        return;
    };
    const t = try allocTask(a, id, .navigate);
    t.flow = .{ .nav = try driver_mod.Driver.NavFlow.init(d, p.target_id, p.session_id, url, request_timeout_ms) };
    t.target_id = try a.dupe(u8, p.target_id);
    try inflight.append(a, t);
}

/// Page.screenshot translation: {format, fullPage, clip} ->
/// {mimeType, clip, quality, omitDeviceScaleFactor}. Juggler has no
/// fullPage flag, so CDP fullPage=true becomes a full-content clip; the
/// default clip is the page's REAL viewport size (measured via an async
/// evaluate, mirroring driver.pageClipSize), not a hardcoded guess. The
/// shot itself is an async wire call.
fn startScreenshot(d: *driver_mod.Driver, a: Allocator, out: *std.array_list.Aligned(u8, null), inflight: *std.array_list.Aligned(*Task, null), id: u32, params: std.json.Value, session_id: ?[]const u8) !void {
    const format = getStringParam(params, "format") orelse "png";
    if (!std.mem.eql(u8, format, "png") and !std.mem.eql(u8, format, "jpeg")) {
        try respondErr(a, out, id, -32602, "format must be png or jpeg");
        return;
    }
    const clip_override = if (params == .object) params.object.get("clip") else null;
    if (clip_override) |clip| {
        if (validateScreenshotClip(clip)) |message| {
            try respondErr(a, out, id, -32602, message);
            return;
        }
    }
    const p = resolvePageFor(d, session_id) orelse {
        try respondErr(a, out, id, -32600, "no page session");
        return;
    };
    const t = try allocTask(a, id, .screenshot);
    t.mime = if (std.mem.eql(u8, format, "jpeg")) "image/jpeg" else "image/png";
    t.quality = getIntParam(params, "quality");
    t.omit_dsf = getBoolParam(params, "omitDeviceScaleFactor");
    t.full_page = getBoolParam(params, "fullPage") orelse false;
    t.target_id = try a.dupe(u8, p.target_id);
    t.session_id = try a.dupe(u8, p.session_id);
    if (clip_override) |clip| {
        t.clip_payload = try std.json.Stringify.valueAlloc(a, clip, .{});
    }
    try inflight.append(a, t);
}

/// Page.getFrameTree — CDP name; derived from driver frame state (Juggler
/// has frameTree, but this keeps the CDP-shaped answer the oracle expects).
fn handleFrameTree(d: *driver_mod.Driver, a: Allocator, out: *std.array_list.Aligned(u8, null), id: u32, session_id: ?[]const u8) !void {
    const p = resolvePageFor(d, session_id) orelse {
        try respondErr(a, out, id, -32600, "no page session");
        return;
    };
    const tree = d.getFrameTree(p.target_id) catch |err| switch (err) {
        error.UnknownTarget => {
            try respondErr(a, out, id, -32600, "no page session");
            return;
        },
        error.NoMainFrame, error.UnknownFrame => {
            try respondErr(a, out, id, -32000, "no frame tree");
            return;
        },
        else => {
            try respondErr(a, out, id, -32000, "frame tree failed");
            return;
        },
    };
    defer a.free(tree);
    return respondOk(a, out, id, tree);
}

/// Runtime.evaluate — async EvalFlow (context wait + one retry, mirroring
/// the removed evaluateWithRetry); response formatting happens in
/// formatTaskOk once the flow resolves.
fn startEvaluate(d: *driver_mod.Driver, a: Allocator, out: *std.array_list.Aligned(u8, null), inflight: *std.array_list.Aligned(*Task, null), id: u32, params: std.json.Value, session_id: ?[]const u8) !void {
    const expr = getStringParam(params, "expression") orelse {
        try respondErr(a, out, id, -32602, "missing expression");
        return;
    };
    const p = resolvePageFor(d, session_id) orelse {
        try respondErr(a, out, id, -32600, "no page session");
        return;
    };
    const t = try allocTask(a, id, .evaluate);
    const flow = try driver_mod.Driver.EvalFlow.init(d, p.target_id, p.session_id, expr, request_timeout_ms, 1);
    // Honor an explicit executionContextId: without this the driver's
    // pickContext silently re-targets every frame-scoped evaluation to
    // the main frame. Absent id keeps the legacy main-frame behavior.
    if (getStringParam(params, "executionContextId")) |ctx_id| {
        flow.pinContext(ctx_id) catch {
            d.allocator.destroy(flow);
            a.destroy(t);
            try respondErr(a, out, id, -32600, "could not pin execution context");
            return;
        };
    }
    t.flow = .{ .eval = flow };
    try inflight.append(a, t);
}

// === Async task servicing (Faz 4 Task 2) ===

fn allocTask(a: Allocator, ipc_id: u32, kind: TaskKind) !*Task {
    const t = try a.create(Task);
    t.* = .{ .ipc_id = ipc_id, .kind = kind, .deadline_ms = nowMs() + request_timeout_ms };
    return t;
}

/// Advance one task's flow one step. Called after the loop drained browser
/// data, so resolved wire calls are visible.
fn advanceTask(d: *driver_mod.Driver, t: *Task) void {
    switch (t.kind) {
        .passthrough => {
            const call = t.flow.wire;
            if (nowMs() >= call.deadline_ms) {
                d.cancelWireCall(call);
                t.flow = .none;
                t.fail("Juggler call failed");
                return;
            }
            if (call.done) t.done = true;
        },
        .evaluate => {
            const f = t.flow.eval;
            f.poll();
            if (f.done) {
                t.done = true;
            } else if (f.failed) {
                t.err_msg = if (f.err == error.NoExecutionContext) "no execution context" else "evaluate failed";
                t.failed = true;
            }
        },
        .navigate => {
            const f = t.flow.nav;
            f.poll();
            if (f.done) {
                t.done = true;
            } else if (f.failed) {
                t.err_msg = if (f.err == error.NavigationAborted) (f.abort_text orelse "navigation aborted") else "navigate failed";
                t.failed = true;
            }
        },
        .new_page => {
            const f = t.flow.page;
            f.poll();
            if (f.done) {
                t.done = true;
            } else if (f.failed) {
                t.err_msg = if (f.err == error.WaitTimeout)
                    "newPage timed out"
                else if (f.err == error.TargetNotAttached)
                    "new page never attached"
                else
                    "newPage failed";
                t.failed = true;
            }
        },
        .screenshot => advanceScreenshot(d, t),
        .close => unreachable,
    }
    t.deadline_ms = taskDeadline(t);
}

/// Screenshot staging: ensure the main frame, measure the clip size via an
/// async evaluate, then fire the Page.screenshot wire call.
fn advanceScreenshot(d: *driver_mod.Driver, t: *Task) void {
    switch (t.shot_stage) {
        0 => {
            const p = d.pages.get(t.target_id.?) orelse return t.fail("screenshot failed");
            if (p.main_frame_id == null) return; // wait for the frame
            if (t.clip_payload != null) {
                t.shot_stage = 2;
            } else {
                const expr = if (t.full_page)
                    "[document.documentElement.scrollWidth, document.documentElement.scrollHeight]"
                else
                    "[window.innerWidth, window.innerHeight]";
                // One settle-and-retry: the navigate grace window returns
                // control at commit, so the main-frame execution context can
                // still be the just-destroyed previous document's when the
                // measure starts. The 100ms settle lets the destroy/create
                // events land and the retry re-picks the live context; a
                // genuinely dead page still fails after the single retry.
                const f = driver_mod.Driver.EvalFlow.init(d, t.target_id.?, t.session_id.?, expr, request_timeout_ms, 1) catch return t.fail("could not measure page size");
                t.flow = .{ .eval = f };
                t.shot_stage = 1;
            }
        },
        1 => {
            const f = t.flow.eval;
            f.poll();
            if (f.failed) {
                t.err_msg = if (f.err == error.WaitTimeout) "measuring page size timed out" else "could not measure page size";
                t.failed = true;
                return;
            }
            if (!f.done) return;
            var res = f.takeResult() catch {
                f.deinit();
                t.flow = .none;
                return t.fail("could not measure page size");
            };
            defer res.deinit(d.allocator);
            const size = driver_mod.sizeFromJson(d.allocator, res.value_json) orelse {
                f.deinit();
                t.flow = .none;
                return t.fail("could not measure page size");
            };
            f.deinit();
            t.flow = .none;
            t.size = size;
            t.shot_stage = 2;
        },
        else => {
            if (t.flow == .none) {
                const p = d.by_session.get(t.session_id.?) orelse return t.fail("screenshot failed");
                const payload = if (t.clip_payload) |cp|
                    d.allocator.dupe(u8, cp) catch return t.fail("screenshot failed")
                else blk: {
                    const s = t.size.?;
                    const Clip = struct { x: f64, y: f64, width: f64, height: f64 };
                    break :blk std.json.Stringify.valueAlloc(
                        d.allocator,
                        .{ .mimeType = t.mime, .clip = Clip{ .x = 0, .y = 0, .width = s.w, .height = s.h }, .quality = t.quality, .omitDeviceScaleFactor = t.omit_dsf },
                        .{ .emit_null_optional_fields = false },
                    ) catch return t.fail("screenshot failed");
                };
                defer d.allocator.free(payload);
                t.flow = .{ .wire = d.sendAsync(p.session_id, "Page.screenshot", payload, request_timeout_ms) catch return t.fail("screenshot failed") };
                return;
            }
            const call = t.flow.wire;
            if (nowMs() >= call.deadline_ms) {
                d.cancelWireCall(call);
                t.flow = .none;
                return t.fail("screenshot failed");
            }
            if (call.done) t.done = true;
        },
    }
}

/// Write the success response for a completed task.
fn formatTaskOk(d: *driver_mod.Driver, a: Allocator, t: *Task, out: *std.array_list.Aligned(u8, null)) !void {
    switch (t.kind) {
        .passthrough => {
            const call = t.flow.wire;
            const cap = if (t.accessibility_tree) max_accessibility_response_bytes else max_json_response_bytes;
            return respondFromRawBounded(a, out, t.ipc_id, call.raw.?, cap);
        },
        .evaluate => {
            const f = t.flow.eval;
            var res = try f.takeResult();
            defer res.deinit(a);
            if (res.value_json.len > max_evaluate_value_bytes) {
                return respondErr(
                    a,
                    out,
                    t.ipc_id,
                    -32000,
                    "Runtime.evaluate result exceeds the bounded sidecar response size",
                );
            }
            if (res.exception_text) |et| {
                const json = try std.json.Stringify.valueAlloc(
                    a,
                    .{
                        .result = .{ .type = "undefined" },
                        .exceptionDetails = .{ .text = et, .stack = res.exception_stack orelse "" },
                    },
                    .{ .emit_null_optional_fields = false },
                );
                defer a.free(json);
                return respondOk(a, out, t.ipc_id, json);
            }
            const json = if (res.value_json.len == 0)
                try a.dupe(u8, "{\"result\":{\"type\":\"undefined\"}}")
            else
                try std.fmt.allocPrint(a, "{{\"result\":{{\"type\":\"{s}\",\"value\":{s}}}}}", .{ jsonType(res.value_json), res.value_json });
            defer a.free(json);
            return respondOk(a, out, t.ipc_id, json);
        },
        .navigate => {
            const f = t.flow.nav;
            const nav_id = try f.takeNavId();
            defer a.free(nav_id);
            const frame_id: []const u8 = if (d.pages.get(t.target_id.?)) |p| (p.main_frame_id orelse "") else "";
            const json = try std.json.Stringify.valueAlloc(a, .{ .frameId = frame_id, .loaderId = nav_id }, .{});
            defer a.free(json);
            return respondOk(a, out, t.ipc_id, json);
        },
        .new_page => {
            const f = t.flow.page;
            const target_id = try f.takeTargetId();
            defer a.free(target_id);
            try setCurrentTarget(a, target_id);
            const json = try std.json.Stringify.valueAlloc(a, .{ .targetId = target_id }, .{});
            defer a.free(json);
            return respondOk(a, out, t.ipc_id, json);
        },
        .screenshot => {
            const call = t.flow.wire;
            return respondFromRawBounded(a, out, t.ipc_id, call.raw.?, max_screenshot_response_bytes);
        },
        .close => unreachable,
    }
}

/// Release a task: cancel its wire call / flows and free owned buffers.
fn freeTask(d: *driver_mod.Driver, a: Allocator, t: *Task) void {
    switch (t.flow) {
        .none => {},
        .wire => |c| d.cancelWireCall(c),
        .eval => |f| f.deinit(),
        .nav => |f| f.deinit(),
        .page => |f| f.deinit(),
    }
    if (t.target_id) |x| a.free(x);
    if (t.session_id) |x| a.free(x);
    if (t.clip_payload) |x| a.free(x);
    a.destroy(t);
}

/// The nearest deadline the loop must wake up for (wire deadlines, flow
/// deadlines, evaluate retry settles).
fn nextPollTimeoutMs(inflight: *std.array_list.Aligned(*Task, null)) i32 {
    var best: ?i64 = null;
    for (inflight.items) |t| {
        var dl = taskDeadline(t);
        if (t.flow == .eval) {
            const settle = t.flow.eval.settle_until_ms;
            if (settle != 0 and settle < dl) dl = settle;
        }
        if (best == null or dl < best.?) best = dl;
    }
    const now = nowMs();
    if (best) |b| {
        const rem = b - now;
        if (rem <= 0) return 0;
        if (rem > 2147483647) return 2147483647;
        return @intCast(rem);
    }
    return -1; // block until fd activity
}

fn taskDeadline(t: *Task) i64 {
    return switch (t.flow) {
        .none => t.deadline_ms,
        .wire => |c| c.deadline_ms,
        .eval => |f| f.deadline_ms,
        .nav => |f| f.deadline_ms,
        .page => |f| f.deadline_ms,
    };
}

/// Progress fingerprint: which flow the task drives and (for screenshots)
/// which stage, packed. The service loop re-advances while it changes, so
/// a stage transition that can immediately send its wire call does so in
/// the same turn instead of waiting for the next poll wakeup.
fn taskFingerprint(t: *Task) u16 {
    return (@as(u16, @intCast(@intFromEnum(std.meta.activeTag(t.flow)))) << 8) | t.shot_stage;
}

/// Per-kind -32000 message when the browser dies with tasks in flight
/// (matches what each synchronous handler produced on a send failure).
fn failureMessage(t: *Task) []const u8 {
    return switch (t.kind) {
        .passthrough => "Juggler call failed",
        .evaluate => "evaluate failed",
        .navigate => "navigate failed",
        .new_page => "newPage failed",
        .screenshot => "screenshot failed",
        .close => "close failed",
    };
}

/// Monotonic time in ms (raw syscall; std.time removed in 0.16).
fn nowMs() i64 {
    var ts: linux.timespec = undefined;
    _ = linux.clock_gettime(linux.CLOCK.MONOTONIC, &ts);
    return @as(i64, @intCast(ts.sec)) * std.time.ms_per_s + @divTrunc(@as(i64, @intCast(ts.nsec)), std.time.ns_per_ms);
}

// === Shared plumbing ===

/// Wrap a Juggler response `{"id":N,"result":...}` / `{"id":N,"error":...}`
/// in the IPC envelope, dropping the Juggler id for ours.
fn respondFromRaw(a: Allocator, out: *std.array_list.Aligned(u8, null), id: u32, raw: []const u8) !void {
    return respondFromRawBounded(a, out, id, raw, max_json_response_bytes);
}

fn respondFromRawBounded(
    a: Allocator,
    out: *std.array_list.Aligned(u8, null),
    id: u32,
    raw: []const u8,
    max_bytes: usize,
) !void {
    if (raw.len > max_bytes) {
        try respondErr(
            a,
            out,
            id,
            -32000,
            "Juggler response exceeds the bounded sidecar response size",
        );
        return;
    }
    const parsed = std.json.parseFromSlice(std.json.Value, a, raw, .{}) catch {
        try respondErr(a, out, id, -32603, "bad Juggler response");
        return;
    };
    defer parsed.deinit();
    const root = parsed.value;
    if (root != .object) {
        try respondErr(a, out, id, -32603, "bad Juggler response");
        return;
    }
    if (root.object.get("error")) |e| {
        if (e != .object) {
            try respondErr(a, out, id, -32603, "Juggler error");
            return;
        }
        const code: i64 = blk: {
            if (e.object.get("code")) |c| if (c == .integer) break :blk c.integer;
            break :blk -32000;
        };
        const msg = blk: {
            if (e.object.get("message")) |m| if (m == .string) break :blk m.string;
            break :blk "Juggler error";
        };
        // Clamp: @intCast traps on out-of-i32 codes in ReleaseSafe.
        const code32: i32 = if (code > std.math.maxInt(i32))
            std.math.maxInt(i32)
        else if (code < std.math.minInt(i32))
            std.math.minInt(i32)
        else
            @intCast(code);
        try respondErr(a, out, id, code32, msg);
        return;
    }
    // No-return Juggler commands (Browser.setCookies, Browser.clearCookies,
    // Browser.setUserAgentOverride, Browser.setDefaultViewport,
    // Browser.clearCache, Page.close, Page.insertText,
    // Page.dispatchMouseEvent, Page.handleDialog, ...) reply with just
    // {"id":N} — neither `result` nor `error` (Playwright tolerates this;
    // a real failure always carries `error`). A well-formed reply with
    // neither key is therefore SUCCESS with an empty result — never -32603.
    // The -32603 path above is reserved for genuinely malformed frames
    // (unparseable JSON, non-object roots).
    const result = root.object.get("result") orelse {
        try respondOk(a, out, id, "{}");
        return;
    };
    const json = try std.json.Stringify.valueAlloc(a, result, .{});
    defer a.free(json);
    try respondOk(a, out, id, json);
}

/// Append one JSON line (newline-terminated) to `out`.
fn writeOut(a: Allocator, out: *std.array_list.Aligned(u8, null), json: []const u8) !void {
    try out.appendSlice(a, json);
    try out.append(a, '\n');
}

fn respondOk(a: Allocator, out: *std.array_list.Aligned(u8, null), id: u32, result_json: []const u8) !void {
    const line = try std.fmt.allocPrint(a, "{{\"id\":{d},\"result\":{s}}}", .{ id, result_json });
    defer a.free(line);
    try writeOut(a, out, line);
}

fn respondErr(a: Allocator, out: *std.array_list.Aligned(u8, null), id: u32, code: i32, message: []const u8) !void {
    const payload = try std.json.Stringify.valueAlloc(a, .{ .code = code, .message = message }, .{});
    defer a.free(payload);
    const line = try std.fmt.allocPrint(a, "{{\"id\":{d},\"error\":{s}}}", .{ id, payload });
    defer a.free(line);
    try writeOut(a, out, line);
}

/// One idle-drain step: read buffered browser data, replay complete
/// messages into the driver state, flush the event sink upward. Shared by
/// the main loop (browser-fd readable) and the router tests.
fn drainIdle(d: *driver_mod.Driver, a: Allocator, out: *std.array_list.Aligned(u8, null)) !void {
    try readChunk(d, a);
    try forwardFromBuf(d);
    try flushSinkEvents(a, out);
    refreshCurrentTarget(d, a);
}

/// Read one chunk from the browser fd into the driver's read buffer. The
/// driver's pump and the forwarder share this single buffer: whatever the
/// pump does not extract is replayed+forwarded by forwardFromBuf, so no
/// event can fall between two buffers.
fn readChunk(d: *driver_mod.Driver, a: Allocator) !void {
    var chunk: [chunk_size]u8 = undefined;
    while (true) {
        const n = linux.read(d.reader.fd, &chunk, chunk.len);
        switch (linux.errno(n)) {
            .SUCCESS => if (n > 0) {
                const slice = chunk[0..n];
                try d.reader.buf.appendSlice(a, slice);
            } else {
                // Clean EOF (read 0): every browser-side write end is closed —
                // the browser exited. Same shutdown signal as POLL.HUP.
                running = false;
            },
            .INTR => continue,
            // A nonblocking descriptor can lose its readiness race.  This is
            // not a transport failure; the next drain can try again.
            .AGAIN => {},
            // EIO/EBADF/etc. mean the browser pipe is unusable.  Do not keep
            // serving requests with a desynchronized driver state.
            else => return error.ReadFailed,
        }
        return;
    }
}

/// Replay every complete message in the driver's read buffer into the
/// driver state (identical to pump's dispatch) and drop it from the
/// buffer; the event sink fires for each event, buffering it for
/// flushSinkEvents — the single upward path. Messages the pump left
/// buffered while a call was in flight (it extracts only what the call
/// needs) are picked up here right after the call, so console/network
/// events are not lost to Python. A trailing partial message stays until
/// the next readChunk completes it. Event names are passed through
/// VERBATIM (Juggler-native; the CDP Runtime.console ->
/// Console.messageAdded translation was removed in Faz 9).
fn forwardFromBuf(d: *driver_mod.Driver) !void {
    while (std.mem.indexOfScalar(u8, d.reader.buf.items, 0)) |idx| {
        const msg = d.reader.buf.items[0..idx];
        try d.dispatchRaw(msg); // driver state first (it must never miss)
        const rest = d.reader.buf.items[idx + 1 ..];
        std.mem.copyForwards(u8, d.reader.buf.items[0..rest.len], rest);
        d.reader.buf.shrinkRetainingCapacity(rest.len);
    }
}

/// Event sink callback (driver.event_sink): the pump dispatched an event
/// while a call was in flight — buffer the raw JSON \x00-framed for
/// flushing once the call returns. `ctx` is the *Allocator from run().
/// Non-raising: OOM drops the event rather than failing the call.
fn eventSink(raw: []const u8, ctx: ?*anyopaque) void {
    const a: *Allocator = @ptrCast(@alignCast(ctx orelse return));
    if (raw.len > max_sink_bytes or sink_buf.items.len > max_sink_bytes -| (raw.len + 1)) {
        sink_dropped_events += 1;
        return;
    }
    sink_buf.appendSlice(a.*, raw) catch return;
    sink_buf.append(a.*, 0) catch return;
}

/// Flush events the sink buffered during a driver call (see eventSink).
fn flushSinkEvents(a: Allocator, out: *std.array_list.Aligned(u8, null)) !void {
    if (sink_dropped_events > 0) {
        const marker = try std.fmt.allocPrint(
            a,
            "{{\"method\":\"Kahin.eventDropped\",\"params\":{{\"count\":{d},\"reason\":\"sink_capacity\"}}}}",
            .{sink_dropped_events},
        );
        defer a.free(marker);
        try writeOut(a, out, marker);
        sink_dropped_events = 0;
    }
    while (std.mem.indexOfScalar(u8, sink_buf.items, 0)) |idx| {
        const msg = sink_buf.items[0..idx];
        // A page can emit a diagnostic event containing a large payload. Do
        // not let event forwarding recreate the serialization cliff that the
        // command response cap protects.
        if (msg.len > max_json_response_bytes or out.items.len >= 2 * max_json_response_bytes) {
            sink_dropped_events += 1;
            const rest = sink_buf.items[idx + 1 ..];
            std.mem.copyForwards(u8, sink_buf.items[0..rest.len], rest);
            sink_buf.shrinkRetainingCapacity(rest.len);
            continue;
        }
        try emitEvent(a, out, msg);
        const rest = sink_buf.items[idx + 1 ..];
        std.mem.copyForwards(u8, sink_buf.items[0..rest.len], rest);
        sink_buf.shrinkRetainingCapacity(rest.len);
    }
    if (sink_buf.items.len > 0) return;
    sink_buf.clearRetainingCapacity();
}

/// Drop a stale current_target: the page the caller last created is gone
/// (Browser.detachedFromTarget was processed by the driver). Keeps
/// session-less Page calls from resolving against a dead target after a
/// kill.
fn refreshCurrentTarget(d: *driver_mod.Driver, a: Allocator) void {
    const t = current_target orelse return;
    if (d.pages.get(t) == null) {
        a.free(t);
        current_target = null;
    }
}

/// Forward one Juggler event upward, names VERBATIM. Only the sessionId is
/// normalized (null when absent).
fn emitEvent(a: Allocator, out: *std.array_list.Aligned(u8, null), msg: []const u8) !void {
    const parsed = std.json.parseFromSlice(std.json.Value, a, msg, .{}) catch return;
    defer parsed.deinit();
    const root = parsed.value;
    if (root != .object) return;
    const method_v = root.object.get("method") orelse return;
    if (method_v != .string) return;
    const session_v = root.object.get("sessionId");
    const session: ?[]const u8 = if (session_v != null and session_v.? == .string) session_v.?.string else null;
    const params = root.object.get("params") orelse std.json.Value{ .object = .empty };

    const Evt = struct { method: []const u8, params: std.json.Value, sessionId: ?[]const u8 = null };
    const json = try std.json.Stringify.valueAlloc(a, Evt{ .method = method_v.string, .params = params, .sessionId = session }, .{ .emit_null_optional_fields = false });
    defer a.free(json);
    try writeOut(a, out, json);
}

/// The page a page-oriented command operates on: the last created/used
/// target (set by Browser.newPage; the caller creates the page explicitly —
/// no implicit about:blank fallback since Faz 9).
fn currentPage(d: *driver_mod.Driver) ?*driver_mod.Page {
    const t = current_target orelse return null;
    return d.pages.get(t);
}

fn setCurrentTarget(a: Allocator, target_id: []const u8) !void {
    if (current_target) |t| a.free(t);
    current_target = try a.dupe(u8, target_id);
}

// === Param helpers ===

fn getStringParam(params: std.json.Value, name: []const u8) ?[]const u8 {
    if (params != .object) return null;
    const v = params.object.get(name) orelse return null;
    if (v != .string) return null;
    return v.string;
}

fn getIntParam(params: std.json.Value, name: []const u8) ?i64 {
    if (params != .object) return null;
    const v = params.object.get(name) orelse return null;
    if (v != .integer) return null;
    return v.integer;
}

fn getNumParam(params: std.json.Value, name: []const u8) ?f64 {
    if (params != .object) return null;
    const v = params.object.get(name) orelse return null;
    return switch (v) {
        .integer => |i| @floatFromInt(i),
        .float => |f| f,
        else => null,
    };
}

fn getBoolParam(params: std.json.Value, name: []const u8) ?bool {
    if (params != .object) return null;
    const v = params.object.get(name) orelse return null;
    if (v != .bool) return null;
    return v.bool;
}

const max_screenshot_dimension: f64 = 32_768;

/// Validate a caller-supplied clip before serializing it to the real
/// browser. Automatically measured clips are bounded by Driver.pageClipSize;
/// this path must enforce the same contract for user input.
fn validateScreenshotClip(clip: std.json.Value) ?[]const u8 {
    if (clip != .object) return "clip must be an object";

    const x = getNumParam(clip, "x") orelse return "clip requires x, y, width, and height";
    const y = getNumParam(clip, "y") orelse return "clip requires x, y, width, and height";
    const width = getNumParam(clip, "width") orelse return "clip requires x, y, width, and height";
    const height = getNumParam(clip, "height") orelse return "clip requires x, y, width, and height";

    if (!std.math.isFinite(x) or !std.math.isFinite(y) or
        !std.math.isFinite(width) or !std.math.isFinite(height)) {
        return "clip values must be finite";
    }
    if (width <= 0 or height <= 0) return "clip width and height must be positive";
    if (width > max_screenshot_dimension or height > max_screenshot_dimension) {
        return "clip width and height exceed maximum 32768";
    }
    return null;
}

/// CDP-ish remote-object type from the serialized JSON value. ponytail:
/// heuristic on the first byte; the oracle does not consume `type`.
fn jsonType(v: []const u8) []const u8 {
    if (v.len == 0) return "undefined";
    return switch (v[0]) {
        '{' => "object",
        '[' => "array",
        '"' => "string",
        't', 'f' => "boolean",
        'n' => "null",
        else => "number",
    };
}

// === stdin/stdout plumbing ===

/// Read one newline-terminated line from stdin (raw syscalls; no stdio
/// buffering). Stdin is O_NONBLOCK, so this never blocks: buffered lines
/// are returned first; only when the buffer holds no complete line does it
/// perform one read (EAGAIN -> null). Clean EOF sets stdin_eof and returns
/// null; a buffered tail without a newline is returned leniently as a final
/// line (same as the old blocking readLine).
fn nextStdinLine(a: Allocator) !?[]u8 {
    while (true) {
        if (std.mem.indexOfScalar(u8, line_buf.items, '\n')) |idx| {
            const line = try a.dupe(u8, line_buf.items[0..idx]);
            const rest = line_buf.items[idx + 1 ..];
            std.mem.copyForwards(u8, line_buf.items[0..rest.len], rest);
            line_buf.shrinkRetainingCapacity(rest.len);
            return line;
        }
        if (line_buf.items.len >= max_line) return error.LineTooLong;

        var chunk: [chunk_size]u8 = undefined;
        const n = linux.read(0, &chunk, chunk.len);
        switch (linux.errno(n)) {
            .SUCCESS => {
                const len: usize = @intCast(n);
                if (len == 0) {
                    stdin_eof = true;
                    if (line_buf.items.len > 0) {
                        const line = try a.dupe(u8, line_buf.items);
                        line_buf.clearRetainingCapacity();
                        return line;
                    }
                    return null;
                }
                try line_buf.appendSlice(a, chunk[0..len]);
            },
            .INTR => continue,
            .AGAIN => return null, // no data right now (O_NONBLOCK)
            else => return error.ReadFailed,
        }
    }
}

/// Put stdin into non-blocking mode so the main loop can drain every
/// pipelined line without ever blocking on a partial read.
fn setNonblockingStdin() void {
    const flags = linux.fcntl(0, linux.F.GETFL, 0);
    if (linux.errno(flags) == .SUCCESS) {
        var oflags: linux.O = @bitCast(@as(u32, @intCast(flags)));
        oflags.NONBLOCK = true;
        _ = linux.fcntl(0, linux.F.SETFL, @as(usize, @intCast(@as(u32, @bitCast(oflags)))));
    }
}

fn writeAllStdout(bytes: []const u8) !void {
    var off: usize = 0;
    while (off < bytes.len) {
        const n = linux.write(1, bytes[off..].ptr, bytes.len - off);
        switch (linux.errno(n)) {
            .SUCCESS => {
                if (n == 0) return error.WriteZero;
                off += n;
            },
            .INTR => continue,
            .PIPE => return error.BrokenPipe,
            else => return error.WriteFailed,
        }
    }
}

fn ignoreSigpipe() void {
    const set: linux.sigset_t = @splat(0);
    var act = linux.Sigaction{
        .handler = .{ .handler = linux.SIG.IGN },
        .mask = set,
        .flags = 0,
    };
    _ = linux.sigaction(linux.SIG.PIPE, &act, null);
}

// === Router tests (Faz 9 Task 2: Juggler-native method wire) ===

const testing = std.testing;

fn testPipe() ![2]i32 {
    var fds: [2]i32 = undefined;
    if (std.os.linux.errno(std.os.linux.pipe2(&fds, .{})) != .SUCCESS) return error.PipeFailed;
    return fds;
}

/// Fake-browser responder thread: read `expected.len` requests, assert each
/// VERBATIM against `expected`, answer with the matching `replies`, then
/// flush `extra` (post-response events). A failed assert aborts the thread;
/// the main side then finds no response and the test fails by timeout.
const FakePeer = struct {
    fn thread(cmd_read: i32, resp_write: i32, expected: []const []const u8, replies: []const []const u8, extra: []const []const u8) void {
        var r = pipe.Reader.init(cmd_read);
        defer r.deinit(testing.allocator);
        for (expected, 0..) |want, i| {
            const msg = (r.readMessage(testing.allocator, 5000) catch return) orelse return;
            defer testing.allocator.free(msg);
            if (!std.mem.eql(u8, msg, want)) {
                std.debug.print("FAKE peer: expected {s}\nFAKE peer: got      {s}\n", .{ want, msg });
                return;
            }
            pipe.writeMessage(testing.allocator, resp_write, replies[i]) catch return;
        }
        for (extra) |ev| {
            pipe.writeMessage(testing.allocator, resp_write, ev) catch return;
        }
    }
};

/// Two-pipe wire rig: `d` reads `resp[0]`, writes `cmd[1]`; the fake browser
/// reads `cmd[0]` and writes `resp[1]`.
fn testRig() !struct { d: driver_mod.Driver, cmd: [2]i32, resp: [2]i32 } {
    var cmd: [2]i32 = undefined;
    var resp: [2]i32 = undefined;
    if (std.os.linux.errno(std.os.linux.pipe2(&cmd, .{})) != .SUCCESS) return error.PipeFailed;
    if (std.os.linux.errno(std.os.linux.pipe2(&resp, .{})) != .SUCCESS) return error.PipeFailed;
    return .{ .d = driver_mod.Driver.init(testing.allocator, resp[0], cmd[1], false), .cmd = cmd, .resp = resp };
}

test "shutdown: stdin EOF cleanup reaps child and releases sidecar state" {
    const inst = try process_manager.Instance.spawnArgv(testing.allocator, &.{ "/bin/true" }, null, false);
    var d = driver_mod.Driver.init(testing.allocator, inst.child.read_fd, inst.child.write_fd, false);
    d.child = inst.child;
    d.instance = inst;
    defer d.deinit();
    defer running = true;

    try testing.expectEqual(@as(usize, 0), line_buf.items.len);
    try testing.expectEqual(@as(usize, 0), sink_buf.items.len);
    try testing.expect(current_target == null);

    try line_buf.appendSlice(testing.allocator, "partial stdin line");
    try sink_buf.appendSlice(testing.allocator, "buffered event");
    current_target = try testing.allocator.dupe(u8, "target-1");
    running = true;

    try cleanupRun(&d, testing.allocator);

    try testing.expectEqual(@as(usize, 0), line_buf.items.len);
    try testing.expectEqual(@as(usize, 0), sink_buf.items.len);
    try testing.expect(current_target == null);
    try testing.expect(!running);
}

test "shutdown: Browser.close returns without waiting for terminal wire reply" {
    const rig = try testRig();
    defer {
        _ = std.os.linux.close(rig.cmd[0]);
        _ = std.os.linux.close(rig.cmd[1]);
        _ = std.os.linux.close(rig.resp[0]);
        _ = std.os.linux.close(rig.resp[1]);
    }
    var d = rig.d;
    defer d.deinit();
    defer running = true;

    const expected = [_][]const u8{"{\"id\":1,\"method\":\"Browser.close\",\"params\":{}}"};
    const reply = [_][]const u8{"{\"id\":1,\"result\":{}}"};
    const extra = [_][]const u8{};
    const thread = try std.Thread.spawn(.{}, FakePeer.thread, .{ rig.cmd[0], rig.resp[1], &expected, &reply, &extra });
    defer thread.join();

    var out: std.array_list.Aligned(u8, null) = .empty;
    defer out.deinit(testing.allocator);
    running = true;
    try handleClose(&d, testing.allocator, &out, 1);

    try testing.expectEqualStrings("{\"id\":1,\"result\":{}}\n", out.items);
    try testing.expect(!running);
}

/// Preload a page session (attachedToTarget + frameAttached) into the driver.
fn seedPage(d: *driver_mod.Driver, resp: [2]i32, target_id: []const u8, session_id: []const u8, frame_id: []const u8) !void {
    const ev1 = try std.fmt.allocPrint(
        testing.allocator,
        "{{\"method\":\"Browser.attachedToTarget\",\"params\":{{\"sessionId\":\"{s}\",\"targetInfo\":{{\"type\":\"page\",\"targetId\":\"{s}\"}}}}}}",
        .{ session_id, target_id },
    );
    defer testing.allocator.free(ev1);
    try pipe.writeMessage(testing.allocator, resp[1], ev1);
    try d.pump(1000);
    if (frame_id.len > 0) {
        const ev2 = try std.fmt.allocPrint(
            testing.allocator,
            "{{\"method\":\"Page.frameAttached\",\"params\":{{\"frameId\":\"{s}\"}},\"sessionId\":\"{s}\"}}",
            .{ frame_id, session_id },
        );
        defer testing.allocator.free(ev2);
        try pipe.writeMessage(testing.allocator, resp[1], ev2);
        try d.pump(1000);
    }
}

/// Run one request through the async pipeline (startTask + pump + advance
/// until the response is produced). Test-only driver of the same state
/// machine the main loop runs: responses are formatted by formatTaskOk,
/// and the sink is deliberately NOT flushed (the sink tests flush it
/// themselves).
fn runRequest(d: *driver_mod.Driver, line: []const u8) ![]const u8 {
    var out: std.array_list.Aligned(u8, null) = .empty;
    defer out.deinit(testing.allocator);
    var arena = std.heap.ArenaAllocator.init(testing.allocator);
    defer arena.deinit();
    var inflight: std.array_list.Aligned(*Task, null) = .empty;
    defer {
        for (inflight.items) |t| freeTask(d, testing.allocator, t);
        inflight.deinit(testing.allocator);
    }
    try startTask(d, testing.allocator, arena.allocator(), line, &out, &inflight);
    const deadline = nowMs() + request_timeout_ms;
    while (inflight.items.len > 0) {
        if (nowMs() >= deadline) return error.WaitTimeout;
        // Advance until no task makes progress: flows start their wire
        // calls here (so the request is in the pipe before the pump below
        // waits for the reply), and screenshot stage transitions drive
        // their next wire call in the same pass.
        var progressed = true;
        while (progressed) {
            progressed = false;
            var i: usize = 0;
            while (i < inflight.items.len) {
                const t = inflight.items[i];
                const before = taskFingerprint(t);
                advanceTask(d, t);
                if (t.done) {
                    try formatTaskOk(d, testing.allocator, t, &out);
                    freeTask(d, testing.allocator, t);
                    _ = inflight.swapRemove(i);
                    progressed = true;
                    continue;
                }
                if (t.failed) {
                    try respondErr(testing.allocator, &out, t.ipc_id, -32000, t.err_msg);
                    freeTask(d, testing.allocator, t);
                    _ = inflight.swapRemove(i);
                    progressed = true;
                    continue;
                }
                if (taskFingerprint(t) != before) progressed = true;
                i += 1;
            }
        }
        if (inflight.items.len == 0) break;
        var rem = nextPollTimeoutMs(&inflight);
        if (rem < 0) rem = 1000;
        const overall = deadline - nowMs();
        if (@as(i64, rem) > overall) rem = @intCast(@max(overall, 1));
        d.pump(rem) catch |err| switch (err) {
            error.WaitTimeout => {}, // flow still settling/waiting
            else => return err,
        };
    }
    return testing.allocator.dupe(u8, out.items);
}

test "router: Browser.health answered locally (no wire)" {
    const rig = try testRig();
    defer {
        _ = std.os.linux.close(rig.cmd[0]);
        _ = std.os.linux.close(rig.cmd[1]);
        _ = std.os.linux.close(rig.resp[0]);
        _ = std.os.linux.close(rig.resp[1]);
    }
    var d = rig.d;
    defer d.deinit();

    const line = try runRequest(&d, "{\"id\":7,\"method\":\"Browser.health\",\"params\":{}}");
    defer testing.allocator.free(line);
    // init-built driver has no process-manager instance -> dead.
    try testing.expectEqualStrings("{\"id\":7,\"result\":{\"alive\":false,\"pid\":-1,\"state\":\"dead\"}}\n", line);
}

test "router: page-session method without a page errors -32600" {
    const rig = try testRig();
    defer {
        _ = std.os.linux.close(rig.cmd[0]);
        _ = std.os.linux.close(rig.cmd[1]);
        _ = std.os.linux.close(rig.resp[0]);
        _ = std.os.linux.close(rig.resp[1]);
    }
    var d = rig.d;
    defer d.deinit();

    // no current target, no sessionId -> -32600, nothing written to the wire
    const line1 = try runRequest(&d, "{\"id\":3,\"method\":\"Page.navigate\",\"params\":{\"url\":\"about:blank\"}}");
    defer testing.allocator.free(line1);
    try testing.expectEqualStrings("{\"id\":3,\"error\":{\"code\":-32600,\"message\":\"no page session\"}}\n", line1);

    // unknown sessionId -> also -32600
    const line2 = try runRequest(&d, "{\"id\":4,\"method\":\"Runtime.evaluate\",\"params\":{\"expression\":\"1+1\"},\"sessionId\":\"nope\"}");
    defer testing.allocator.free(line2);
    try testing.expectEqualStrings("{\"id\":4,\"error\":{\"code\":-32600,\"message\":\"no page session\"}}\n", line2);
}

test "router: Page.navigate forwards to the page session (frameId/loaderId)" {
    const rig = try testRig();
    defer {
        _ = std.os.linux.close(rig.cmd[0]);
        _ = std.os.linux.close(rig.cmd[1]);
        _ = std.os.linux.close(rig.resp[0]);
        _ = std.os.linux.close(rig.resp[1]);
    }
    var d = rig.d;
    defer d.deinit();

    try seedPage(&d, rig.resp, "t1", "s1", "f1");
    try setCurrentTarget(testing.allocator, "t1");
    defer {
        if (current_target) |t| testing.allocator.free(t);
        current_target = null;
    }

    const expected = [_][]const u8{"{\"id\":1,\"sessionId\":\"s1\",\"method\":\"Page.navigate\",\"params\":{\"frameId\":\"f1\",\"url\":\"about:blank\"}}"};
    const reply = [_][]const u8{"{\"id\":1,\"result\":{\"navigationId\":\"nav-1\"}}"};
    const events = [_][]const u8{
        "{\"method\":\"Page.navigationStarted\",\"params\":{\"frameId\":\"f1\",\"navigationId\":\"nav-1\"},\"sessionId\":\"s1\"}",
        "{\"method\":\"Page.navigationCommitted\",\"params\":{\"frameId\":\"f1\",\"navigationId\":\"nav-1\",\"url\":\"about:blank\",\"name\":\"navigate\"},\"sessionId\":\"s1\"}",
        "{\"method\":\"Page.eventFired\",\"params\":{\"frameId\":\"f1\",\"name\":\"load\"},\"sessionId\":\"s1\"}",
    };
    const thread = try std.Thread.spawn(.{}, FakePeer.thread, .{ rig.cmd[0], rig.resp[1], &expected, &reply, &events });
    defer thread.join();

    const line = try runRequest(&d, "{\"id\":1,\"method\":\"Page.navigate\",\"params\":{\"url\":\"about:blank\"}}");
    defer testing.allocator.free(line);
    try testing.expectEqualStrings("{\"id\":1,\"result\":{\"frameId\":\"f1\",\"loaderId\":\"nav-1\"}}\n", line);
}

test "router: page-domain method uses the explicit sessionId" {
    const rig = try testRig();
    defer {
        _ = std.os.linux.close(rig.cmd[0]);
        _ = std.os.linux.close(rig.cmd[1]);
        _ = std.os.linux.close(rig.resp[0]);
        _ = std.os.linux.close(rig.resp[1]);
    }
    var d = rig.d;
    defer d.deinit();

    // page seeded, but NO current target: routing must pick the sessionId
    try seedPage(&d, rig.resp, "t1", "s1", "f1");

    const expected = [_][]const u8{"{\"id\":1,\"sessionId\":\"s1\",\"method\":\"Page.frameTree\",\"params\":{}}"};
    const reply = [_][]const u8{"{\"id\":1,\"result\":{\"frameTree\":{\"frame\":{\"id\":\"f1\"}}}}"};
    const thread = try std.Thread.spawn(.{}, FakePeer.thread, .{ rig.cmd[0], rig.resp[1], &expected, &reply, &[_][]const u8{} });
    defer thread.join();

    const line = try runRequest(&d, "{\"id\":5,\"method\":\"Page.frameTree\",\"params\":{},\"sessionId\":\"s1\"}");
    defer testing.allocator.free(line);
    try testing.expectEqualStrings("{\"id\":5,\"result\":{\"frameTree\":{\"frame\":{\"id\":\"f1\"}}}}\n", line);
}

test "router: unknown method forwards to root and propagates the Juggler error" {
    const rig = try testRig();
    defer {
        _ = std.os.linux.close(rig.cmd[0]);
        _ = std.os.linux.close(rig.cmd[1]);
        _ = std.os.linux.close(rig.resp[0]);
        _ = std.os.linux.close(rig.resp[1]);
    }
    var d = rig.d;
    defer d.deinit();

    // Console.enable used to be a local no-op — it must now reach the
    // browser and surface the real -32601 error (no-op removed).
    const expected = [_][]const u8{"{\"id\":1,\"method\":\"Console.enable\",\"params\":{}}"};
    const reply = [_][]const u8{"{\"id\":1,\"error\":{\"code\":-32601,\"message\":\"Method not found: Console.enable\"}}"};
    const thread = try std.Thread.spawn(.{}, FakePeer.thread, .{ rig.cmd[0], rig.resp[1], &expected, &reply, &[_][]const u8{} });
    defer thread.join();

    const line = try runRequest(&d, "{\"id\":6,\"method\":\"Console.enable\",\"params\":{}}");
    defer testing.allocator.free(line);
    try testing.expectEqualStrings("{\"id\":6,\"error\":{\"code\":-32601,\"message\":\"Method not found: Console.enable\"}}\n", line);
}

test "router: Network.enable is forwarded (no-op removed)" {
    const rig = try testRig();
    defer {
        _ = std.os.linux.close(rig.cmd[0]);
        _ = std.os.linux.close(rig.cmd[1]);
        _ = std.os.linux.close(rig.resp[0]);
        _ = std.os.linux.close(rig.resp[1]);
    }
    var d = rig.d;
    defer d.deinit();

    // Network.enable is a page-domain method: it routes on the request's
    // sessionId (no-op handler was removed — must reach the browser).
    try seedPage(&d, rig.resp, "t1", "s1", "f1");
    const expected = [_][]const u8{"{\"id\":1,\"sessionId\":\"s1\",\"method\":\"Network.enable\",\"params\":{}}"};
    const reply = [_][]const u8{"{\"id\":1,\"result\":{}}"};
    const thread = try std.Thread.spawn(.{}, FakePeer.thread, .{ rig.cmd[0], rig.resp[1], &expected, &reply, &[_][]const u8{} });
    defer thread.join();

    const line = try runRequest(&d, "{\"id\":9,\"method\":\"Network.enable\",\"params\":{},\"sessionId\":\"s1\"}");
    defer testing.allocator.free(line);
    try testing.expectEqualStrings("{\"id\":9,\"result\":{}}\n", line);
}

test "router: Accessibility.getFullAXTree forwards on the page session" {
    const rig = try testRig();
    defer {
        _ = std.os.linux.close(rig.cmd[0]);
        _ = std.os.linux.close(rig.cmd[1]);
        _ = std.os.linux.close(rig.resp[0]);
        _ = std.os.linux.close(rig.resp[1]);
    }
    var d = rig.d;
    defer d.deinit();

    // Accessibility is a Camoufox-only page domain (targets ['page'] in the
    // vendored Protocol.js). The root session has NO handler for it — the
    // browser answers "Handler for does not implement method" — so the
    // sidecar must keep the request on the page session, not drop it to
    // root like unknown methods.
    try seedPage(&d, rig.resp, "t1", "s1", "f1");
    const expected = [_][]const u8{"{\"id\":1,\"sessionId\":\"s1\",\"method\":\"Accessibility.getFullAXTree\",\"params\":{}}"};
    const reply = [_][]const u8{"{\"id\":1,\"result\":{\"tree\":{\"role\":\"document\",\"name\":\"\",\"children\":[{\"role\":\"button\",\"name\":\"Close\"}]}}}"};
    const thread = try std.Thread.spawn(.{}, FakePeer.thread, .{ rig.cmd[0], rig.resp[1], &expected, &reply, &[_][]const u8{} });
    defer thread.join();

    const line = try runRequest(&d, "{\"id\":11,\"method\":\"Accessibility.getFullAXTree\",\"params\":{},\"sessionId\":\"s1\"}");
    defer testing.allocator.free(line);
    try testing.expectEqualStrings(
        "{\"id\":11,\"result\":{\"tree\":{\"role\":\"document\",\"name\":\"\",\"children\":[{\"role\":\"button\",\"name\":\"Close\"}]}}}\n",
        line,
    );
}

test "router: Accessibility.getFullAXTree without sessionId uses the current page" {
    const rig = try testRig();
    defer {
        _ = std.os.linux.close(rig.cmd[0]);
        _ = std.os.linux.close(rig.cmd[1]);
        _ = std.os.linux.close(rig.resp[0]);
        _ = std.os.linux.close(rig.resp[1]);
    }
    var d = rig.d;
    defer d.deinit();

    try seedPage(&d, rig.resp, "t1", "s1", "f1");
    try setCurrentTarget(testing.allocator, "t1");
    defer {
        if (current_target) |t| testing.allocator.free(t);
        current_target = null;
    }

    const expected = [_][]const u8{"{\"id\":1,\"sessionId\":\"s1\",\"method\":\"Accessibility.getFullAXTree\",\"params\":{}}"};
    const reply = [_][]const u8{"{\"id\":1,\"result\":{\"tree\":{\"role\":\"document\"}}}"};
    const thread = try std.Thread.spawn(.{}, FakePeer.thread, .{ rig.cmd[0], rig.resp[1], &expected, &reply, &[_][]const u8{} });
    defer thread.join();

    const line = try runRequest(&d, "{\"id\":12,\"method\":\"Accessibility.getFullAXTree\",\"params\":{}}");
    defer testing.allocator.free(line);
    try testing.expectEqualStrings("{\"id\":12,\"result\":{\"tree\":{\"role\":\"document\"}}}\n", line);
}

test "router: Browser.* methods go to the root session" {
    const rig = try testRig();
    defer {
        _ = std.os.linux.close(rig.cmd[0]);
        _ = std.os.linux.close(rig.cmd[1]);
        _ = std.os.linux.close(rig.resp[0]);
        _ = std.os.linux.close(rig.resp[1]);
    }
    var d = rig.d;
    defer d.deinit();

    const expected = [_][]const u8{"{\"id\":1,\"method\":\"Browser.createBrowserContext\",\"params\":{}}"};
    const reply = [_][]const u8{"{\"id\":1,\"result\":{\"browserContextId\":\"ctx-1\"}}"};
    const thread = try std.Thread.spawn(.{}, FakePeer.thread, .{ rig.cmd[0], rig.resp[1], &expected, &reply, &[_][]const u8{} });
    defer thread.join();

    const line = try runRequest(&d, "{\"id\":8,\"method\":\"Browser.createBrowserContext\",\"params\":{}}");
    defer testing.allocator.free(line);
    try testing.expectEqualStrings("{\"id\":8,\"result\":{\"browserContextId\":\"ctx-1\"}}\n", line);
}

test "router: result-less Juggler reply (no-return command) resolves as empty result" {
    const rig = try testRig();
    defer {
        _ = std.os.linux.close(rig.cmd[0]);
        _ = std.os.linux.close(rig.cmd[1]);
        _ = std.os.linux.close(rig.resp[0]);
        _ = std.os.linux.close(rig.resp[1]);
    }
    var d = rig.d;
    defer d.deinit();

    // Browser.setCookies is a no-return Juggler command: the browser
    // replies {"id":N} with NO result key (and no error). Playwright
    // treats that as success — the sidecar must answer the caller with an
    // empty result instead of -32603 "Juggler response has no result".
    const expected = [_][]const u8{"{\"id\":1,\"method\":\"Browser.setCookies\",\"params\":{\"cookies\":[]}}"};
    const reply = [_][]const u8{"{\"id\":1}"};
    const thread = try std.Thread.spawn(.{}, FakePeer.thread, .{ rig.cmd[0], rig.resp[1], &expected, &reply, &[_][]const u8{} });
    defer thread.join();

    const line = try runRequest(&d, "{\"id\":12,\"method\":\"Browser.setCookies\",\"params\":{\"cookies\":[]}}");
    defer testing.allocator.free(line);
    try testing.expectEqualStrings("{\"id\":12,\"result\":{}}\n", line);
}

test "router: Runtime.evaluate keeps the evaluateWithRetry path" {
    const rig = try testRig();
    defer {
        _ = std.os.linux.close(rig.cmd[0]);
        _ = std.os.linux.close(rig.cmd[1]);
        _ = std.os.linux.close(rig.resp[0]);
        _ = std.os.linux.close(rig.resp[1]);
    }
    var d = rig.d;
    defer d.deinit();

    try seedPage(&d, rig.resp, "t1", "s1", "f1");
    try pipe.writeMessage(
        testing.allocator,
        rig.resp[1],
        "{\"method\":\"Runtime.executionContextCreated\",\"params\":{\"executionContextId\":\"ctx-9\",\"auxData\":{\"frameId\":\"f1\"}},\"sessionId\":\"s1\"}",
    );
    try d.pump(1000);
    try setCurrentTarget(testing.allocator, "t1");
    defer {
        if (current_target) |t| testing.allocator.free(t);
        current_target = null;
    }

    const expected = [_][]const u8{"{\"id\":1,\"sessionId\":\"s1\",\"method\":\"Runtime.evaluate\",\"params\":{\"executionContextId\":\"ctx-9\",\"expression\":\"1+1\",\"returnByValue\":true}}"};
    const reply = [_][]const u8{"{\"id\":1,\"result\":{\"result\":{\"type\":\"number\",\"value\":2}}}"};
    const thread = try std.Thread.spawn(.{}, FakePeer.thread, .{ rig.cmd[0], rig.resp[1], &expected, &reply, &[_][]const u8{} });
    defer thread.join();

    const line = try runRequest(&d, "{\"id\":10,\"method\":\"Runtime.evaluate\",\"params\":{\"expression\":\"1+1\"}}");
    defer testing.allocator.free(line);
    try testing.expectEqualStrings("{\"id\":10,\"result\":{\"result\":{\"type\":\"number\",\"value\":2}}}\n", line);
}

test "router: Runtime.evaluate honors an explicit executionContextId" {
    const rig = try testRig();
    defer {
        _ = std.os.linux.close(rig.cmd[0]);
        _ = std.os.linux.close(rig.resp[1]);
        _ = std.os.linux.close(rig.resp[0]);
        _ = std.os.linux.close(rig.cmd[1]);
    }
    var d = rig.d;
    defer d.deinit();

    try seedPage(&d, rig.resp, "t1", "s1", "main");
    try pipe.writeMessage(
        testing.allocator,
        rig.resp[1],
        "{\"method\":\"Runtime.executionContextCreated\",\"params\":{\"executionContextId\":\"ctx-main\",\"auxData\":{\"frameId\":\"main\"}},\"sessionId\":\"s1\"}",
    );
    try d.pump(1000);
    try pipe.writeMessage(
        testing.allocator,
        rig.resp[1],
        "{\"method\":\"Runtime.executionContextCreated\",\"params\":{\"executionContextId\":\"ctx-sub\",\"auxData\":{\"frameId\":\"sub\"}},\"sessionId\":\"s1\"}",
    );
    try d.pump(1000);
    try setCurrentTarget(testing.allocator, "t1");
    defer {
        if (current_target) |t| testing.allocator.free(t);
        current_target = null;
    }

    // pickContext would choose the LAST main-frame context (or, when the
    // main id is unknown, the newest context overall = ctx-sub). To prove
    // the pin — not the fallback — is honored, both contexts must exist
    // and the wire request must carry the requested one verbatim.
    const expected = [_][]const u8{"{\"id\":1,\"sessionId\":\"s1\",\"method\":\"Runtime.evaluate\",\"params\":{\"executionContextId\":\"ctx-main\",\"expression\":\"2+2\",\"returnByValue\":true}}"};
    const reply = [_][]const u8{"{\"id\":1,\"result\":{\"result\":{\"type\":\"number\",\"value\":4}}}"};
    const thread = try std.Thread.spawn(.{}, FakePeer.thread, .{ rig.cmd[0], rig.resp[1], &expected, &reply, &[_][]const u8{} });
    defer thread.join();

    const line = try runRequest(&d, "{\"id\":11,\"method\":\"Runtime.evaluate\",\"params\":{\"expression\":\"2+2\",\"executionContextId\":\"ctx-main\"}}");
    defer testing.allocator.free(line);
    try testing.expectEqualStrings("{\"id\":11,\"result\":{\"result\":{\"type\":\"number\",\"value\":4}}}\n", line);
}

test "router: events forwarded verbatim (no CDP translation)" {
    const a = testing.allocator;
    var out: std.array_list.Aligned(u8, null) = .empty;
    defer out.deinit(a);

    // Runtime.console stays Runtime.console (Console.messageAdded is gone)
    try emitEvent(a, &out, "{\"method\":\"Runtime.console\",\"params\":{\"type\":\"log\"},\"sessionId\":\"s1\"}");
    try testing.expectEqualStrings("{\"method\":\"Runtime.console\",\"params\":{\"type\":\"log\"},\"sessionId\":\"s1\"}\n", out.items);

    out.clearRetainingCapacity();
    // Browser.attachedToTarget stays Browser.* (no Target. rename)
    try emitEvent(a, &out, "{\"method\":\"Browser.attachedToTarget\",\"params\":{\"sessionId\":\"s1\",\"targetInfo\":{\"type\":\"page\",\"targetId\":\"t1\"}}}");
    try testing.expectEqualStrings("{\"method\":\"Browser.attachedToTarget\",\"params\":{\"sessionId\":\"s1\",\"targetInfo\":{\"type\":\"page\",\"targetId\":\"t1\"}}}\n", out.items);

    out.clearRetainingCapacity();
    // no sessionId -> field omitted
    try emitEvent(a, &out, "{\"method\":\"Page.eventFired\",\"params\":{\"frameId\":\"f1\",\"name\":\"load\"}}");
    try testing.expectEqualStrings("{\"method\":\"Page.eventFired\",\"params\":{\"frameId\":\"f1\",\"name\":\"load\"}}\n", out.items);
}

test "router: id out of range errors -32600 instead of trapping" {
    const rig = try testRig();
    defer {
        _ = std.os.linux.close(rig.cmd[0]);
        _ = std.os.linux.close(rig.cmd[1]);
        _ = std.os.linux.close(rig.resp[0]);
        _ = std.os.linux.close(rig.resp[1]);
    }
    var d = rig.d;
    defer d.deinit();

    // negative id: @intCast would trap in ReleaseSafe
    const neg = try runRequest(&d, "{\"id\":-1,\"method\":\"Browser.health\",\"params\":{}}");
    defer testing.allocator.free(neg);
    try testing.expectEqualStrings("{\"id\":0,\"error\":{\"code\":-32600,\"message\":\"id out of range\"}}\n", neg);

    // id above u32::MAX
    const big = try runRequest(&d, "{\"id\":4294967296,\"method\":\"Browser.health\",\"params\":{}}");
    defer testing.allocator.free(big);
    try testing.expectEqualStrings("{\"id\":0,\"error\":{\"code\":-32600,\"message\":\"id out of range\"}}\n", big);
}

test "router: user screenshot clip is bounded before reaching the browser" {
    const rig = try testRig();
    defer {
        _ = std.os.linux.close(rig.cmd[0]);
        _ = std.os.linux.close(rig.cmd[1]);
        _ = std.os.linux.close(rig.resp[0]);
        _ = std.os.linux.close(rig.resp[1]);
    }
    var d = rig.d;
    defer d.deinit();

    try seedPage(&d, rig.resp, "t1", "s1", "f1");
    try setCurrentTarget(testing.allocator, "t1");
    defer {
        if (current_target) |t| testing.allocator.free(t);
        current_target = null;
    }

    const too_wide = try runRequest(
        &d,
        "{\"id\":2,\"method\":\"Page.captureScreenshot\",\"params\":{\"clip\":{\"x\":0,\"y\":0,\"width\":32769,\"height\":100}}}",
    );
    defer testing.allocator.free(too_wide);
    try testing.expectEqualStrings(
        "{\"id\":2,\"error\":{\"code\":-32602,\"message\":\"clip width and height exceed maximum 32768\"}}\n",
        too_wide,
    );

    const malformed = try runRequest(
        &d,
        "{\"id\":3,\"method\":\"Page.captureScreenshot\",\"params\":{\"clip\":{\"x\":0,\"y\":0,\"width\":0,\"height\":100}}}",
    );
    defer testing.allocator.free(malformed);
    try testing.expectEqualStrings(
        "{\"id\":3,\"error\":{\"code\":-32602,\"message\":\"clip width and height must be positive\"}}\n",
        malformed,
    );
}

test "router: non-retryable browser pipe read errors surface" {
    const rig = try testRig();
    defer {
        _ = std.os.linux.close(rig.cmd[0]);
        _ = std.os.linux.close(rig.cmd[1]);
        _ = std.os.linux.close(rig.resp[0]);
        _ = std.os.linux.close(rig.resp[1]);
    }
    var d = rig.d;
    defer d.deinit();

    // EBADF must not be treated like an empty/nonblocking read.
    d.reader.fd = -1;
    try testing.expectError(error.ReadFailed, readChunk(&d, testing.allocator));
}

test "router: screenshot clip comes from the real viewport, not a guess" {
    const rig = try testRig();
    defer {
        _ = std.os.linux.close(rig.cmd[0]);
        _ = std.os.linux.close(rig.cmd[1]);
        _ = std.os.linux.close(rig.resp[0]);
        _ = std.os.linux.close(rig.resp[1]);
    }
    var d = rig.d;
    defer d.deinit();

    try seedPage(&d, rig.resp, "t1", "s1", "f1");
    try pipe.writeMessage(
        testing.allocator,
        rig.resp[1],
        "{\"method\":\"Runtime.executionContextCreated\",\"params\":{\"executionContextId\":\"ctx-9\",\"auxData\":{\"frameId\":\"f1\"}},\"sessionId\":\"s1\"}",
    );
    try d.pump(1000);
    try setCurrentTarget(testing.allocator, "t1");
    defer {
        if (current_target) |t| testing.allocator.free(t);
        current_target = null;
    }

    // Size probe first, then the screenshot carrying the measured clip.
    const expected = [_][]const u8{
        "{\"id\":1,\"sessionId\":\"s1\",\"method\":\"Runtime.evaluate\",\"params\":{\"executionContextId\":\"ctx-9\",\"expression\":\"[window.innerWidth, window.innerHeight]\",\"returnByValue\":true}}",
        "{\"id\":2,\"sessionId\":\"s1\",\"method\":\"Page.screenshot\",\"params\":{\"mimeType\":\"image/png\",\"clip\":{\"x\":0,\"y\":0,\"width\":900,\"height\":600}}}",
    };
    const reply = [_][]const u8{
        "{\"id\":1,\"result\":{\"result\":{\"value\":[900,600]}}}",
        "{\"id\":2,\"result\":{\"data\":\"QUJD\"}}",
    };
    const thread = try std.Thread.spawn(.{}, FakePeer.thread, .{ rig.cmd[0], rig.resp[1], &expected, &reply, &[_][]const u8{} });
    defer thread.join();

    const line = try runRequest(&d, "{\"id\":2,\"method\":\"Page.captureScreenshot\",\"params\":{}}");
    defer testing.allocator.free(line);
    try testing.expectEqualStrings("{\"id\":2,\"result\":{\"data\":\"QUJD\"}}\n", line);
}

test "router: screenshot fullPage=true probes the full-content size" {
    const rig = try testRig();
    defer {
        _ = std.os.linux.close(rig.cmd[0]);
        _ = std.os.linux.close(rig.cmd[1]);
        _ = std.os.linux.close(rig.resp[0]);
        _ = std.os.linux.close(rig.resp[1]);
    }
    var d = rig.d;
    defer d.deinit();

    try seedPage(&d, rig.resp, "t1", "s1", "f1");
    try pipe.writeMessage(
        testing.allocator,
        rig.resp[1],
        "{\"method\":\"Runtime.executionContextCreated\",\"params\":{\"executionContextId\":\"ctx-9\",\"auxData\":{\"frameId\":\"f1\"}},\"sessionId\":\"s1\"}",
    );
    try d.pump(1000);
    try setCurrentTarget(testing.allocator, "t1");
    defer {
        if (current_target) |t| testing.allocator.free(t);
        current_target = null;
    }

    const expected = [_][]const u8{
        "{\"id\":1,\"sessionId\":\"s1\",\"method\":\"Runtime.evaluate\",\"params\":{\"executionContextId\":\"ctx-9\",\"expression\":\"[document.documentElement.scrollWidth, document.documentElement.scrollHeight]\",\"returnByValue\":true}}",
        "{\"id\":2,\"sessionId\":\"s1\",\"method\":\"Page.screenshot\",\"params\":{\"mimeType\":\"image/png\",\"clip\":{\"x\":0,\"y\":0,\"width\":1920,\"height\":5000}}}",
    };
    const reply = [_][]const u8{
        "{\"id\":1,\"result\":{\"result\":{\"value\":[1920,5000]}}}",
        "{\"id\":2,\"result\":{\"data\":\"QUJD\"}}",
    };
    const thread = try std.Thread.spawn(.{}, FakePeer.thread, .{ rig.cmd[0], rig.resp[1], &expected, &reply, &[_][]const u8{} });
    defer thread.join();

    const line = try runRequest(&d, "{\"id\":2,\"method\":\"Page.captureScreenshot\",\"params\":{\"fullPage\":true}}");
    defer testing.allocator.free(line);
    try testing.expectEqualStrings("{\"id\":2,\"result\":{\"data\":\"QUJD\"}}\n", line);
}

test "router: screenshot size-probe failure responds -32000 (no swallowed error)" {
    const rig = try testRig();
    defer {
        _ = std.os.linux.close(rig.cmd[0]);
        _ = std.os.linux.close(rig.cmd[1]);
        _ = std.os.linux.close(rig.resp[0]);
        _ = std.os.linux.close(rig.resp[1]);
    }
    var d = rig.d;
    defer d.deinit();

    try seedPage(&d, rig.resp, "t1", "s1", "f1");
    try pipe.writeMessage(
        testing.allocator,
        rig.resp[1],
        "{\"method\":\"Runtime.executionContextCreated\",\"params\":{\"executionContextId\":\"ctx-9\",\"auxData\":{\"frameId\":\"f1\"}},\"sessionId\":\"s1\"}",
    );
    try d.pump(1000);
    try setCurrentTarget(testing.allocator, "t1");
    defer {
        if (current_target) |t| testing.allocator.free(t);
        current_target = null;
    }

    const expected = [_][]const u8{
        "{\"id\":1,\"sessionId\":\"s1\",\"method\":\"Runtime.evaluate\",\"params\":{\"executionContextId\":\"ctx-9\",\"expression\":\"[window.innerWidth, window.innerHeight]\",\"returnByValue\":true}}",
        // The size probe retries once after a JugglerError so a stale
        // (just-destroyed) navigation context is re-picked after the settle.
        "{\"id\":2,\"sessionId\":\"s1\",\"method\":\"Runtime.evaluate\",\"params\":{\"executionContextId\":\"ctx-9\",\"expression\":\"[window.innerWidth, window.innerHeight]\",\"returnByValue\":true}}",
    };
    const reply = [_][]const u8{
        "{\"id\":1,\"error\":{\"code\":-32000,\"message\":\"evaluate failed\"}}",
        "{\"id\":2,\"error\":{\"code\":-32000,\"message\":\"evaluate failed\"}}",
    };
    const thread = try std.Thread.spawn(.{}, FakePeer.thread, .{ rig.cmd[0], rig.resp[1], &expected, &reply, &[_][]const u8{} });
    defer thread.join();

    const line = try runRequest(&d, "{\"id\":2,\"method\":\"Page.captureScreenshot\",\"params\":{}}");
    defer testing.allocator.free(line);
    try testing.expectEqualStrings("{\"id\":2,\"error\":{\"code\":-32000,\"message\":\"could not measure page size\"}}\n", line);
}

test "router: events pumped during a driver call are flushed upward (sink)" {
    const rig = try testRig();
    defer {
        _ = std.os.linux.close(rig.cmd[0]);
        _ = std.os.linux.close(rig.cmd[1]);
        _ = std.os.linux.close(rig.resp[0]);
        _ = std.os.linux.close(rig.resp[1]);
    }
    var d = rig.d;
    defer d.deinit();

    try seedPage(&d, rig.resp, "t1", "s1", "f1");
    try pipe.writeMessage(
        testing.allocator,
        rig.resp[1],
        "{\"method\":\"Runtime.executionContextCreated\",\"params\":{\"executionContextId\":\"ctx-9\",\"auxData\":{\"frameId\":\"f1\"}},\"sessionId\":\"s1\"}",
    );
    try d.pump(1000);
    try setCurrentTarget(testing.allocator, "t1");
    defer {
        if (current_target) |t| testing.allocator.free(t);
        current_target = null;
    }

    // Sink wired exactly like run() does.
    var sink_alloc = testing.allocator;
    d.event_sink = &eventSink;
    d.event_sink_ctx = @ptrCast(&sink_alloc);
    defer {
        sink_buf.deinit(testing.allocator);
        sink_buf = .empty;
    }

    // The console event lands BEFORE the evaluate response: the driver's
    // pump dispatches it during the call (consumed into state, teed by the
    // sink). flushSinkEvents must emit it after the call.
    try pipe.writeMessage(
        testing.allocator,
        rig.resp[1],
        "{\"method\":\"Runtime.console\",\"params\":{\"type\":\"log\",\"text\":\"hi\"},\"sessionId\":\"s1\"}",
    );
    const expected = [_][]const u8{"{\"id\":1,\"sessionId\":\"s1\",\"method\":\"Runtime.evaluate\",\"params\":{\"executionContextId\":\"ctx-9\",\"expression\":\"1+1\",\"returnByValue\":true}}"};
    const reply = [_][]const u8{"{\"id\":1,\"result\":{\"result\":{\"type\":\"number\",\"value\":2}}}"};
    const thread = try std.Thread.spawn(.{}, FakePeer.thread, .{ rig.cmd[0], rig.resp[1], &expected, &reply, &[_][]const u8{} });
    defer thread.join();

    const line = try runRequest(&d, "{\"id\":10,\"method\":\"Runtime.evaluate\",\"params\":{\"expression\":\"1+1\"}}");
    defer testing.allocator.free(line);
    try testing.expectEqualStrings("{\"id\":10,\"result\":{\"result\":{\"type\":\"number\",\"value\":2}}}\n", line);

    var out2: std.array_list.Aligned(u8, null) = .empty;
    defer out2.deinit(testing.allocator);
    try flushSinkEvents(testing.allocator, &out2);
    try testing.expectEqualStrings("{\"method\":\"Runtime.console\",\"params\":{\"type\":\"log\",\"text\":\"hi\"},\"sessionId\":\"s1\"}\n", out2.items);
}

test "router: idle events drain through the shared read buffer (drainEvents)" {
    const rig = try testRig();
    defer {
        _ = std.os.linux.close(rig.cmd[0]);
        _ = std.os.linux.close(rig.cmd[1]);
        _ = std.os.linux.close(rig.resp[0]);
        _ = std.os.linux.close(rig.resp[1]);
    }
    var d = rig.d;
    defer d.deinit();

    var sink_alloc = testing.allocator;
    d.event_sink = &eventSink;
    d.event_sink_ctx = @ptrCast(&sink_alloc);
    defer {
        sink_buf.deinit(testing.allocator);
        sink_buf = .empty;
    }

    try pipe.writeMessage(
        testing.allocator,
        rig.resp[1],
        "{\"method\":\"Runtime.console\",\"params\":{\"type\":\"warning\",\"text\":\"w\"},\"sessionId\":\"s1\"}",
    );
    var out: std.array_list.Aligned(u8, null) = .empty;
    defer out.deinit(testing.allocator);
    try drainIdle(&d, testing.allocator, &out);
    try testing.expectEqualStrings("{\"method\":\"Runtime.console\",\"params\":{\"type\":\"warning\",\"text\":\"w\"},\"sessionId\":\"s1\"}\n", out.items);
    // forwarded exactly once
    try testing.expectEqual(@as(usize, 1), std.mem.count(u8, out.items, "Runtime.console"));
}

test "router: detachedFromTarget clears a stale current_target" {
    const rig = try testRig();
    defer {
        _ = std.os.linux.close(rig.cmd[0]);
        _ = std.os.linux.close(rig.cmd[1]);
        _ = std.os.linux.close(rig.resp[0]);
        _ = std.os.linux.close(rig.resp[1]);
    }
    var d = rig.d;
    defer d.deinit();

    try seedPage(&d, rig.resp, "t1", "s1", "f1");
    try setCurrentTarget(testing.allocator, "t1");
    defer {
        if (current_target) |t| testing.allocator.free(t);
        current_target = null;
    }

    // Detach event: the driver drops the page; refreshCurrentTarget must
    // drop the stale target id (freeing it) so session-less Page calls
    // stop resolving against a dead target.
    try pipe.writeMessage(
        testing.allocator,
        rig.resp[1],
        "{\"method\":\"Browser.detachedFromTarget\",\"params\":{\"sessionId\":\"s1\",\"targetId\":\"t1\"}}",
    );
    var out: std.array_list.Aligned(u8, null) = .empty;
    defer out.deinit(testing.allocator);
    try drainIdle(&d, testing.allocator, &out);
    try testing.expect(current_target == null);

    // After the kill, a session-less Page call errors cleanly.
    const line = try runRequest(&d, "{\"id\":3,\"method\":\"Page.navigate\",\"params\":{\"url\":\"about:blank\"}}");
    defer testing.allocator.free(line);
    try testing.expectEqualStrings("{\"id\":3,\"error\":{\"code\":-32600,\"message\":\"no page session\"}}\n", line);
}
