//! Faz 2 driver: orchestration layer over the Juggler pipe transport.
//!
//! Spawns Camoufox, performs the Browser.enable handshake, then exposes the
//! minimum live surface: browser contexts, pages, navigation with lifecycle
//! tracking, and Runtime.evaluate. Wire format stays Juggler (no CDP shim);
//! the internal call language is CDP-shaped: send(session, method, params).
//!
//! Session model: `Browser.attachedToTarget` events register new page
//! sessions (targetId -> sessionId map in `pages`/`by_session`). Events
//! without a sessionId belong to the root (browser) session.
//!
//! All reads funnel through `pump`, which routes responses to their pending
//! caller and feeds events into the page state (sessions, frames, execution
//! contexts, navigation lifecycle) — so synchronous calls never miss the
//! events they depend on.

const std = @import("std");
const Allocator = std.mem.Allocator;

const pipe = @import("src/transport/pipe.zig");
const session = @import("src/transport/session.zig");
pub const browser = @import("adapters/browser.zig");
const page = @import("adapters/page.zig");
const runtime = @import("adapters/runtime.zig");
const console = @import("adapters/console.zig");
const emulation = @import("adapters/emulation.zig");
const network = @import("adapters/network.zig");
const target = @import("adapters/target.zig");
const input = @import("adapters/input.zig");
/// Process manager (binary resolution, profile isolation, lifecycle,
/// health-check). Re-exported so consumers of the driver module can spawn
/// instances directly (smoke/health evidence).
pub const pm = @import("process-manager/lifecycle.zig");

pub const default_timeout_ms: i32 = 30_000;
pub const enable_timeout_ms: i32 = 30_000;

/// Cap on retained console messages / network events (ring drop of the
/// oldest). Kahin's own collectors keep bounded lists too.
const max_console_messages: usize = 2_000;
const max_network_events: usize = 5_000;
const max_screenshot_dimension: f64 = 32_768;

/// One response message returned by `send`; `raw` is an owned copy.
pub const SendResult = struct {
    is_error: bool,
    raw: []u8,

    pub fn deinit(self: *SendResult, allocator: Allocator) void {
        allocator.free(self.raw);
        self.* = undefined;
    }
};

/// A wire request in flight (Faz 4 Task 2). Heap-owned by the caller; the
/// router's pending map resolves it by id when the matching browser reply
/// arrives. `raw` is an owned copy of the Juggler response on resolution.
/// The same struct backs the synchronous `send` (registered + pumped by the
/// caller) and the sidecar's non-blocking `sendAsync` (resolved by the main
/// loop's drain).
pub const WireCall = struct {
    wire_id: u32,
    deadline_ms: i64,
    done: bool = false,
    is_error: bool = false,
    raw: ?[]u8 = null,
};

/// Settle delay between evaluate retries (mirrors the removed
/// evaluateWithRetry pump(100) grace so context create/destroy events land
/// before the retry).
const eval_retry_settle_ms: i64 = 100;
/// Give a started navigation a short window for commit/abort before the
/// sidecar returns control to Kahin's Python wait contract. Some Camoufox
/// pages never resolve the native Page.navigate promise even after emitting
/// navigationStarted; holding the IPC request for 30s makes every later tool
/// look dead although the browser process is healthy.
const nav_started_grace_ms: i64 = 500;

const ContextInfo = struct {
    id: []u8,
    frame_id: ?[]u8,

    fn deinit(self: *ContextInfo, allocator: Allocator) void {
        allocator.free(self.id);
        if (self.frame_id) |f| allocator.free(f);
    }
};

/// Viewport / full-content size measured from the page itself (Faz 9).
pub const Size = struct { w: f64, h: f64 };

/// One frame of the frame registry (Faz 3): id is the map key, parent/url
/// are owned copies. `parent_id == null` marks the main frame.
const FrameEntry = struct {
    parent_id: ?[]u8,
    url: ?[]u8,

    fn deinit(self: *FrameEntry, allocator: Allocator) void {
        if (self.parent_id) |p| allocator.free(p);
        if (self.url) |u| allocator.free(u);
    }
};

/// One pending intercepted request (Faz 4). Keyed by the Juggler requestId
/// from Network.requestWillBeSent{isIntercepted:true}; `session_id` is an
/// OWNED copy (by_session keys are freed on detach — never alias them).
/// Freed when a decision (resume/fulfill/abort) is sent, when the owning
/// page detaches, or at driver deinit.
const InterceptedEntry = struct {
    session_id: []u8,
    url: []u8,
    method: []u8,

    fn deinit(self: *InterceptedEntry, allocator: Allocator) void {
        allocator.free(self.session_id);
        allocator.free(self.url);
        allocator.free(self.method);
    }
};

/// Per-page state, keyed by target id.
pub const Page = struct {
    /// Aliases the `pages` map key buffer.
    target_id: []u8,
    /// Session id from Browser.attachedToTarget; "" until attached.
    session_id: []u8,
    /// Owning context from Browser.attachedToTarget targetInfo (optional).
    browser_context_id: ?[]u8,
    /// Owned copy of the main frame id (the registry's parentless frame).
    main_frame_id: ?[]u8,
    /// Page-level URL: url of the last main-frame navigationCommitted /
    /// sameDocumentNavigation (owned). "" until first commit.
    current_url: ?[]u8,
    /// frameId -> FrameEntry (owning keys and values).
    frames: std.StringHashMap(FrameEntry),
    lifecycle: page.Lifecycle,
    contexts: std.array_list.Aligned(ContextInfo, null) = .empty,
};

/// Monotonic time in ms (std.time.milliTimestamp removed in 0.16; raw
/// syscall keeps this dependency-free).
fn nowMs() i64 {
    var ts: std.os.linux.timespec = undefined;
    _ = std.os.linux.clock_gettime(std.os.linux.CLOCK.MONOTONIC, &ts);
    return @as(i64, @intCast(ts.sec)) * std.time.ms_per_s + @divTrunc(@as(i64, @intCast(ts.nsec)), std.time.ns_per_ms);
}

pub const Driver = struct {
    allocator: Allocator,
    verbose: bool,
    child: pipe.Spawned,
    reader: pipe.Reader,
    /// Process-manager instance (set by `start`; owns spawn/reap lifecycle).
    /// Null for `init`-built drivers (tests with self-pipes).
    instance: ?*pm.Instance = null,
    router: session.Router,
    /// targetId -> Page (owns the Page structs).
    pages: std.StringHashMap(*Page),
    /// sessionId -> Page (same pointers as `pages`; keys freed in deinit).
    by_session: std.StringHashMap(*Page),
    /// errorText of the last aborted navigation (owned; freed on next use).
    last_abort_text: ?[]u8 = null,
    /// Normalized console messages (Runtime.console -> Console.messageAdded).
    console_messages: std.array_list.Aligned(console.Message, null) = .empty,
    /// Raw Network.* event JSON strings (passive passthrough, Faz 3).
    network_events: std.array_list.Aligned([]u8, null) = .empty,
    /// requestId -> InterceptedEntry (Faz 4 pending interception registry).
    intercepted: std.StringHashMap(InterceptedEntry),
    /// Optional event tee (Faz 9): called with each raw event the pump
    /// dispatches, so the sidecar can forward events that arrive while a
    /// synchronous call is pumping (they would otherwise be consumed into
    /// driver state and lost upward). Null in tests/embedded use; the sink
    /// must copy `raw` (it borrows the pump's read buffer).
    event_sink: ?*const fn (raw: []const u8, ctx: ?*anyopaque) void = null,
    event_sink_ctx: ?*anyopaque = null,

    /// Wire a Driver onto existing fds (no spawn). Used by tests with a
    /// self-pipe; `start` spawns the browser and calls this.
    pub fn init(allocator: Allocator, read_fd: i32, write_fd: i32, verbose: bool) Driver {
        var d = Driver{
            .allocator = allocator,
            .verbose = verbose,
            .child = .{ .pid = -1, .read_fd = read_fd, .write_fd = write_fd },
            .reader = pipe.Reader.init(read_fd),
            .router = session.Router.init(allocator),
            .pages = std.StringHashMap(*Page).init(allocator),
            .by_session = std.StringHashMap(*Page).init(allocator),
            .intercepted = std.StringHashMap(InterceptedEntry).init(allocator),
        };
        d.router.registerSession(.{ .id = "", .target_type = "browser", .name = "root" }) catch unreachable;
        return d;
    }

    /// Spawn Camoufox through the process manager (binary resolution, pin
    /// warning, isolated profile, lifecycle management), then perform the
    /// Browser.enable handshake on the root session. `exe` null resolves via
    /// KAHIN_CAMOUFOX_BIN / $HOME/.cache scan (mirage.py rule); `profile`
    /// null creates an isolated per-instance profile dir.
    pub fn start(allocator: Allocator, exe: ?[]const u8, profile: ?[]const u8, verbose: bool, visible: bool) !Driver {
        const inst = try pm.Instance.spawn(allocator, exe, profile, verbose, !visible);
        var d = Driver.init(allocator, inst.child.read_fd, inst.child.write_fd, verbose);
        d.child = inst.child;
        d.instance = inst;
        errdefer d.deinit();
        // Error path must not leak the child: close the fds (browser exits on
        // EOF), reap it. `stop` before `deinit` — deinit only frees memory.
        errdefer _ = inst.stop(5_000) catch {};

        const params = try browser.enableParams(allocator, true);
        defer allocator.free(params);
        var resp = try d.send(null, browser.method_enable, params, enable_timeout_ms);
        defer resp.deinit(allocator);
        if (resp.is_error) return error.HandshakeFailed;
        return d;
    }
    pub fn deinit(self: *Driver) void {
        // `self.* = undefined` below poisons the struct before defers run, so
        // capture the allocator up front.
        const allocator = self.allocator;
        // Two passes: collect page pointers first, then free keys (the map
        // iterator must not run while its keys are being freed).
        var pages: std.array_list.Aligned(*Page, null) = .empty;
        defer pages.deinit(allocator);
        var it = self.pages.iterator();
        while (it.next()) |kv| pages.append(allocator, kv.value_ptr.*) catch {};
        for (pages.items) |p| self.freePage(p);
        self.pages.deinit();

        // by_session owns the session id buffers; Page.session_id aliases them.
        var sit = self.by_session.iterator();
        while (sit.next()) |kv| self.allocator.free(kv.key_ptr.*);
        self.by_session.deinit();

        for (self.console_messages.items) |*m| m.deinit(allocator);
        self.console_messages.deinit(allocator);
        for (self.network_events.items) |raw| allocator.free(raw);
        self.network_events.deinit(allocator);

        // Pending intercepted requests outlive their pages? No — detach
        // purges them; deinit frees whatever remains (defensive).
        var iit = self.intercepted.iterator();
        while (iit.next()) |kv| {
            allocator.free(kv.key_ptr.*);
            kv.value_ptr.deinit(allocator);
        }
        self.intercepted.deinit();

        if (self.last_abort_text) |t| self.allocator.free(t);
        self.router.deinit();
        self.reader.deinit(self.allocator);
        // Instance owns profile dir + binary path buffers (spawn already
        // reaped the child via stop()); free its memory here.
        if (self.instance) |inst| inst.deinit(self.allocator);
        self.* = undefined;
    }

    fn freePage(self: *Driver, p: *Page) void {
        for (p.contexts.items) |*c| c.deinit(self.allocator);
        p.contexts.deinit(self.allocator);
        if (p.main_frame_id) |f| self.allocator.free(f);
        if (p.browser_context_id) |b| self.allocator.free(b);
        if (p.current_url) |u| self.allocator.free(u);
        var fit = p.frames.iterator();
        while (fit.next()) |kv| {
            self.allocator.free(kv.key_ptr.*);
            kv.value_ptr.deinit(self.allocator);
        }
        p.frames.deinit();
        p.lifecycle.deinit();
        // p.session_id aliases the by_session key; that map owns it.
        self.allocator.free(p.target_id); // aliases the map key
        self.allocator.destroy(p);
    }

    /// CDP-shaped call: send one request (optionally to a session), pump
    /// until the matching response. Returns an owned copy of the response.
    /// Synchronous convenience over sendAsync: registers the wire call and
    /// pumps until the reply (or deadline). Direct-driver consumers (perf
    /// harness, smoke driver) keep this blocking form; the sidecar uses
    /// sendAsync and drives the same pump state from its main loop.
    pub fn send(
        self: *Driver,
        session_id: ?[]const u8,
        method: []const u8,
        params_json: []const u8,
        timeout_ms: i32,
    ) !SendResult {
        const call = try self.sendAsync(session_id, method, params_json, timeout_ms);
        defer self.cancelWireCall(call);
        while (!call.done) {
            const rem = remainingMs(call.deadline_ms) orelse return error.WaitTimeout;
            try self.pump(rem);
        }
        const raw = call.raw.?;
        call.raw = null;
        return .{ .is_error = call.is_error, .raw = raw };
    }

    /// Non-blocking wire call (Faz 4 Task 2): register a heap pending and
    /// write the request, then return immediately. The caller polls
    /// `call.done`/`call.deadline_ms` from its own event loop; dispatchRaw
    /// resolves the call by id when the browser replies. The returned
    /// pointer is owned by the caller and must be released with
    /// cancelWireCall (which also pops the router entry and frees any
    /// resolved raw, so late replies become .invalid and are dropped).
    pub fn sendAsync(
        self: *Driver,
        session_id: ?[]const u8,
        method: []const u8,
        params_json: []const u8,
        timeout_ms: i32,
    ) !*WireCall {
        const id = self.router.nextId();
        const call = try self.allocator.create(WireCall);
        errdefer self.allocator.destroy(call);
        call.* = .{ .wire_id = id, .deadline_ms = nowMs() + timeout_ms };

        try self.router.registerPending(id, @ptrCast(call));
        errdefer _ = self.router.pending.fetchRemove(id);

        const req = try buildRequest(self.allocator, id, session_id, method, params_json);
        defer self.allocator.free(req);
        if (self.verbose) std.debug.print("SENT: {s}\n", .{req});

        pipe.writeMessage(self.allocator, self.child.write_fd, req) catch |err| switch (err) {
            error.BrokenPipe, error.WriteZero => return error.BrowserDead,
            else => return err,
        };
        return call;
    }

    /// Release an in-flight wire call: pop the router entry (a late reply
    /// then resolves as .invalid and is dropped — same as the sync path's
    /// timeout), free a resolved raw, destroy the call.
    pub fn cancelWireCall(self: *Driver, call: *WireCall) void {
        _ = self.router.pending.fetchRemove(call.wire_id);
        if (call.raw) |raw| self.allocator.free(raw);
        self.allocator.destroy(call);
    }

    /// Read + route one message. Responses resolve pending callers; events
    /// update session/frame/context/lifecycle state. Timeout maps to
    /// error.WaitTimeout.
    pub fn pump(self: *Driver, timeout_ms: i32) !void {
        const raw = self.reader.readMessage(self.allocator, timeout_ms) catch |err| switch (err) {
            error.Timeout => return error.WaitTimeout,
            else => return err,
        } orelse return error.BrowserClosed;
        defer self.allocator.free(raw);
        if (self.verbose) std.debug.print("RECV: {s}\n", .{raw});
        try self.dispatchRaw(raw);
    }

    /// Route one raw message exactly as pump does: resolve pending
    /// responses, feed events into state and the event sink. Shared with
    /// the sidecar's read-buffer forwarder so replayed events update state
    /// identically to live ones.
    pub fn dispatchRaw(self: *Driver, raw: []const u8) !void {
        switch (try self.router.dispatch(raw)) {
            .response => |resp| {
                const call: *WireCall = @ptrCast(@alignCast(resp.context));
                call.done = true;
                call.is_error = resp.is_error;
                if (call.raw == null) call.raw = try self.allocator.dupe(u8, resp.raw);
            },
            .event => |ev| {
                if (self.verbose) std.debug.print("EVENT: {s} session={?s}\n", .{ ev.method, ev.session_id });
                if (self.event_sink) |sink| sink(ev.raw, self.event_sink_ctx);
                try self.handleEvent(ev);
            },
            .invalid => {},
        }
    }

    // ---- Faz 4 Task 2: non-blocking flows ------------------------------
    //
    // The sidecar's main loop owns polling; these flows are small state
    // machines that the loop advances once per turn after draining browser
    // data. Each poll() either makes progress (starts a wire call, absorbs
    // a reply, fails) or returns pending. No threads, no locks: the
    // router's id->WireCall map is the single rendezvous point, exactly as
    // in the synchronous path.

    /// Async Runtime.evaluate: wait for an execution context, fire the wire
    /// call, parse the result; on JugglerError/WaitTimeout settle briefly
    /// and retry once (mirrors the removed evaluateWithRetry).
    pub const EvalFlow = struct {
        driver: *Driver,
        /// Owned copies; the page map may mutate while the flow waits.
        session_id: []u8,
        target_id: []u8,
        expr: []u8,
        /// Caller-requested execution context (owned copy) or null for the
        /// legacy main-frame pick. When set, the flow NEVER falls back to
        /// another context: a missing id waits for context events like the
        /// legacy path, and the request fails instead of silently running
        /// in the wrong frame.
        ctx_id: ?[]u8 = null,
        timeout_ms: i32,
        deadline_ms: i64,
        call: ?*WireCall = null,
        retry_left: u8 = 1,
        settle_until_ms: i64 = 0,
        done: bool = false,
        failed: bool = false,
        err: anyerror = error.UnknownTarget,
        result: ?runtime.EvaluateResult = null,

        /// `retry_left` 0 disables the settle-and-retry (the screenshot size
        /// probe must not retry — the driver's synchronous evalSize never
        /// did).
        pub fn init(d: *Driver, target_id: []const u8, session_id: []const u8, expr: []const u8, timeout_ms: i32, retry_left: u8) !*EvalFlow {
            const f = try d.allocator.create(EvalFlow);
            errdefer d.allocator.destroy(f);
            f.* = .{
                .driver = d,
                .session_id = try d.allocator.dupe(u8, session_id),
                .target_id = try d.allocator.dupe(u8, target_id),
                .expr = try d.allocator.dupe(u8, expr),
                .timeout_ms = timeout_ms,
                .deadline_ms = nowMs() + timeout_ms,
                .retry_left = retry_left,
            };
            return f;
        }

        /// Pin the flow to one execution context id. Takes ownership of a
        /// copy; call right after init when the client supplied one.
        pub fn pinContext(self: *EvalFlow, ctx_id: []const u8) !void {
            const d = self.driver;
            if (self.ctx_id) |old| d.allocator.free(old);
            self.ctx_id = try d.allocator.dupe(u8, ctx_id);
        }

        pub fn deinit(self: *EvalFlow) void {
            const d = self.driver;
            if (self.call) |c| d.cancelWireCall(c);
            d.allocator.free(self.session_id);
            d.allocator.free(self.target_id);
            d.allocator.free(self.expr);
            if (self.ctx_id) |c| d.allocator.free(c);
            if (self.result) |*r| r.deinit(d.allocator);
            d.allocator.destroy(self);
        }

        /// Advance the state machine. Call after the loop drained browser
        /// data (dispatchRaw resolves the wire call).
        pub fn poll(self: *EvalFlow) void {
            const d = self.driver;
            if (self.done or self.failed) return;

            if (self.call) |call| {
                if (!call.done) {
                    if (nowMs() >= call.deadline_ms) {
                        d.cancelWireCall(call);
                        self.call = null;
                        return self.failOrRetry(error.WaitTimeout);
                    }
                    return;
                }
                const raw = call.raw.?;
                call.raw = null;
                const is_error = call.is_error;
                d.cancelWireCall(call);
                self.call = null;
                if (is_error) {
                    d.allocator.free(raw);
                    return self.failOrRetry(error.JugglerError);
                }
                const res = runtime.parseEvaluateResult(d.allocator, raw) catch {
                    d.allocator.free(raw);
                    return self.fail(error.InvalidResponse);
                };
                d.allocator.free(raw);
                self.result = res;
                self.done = true;
                return;
            }

            // No wire call in flight: context wait, retry settle, or start.
            if (nowMs() >= self.deadline_ms) return self.fail(error.WaitTimeout);
            if (self.settle_until_ms != 0) {
                if (nowMs() < self.settle_until_ms) return;
                self.settle_until_ms = 0;
            }
            const p = d.pages.get(self.target_id) orelse return self.fail(error.UnknownTarget);
            if (p.session_id.len == 0) return self.fail(error.TargetNotAttached);
            // Pinned context (client-supplied executionContextId): resolve
            // it and only it. Absent from the live list means not yet
            // announced — wait for context events exactly like the legacy
            // path waits; the deadline still bounds the wait.
            var ctx_id: []const u8 = undefined;
            if (self.ctx_id) |pinned| {
                var found = false;
                for (p.contexts.items) |*c| {
                    if (std.mem.eql(u8, c.id, pinned)) {
                        found = true;
                        break;
                    }
                }
                if (!found) {
                    if (nowMs() >= self.deadline_ms) return self.fail(error.NoExecutionContext);
                    return; // wait for context events
                }
                ctx_id = pinned;
            } else {
                const ctx = pickContext(p) orelse return; // wait for context events
                ctx_id = ctx.id;
            }
            const params = runtime.evaluateParams(d.allocator, ctx_id, self.expr, true) catch return self.fail(error.OutOfMemory);
            defer d.allocator.free(params);
            self.deadline_ms = nowMs() + self.timeout_ms;
            self.call = d.sendAsync(p.session_id, runtime.method_evaluate, params, self.timeout_ms) catch |err| return self.fail(err);
        }

        fn failOrRetry(self: *EvalFlow, err: anyerror) void {
            if (self.retry_left > 0) {
                self.retry_left -= 1;
                // Fresh window for the retry (the sync path re-issued
                // evaluate with a full timeout).
                self.deadline_ms = nowMs() + self.timeout_ms;
                self.settle_until_ms = nowMs() + eval_retry_settle_ms;
                return;
            }
            self.fail(err);
        }

        fn fail(self: *EvalFlow, err: anyerror) void {
            self.failed = true;
            self.err = err;
        }

        /// Owned EvaluateResult on success (moves out; flow then deinit-able).
        pub fn takeResult(self: *EvalFlow) !runtime.EvaluateResult {
            if (self.result) |r| {
                self.result = null;
                return r;
            }
            return error.NotDone;
        }
    };

    /// Async Page.navigate: wait for the main frame, fire the wire call,
    /// then gate on the lifecycle (load / abort / navigationSatisfied) —
    /// the same states the synchronous navigate waits for.
    pub const NavFlow = struct {
        driver: *Driver,
        session_id: []u8,
        target_id: []u8,
        url: []u8,
        timeout_ms: i32,
        deadline_ms: i64,
        call: ?*WireCall = null,
        nav_id: ?[]u8 = null,
        nav_started_grace_until_ms: i64 = 0,
        done: bool = false,
        failed: bool = false,
        err: anyerror = error.UnknownTarget,
        abort_text: ?[]u8 = null,

        pub fn init(d: *Driver, target_id: []const u8, session_id: []const u8, url: []const u8, timeout_ms: i32) !*NavFlow {
            const f = try d.allocator.create(NavFlow);
            errdefer d.allocator.destroy(f);
            f.* = .{
                .driver = d,
                .session_id = try d.allocator.dupe(u8, session_id),
                .target_id = try d.allocator.dupe(u8, target_id),
                .url = try d.allocator.dupe(u8, url),
                .timeout_ms = timeout_ms,
                .deadline_ms = nowMs() + timeout_ms,
            };
            return f;
        }

        pub fn deinit(self: *NavFlow) void {
            const d = self.driver;
            if (self.call) |c| d.cancelWireCall(c);
            d.allocator.free(self.session_id);
            d.allocator.free(self.target_id);
            d.allocator.free(self.url);
            if (self.nav_id) |n| d.allocator.free(n);
            if (self.abort_text) |t| d.allocator.free(t);
            d.allocator.destroy(self);
        }

        pub fn poll(self: *NavFlow) void {
            const d = self.driver;
            if (self.done or self.failed) return;
            if (nowMs() >= self.deadline_ms) return self.fail(error.WaitTimeout);

            if (self.call) |call| {
                if (!call.done) {
                    if (d.pages.get(self.target_id)) |p| {
                        if (p.lifecycle.nav_started and p.lifecycle.navigation_id.len > 0) {
                            if (p.lifecycle.state == .aborted) {
                                self.abort_text = d.allocator.dupe(u8, p.lifecycle.abort_text) catch "";
                                return self.fail(error.NavigationAborted);
                            }
                            if (self.nav_started_grace_until_ms == 0) {
                                self.nav_started_grace_until_ms = nowMs() + nav_started_grace_ms;
                            }
                            if (p.lifecycle.committed_current or nowMs() >= self.nav_started_grace_until_ms) {
                                self.nav_id = d.allocator.dupe(u8, p.lifecycle.navigation_id) catch return self.fail(error.OutOfMemory);
                                d.cancelWireCall(call);
                                self.call = null;
                                // The request is now owned by Python's URL /
                                // readyState wait. Mark the driver lifecycle
                                // reusable for the next navigation.
                                p.lifecycle.state = .done;
                                self.done = true;
                                return;
                            }
                        }
                    }
                    return;
                }
                const raw = call.raw.?;
                call.raw = null;
                const is_error = call.is_error;
                d.cancelWireCall(call);
                self.call = null;
                if (is_error) return self.fail(error.JugglerError);
                const nav_id = page.parseNavigationId(d.allocator, raw) catch {
                    d.allocator.free(raw);
                    return self.fail(error.InvalidResponse);
                };
                d.allocator.free(raw);
                self.nav_id = nav_id;
                if (d.pages.get(self.target_id)) |p| {
                    if (p.main_frame_id) |mf| p.lifecycle.setNavigationId(mf, nav_id);
                }
                // Fall through to the lifecycle gate.
            } else if (self.nav_id == null) {
                // Start: wait for the main frame, then fire Page.navigate.
                const p = d.pages.get(self.target_id) orelse return self.fail(error.UnknownTarget);
                if (p.session_id.len == 0) return self.fail(error.TargetNotAttached);
                const mf = p.main_frame_id orelse return; // wait for frame attach
                p.lifecycle.begin(mf);
                const rem = remainingMs(self.deadline_ms) orelse return self.fail(error.WaitTimeout);
                const params = page.navigateParams(d.allocator, mf, self.url, null) catch return self.fail(error.OutOfMemory);
                defer d.allocator.free(params);
                self.call = d.sendAsync(p.session_id, page.method_navigate, params, rem) catch |err| return self.fail(err);
                return;
            }

            // Lifecycle gate (same states as the synchronous navigate).
            const p = d.pages.get(self.target_id) orelse return self.fail(error.UnknownTarget);
            if (p.lifecycle.state == .aborted) {
                if (self.abort_text == null) {
                    self.abort_text = d.allocator.dupe(u8, p.lifecycle.abort_text) catch "";
                }
                return self.fail(error.NavigationAborted);
            }
            const nav_id = self.nav_id.?;
            // Kahin's Python layer owns the requested wait_until contract
            // (commit/domcontentloaded/load/networkidle). The sidecar must
            // finish the wire navigation as soon as the new document is
            // genuinely committed; waiting for load here as well created a
            // second gate and could strand Page.navigate after a large
            // DOM/accessibility operation even though the browser had
            // already accepted the next URL.
            if (p.lifecycle.committed_current and
                (p.lifecycle.navigation_id.len == 0 or std.mem.eql(u8, p.lifecycle.navigation_id, nav_id))) {
                // The sidecar completes Page.navigate at commit. Keep the
                // per-page lifecycle in the same terminal state so the next
                // navigation can call Lifecycle.begin() and reset the
                // navigation id/commit flags instead of seeing a stale
                // `.waiting` navigation forever.
                p.lifecycle.state = .done;
                self.done = true;
                return;
            }
            if (d.urlCommitted(p, self.url)) {
                p.lifecycle.state = .done;
                self.done = true;
                return;
            }
            if (p.lifecycle.state == .done and d.navigationSatisfied(p, nav_id, self.url)) {
                self.done = true;
                return;
            }
            // .waiting or done-but-not-satisfied: browser events will
            // advance the lifecycle; the loop keeps draining.
        }

        fn fail(self: *NavFlow, err: anyerror) void {
            self.failed = true;
            self.err = err;
        }

        /// Owned navigationId on success (moves out).
        pub fn takeNavId(self: *NavFlow) ![]u8 {
            if (self.nav_id) |n| {
                self.nav_id = null;
                return n;
            }
            return error.NotDone;
        }
    };

    /// Async Browser.newPage: wire call, wait for the attachedToTarget
    /// event, optionally navigate (nested NavFlow).
    pub const NewPageFlow = struct {
        driver: *Driver,
        browser_context_id: ?[]u8,
        url: ?[]u8,
        timeout_ms: i32,
        deadline_ms: i64,
        call: ?*WireCall = null,
        target_id: ?[]u8 = null,
        nav: ?*NavFlow = null,
        done: bool = false,
        failed: bool = false,
        err: anyerror = error.UnknownTarget,

        pub fn init(d: *Driver, browser_context_id: ?[]const u8, url: ?[]const u8, timeout_ms: i32) !*NewPageFlow {
            const f = try d.allocator.create(NewPageFlow);
            errdefer d.allocator.destroy(f);
            f.* = .{
                .driver = d,
                .browser_context_id = if (browser_context_id) |b| try d.allocator.dupe(u8, b) else null,
                .url = if (url) |u| try d.allocator.dupe(u8, u) else null,
                .timeout_ms = timeout_ms,
                .deadline_ms = nowMs() + timeout_ms,
            };
            return f;
        }

        pub fn deinit(self: *NewPageFlow) void {
            const d = self.driver;
            if (self.call) |c| d.cancelWireCall(c);
            if (self.nav) |n| n.deinit();
            if (self.browser_context_id) |b| d.allocator.free(b);
            if (self.url) |u| d.allocator.free(u);
            if (self.target_id) |t| d.allocator.free(t);
            d.allocator.destroy(self);
        }

        pub fn poll(self: *NewPageFlow) void {
            const d = self.driver;
            if (self.done or self.failed) return;

            if (self.nav) |nav| {
                nav.poll();
                if (nav.done) {
                    self.done = true;
                } else if (nav.failed) {
                    self.fail(nav.err);
                }
                return;
            }

            if (self.call) |call| {
                if (!call.done) {
                    if (nowMs() >= self.deadline_ms) return self.fail(error.WaitTimeout);
                    return;
                }
                const raw = call.raw.?;
                call.raw = null;
                const is_error = call.is_error;
                d.cancelWireCall(call);
                self.call = null;
                if (is_error) return self.fail(error.JugglerError);
                const target_id = browser.parseTargetId(d.allocator, raw) catch {
                    d.allocator.free(raw);
                    return self.fail(error.InvalidResponse);
                };
                d.allocator.free(raw);
                self.target_id = target_id;
                // Fall through to the attach wait below.
            } else if (self.target_id == null) {
                // Start: fire Browser.newPage.
                if (nowMs() >= self.deadline_ms) return self.fail(error.WaitTimeout);
                const params = browser.newPageParams(d.allocator, self.browser_context_id) catch return self.fail(error.OutOfMemory);
                defer d.allocator.free(params);
                self.call = d.sendAsync(null, browser.method_new_page, params, self.timeout_ms) catch |err| return self.fail(err);
                return;
            }

            // Wait for the attachedToTarget event to register the session
            // (matches the sync path's TargetNotAttached on expiry).
            if (nowMs() >= self.deadline_ms) return self.fail(error.TargetNotAttached);
            const p = d.pages.get(self.target_id.?) orelse return; // wait
            if (p.session_id.len == 0) return; // wait

            if (self.url) |u| {
                const rem = remainingMs(self.deadline_ms) orelse return self.fail(error.WaitTimeout);
                const nav = NavFlow.init(d, self.target_id.?, p.session_id, u, rem) catch return self.fail(error.OutOfMemory);
                self.nav = nav;
                return;
            }
            self.done = true;
        }

        fn fail(self: *NewPageFlow, err: anyerror) void {
            self.failed = true;
            self.err = err;
        }

        /// Owned targetId on success (moves out).
        pub fn takeTargetId(self: *NewPageFlow) ![]u8 {
            if (self.target_id) |t| {
                self.target_id = null;
                return t;
            }
            return error.NotDone;
        }
    };

    /// Browser.createBrowserContext -> owned browserContextId.
    pub fn newContext(self: *Driver, timeout_ms: i32) ![]u8 {
        const params = try browser.createBrowserContextParams(self.allocator, null);
        defer self.allocator.free(params);
        var resp = try self.send(null, browser.method_create_browser_context, params, timeout_ms);
        defer resp.deinit(self.allocator);
        if (resp.is_error) return error.JugglerError;
        return browser.parseBrowserContextId(self.allocator, resp.raw);
    }

    /// Browser.newPage; waits until the attachedToTarget event registered the
    /// page session. If `url` is given, navigates and waits for load. Note:
    /// Juggler's Browser.newPage has NO url param (schema fact) — navigation
    /// is a separate Page.navigate call.
    pub fn newPage(self: *Driver, browser_context_id: ?[]const u8, url: ?[]const u8, timeout_ms: i32) ![]u8 {
        const deadline = nowMs() + timeout_ms;
        const params = try browser.newPageParams(self.allocator, browser_context_id);
        defer self.allocator.free(params);
        var resp = try self.send(null, browser.method_new_page, params, timeout_ms);
        defer resp.deinit(self.allocator);
        if (resp.is_error) return error.JugglerError;
        const target_id = try browser.parseTargetId(self.allocator, resp.raw);
        errdefer self.allocator.free(target_id);

        // The attachedToTarget event may precede or follow the response.
        while (true) {
            const p = self.pages.get(target_id) orelse {
                const rem = remainingMs(deadline) orelse return error.TargetNotAttached;
                try self.pump(rem);
                continue;
            };
            if (p.session_id.len > 0) break;
            const rem = remainingMs(deadline) orelse return error.TargetNotAttached;
            try self.pump(rem);
        }

        if (url) |u| {
            const rem = remainingMs(deadline) orelse return error.WaitTimeout;
            const nav_id = try self.navigate(target_id, u, rem);
            self.allocator.free(nav_id);
        }
        return target_id;
    }

    /// Browser.removeBrowserContext.
    pub fn removeBrowserContext(self: *Driver, browser_context_id: []const u8, timeout_ms: i32) !void {
        const params = try browser.removeBrowserContextParams(self.allocator, browser_context_id);
        defer self.allocator.free(params);
        var resp = try self.send(null, browser.method_remove_browser_context, params, timeout_ms);
        defer resp.deinit(self.allocator);
        if (resp.is_error) return error.JugglerError;
    }

    /// Browser.setExtraHTTPHeaders.
    pub fn setExtraHTTPHeaders(self: *Driver, browser_context_id: ?[]const u8, headers: []const browser.Header, timeout_ms: i32) !void {
        const params = try browser.setExtraHTTPHeadersParams(self.allocator, browser_context_id, headers);
        defer self.allocator.free(params);
        var resp = try self.send(null, browser.method_set_extra_http_headers, params, timeout_ms);
        defer resp.deinit(self.allocator);
        if (resp.is_error) return error.JugglerError;
    }

    /// Browser.close (root session).
    pub fn close(self: *Driver, timeout_ms: i32) !void {
        var resp = try self.send(null, browser.method_close, browser.closeParams(), timeout_ms);
        defer resp.deinit(self.allocator);
        if (resp.is_error) return error.JugglerError;
    }

    /// Page.navigate on the page's main frame, waiting for the load
    /// lifecycle event. Returns an owned navigationId.
    ///
    /// (Faz 2 Minor a fix) The lifecycle is keyed on the frame id only, so a
    /// stale eventFired from the previous document (e.g. the initial
    /// about:blank load racing our navigate) can complete it early. The
    /// return is therefore gated on the navigation being genuinely done
    /// (navigationSatisfied): the started event for our navigation id, or
    /// the committed URL matching the target — a screenshot right after
    /// navigate() captures the NEW document. Redirects pass via the started
    /// event (final URL may differ).
    pub fn navigate(self: *Driver, target_id: []const u8, url: []const u8, timeout_ms: i32) ![]u8 {
        const deadline = nowMs() + timeout_ms;
        const p = self.pages.get(target_id) orelse return error.UnknownTarget;
        if (p.session_id.len == 0) return error.TargetNotAttached;
        try self.waitForMainFrame(p, deadline);

        p.lifecycle.begin(p.main_frame_id.?);
        const params = try page.navigateParams(self.allocator, p.main_frame_id.?, url, null);
        defer self.allocator.free(params);
        const send_timeout = remainingMs(deadline) orelse return error.WaitTimeout;
        var resp = try self.send(p.session_id, page.method_navigate, params, send_timeout);
        defer resp.deinit(self.allocator);
        if (resp.is_error) return error.JugglerError;

        const nav_id = try page.parseNavigationId(self.allocator, resp.raw);
        errdefer self.allocator.free(nav_id);
        p.lifecycle.setNavigationId(p.main_frame_id.?, nav_id);

        try self.waitForLoad(p, deadline);
        // Return gate: keep pumping until the navigation is genuinely done
        // (see navigationSatisfied).
        while (p.lifecycle.state != .aborted and !self.navigationSatisfied(p, nav_id, url)) {
            if (p.lifecycle.state == .waiting) {
                try self.waitForLoad(p, deadline);
            } else {
                const rem = remainingMs(deadline) orelse return error.WaitTimeout;
                try self.pump(rem);
            }
        }
        if (p.lifecycle.state == .aborted) {
            if (self.last_abort_text) |t| self.allocator.free(t);
            self.last_abort_text = try self.allocator.dupe(u8, p.lifecycle.abort_text);
            return error.NavigationAborted;
        }
        return nav_id;
    }

    /// The navigate return gate (Faz 2 Minor a): the lifecycle alone
    /// completes early on a stale load event from the previous document.
    /// Satisfaction requires the done state plus:
    ///   - our navigation started event was observed (the browser accepted
    ///     OUR navigation), AND
    ///   - our navigation committed (the new document exists — its contexts
    ///     are live, so a follow-up evaluate/screenshot cannot hit the
    ///     previous document), or the committed URL matches the requested
    ///     one (same-document navigations).
    /// A newer navigation superseding ours passes on URL match only.
    pub fn navigationSatisfied(self: *Driver, p: *Page, nav_id: []const u8, url: []const u8) bool {
        if (p.lifecycle.state != .done) return false;
        if (p.lifecycle.navigation_id.len == 0 or std.mem.eql(u8, p.lifecycle.navigation_id, nav_id)) {
            if (p.lifecycle.nav_started) {
                return p.lifecycle.committed_current or self.urlCommitted(p, url);
            }
            return self.urlCommitted(p, url);
        }
        return self.urlCommitted(p, url);
    }

    /// True when the page's committed URL equals `url` ("" while unknown —
    /// the gate keeps pumping).
    fn urlCommitted(self: *Driver, p: *Page, url: []const u8) bool {
        _ = self;
        if (p.current_url) |u| return std.mem.eql(u8, u, url);
        return false;
    }

    /// Runtime.evaluate on the page's (main-frame preferred) execution
    /// context. Returns an owned EvaluateResult.
    pub fn evaluate(self: *Driver, target_id: []const u8, expression: []const u8, timeout_ms: i32) !runtime.EvaluateResult {
        const p = self.pages.get(target_id) orelse return error.UnknownTarget;
        if (p.session_id.len == 0) return error.TargetNotAttached;
        try self.waitForContext(p, nowMs() + timeout_ms);
        const ctx = pickContext(p) orelse return error.NoExecutionContext;

        const params = try runtime.evaluateParams(self.allocator, ctx.id, expression, true);
        defer self.allocator.free(params);
        var resp = try self.send(p.session_id, runtime.method_evaluate, params, timeout_ms);
        defer resp.deinit(self.allocator);
        if (resp.is_error) return error.JugglerError;
        return runtime.parseEvaluateResult(self.allocator, resp.raw);
    }

    /// Target.getTargets equivalent (no wire call — Juggler has no Target
    /// domain): CDP-shaped target list from the driver's page map.
    /// Returns owned JSON: {"targetInfos":[{targetId,type:"page",
    /// browserContextId?,url}]}.
    pub fn getTargets(self: *Driver) ![]u8 {
        var infos: std.array_list.Aligned(target.TargetInfo, null) = .empty;
        defer infos.deinit(self.allocator);
        var it = self.pages.iterator();
        while (it.next()) |kv| {
            const p = kv.value_ptr.*;
            try infos.append(self.allocator, .{
                .targetId = p.target_id,
                .url = p.current_url orelse "",
                .browserContextId = p.browser_context_id,
            });
        }
        return target.buildTargetInfos(self.allocator, infos.items);
    }

    /// Target.createTarget equivalent: Browser.newPage on the default
    /// context (Juggler's newPage has no url param — the url is a separate
    /// Page.navigate inside newPage's wrapper) then navigate. Returns the
    /// CDP result {"targetId": "..."}.
    pub fn createTarget(self: *Driver, url: []const u8, timeout_ms: i32) ![]u8 {
        const target_id = try self.newPage(null, url, timeout_ms);
        defer self.allocator.free(target_id);
        return target.buildCreateTargetResult(self.allocator, target_id);
    }

    /// Target.closeTarget equivalent: Page.close (schema) on the target's
    /// session, then wait for Browser.detachedFromTarget to clean the state.
    /// Result: "{}" (CDP shape).
    pub fn closeTarget(self: *Driver, target_id: []const u8, timeout_ms: i32) ![]u8 {
        const p = self.pages.get(target_id) orelse return error.UnknownTarget;
        if (p.session_id.len == 0) return error.TargetNotAttached;
        var resp = try self.send(p.session_id, page.method_close, page.closeParams(), timeout_ms);
        defer resp.deinit(self.allocator);
        if (resp.is_error) return error.JugglerError;
        const deadline = nowMs() + timeout_ms;
        while (self.pages.get(target_id) != null) {
            const rem = remainingMs(deadline) orelse return error.WaitTimeout;
            try self.pump(rem);
        }
        return self.allocator.dupe(u8, target.closeTargetResult());
    }

    /// Emulation.setDeviceMetricsOverride equivalent:
    /// Browser.setDefaultViewport (schema). Context-wide; per-page size
    /// changes go through setViewportSize.
    pub fn setDefaultViewport(
        self: *Driver,
        browser_context_id: ?[]const u8,
        width: f64,
        height: f64,
        device_scale_factor: ?f64,
        timeout_ms: i32,
    ) !void {
        const params = try emulation.setDefaultViewportParams(self.allocator, browser_context_id, .{
            .viewportSize = .{ .width = width, .height = height },
            .deviceScaleFactor = device_scale_factor,
        });
        defer self.allocator.free(params);
        var resp = try self.send(null, emulation.method_set_default_viewport, params, timeout_ms);
        defer resp.deinit(self.allocator);
        if (resp.is_error) return error.JugglerError;
    }

    /// Page.setViewportSize on the target's session.
    pub fn setViewportSize(self: *Driver, target_id: []const u8, width: f64, height: f64, timeout_ms: i32) !void {
        const p = self.pages.get(target_id) orelse return error.UnknownTarget;
        if (p.session_id.len == 0) return error.TargetNotAttached;
        const params = try emulation.setViewportSizeParams(self.allocator, .{ .width = width, .height = height });
        defer self.allocator.free(params);
        var resp = try self.send(p.session_id, emulation.method_set_viewport_size, params, timeout_ms);
        defer resp.deinit(self.allocator);
        if (resp.is_error) return error.JugglerError;
    }

    /// Page.captureScreenshot equivalent: Page.screenshot (schema).
    /// Returns RAW image bytes (the Juggler `data` string is base64 and is
    /// decoded here).
    ///
    /// The `clip` parameter is REQUIRED in this Juggler build — the schema
    /// marks it mandatory (no `optional` flag; observed: "Object
    /// \"<root>.clip\" is undefined, but has some scheme" when omitted). The
    /// clip is computed from the page itself: viewport size via
    /// window.innerWidth/Height, full-page size via
    /// documentElement.scrollWidth/Height (Juggler has no getLayoutMetrics;
    /// this evaluate is the schema-faithful substitute).
    pub fn screenshot(self: *Driver, target_id: []const u8, full_page: bool, timeout_ms: i32) ![]u8 {
        const p = self.pages.get(target_id) orelse return error.UnknownTarget;
        if (p.session_id.len == 0) return error.TargetNotAttached;
        const size = try self.pageClipSize(target_id, full_page, timeout_ms);
        if (size.w <= 0 or size.h <= 0) return error.SizeUnavailable;

        const params = try page.screenshotParams(self.allocator, "image/png", .{ .width = size.w, .height = size.h }, null, null);
        defer self.allocator.free(params);
        var resp = try self.send(p.session_id, page.method_screenshot, params, timeout_ms);
        defer resp.deinit(self.allocator);
        if (resp.is_error) return error.JugglerError;
        const b64 = try page.parseScreenshotData(self.allocator, resp.raw);
        defer self.allocator.free(b64);
        return page.decodeScreenshot(self.allocator, b64);
    }

    /// Clip size for a screenshot: the page's REAL viewport
    /// (window.innerWidth/Height) or full-content size
    /// (documentElement.scrollWidth/Height). Juggler's Page.screenshot has
    /// no fullPage flag — full page means a full-content clip. Shared with
    /// the sidecar's screenshot handler so the clip is never a hardcoded
    /// guess.
    pub fn pageClipSize(self: *Driver, target_id: []const u8, full_page: bool, timeout_ms: i32) !Size {
        const p = self.pages.get(target_id) orelse return error.UnknownTarget;
        if (p.session_id.len == 0) return error.TargetNotAttached;
        try self.waitForMainFrame(p, nowMs() + timeout_ms);
        const expr = if (full_page)
            "[document.documentElement.scrollWidth, document.documentElement.scrollHeight]"
        else
            "[window.innerWidth, window.innerHeight]";
        const size = try self.evalSize(p, expr, timeout_ms);
        if (size.w <= 0 or size.h <= 0 or size.w != size.w or size.h != size.h or
            size.w > max_screenshot_dimension or size.h > max_screenshot_dimension)
            return error.SizeUnavailable;
        return size;
    }

    /// Evaluate a [width, height] pair in the page (viewport or full
    /// content size).
    fn evalSize(self: *Driver, p: *Page, expr: []const u8, timeout_ms: i32) !Size {
        var res = try self.evaluate(p.target_id, expr, timeout_ms);
        defer res.deinit(self.allocator);
        if (res.exception_text != null or res.value_json.len == 0) return error.SizeUnavailable;
        const parsed = try std.json.parseFromSlice(std.json.Value, self.allocator, res.value_json, .{});
        defer parsed.deinit();
        const v = parsed.value;
        if (v != .array or v.array.items.len < 2) return error.SizeUnavailable;
        const w = numF64(v.array.items[0]) orelse return error.SizeUnavailable;
        const h = numF64(v.array.items[1]) orelse return error.SizeUnavailable;
        return .{ .w = w, .h = h };
    }

    /// Page.getFrameTree equivalent (no wire call — Juggler has no
    /// getFrameTree): nested tree from the frame registry, CDP shape:
    /// {"frameTree":{"frame":{"id","parentId"?,"url"},"childFrames":[...]}}.
    pub fn getFrameTree(self: *Driver, target_id: []const u8) ![]u8 {
        const p = self.pages.get(target_id) orelse return error.UnknownTarget;
        return buildFrameTreeJson(self.allocator, p);
    }

    /// Console messages normalized to the Console.messageAdded
    /// params.message shape, as a JSON array.
    pub fn getConsoleMessages(self: *Driver) ![]u8 {
        var out: std.Io.Writer.Allocating = .init(self.allocator);
        defer out.deinit();
        const w = &out.writer;
        try w.writeAll("[");
        for (self.console_messages.items, 0..) |*m, i| {
            if (i > 0) try w.writeAll(",");
            const j = try console.toJson(self.allocator, m);
            defer self.allocator.free(j);
            try w.writeAll(j);
        }
        try w.writeAll("]");
        return self.allocator.dupe(u8, out.written());
    }

    /// Raw Network.* events (passive passthrough, no interception) as a JSON
    /// array of the original wire events, newest last. `limit` 0 = all.
    pub fn getNetworkEvents(self: *Driver, limit: usize) ![]u8 {
        const n = @min(limit, self.network_events.items.len);
        const first = self.network_events.items.len - n;
        var out: std.Io.Writer.Allocating = .init(self.allocator);
        defer out.deinit();
        const w = &out.writer;
        try w.writeAll("[");
        for (self.network_events.items[first..], 0..) |raw, i| {
            if (i > 0) try w.writeAll(",");
            try w.writeAll(raw);
        }
        try w.writeAll("]");
        return self.allocator.dupe(u8, out.written());
    }

    // ---- Faz 4: network interception -------------------------------------

    /// CDP Network.setRequestInterception equivalent: page-scoped toggle.
    /// Juggler intercepts ALL requests of the page while enabled (the CDP
    /// `patterns` filter has no Juggler equivalent — filter client-side).
    pub fn setInterception(self: *Driver, target_id: []const u8, enabled: bool, timeout_ms: i32) !void {
        const p = self.pages.get(target_id) orelse return error.UnknownTarget;
        if (p.session_id.len == 0) return error.TargetNotAttached;
        const params = try network.setInterceptionParams(self.allocator, enabled);
        defer self.allocator.free(params);
        var resp = try self.send(p.session_id, network.method_set_request_interception, params, timeout_ms);
        defer resp.deinit(self.allocator);
        if (resp.is_error) return error.JugglerError;
    }

    /// Browser.setRequestInterception (schema): context-scoped toggle;
    /// `browser_context_id` null = default context.
    pub fn setContextInterception(self: *Driver, browser_context_id: ?[]const u8, enabled: bool, timeout_ms: i32) !void {
        const params = try network.setContextInterceptionParams(self.allocator, browser_context_id, enabled);
        defer self.allocator.free(params);
        var resp = try self.send(null, network.method_set_request_interception_context, params, timeout_ms);
        defer resp.deinit(self.allocator);
        if (resp.is_error) return error.JugglerError;
    }

    /// Pump until at least one request is pending interception (smoke
    /// helper; mirrors waitForConsole).
    pub fn waitForIntercepted(self: *Driver, timeout_ms: i32) !void {
        const deadline = nowMs() + timeout_ms;
        while (self.intercepted.count() == 0) {
            const rem = remainingMs(deadline) orelse return error.WaitTimeout;
            try self.pump(rem);
        }
    }

    /// Pending intercepted requests as JSON:
    /// [{"requestId":..,"url":..,"method":..}, ...] (route ids for the
    /// decision methods). Order is unspecified.
    pub fn listInterceptedRequests(self: *Driver) ![]u8 {
        var out: std.Io.Writer.Allocating = .init(self.allocator);
        defer out.deinit();
        const w = &out.writer;
        try w.writeAll("[");
        var first = true;
        var it = self.intercepted.iterator();
        while (it.next()) |kv| {
            if (!first) try w.writeAll(",");
            first = false;
            try std.json.Stringify.value(
                .{
                    .requestId = kv.key_ptr.*,
                    .url = kv.value_ptr.url,
                    .method = kv.value_ptr.method,
                },
                .{ .emit_null_optional_fields = false },
                w,
            );
        }
        try w.writeAll("]");
        return self.allocator.dupe(u8, out.written());
    }

    /// CDP Network.continueInterceptedRequest equivalent:
    /// Network.resumeInterceptedRequest (schema name) on the session that
    /// owns the request. Optional overrides (url/method/headers/postData)
    /// change the request before it proceeds. The pending entry is consumed
    /// by the first decision — a second call fails with
    /// error.UnknownInterceptedRequest.
    pub fn continueInterceptedRequest(
        self: *Driver,
        request_id: []const u8,
        url: ?[]const u8,
        method: ?[]const u8,
        headers: ?[]const browser.Header,
        post_data: ?[]const u8,
        timeout_ms: i32,
    ) !void {
        var req = self.takeIntercepted(request_id) orelse return error.UnknownInterceptedRequest;
        defer req.deinit(self.allocator);
        var restore = true;
        defer {
            if (restore) self.restoreIntercepted(request_id, &req) catch {};
        }
        const params = try network.resumeParams(self.allocator, request_id, url, method, headers, post_data);
        defer self.allocator.free(params);
        var resp = try self.send(req.session_id, network.method_resume_intercepted_request, params, timeout_ms);
        defer resp.deinit(self.allocator);
        if (resp.is_error) return error.JugglerError;
        restore = false;
    }

    /// CDP Network.fulfillInterceptedRequest equivalent (same schema name).
    /// `base64_body` null = empty body.
    pub fn fulfillInterceptedRequest(
        self: *Driver,
        request_id: []const u8,
        status: u32,
        status_text: []const u8,
        headers: []const browser.Header,
        base64_body: ?[]const u8,
        timeout_ms: i32,
    ) !void {
        var req = self.takeIntercepted(request_id) orelse return error.UnknownInterceptedRequest;
        defer req.deinit(self.allocator);
        var restore = true;
        defer {
            if (restore) self.restoreIntercepted(request_id, &req) catch {};
        }
        const params = try network.fulfillParams(self.allocator, request_id, status, status_text, headers, base64_body);
        defer self.allocator.free(params);
        var resp = try self.send(req.session_id, network.method_fulfill_intercepted_request, params, timeout_ms);
        defer resp.deinit(self.allocator);
        if (resp.is_error) return error.JugglerError;
        restore = false;
    }

    /// CDP Network.abortInterceptedRequest equivalent (same schema name).
    /// `error_code` is a Firefox Components.results member, e.g.
    /// "NS_ERROR_ABORT" (the Juggler side cancels the channel with it).
    pub fn abortInterceptedRequest(self: *Driver, request_id: []const u8, error_code: []const u8, timeout_ms: i32) !void {
        var req = self.takeIntercepted(request_id) orelse return error.UnknownInterceptedRequest;
        defer req.deinit(self.allocator);
        var restore = true;
        defer {
            if (restore) self.restoreIntercepted(request_id, &req) catch {};
        }
        const params = try network.abortParams(self.allocator, request_id, error_code);
        defer self.allocator.free(params);
        var resp = try self.send(req.session_id, network.method_abort_intercepted_request, params, timeout_ms);
        defer resp.deinit(self.allocator);
        if (resp.is_error) return error.JugglerError;
        restore = false;
    }

    // ---- Faz 4: input (Page domain — Juggler has no Input domain) --------

    /// CDP Input.dispatchKeyEvent equivalent: Page.dispatchKeyEvent
    /// (schema). CDP type strings and windowsVirtualKeyCode/autoRepeat are
    /// mapped onto the Juggler spellings by the input adapter.
    pub fn dispatchKeyEvent(
        self: *Driver,
        target_id: []const u8,
        type_cdp: []const u8,
        key: []const u8,
        key_code: u32,
        location: u32,
        code: []const u8,
        repeat: bool,
        text: ?[]const u8,
        timeout_ms: i32,
    ) !void {
        const p = self.pages.get(target_id) orelse return error.UnknownTarget;
        if (p.session_id.len == 0) return error.TargetNotAttached;
        const tj = input.keyTypeJuggler(type_cdp) orelse return error.InvalidKeyType;
        const params = try input.dispatchKeyEventParams(self.allocator, tj, key, key_code, location, code, repeat, text);
        defer self.allocator.free(params);
        var resp = try self.send(p.session_id, input.method_dispatch_key_event, params, timeout_ms);
        defer resp.deinit(self.allocator);
        if (resp.is_error) return error.JugglerError;
    }

    /// CDP Input.dispatchMouseEvent equivalent: Page.dispatchMouseEvent
    /// (schema). `buttons` (optional in CDP, REQUIRED in Juggler) is
    /// derived from type+button when null. "mouseWheel" type is NOT
    /// accepted here — use dispatchWheelEvent.
    pub fn dispatchMouseEvent(
        self: *Driver,
        target_id: []const u8,
        type_cdp: []const u8,
        x: f64,
        y: f64,
        button_cdp: []const u8,
        modifiers: u16,
        click_count: ?u32,
        buttons: ?u16,
        timeout_ms: i32,
    ) !void {
        const p = self.pages.get(target_id) orelse return error.UnknownTarget;
        if (p.session_id.len == 0) return error.TargetNotAttached;
        const tj = input.mouseTypeJuggler(type_cdp) orelse return error.InvalidMouseType;
        const bnum = input.buttonNumber(button_cdp) orelse return error.InvalidButton;
        const bts = input.buttonsFor(button_cdp, tj, buttons) orelse return error.InvalidButton;
        const params = try input.dispatchMouseEventParams(self.allocator, tj, bnum, x, y, modifiers, click_count, bts);
        defer self.allocator.free(params);
        var resp = try self.send(p.session_id, input.method_dispatch_mouse_event, params, timeout_ms);
        defer resp.deinit(self.allocator);
        if (resp.is_error) return error.JugglerError;
    }

    /// CDP Input.dispatchMouseEvent(mouseWheel) equivalent:
    /// Page.dispatchWheelEvent (schema — a separate method in Juggler).
    pub fn dispatchWheelEvent(
        self: *Driver,
        target_id: []const u8,
        x: f64,
        y: f64,
        delta_x: f64,
        delta_y: f64,
        modifiers: u16,
        timeout_ms: i32,
    ) !void {
        const p = self.pages.get(target_id) orelse return error.UnknownTarget;
        if (p.session_id.len == 0) return error.TargetNotAttached;
        const params = try input.dispatchWheelEventParams(self.allocator, x, y, delta_x, delta_y, modifiers);
        defer self.allocator.free(params);
        var resp = try self.send(p.session_id, input.method_dispatch_wheel_event, params, timeout_ms);
        defer resp.deinit(self.allocator);
        if (resp.is_error) return error.JugglerError;
    }

    /// CDP Input.insertText equivalent: Page.insertText (schema).
    pub fn insertText(self: *Driver, target_id: []const u8, text: []const u8, timeout_ms: i32) !void {
        const p = self.pages.get(target_id) orelse return error.UnknownTarget;
        if (p.session_id.len == 0) return error.TargetNotAttached;
        const params = try input.insertTextParams(self.allocator, text);
        defer self.allocator.free(params);
        var resp = try self.send(p.session_id, input.method_insert_text, params, timeout_ms);
        defer resp.deinit(self.allocator);
        if (resp.is_error) return error.JugglerError;
    }

    /// Convenience: left-click at (x, y) = mousedown + mouseup (clickCount
    /// 1, buttons derived). CDP Input.dispatchMouseEvent pair.
    pub fn click(self: *Driver, target_id: []const u8, x: f64, y: f64, timeout_ms: i32) !void {
        try self.dispatchMouseEvent(target_id, "mousePressed", x, y, "left", 0, 1, null, timeout_ms);
        try self.dispatchMouseEvent(target_id, "mouseReleased", x, y, "left", 0, 1, null, timeout_ms);
    }

    /// Pump until at least one console message arrived (smoke helper).
    pub fn waitForConsole(self: *Driver, timeout_ms: i32) !void {
        const deadline = nowMs() + timeout_ms;
        while (self.console_messages.items.len == 0) {
            const rem = remainingMs(deadline) orelse return error.WaitTimeout;
            try self.pump(rem);
        }
    }

    /// Console.enable equivalent — no-op by design: Juggler has no Console
    /// domain; Runtime.console events flow unconditionally (schema fact).
    pub fn enableConsole(self: *Driver) void {
        _ = self;
    }

    /// Network.enable equivalent — no-op by design: Juggler emits Network.*
    /// events unconditionally; there is no Network.enable in the schema.
    pub fn enableNetwork(self: *Driver) void {
        _ = self;
    }

    /// Runtime.enable equivalent — no-op by design: no Runtime.enable in the
    /// schema; executionContextCreated/Destroyed events flow unconditionally
    /// (observed in Faz 2 smoke).
    pub fn enableRuntime(self: *Driver) void {
        _ = self;
    }

    /// Close the pipes (browser exits cleanly) and reap it. Returns the
    /// browser's exit code. Managed instances get a bounded wait with a
    /// SIGKILL fallback for wedged children.
    pub fn stop(self: *Driver) !u8 {
        if (self.instance) |inst| return inst.stop(10_000);
        pipe.closeFds(&self.child);
        return pipe.wait(&self.child);
    }

    fn waitForMainFrame(self: *Driver, p: *Page, deadline_ms: i64) !void {
        while (p.main_frame_id == null) {
            const rem = remainingMs(deadline_ms) orelse return error.WaitTimeout;
            try self.pump(rem);
        }
    }

    fn waitForLoad(self: *Driver, p: *Page, deadline_ms: i64) !void {
        while (p.lifecycle.state == .waiting) {
            const rem = remainingMs(deadline_ms) orelse return error.WaitTimeout;
            try self.pump(rem);
        }
    }

    fn waitForContext(self: *Driver, p: *Page, deadline_ms: i64) !void {
        while (p.contexts.items.len == 0) {
            const rem = remainingMs(deadline_ms) orelse return error.WaitTimeout;
            try self.pump(rem);
        }
    }

    fn handleEvent(self: *Driver, ev: session.Router.Event) !void {
        if (std.mem.eql(u8, ev.method, "Browser.attachedToTarget")) {
            try self.onAttachedToTarget(ev.raw);
        } else if (std.mem.eql(u8, ev.method, "Browser.detachedFromTarget")) {
            try self.onDetachedFromTarget(ev.raw);
        } else if (std.mem.eql(u8, ev.method, "Runtime.executionContextCreated")) {
            try self.onExecutionContextCreated(ev);
        } else if (std.mem.eql(u8, ev.method, "Runtime.executionContextsCleared")) {
            try self.onExecutionContextsCleared(ev);
        } else if (std.mem.eql(u8, ev.method, "Runtime.executionContextDestroyed")) {
            try self.onExecutionContextDestroyed(ev);
        } else if (std.mem.eql(u8, ev.method, "Page.eventFired")) {
            try self.onEventFired(ev);
        } else if (std.mem.eql(u8, ev.method, "Page.navigationAborted")) {
            try self.onNavigationAborted(ev);
        } else if (std.mem.eql(u8, ev.method, "Page.navigationStarted")) {
            try self.onNavigationStarted(ev);
        } else if (std.mem.eql(u8, ev.method, "Page.frameAttached")) {
            try self.onFrameAttached(ev);
        } else if (std.mem.eql(u8, ev.method, "Page.frameDetached")) {
            try self.onFrameDetached(ev);
        } else if (std.mem.eql(u8, ev.method, "Page.navigationCommitted")) {
            try self.onNavigationCommitted(ev);
        } else if (std.mem.eql(u8, ev.method, "Page.sameDocumentNavigation")) {
            try self.onSameDocument(ev);
        } else if (std.mem.eql(u8, ev.method, "Runtime.console")) {
            try self.onConsoleEvent(ev);
        } else if (std.mem.eql(u8, ev.method, network.event_request_will_be_sent)) {
            try self.onRequestWillBeSent(ev);
        } else if (std.mem.startsWith(u8, ev.method, "Network.")) {
            try self.onNetworkEvent(ev);
        }
        // Other events (Page.ready, screencastFrame, Browser.*, ...) are ignored.
    }

    /// Browser.attachedToTarget {sessionId, targetInfo{type, targetId, ...}}
    /// arrives on the ROOT session; the new page session id is in params.
    fn onAttachedToTarget(self: *Driver, raw: []const u8) !void {
        const parsed = try std.json.parseFromSlice(std.json.Value, self.allocator, raw, .{});
        defer parsed.deinit();
        const root = parsed.value;
        if (root != .object) return;
        const params = root.object.get("params") orelse return;
        if (params != .object) return;
        const sid = params.object.get("sessionId") orelse return;
        const ti = params.object.get("targetInfo") orelse return;
        if (sid != .string or ti != .object) return;
        const tid = ti.object.get("targetId") orelse return;
        const ttype = ti.object.get("type") orelse return;
        if (tid != .string or ttype != .string) return;
        var bc_owned: ?[]u8 = null;
        if (ti.object.get("browserContextId")) |bc| {
            if (bc == .string) bc_owned = try self.allocator.dupe(u8, bc.string);
        }
        if (self.verbose) {
            std.debug.print("  -> attachedToTarget sessionId={s} targetId={s} type={s}\n", .{ sid.string, tid.string, ttype.string });
        }

        try self.router.registerSession(.{ .id = sid.string, .target_type = ttype.string, .name = "" });

        const tid_owned = try self.allocator.dupe(u8, tid.string);
        const sid_owned = try self.allocator.dupe(u8, sid.string);
        const gop = try self.pages.getOrPut(tid_owned);
        if (gop.found_existing) {
            self.allocator.free(tid_owned); // key already stored; target_id aliases it
            // Old session id buffer is the by_session map key; re-key first.
            // ("" placeholder never enters by_session and has no buffer.)
            const old_sid = gop.value_ptr.*.session_id;
            if (self.by_session.fetchRemove(old_sid)) |bkv| self.allocator.free(bkv.key);
            gop.value_ptr.*.session_id = sid_owned;
            // Re-key the context id if it changed (rare).
            if (gop.value_ptr.*.browser_context_id) |old_bc| self.allocator.free(old_bc);
            gop.value_ptr.*.browser_context_id = bc_owned;
        } else {
            const p = try self.allocator.create(Page);
            p.* = .{
                .target_id = tid_owned,
                .session_id = sid_owned,
                .browser_context_id = bc_owned,
                .main_frame_id = null,
                .current_url = null,
                .frames = std.StringHashMap(FrameEntry).init(self.allocator),
                .lifecycle = .{ .allocator = self.allocator },
                .contexts = .empty,
            };
            gop.value_ptr.* = p;
        }
        // by_session owns the session id buffer; Page.session_id aliases it.
        const sgop = try self.by_session.getOrPut(sid_owned);
        if (sgop.found_existing) {
            self.allocator.free(sid_owned); // same id already mapped
        }
        gop.value_ptr.*.session_id = @constCast(sgop.key_ptr.*);
        sgop.value_ptr.* = gop.value_ptr.*;
    }

    fn onDetachedFromTarget(self: *Driver, raw: []const u8) !void {
        const parsed = try std.json.parseFromSlice(std.json.Value, self.allocator, raw, .{});
        defer parsed.deinit();
        const root = parsed.value;
        if (root != .object) return;
        const params = root.object.get("params") orelse return;
        if (params != .object) return;
        const sid = params.object.get("sessionId") orelse return;
        const tid = params.object.get("targetId") orelse return;
        if (sid != .string or tid != .string) return;

        // An old detach can arrive after the same target was reattached with
        // a new session. Never tear down the new live page for that stale
        // lifecycle event.
        if (self.pages.get(tid.string)) |page_state| {
            if (!std.mem.eql(u8, page_state.session_id, sid.string)) return;
        }

        self.router.removeSession(sid.string);
        if (self.pages.fetchRemove(tid.string)) |kv| {
            // by_session key aliases p.session_id; drop + free it, then the page.
            if (self.by_session.fetchRemove(kv.value.session_id)) |bkv| self.allocator.free(bkv.key);
            self.freePage(kv.value);
        }
        // Faz 4: pending intercepted requests of the detached page can never
        // be decided — purge them (their session ids are owned copies).
        var doomed: std.array_list.Aligned([]const u8, null) = .empty;
        defer doomed.deinit(self.allocator);
        var it = self.intercepted.iterator();
        while (it.next()) |kv| {
            if (std.mem.eql(u8, kv.value_ptr.session_id, sid.string)) {
                doomed.append(self.allocator, kv.key_ptr.*) catch {};
            }
        }
        for (doomed.items) |key| {
            const kv = self.intercepted.fetchRemove(key).?;
            var val = kv.value; // const capture; copy to mutate
            self.allocator.free(kv.key);
            val.deinit(self.allocator);
        }
    }

    /// Runtime.executionContextCreated {executionContextId, auxData{frameId?}}
    /// on a page session.
    fn onExecutionContextCreated(self: *Driver, ev: session.Router.Event) !void {
        const sid = ev.session_id orelse return;
        const p = self.by_session.get(sid) orelse return; // unknown session
        const parsed = try std.json.parseFromSlice(std.json.Value, self.allocator, ev.raw, .{});
        defer parsed.deinit();
        const root = parsed.value;
        if (root != .object) return;
        const params = root.object.get("params") orelse return;
        if (params != .object) return;
        const ctx = params.object.get("executionContextId") orelse return;
        if (ctx != .string) return;

        var frame_id: ?[]u8 = null;
        defer if (frame_id) |f| self.allocator.free(f);
        if (params.object.get("auxData")) |ad| {
            if (ad == .object) {
                if (ad.object.get("frameId")) |f| {
                    if (f == .string) frame_id = try self.allocator.dupe(u8, f.string);
                }
            }
        }
        try p.contexts.append(self.allocator, .{
            .id = try self.allocator.dupe(u8, ctx.string),
            .frame_id = frame_id,
        });
        frame_id = null; // ownership moved into the list
    }

    fn onExecutionContextsCleared(self: *Driver, ev: session.Router.Event) !void {
        const sid = ev.session_id orelse return;
        const p = self.by_session.get(sid) orelse return;
        for (p.contexts.items) |*c| c.deinit(self.allocator);
        p.contexts.clearRetainingCapacity();
    }

    /// Runtime.executionContextDestroyed {executionContextId} on a page
    /// session: drop the context so pickContext never selects a dead id.
    fn onExecutionContextDestroyed(self: *Driver, ev: session.Router.Event) !void {
        const sid = ev.session_id orelse return;
        const p = self.by_session.get(sid) orelse return;
        const parsed = try std.json.parseFromSlice(std.json.Value, self.allocator, ev.raw, .{});
        defer parsed.deinit();
        const root = parsed.value;
        if (root != .object) return;
        const params = root.object.get("params") orelse return;
        if (params != .object) return;
        const ctx = params.object.get("executionContextId") orelse return;
        if (ctx != .string) return;
        for (p.contexts.items, 0..) |*c, i| {
            if (std.mem.eql(u8, c.id, ctx.string)) {
                var removed = p.contexts.swapRemove(i);
                removed.deinit(self.allocator);
                return;
            }
        }
    }

    /// Page.eventFired {frameId, name: "load"|"DOMContentLoaded"}.
    fn onEventFired(self: *Driver, ev: session.Router.Event) !void {
        const sid = ev.session_id orelse return;
        const p = self.by_session.get(sid) orelse return;
        const parsed = try std.json.parseFromSlice(std.json.Value, self.allocator, ev.raw, .{});
        defer parsed.deinit();
        const root = parsed.value;
        if (root != .object) return;
        const params = root.object.get("params") orelse return;
        if (params != .object) return;
        const name = params.object.get("name") orelse return;
        const frame_id = params.object.get("frameId") orelse return;
        if (name != .string or frame_id != .string) return;
        // Both "load" and "DOMContentLoaded" mean the document is ready;
        // the state machine completes on either.
        if (std.mem.eql(u8, name.string, "load") or std.mem.eql(u8, name.string, "DOMContentLoaded")) {
            p.lifecycle.onLoad(frame_id.string);
        }
    }

    fn onNavigationAborted(self: *Driver, ev: session.Router.Event) !void {
        const sid = ev.session_id orelse return;
        const p = self.by_session.get(sid) orelse return;
        const parsed = try std.json.parseFromSlice(std.json.Value, self.allocator, ev.raw, .{});
        defer parsed.deinit();
        const root = parsed.value;
        if (root != .object) return;
        const params = root.object.get("params") orelse return;
        if (params != .object) return;
        const frame_id = params.object.get("frameId") orelse return;
        const nav_id = params.object.get("navigationId") orelse return;
        const error_text = params.object.get("errorText") orelse return;
        if (frame_id != .string or nav_id != .string or error_text != .string) return;
        p.lifecycle.onAbort(frame_id.string, nav_id.string, error_text.string);
    }

    fn onNavigationStarted(self: *Driver, ev: session.Router.Event) !void {
        const sid = ev.session_id orelse return;
        const p = self.by_session.get(sid) orelse return;
        const parsed = try std.json.parseFromSlice(std.json.Value, self.allocator, ev.raw, .{});
        defer parsed.deinit();
        const root = parsed.value;
        if (root != .object) return;
        const params = root.object.get("params") orelse return;
        if (params != .object) return;
        const frame_id = params.object.get("frameId") orelse return;
        const nav_id = params.object.get("navigationId") orelse return;
        if (frame_id != .string or nav_id != .string) return;
        p.lifecycle.onNavigationStarted(frame_id.string, nav_id.string);
    }

    /// Page.frameAttached {frameId, parentFrameId?}: registers the frame in
    /// the registry (the parentless frame is the page's main frame).
    fn onFrameAttached(self: *Driver, ev: session.Router.Event) !void {
        const sid = ev.session_id orelse return;
        const p = self.by_session.get(sid) orelse return;
        const parsed = try std.json.parseFromSlice(std.json.Value, self.allocator, ev.raw, .{});
        defer parsed.deinit();
        const root = parsed.value;
        if (root != .object) return;
        const params = root.object.get("params") orelse return;
        if (params != .object) return;
        const frame_id = params.object.get("frameId") orelse return;
        if (frame_id != .string) return;

        var parent_owned: ?[]u8 = null;
        if (params.object.get("parentFrameId")) |pf| {
            if (pf == .string) parent_owned = try self.allocator.dupe(u8, pf.string);
        }
        if (parent_owned == null) {
            // Main frame: a new main frame id means a new document — reset.
            if (p.main_frame_id) |mf| {
                if (!std.mem.eql(u8, mf, frame_id.string)) {
                    self.allocator.free(mf);
                    p.main_frame_id = null;
                }
            }
            if (p.main_frame_id == null) {
                p.main_frame_id = try self.allocator.dupe(u8, frame_id.string);
            }
        }

        const key = try self.allocator.dupe(u8, frame_id.string);
        const gop = try p.frames.getOrPut(key);
        if (gop.found_existing) {
            self.allocator.free(key); // same content already stored
            if (gop.value_ptr.parent_id) |op| self.allocator.free(op);
        } else {
            gop.value_ptr.* = .{ .parent_id = null, .url = null };
        }
        gop.value_ptr.parent_id = parent_owned;
    }

    /// Page.frameDetached {frameId}: drop the registry entry; a detached
    /// main frame clears the main frame id so the next frameAttached
    /// re-registers the new one.
    fn onFrameDetached(self: *Driver, ev: session.Router.Event) !void {
        const sid = ev.session_id orelse return;
        const p = self.by_session.get(sid) orelse return;
        const parsed = try std.json.parseFromSlice(std.json.Value, self.allocator, ev.raw, .{});
        defer parsed.deinit();
        const root = parsed.value;
        if (root != .object) return;
        const params = root.object.get("params") orelse return;
        if (params != .object) return;
        const frame_id = params.object.get("frameId") orelse return;
        if (frame_id != .string) return;

        if (p.frames.fetchRemove(frame_id.string)) |kv| {
            self.allocator.free(kv.key);
            var entry = kv.value; // const capture; copy to mutate
            entry.deinit(self.allocator);
        }
        if (p.main_frame_id) |mf| {
            if (std.mem.eql(u8, mf, frame_id.string)) {
                self.allocator.free(mf);
                p.main_frame_id = null;
            }
        }
    }

    /// Page.navigationCommitted {frameId, navigationId, url}: record the
    /// committed URL in the registry and, for main frames, on the page
    /// (TargetInfo.url + the navigate URL gate).
    fn onNavigationCommitted(self: *Driver, ev: session.Router.Event) !void {
        const sid = ev.session_id orelse return;
        const p = self.by_session.get(sid) orelse return;
        const parsed = try std.json.parseFromSlice(std.json.Value, self.allocator, ev.raw, .{});
        defer parsed.deinit();
        const root = parsed.value;
        if (root != .object) return;
        const params = root.object.get("params") orelse return;
        if (params != .object) return;
        const frame_id = params.object.get("frameId") orelse return;
        const url = params.object.get("url") orelse return;
        if (frame_id != .string or url != .string) return;

        if (p.frames.getPtr(frame_id.string)) |f| {
            if (f.url) |u| self.allocator.free(u);
            f.url = try self.allocator.dupe(u8, url.string);
        }
        const is_main = if (p.main_frame_id) |mf| std.mem.eql(u8, mf, frame_id.string) else false;
        if (!is_main) return;
        if (p.current_url) |u| self.allocator.free(u);
        p.current_url = try self.allocator.dupe(u8, url.string);
        // Feed the navigate gate (the event also carries the navigationId).
        if (params.object.get("navigationId")) |nav_id| {
            if (nav_id == .string) p.lifecycle.onCommitted(frame_id.string, nav_id.string);
        }
    }

    /// Page.sameDocumentNavigation {frameId, navigationId, url}: hashchange
    /// / pushState. Main-frame ones update the page url and complete a
    /// waiting navigation (no load event follows).
    fn onSameDocument(self: *Driver, ev: session.Router.Event) !void {
        const sid = ev.session_id orelse return;
        const p = self.by_session.get(sid) orelse return;
        const parsed = try std.json.parseFromSlice(std.json.Value, self.allocator, ev.raw, .{});
        defer parsed.deinit();
        const root = parsed.value;
        if (root != .object) return;
        const params = root.object.get("params") orelse return;
        if (params != .object) return;
        const frame_id = params.object.get("frameId") orelse return;
        const url = params.object.get("url") orelse return;
        if (frame_id != .string or url != .string) return;
        const is_main = if (p.main_frame_id) |mf| std.mem.eql(u8, mf, frame_id.string) else false;
        if (!is_main) return;
        if (p.current_url) |u| self.allocator.free(u);
        p.current_url = try self.allocator.dupe(u8, url.string);
        p.lifecycle.onSameDocument(frame_id.string);
    }

    /// Runtime.console {executionContextId, args, type, location} →
    /// normalized Console.messageAdded message (ring-dropped at the cap).
    fn onConsoleEvent(self: *Driver, ev: session.Router.Event) !void {
        var msg = console.normalize(self.allocator, ev.raw) catch return; // malformed console events are non-fatal
        if (self.console_messages.items.len >= max_console_messages) {
            var old = self.console_messages.orderedRemove(0);
            old.deinit(self.allocator);
        }
        self.console_messages.append(self.allocator, msg) catch |err| {
            msg.deinit(self.allocator);
            return err;
        };
    }

    /// Network.requestWillBeSent: passive passthrough (Faz 3) PLUS the
    /// interception signal (Faz 4) — when `isIntercepted` is true the
    /// request is paused in the browser until a decision arrives, so the
    /// requestId is registered as a pending route on its page session.
    /// Malformed events stay non-fatal (interception registration is best
    /// effort; the passive copy is what Kahin's collector consumes).
    fn onRequestWillBeSent(self: *Driver, ev: session.Router.Event) !void {
        // Passive retention (identical to the old onNetworkEvent path).
        if (self.network_events.items.len >= max_network_events) {
            self.allocator.free(self.network_events.orderedRemove(0));
        }
        self.network_events.append(self.allocator, try self.allocator.dupe(u8, ev.raw)) catch |err| return err;

        const sid = ev.session_id orelse return;
        var info = network.parseIntercepted(self.allocator, ev.raw) catch return;
        defer info.deinit(self.allocator);
        if (!info.is_intercepted) return;

        const key = try self.allocator.dupe(u8, info.request_id);
        errdefer self.allocator.free(key);
        const gop = try self.intercepted.getOrPut(key);
        if (gop.found_existing) {
            self.allocator.free(key); // already pending; first registration wins
            return;
        }
        gop.value_ptr.* = .{
            .session_id = try self.allocator.dupe(u8, sid),
            .url = try self.allocator.dupe(u8, info.url),
            .method = try self.allocator.dupe(u8, info.method),
        };
    }

    /// Network.* events (other than requestWillBeSent): passive passthrough
    /// — the raw wire event JSON is retained (Juggler's Network event names
    /// are already CDP-shaped). Ring-dropped at the cap.
    fn onNetworkEvent(self: *Driver, ev: session.Router.Event) !void {
        if (self.network_events.items.len >= max_network_events) {
            self.allocator.free(self.network_events.orderedRemove(0));
        }
        self.network_events.append(self.allocator, try self.allocator.dupe(u8, ev.raw)) catch |err| return err;
    }

    /// Pop the pending entry for `request_id` (the decision consumes it;
    /// the requestId map key is freed here).
    fn takeIntercepted(self: *Driver, request_id: []const u8) ?InterceptedEntry {
        const kv = self.intercepted.fetchRemove(request_id) orelse return null;
        self.allocator.free(kv.key);
        return kv.value;
    }

    /// Restore a decision entry when the browser rejected/timed out the wire
    /// command. A failed continue/fulfill/abort must not silently lose a
    /// paused request that the agent can still decide.
    fn restoreIntercepted(self: *Driver, request_id: []const u8, req: *const InterceptedEntry) !void {
        const key = try self.allocator.dupe(u8, request_id);
        errdefer self.allocator.free(key);
        const entry = InterceptedEntry{
            .session_id = try self.allocator.dupe(u8, req.session_id),
            .url = try self.allocator.dupe(u8, req.url),
            .method = try self.allocator.dupe(u8, req.method),
        };
        errdefer {
            self.allocator.free(entry.session_id);
            self.allocator.free(entry.url);
            self.allocator.free(entry.method);
        }
        const gop = try self.intercepted.getOrPut(key);
        if (gop.found_existing) {
            self.allocator.free(key);
            var old = gop.value_ptr.*;
            old.deinit(self.allocator);
        }
        gop.value_ptr.* = entry;
    }
};

/// Preferred evaluate context: the latest context of the main frame; falls
/// back to the latest context of the session.
pub fn pickContext(p: *Page) ?*ContextInfo {
    var chosen: ?*ContextInfo = null;
    for (p.contexts.items) |*c| {
        if (c.frame_id) |f| {
            if (p.main_frame_id) |mf| {
                if (std.mem.eql(u8, f, mf)) chosen = c;
            }
        }
    }
    return chosen orelse if (p.contexts.items.len > 0) &p.contexts.items[p.contexts.items.len - 1] else null;
}

/// Build the wire request: {"id":N,"sessionId":"...","method":"...","params":{...}}
/// (sessionId omitted for the root session).
fn buildRequest(
    allocator: Allocator,
    id: u32,
    session_id: ?[]const u8,
    method: []const u8,
    params_json: []const u8,
) Allocator.Error![]u8 {
    if (session_id) |sid| {
        return std.fmt.allocPrint(
            allocator,
            "{{\"id\":{d},\"sessionId\":\"{s}\",\"method\":\"{s}\",\"params\":{s}}}",
            .{ id, sid, method, params_json },
        );
    }
    return std.fmt.allocPrint(
        allocator,
        "{{\"id\":{d},\"method\":\"{s}\",\"params\":{s}}}",
        .{ id, method, params_json },
    );
}

/// Milliseconds left until `deadline_ms` (wall clock; fine for timeouts),
/// or null when expired.
fn remainingMs(deadline_ms: i64) ?i32 {
    const now = nowMs();
    const rem = deadline_ms - now;
    if (rem <= 0) return null;
    if (rem > 2147483647) return 2147483647;
    return @intCast(rem);
}

/// Number value of a JSON node, or null for non-numbers.
fn numF64(v: std.json.Value) ?f64 {
    return switch (v) {
        .integer => |i| @floatFromInt(i),
        .float => |f| f,
        else => null,
    };
}

/// `[w, h]` JSON pair -> Size with evalSize's bounds (positive, finite,
/// <= max_screenshot_dimension); null when invalid. Shared with the
/// sidecar's async screenshot flow (which cannot call the blocking
/// evalSize from the event loop).
pub fn sizeFromJson(allocator: Allocator, value_json: []const u8) ?Size {
    const parsed = std.json.parseFromSlice(std.json.Value, allocator, value_json, .{}) catch return null;
    defer parsed.deinit();
    const v = parsed.value;
    if (v != .array or v.array.items.len < 2) return null;
    const w = numF64(v.array.items[0]) orelse return null;
    const h = numF64(v.array.items[1]) orelse return null;
    if (w <= 0 or h <= 0 or w != w or h != h or
        w > max_screenshot_dimension or h > max_screenshot_dimension) return null;
    return .{ .w = w, .h = h };
}

/// CDP Page.getFrameTree result from the frame registry: the parentless
/// frame is the root; children are found by parentId (Faz 3). Owned JSON:
/// {"frameTree":{"frame":{"id","parentId"?,"url"},"childFrames":[...]}}.
fn buildFrameTreeJson(allocator: Allocator, p: *Page) ![]u8 {
    // Write the nested tree by hand; Stringify of a recursive structure
    // would need a custom type for the recursion.
    var out: std.Io.Writer.Allocating = .init(allocator);
    defer out.deinit();
    const w = &out.writer;
    try w.writeAll("{\"frameTree\":");
    try writeFrameNode(allocator, w, p, p.main_frame_id orelse return error.NoMainFrame);
    try w.writeAll("}");
    return allocator.dupe(u8, out.written());
}

fn writeFrameNode(allocator: Allocator, w: anytype, p: *Page, frame_id: []const u8) !void {
    const e = p.frames.get(frame_id) orelse return error.UnknownFrame;
    try w.writeAll("{\"frame\":{\"id\":");
    try std.json.Stringify.value(frame_id, .{}, w);
    if (e.parent_id) |pid| {
        try w.writeAll(",\"parentId\":");
        try std.json.Stringify.value(pid, .{}, w);
    }
    try w.writeAll(",\"url\":");
    try std.json.Stringify.value(e.url orelse "", .{}, w);
    try w.writeAll("},\"childFrames\":[");
    var first = true;
    var it = p.frames.iterator();
    while (it.next()) |kv| {
        if (kv.value_ptr.parent_id == null) continue;
        if (!std.mem.eql(u8, kv.value_ptr.parent_id.?, frame_id)) continue;
        if (!first) try w.writeAll(",");
        first = false;
        try writeFrameNode(allocator, w, p, kv.key_ptr.*);
    }
    try w.writeAll("]}");
}

const testing = std.testing;

fn testPipe() ![2]i32 {
    var fds: [2]i32 = undefined;
    if (std.os.linux.errno(std.os.linux.pipe2(&fds, .{})) != .SUCCESS) return error.PipeFailed;
    return fds;
}

fn writeFake(d: *Driver, fds: [2]i32, json: []const u8) !void {
    _ = d;
    try pipe.writeMessage(testing.allocator, fds[1], json);
}

test "driver: buildRequest shapes match Juggler wire format" {
    const root = try buildRequest(testing.allocator, 1, null, "Browser.enable", "{}");
    defer testing.allocator.free(root);
    try testing.expectEqualStrings("{\"id\":1,\"method\":\"Browser.enable\",\"params\":{}}", root);

    const sub = try buildRequest(testing.allocator, 2, "sess-1", "Page.navigate", "{\"frameId\":\"f\"}");
    defer testing.allocator.free(sub);
    try testing.expectEqualStrings(
        "{\"id\":2,\"sessionId\":\"sess-1\",\"method\":\"Page.navigate\",\"params\":{\"frameId\":\"f\"}}",
        sub,
    );
}

test "driver: attachedToTarget registers targetId -> sessionId" {
    const fds = try testPipe();
    defer {
        _ = std.os.linux.close(fds[0]);
        _ = std.os.linux.close(fds[1]);
    }
    var d = Driver.init(testing.allocator, fds[0], fds[1], false);
    defer d.deinit();

    try writeFake(&d, fds, "{\"method\":\"Browser.attachedToTarget\",\"params\":{\"sessionId\":\"sess-1\",\"targetInfo\":{\"type\":\"page\",\"targetId\":\"t-1\",\"browserContextId\":\"ctx-1\"}}}");
    try d.pump(1000);

    const p = d.pages.get("t-1") orelse return error.TestUnexpected;
    try testing.expectEqualStrings("sess-1", p.session_id);
    try testing.expect(d.by_session.get("sess-1") == p);
    try testing.expect(d.router.sessions.contains("sess-1"));
}

test "driver: frameAttached without parent sets main frame" {
    const fds = try testPipe();
    defer {
        _ = std.os.linux.close(fds[0]);
        _ = std.os.linux.close(fds[1]);
    }
    var d = Driver.init(testing.allocator, fds[0], fds[1], false);
    defer d.deinit();

    try writeFake(&d, fds, "{\"method\":\"Browser.attachedToTarget\",\"params\":{\"sessionId\":\"s1\",\"targetInfo\":{\"type\":\"page\",\"targetId\":\"t1\"}}}");
    try d.pump(1000);
    try writeFake(&d, fds, "{\"method\":\"Page.frameAttached\",\"params\":{\"frameId\":\"f-main\"},\"sessionId\":\"s1\"}");
    try d.pump(1000);
    try writeFake(&d, fds, "{\"method\":\"Page.frameAttached\",\"params\":{\"frameId\":\"f-sub\",\"parentFrameId\":\"f-main\"},\"sessionId\":\"s1\"}");
    try d.pump(1000);

    const p = d.pages.get("t1") orelse return error.TestUnexpected;
    try testing.expectEqualStrings("f-main", p.main_frame_id.?);
}

test "driver: executionContextCreated recorded per session" {
    const fds = try testPipe();
    defer {
        _ = std.os.linux.close(fds[0]);
        _ = std.os.linux.close(fds[1]);
    }
    var d = Driver.init(testing.allocator, fds[0], fds[1], false);
    defer d.deinit();

    try writeFake(&d, fds, "{\"method\":\"Browser.attachedToTarget\",\"params\":{\"sessionId\":\"s1\",\"targetInfo\":{\"type\":\"page\",\"targetId\":\"t1\"}}}");
    try d.pump(1000);
    try writeFake(&d, fds, "{\"method\":\"Page.frameAttached\",\"params\":{\"frameId\":\"f1\"},\"sessionId\":\"s1\"}");
    try d.pump(1000);
    try writeFake(&d, fds, "{\"method\":\"Runtime.executionContextCreated\",\"params\":{\"executionContextId\":\"ctx-9\",\"auxData\":{\"frameId\":\"f1\"}},\"sessionId\":\"s1\"}");
    try d.pump(1000);

    const p = d.pages.get("t1") orelse return error.TestUnexpected;
    try testing.expectEqual(@as(usize, 1), p.contexts.items.len);
    try testing.expectEqualStrings("ctx-9", p.contexts.items[0].id);
    try testing.expectEqualStrings("f1", p.contexts.items[0].frame_id.?);
    try testing.expectEqualStrings("ctx-9", pickContext(p).?.id);
}

test "driver: eventFired load drives lifecycle to done" {
    const fds = try testPipe();
    defer {
        _ = std.os.linux.close(fds[0]);
        _ = std.os.linux.close(fds[1]);
    }
    var d = Driver.init(testing.allocator, fds[0], fds[1], false);
    defer d.deinit();

    try writeFake(&d, fds, "{\"method\":\"Browser.attachedToTarget\",\"params\":{\"sessionId\":\"s1\",\"targetInfo\":{\"type\":\"page\",\"targetId\":\"t1\"}}}");
    try d.pump(1000);
    try writeFake(&d, fds, "{\"method\":\"Page.frameAttached\",\"params\":{\"frameId\":\"f1\"},\"sessionId\":\"s1\"}");
    try d.pump(1000);

    const p = d.pages.get("t1") orelse return error.TestUnexpected;
    p.lifecycle.begin("f1");
    try writeFake(&d, fds, "{\"method\":\"Page.eventFired\",\"params\":{\"frameId\":\"f1\",\"name\":\"load\"},\"sessionId\":\"s1\"}");
    try d.pump(1000);
    try testing.expectEqual(page.Lifecycle.State.done, p.lifecycle.state);
}

test "driver: navigationAborted drives lifecycle to aborted" {
    const fds = try testPipe();
    defer {
        _ = std.os.linux.close(fds[0]);
        _ = std.os.linux.close(fds[1]);
    }
    var d = Driver.init(testing.allocator, fds[0], fds[1], false);
    defer d.deinit();

    try writeFake(&d, fds, "{\"method\":\"Browser.attachedToTarget\",\"params\":{\"sessionId\":\"s1\",\"targetInfo\":{\"type\":\"page\",\"targetId\":\"t1\"}}}");
    try d.pump(1000);
    try writeFake(&d, fds, "{\"method\":\"Page.frameAttached\",\"params\":{\"frameId\":\"f1\"},\"sessionId\":\"s1\"}");
    try d.pump(1000);

    const p = d.pages.get("t1") orelse return error.TestUnexpected;
    p.lifecycle.begin("f1");
    p.lifecycle.setNavigationId("f1", "n1");
    try writeFake(&d, fds, "{\"method\":\"Page.navigationAborted\",\"params\":{\"frameId\":\"f1\",\"navigationId\":\"n1\",\"errorText\":\"NS_BINDING_ABORTED\"},\"sessionId\":\"s1\"}");
    try d.pump(1000);
    try testing.expectEqual(page.Lifecycle.State.aborted, p.lifecycle.state);
}

test "driver: navigationAborted for a superseded navigation is ignored" {
    const fds = try testPipe();
    defer {
        _ = std.os.linux.close(fds[0]);
        _ = std.os.linux.close(fds[1]);
    }
    var d = Driver.init(testing.allocator, fds[0], fds[1], false);
    defer d.deinit();

    try writeFake(&d, fds, "{\"method\":\"Browser.attachedToTarget\",\"params\":{\"sessionId\":\"s1\",\"targetInfo\":{\"type\":\"page\",\"targetId\":\"t1\"}}}");
    try d.pump(1000);
    try writeFake(&d, fds, "{\"method\":\"Page.frameAttached\",\"params\":{\"frameId\":\"f1\"},\"sessionId\":\"s1\"}");
    try d.pump(1000);

    const p = d.pages.get("t1") orelse return error.TestUnexpected;
    p.lifecycle.begin("f1");
    p.lifecycle.setNavigationId("f1", "n1");
    try writeFake(&d, fds, "{\"method\":\"Page.navigationAborted\",\"params\":{\"frameId\":\"f1\",\"navigationId\":\"n0\",\"errorText\":\"NS_BINDING_ABORTED\"},\"sessionId\":\"s1\"}");
    try d.pump(1000);
    try testing.expectEqual(page.Lifecycle.State.waiting, p.lifecycle.state);
    try testing.expectEqualStrings("", p.lifecycle.abort_text);
}

test "driver: executionContextDestroyed removes the context" {
    const fds = try testPipe();
    defer {
        _ = std.os.linux.close(fds[0]);
        _ = std.os.linux.close(fds[1]);
    }
    var d = Driver.init(testing.allocator, fds[0], fds[1], false);
    defer d.deinit();

    try writeFake(&d, fds, "{\"method\":\"Browser.attachedToTarget\",\"params\":{\"sessionId\":\"s1\",\"targetInfo\":{\"type\":\"page\",\"targetId\":\"t1\"}}}");
    try d.pump(1000);
    try writeFake(&d, fds, "{\"method\":\"Page.frameAttached\",\"params\":{\"frameId\":\"f1\"},\"sessionId\":\"s1\"}");
    try d.pump(1000);
    try writeFake(&d, fds, "{\"method\":\"Runtime.executionContextCreated\",\"params\":{\"executionContextId\":\"ctx-1\",\"auxData\":{\"frameId\":\"f1\"}},\"sessionId\":\"s1\"}");
    try d.pump(1000);
    try writeFake(&d, fds, "{\"method\":\"Runtime.executionContextCreated\",\"params\":{\"executionContextId\":\"ctx-2\",\"auxData\":{\"frameId\":\"f1\"}},\"sessionId\":\"s1\"}");
    try d.pump(1000);
    try writeFake(&d, fds, "{\"method\":\"Runtime.executionContextDestroyed\",\"params\":{\"executionContextId\":\"ctx-1\"},\"sessionId\":\"s1\"}");
    try d.pump(1000);

    const p = d.pages.get("t1") orelse return error.TestUnexpected;
    try testing.expectEqual(@as(usize, 1), p.contexts.items.len);
    try testing.expectEqualStrings("ctx-2", p.contexts.items[0].id);
    try testing.expectEqualStrings("ctx-2", pickContext(p).?.id);
}

test "driver: executionContextDestroyed for unknown id is a no-op" {
    const fds = try testPipe();
    defer {
        _ = std.os.linux.close(fds[0]);
        _ = std.os.linux.close(fds[1]);
    }
    var d = Driver.init(testing.allocator, fds[0], fds[1], false);
    defer d.deinit();

    try writeFake(&d, fds, "{\"method\":\"Browser.attachedToTarget\",\"params\":{\"sessionId\":\"s1\",\"targetInfo\":{\"type\":\"page\",\"targetId\":\"t1\"}}}");
    try d.pump(1000);
    try writeFake(&d, fds, "{\"method\":\"Runtime.executionContextCreated\",\"params\":{\"executionContextId\":\"ctx-1\",\"auxData\":{\"frameId\":\"f1\"}},\"sessionId\":\"s1\"}");
    try d.pump(1000);
    try writeFake(&d, fds, "{\"method\":\"Runtime.executionContextDestroyed\",\"params\":{\"executionContextId\":\"ctx-9\"},\"sessionId\":\"s1\"}");
    try d.pump(1000);

    const p = d.pages.get("t1") orelse return error.TestUnexpected;
    try testing.expectEqual(@as(usize, 1), p.contexts.items.len);
    try testing.expectEqualStrings("ctx-1", p.contexts.items[0].id);
}

test "driver: frameDetached clears main frame, re-attach re-registers" {
    const fds = try testPipe();
    defer {
        _ = std.os.linux.close(fds[0]);
        _ = std.os.linux.close(fds[1]);
    }
    var d = Driver.init(testing.allocator, fds[0], fds[1], false);
    defer d.deinit();

    try writeFake(&d, fds, "{\"method\":\"Browser.attachedToTarget\",\"params\":{\"sessionId\":\"s1\",\"targetInfo\":{\"type\":\"page\",\"targetId\":\"t1\"}}}");
    try d.pump(1000);
    try writeFake(&d, fds, "{\"method\":\"Page.frameAttached\",\"params\":{\"frameId\":\"f-old\"},\"sessionId\":\"s1\"}");
    try d.pump(1000);
    try writeFake(&d, fds, "{\"method\":\"Page.frameDetached\",\"params\":{\"frameId\":\"f-old\"},\"sessionId\":\"s1\"}");
    try d.pump(1000);

    const p = d.pages.get("t1") orelse return error.TestUnexpected;
    try testing.expect(p.main_frame_id == null);

    try writeFake(&d, fds, "{\"method\":\"Page.frameAttached\",\"params\":{\"frameId\":\"f-new\"},\"sessionId\":\"s1\"}");
    try d.pump(1000);
    try testing.expectEqualStrings("f-new", p.main_frame_id.?);
}

test "driver: frameDetached for sub-frame keeps main frame" {
    const fds = try testPipe();
    defer {
        _ = std.os.linux.close(fds[0]);
        _ = std.os.linux.close(fds[1]);
    }
    var d = Driver.init(testing.allocator, fds[0], fds[1], false);
    defer d.deinit();

    try writeFake(&d, fds, "{\"method\":\"Browser.attachedToTarget\",\"params\":{\"sessionId\":\"s1\",\"targetInfo\":{\"type\":\"page\",\"targetId\":\"t1\"}}}");
    try d.pump(1000);
    try writeFake(&d, fds, "{\"method\":\"Page.frameAttached\",\"params\":{\"frameId\":\"f-main\"},\"sessionId\":\"s1\"}");
    try d.pump(1000);
    try writeFake(&d, fds, "{\"method\":\"Page.frameDetached\",\"params\":{\"frameId\":\"f-sub\"},\"sessionId\":\"s1\"}");
    try d.pump(1000);

    const p = d.pages.get("t1") orelse return error.TestUnexpected;
    try testing.expectEqualStrings("f-main", p.main_frame_id.?);
}

test "driver: removeBrowserContext/close/setExtraHTTPHeaders send schema methods" {
    var cmd: [2]i32 = undefined;
    var resp: [2]i32 = undefined;
    if (std.os.linux.errno(std.os.linux.pipe2(&cmd, .{})) != .SUCCESS) return error.PipeFailed;
    if (std.os.linux.errno(std.os.linux.pipe2(&resp, .{})) != .SUCCESS) return error.PipeFailed;
    defer {
        _ = std.os.linux.close(cmd[0]);
        _ = std.os.linux.close(cmd[1]);
        _ = std.os.linux.close(resp[0]);
        _ = std.os.linux.close(resp[1]);
    }
    var d = Driver.init(testing.allocator, resp[0], cmd[1], false);
    defer d.deinit();

    const T = struct {
        fn run(cmd_read: i32, resp_write: i32) void {
            var r = pipe.Reader.init(cmd_read);
            defer r.deinit(testing.allocator);
            const expected = [_][]const u8{
                "{\"id\":1,\"method\":\"Browser.removeBrowserContext\",\"params\":{\"browserContextId\":\"ctx-1\"}}",
                "{\"id\":2,\"method\":\"Browser.close\",\"params\":{}}",
                "{\"id\":3,\"method\":\"Browser.setExtraHTTPHeaders\",\"params\":{\"headers\":[]}}",
            };
            const replies = [_][]const u8{
                "{\"id\":1,\"result\":{}}",
                "{\"id\":2,\"result\":{}}",
                "{\"id\":3,\"result\":{}}",
            };
            for (expected, 0..) |want, i| {
                const msg = (r.readMessage(testing.allocator, 5000) catch return) orelse return;
                defer testing.allocator.free(msg);
                if (!std.mem.eql(u8, msg, want)) return;
                pipe.writeMessage(testing.allocator, resp_write, replies[i]) catch return;
            }
        }
    };
    const thread = try std.Thread.spawn(.{}, T.run, .{ cmd[0], resp[1] });
    defer thread.join();

    try d.removeBrowserContext("ctx-1", 2000);
    try d.close(2000);
    const headers = [_]browser.Header{};
    try d.setExtraHTTPHeaders(null, &headers, 2000);
}

test "driver: send writes request and matches response by id" {
    // Real browser wiring uses two pipes (cmd: parent->child, resp: child->parent);
    // a single pipe would loop the request back to the driver.
    var cmd: [2]i32 = undefined;
    var resp: [2]i32 = undefined;
    if (std.os.linux.errno(std.os.linux.pipe2(&cmd, .{})) != .SUCCESS) return error.PipeFailed;
    if (std.os.linux.errno(std.os.linux.pipe2(&resp, .{})) != .SUCCESS) return error.PipeFailed;
    defer {
        _ = std.os.linux.close(cmd[0]);
        _ = std.os.linux.close(cmd[1]);
        _ = std.os.linux.close(resp[0]);
        _ = std.os.linux.close(resp[1]);
    }
    var d = Driver.init(testing.allocator, resp[0], cmd[1], false);
    defer d.deinit();

    // Responder thread: assert the exact request the driver wrote (first
    // request on a fresh driver has id 1), then answer it on the resp pipe.
    const T = struct {
        fn run(cmd_read: i32, resp_write: i32) void {
            var r = pipe.Reader.init(cmd_read);
            defer r.deinit(testing.allocator);
            const msg = (r.readMessage(testing.allocator, 5000) catch return) orelse return;
            defer testing.allocator.free(msg);
            if (!std.mem.eql(u8, msg, "{\"id\":1,\"method\":\"Browser.createBrowserContext\",\"params\":{}}")) return;
            pipe.writeMessage(testing.allocator, resp_write, "{\"id\":1,\"result\":{\"browserContextId\":\"ctx-9\"}}") catch return;
        }
    };
    const thread = try std.Thread.spawn(.{}, T.run, .{ cmd[0], resp[1] });
    defer thread.join();

    var res = try d.send(null, browser.method_create_browser_context, "{}", 2000);
    defer res.deinit(testing.allocator);
    try testing.expect(!res.is_error);
    const id = try browser.parseBrowserContextId(testing.allocator, res.raw);
    defer testing.allocator.free(id);
    try testing.expectEqualStrings("ctx-9", id);
}

// ---- Faz 4 driver tests ------------------------------------------------

/// Responder thread for Faz 4 wire tests: reads `replies.len` requests from
/// cmd_read, records each into got1/got2, replies with the given responses
/// (ids must match the driver's request ids). Main thread asserts `got`
/// after join — no races (join is a happens-before).
const FakeWire = struct {
    got1: [2048]u8 = undefined,
    got1_len: usize = 0,
    got2: [2048]u8 = undefined,
    got2_len: usize = 0,

    fn run(self: *FakeWire, cmd_read: i32, resp_write: i32, replies: []const []const u8) void {
        var r = pipe.Reader.init(cmd_read);
        defer r.deinit(testing.allocator);
        for (replies, 0..) |reply, i| {
            const msg = (r.readMessage(testing.allocator, 5000) catch return) orelse return;
            defer testing.allocator.free(msg);
            const dst: []u8 = if (i == 0) self.got1[0..] else self.got2[0..];
            const len = @min(msg.len, dst.len);
            @memcpy(dst[0..len], msg[0..len]);
            if (i == 0) self.got1_len = len else self.got2_len = len;
            pipe.writeMessage(testing.allocator, resp_write, reply) catch return;
        }
    }

    fn got1Slice(self: *FakeWire) []const u8 {
        return self.got1[0..self.got1_len];
    }
    fn got2Slice(self: *FakeWire) []const u8 {
        return self.got2[0..self.got2_len];
    }
};

fn twoPipes() ![4]i32 {
    var cmd: [2]i32 = undefined;
    var resp: [2]i32 = undefined;
    if (std.os.linux.errno(std.os.linux.pipe2(&cmd, .{})) != .SUCCESS) return error.PipeFailed;
    if (std.os.linux.errno(std.os.linux.pipe2(&resp, .{})) != .SUCCESS) return error.PipeFailed;
    return .{ cmd[0], cmd[1], resp[0], resp[1] };
}

fn closePipes(fds: [4]i32) void {
    for (fds) |fd| _ = std.os.linux.close(fd);
}

/// Seed a page session s1/t1 via attachedToTarget event (single-pipe style:
/// writes into the driver's read pipe).
fn seedPage(d: *Driver, read_fd: i32) !void {
    try pipe.writeMessage(testing.allocator, read_fd, "{\"method\":\"Browser.attachedToTarget\",\"params\":{\"sessionId\":\"s1\",\"targetInfo\":{\"type\":\"page\",\"targetId\":\"t1\"}}}");
    try d.pump(1000);
}

const rwbs_intercepted =
    \\{"method":"Network.requestWillBeSent","sessionId":"s1","params":{"requestId":"r-1","isIntercepted":true,"url":"http://127.0.0.1:8333/ok.png","method":"GET","headers":[],"cause":"script","internalCause":"script"}}
;

test "driver: intercepted request registered with owning session" {
    const fds = try testPipe();
    defer {
        _ = std.os.linux.close(fds[0]);
        _ = std.os.linux.close(fds[1]);
    }
    var d = Driver.init(testing.allocator, fds[0], fds[1], false);
    defer d.deinit();

    try seedPage(&d, fds[1]);
    try writeFake(&d, fds, rwbs_intercepted);
    try d.pump(1000);

    const e = d.intercepted.get("r-1") orelse return error.TestUnexpected;
    try testing.expectEqualStrings("s1", e.session_id);
    try testing.expectEqualStrings("http://127.0.0.1:8333/ok.png", e.url);
    try testing.expectEqualStrings("GET", e.method);
    // Passive passthrough unaffected (Faz 3 invariant).
    try testing.expectEqual(@as(usize, 1), d.network_events.items.len);
    try testing.expect(std.mem.indexOf(u8, d.network_events.items[0], "requestWillBeSent") != null);
}

test "driver: non-intercepted request is not registered" {
    const fds = try testPipe();
    defer {
        _ = std.os.linux.close(fds[0]);
        _ = std.os.linux.close(fds[1]);
    }
    var d = Driver.init(testing.allocator, fds[0], fds[1], false);
    defer d.deinit();

    try seedPage(&d, fds[1]);
    try writeFake(&d, fds, "{\"method\":\"Network.requestWillBeSent\",\"sessionId\":\"s1\",\"params\":{\"requestId\":\"r-2\",\"isIntercepted\":false,\"url\":\"http://x/\",\"method\":\"GET\",\"headers\":[],\"cause\":\"script\",\"internalCause\":\"script\"}}");
    try d.pump(1000);

    try testing.expectEqual(@as(usize, 0), d.intercepted.count());
    // ...but the passive copy is still retained.
    try testing.expectEqual(@as(usize, 1), d.network_events.items.len);
}

test "driver: detachedFromTarget purges pending entries of that session" {
    const fds = try testPipe();
    defer {
        _ = std.os.linux.close(fds[0]);
        _ = std.os.linux.close(fds[1]);
    }
    var d = Driver.init(testing.allocator, fds[0], fds[1], false);
    defer d.deinit();

    try seedPage(&d, fds[1]);
    try writeFake(&d, fds, rwbs_intercepted);
    try d.pump(1000);
    try testing.expectEqual(@as(usize, 1), d.intercepted.count());

    try writeFake(&d, fds, "{\"method\":\"Browser.detachedFromTarget\",\"params\":{\"sessionId\":\"s1\",\"targetId\":\"t1\"}}");
    try d.pump(1000);
    try testing.expectEqual(@as(usize, 0), d.intercepted.count());
}

test "driver: continueInterceptedRequest sends resume on the owning session" {
    const fds = try twoPipes();
    defer closePipes(fds);
    var d = Driver.init(testing.allocator, fds[2], fds[1], false);
    defer d.deinit();

    try seedPage(&d, fds[3]);
    try pipe.writeMessage(testing.allocator, fds[3], rwbs_intercepted);
    try d.pump(1000);

    var fw = FakeWire{};
    const thread = try std.Thread.spawn(.{}, FakeWire.run, .{ &fw, fds[0], fds[3], &[_][]const u8{"{\"id\":1,\"result\":{}}"} });
    defer thread.join();

    try d.continueInterceptedRequest("r-1", "http://x/2", "POST", null, null, 2000);
    try testing.expectEqualStrings(
        "{\"id\":1,\"sessionId\":\"s1\",\"method\":\"Network.resumeInterceptedRequest\",\"params\":{\"requestId\":\"r-1\",\"url\":\"http://x/2\",\"method\":\"POST\"}}",
        fw.got1Slice(),
    );
    // The decision consumed the pending entry.
    try testing.expectEqual(@as(usize, 0), d.intercepted.count());
}

test "driver: fulfillInterceptedRequest wire carries schema names" {
    const fds = try twoPipes();
    defer closePipes(fds);
    var d = Driver.init(testing.allocator, fds[2], fds[1], false);
    defer d.deinit();

    try seedPage(&d, fds[3]);
    try pipe.writeMessage(testing.allocator, fds[3], rwbs_intercepted);
    try d.pump(1000);

    var fw = FakeWire{};
    const thread = try std.Thread.spawn(.{}, FakeWire.run, .{ &fw, fds[0], fds[3], &[_][]const u8{"{\"id\":1,\"result\":{}}"} });
    defer thread.join();

    const headers = [_]browser.Header{.{ .name = "Content-Type", .value = "text/javascript" }};
    try d.fulfillInterceptedRequest("r-1", 200, "OK", &headers, "d2luZG93Lng9MQ==", 2000);
    try testing.expectEqualStrings(
        "{\"id\":1,\"sessionId\":\"s1\",\"method\":\"Network.fulfillInterceptedRequest\",\"params\":{\"requestId\":\"r-1\",\"status\":200,\"statusText\":\"OK\",\"headers\":[{\"name\":\"Content-Type\",\"value\":\"text/javascript\"}],\"base64body\":\"d2luZG93Lng9MQ==\"}}",
        fw.got1Slice(),
    );
    try testing.expectEqual(@as(usize, 0), d.intercepted.count());
}

test "driver: abortInterceptedRequest wire carries errorCode" {
    const fds = try twoPipes();
    defer closePipes(fds);
    var d = Driver.init(testing.allocator, fds[2], fds[1], false);
    defer d.deinit();

    try seedPage(&d, fds[3]);
    try pipe.writeMessage(testing.allocator, fds[3], rwbs_intercepted);
    try d.pump(1000);

    var fw = FakeWire{};
    const thread = try std.Thread.spawn(.{}, FakeWire.run, .{ &fw, fds[0], fds[3], &[_][]const u8{"{\"id\":1,\"result\":{}}"} });
    defer thread.join();

    try d.abortInterceptedRequest("r-1", "NS_ERROR_ABORT", 2000);
    try testing.expectEqualStrings(
        "{\"id\":1,\"sessionId\":\"s1\",\"method\":\"Network.abortInterceptedRequest\",\"params\":{\"requestId\":\"r-1\",\"errorCode\":\"NS_ERROR_ABORT\"}}",
        fw.got1Slice(),
    );
    try testing.expectEqual(@as(usize, 0), d.intercepted.count());
}

test "driver: decision on unknown request errors, second decision consumed" {
    const fds = try testPipe();
    defer {
        _ = std.os.linux.close(fds[0]);
        _ = std.os.linux.close(fds[1]);
    }
    var d = Driver.init(testing.allocator, fds[0], fds[1], false);
    defer d.deinit();

    try testing.expectError(
        error.UnknownInterceptedRequest,
        d.abortInterceptedRequest("r-9", "NS_ERROR_ABORT", 500),
    );

    try seedPage(&d, fds[1]);
    try writeFake(&d, fds, rwbs_intercepted);
    try d.pump(1000);
    // Simulate a consumed decision without a wire: pop the entry directly.
    var taken = d.takeIntercepted("r-1") orelse return error.TestUnexpected;
    taken.deinit(testing.allocator);
    try testing.expectError(
        error.UnknownInterceptedRequest,
        d.continueInterceptedRequest("r-1", null, null, null, null, 500),
    );
}

test "driver: setInterception wire (page-scoped, schema name)" {
    const fds = try twoPipes();
    defer closePipes(fds);
    var d = Driver.init(testing.allocator, fds[2], fds[1], false);
    defer d.deinit();

    try seedPage(&d, fds[3]);

    var fw = FakeWire{};
    const thread = try std.Thread.spawn(.{}, FakeWire.run, .{ &fw, fds[0], fds[3], &[_][]const u8{"{\"id\":1,\"result\":{}}"} });
    defer thread.join();

    try d.setInterception("t1", true, 2000);
    try testing.expectEqualStrings(
        "{\"id\":1,\"sessionId\":\"s1\",\"method\":\"Network.setRequestInterception\",\"params\":{\"enabled\":true}}",
        fw.got1Slice(),
    );
}

test "driver: setContextInterception wire (root session)" {
    const fds = try twoPipes();
    defer closePipes(fds);
    var d = Driver.init(testing.allocator, fds[2], fds[1], false);
    defer d.deinit();

    var fw = FakeWire{};
    const thread = try std.Thread.spawn(.{}, FakeWire.run, .{ &fw, fds[0], fds[3], &[_][]const u8{"{\"id\":1,\"result\":{}}"} });
    defer thread.join();

    try d.setContextInterception("ctx-1", true, 2000);
    try testing.expectEqualStrings(
        "{\"id\":1,\"method\":\"Browser.setRequestInterception\",\"params\":{\"browserContextId\":\"ctx-1\",\"enabled\":true}}",
        fw.got1Slice(),
    );
}

test "driver: listInterceptedRequests renders route ids" {
    const fds = try testPipe();
    defer {
        _ = std.os.linux.close(fds[0]);
        _ = std.os.linux.close(fds[1]);
    }
    var d = Driver.init(testing.allocator, fds[0], fds[1], false);
    defer d.deinit();

    try seedPage(&d, fds[1]);
    try writeFake(&d, fds, rwbs_intercepted);
    try d.pump(1000);

    const list = try d.listInterceptedRequests();
    defer testing.allocator.free(list);
    try testing.expectEqualStrings(
        "[{\"requestId\":\"r-1\",\"url\":\"http://127.0.0.1:8333/ok.png\",\"method\":\"GET\"}]",
        list,
    );
}

test "driver: dispatchKeyEvent wire maps CDP type and carries schema params" {
    const fds = try twoPipes();
    defer closePipes(fds);
    var d = Driver.init(testing.allocator, fds[2], fds[1], false);
    defer d.deinit();

    try seedPage(&d, fds[3]);

    var fw = FakeWire{};
    const thread = try std.Thread.spawn(.{}, FakeWire.run, .{ &fw, fds[0], fds[3], &[_][]const u8{"{\"id\":1,\"result\":{}}"} });
    defer thread.join();

    try d.dispatchKeyEvent("t1", "keyDown", "a", 65, 0, "KeyA", false, "a", 2000);
    try testing.expectEqualStrings(
        "{\"id\":1,\"sessionId\":\"s1\",\"method\":\"Page.dispatchKeyEvent\",\"params\":{\"type\":\"keydown\",\"key\":\"a\",\"keyCode\":65,\"location\":0,\"code\":\"KeyA\",\"repeat\":false,\"text\":\"a\"}}",
        fw.got1Slice(),
    );
}

test "driver: dispatchKeyEvent rejects unknown CDP type" {
    const fds = try testPipe();
    defer {
        _ = std.os.linux.close(fds[0]);
        _ = std.os.linux.close(fds[1]);
    }
    var d = Driver.init(testing.allocator, fds[0], fds[1], false);
    defer d.deinit();
    try seedPage(&d, fds[1]);
    try testing.expectError(
        error.InvalidKeyType,
        d.dispatchKeyEvent("t1", "keyPress", "a", 65, 0, "KeyA", false, null, 500),
    );
}

test "driver: dispatchMouseEvent derives buttons when absent" {
    const fds = try twoPipes();
    defer closePipes(fds);
    var d = Driver.init(testing.allocator, fds[2], fds[1], false);
    defer d.deinit();

    try seedPage(&d, fds[3]);

    var fw = FakeWire{};
    const thread = try std.Thread.spawn(.{}, FakeWire.run, .{ &fw, fds[0], fds[3], &[_][]const u8{"{\"id\":1,\"result\":{}}"} });
    defer thread.join();

    try d.dispatchMouseEvent("t1", "mousePressed", 100, 20, "left", 8, 1, null, 2000);
    try testing.expectEqualStrings(
        "{\"id\":1,\"sessionId\":\"s1\",\"method\":\"Page.dispatchMouseEvent\",\"params\":{\"type\":\"mousedown\",\"button\":0,\"x\":100,\"y\":20,\"modifiers\":8,\"clickCount\":1,\"buttons\":1}}",
        fw.got1Slice(),
    );
}

test "driver: dispatchMouseEvent rejects mouseWheel (separate method)" {
    const fds = try testPipe();
    defer {
        _ = std.os.linux.close(fds[0]);
        _ = std.os.linux.close(fds[1]);
    }
    var d = Driver.init(testing.allocator, fds[0], fds[1], false);
    defer d.deinit();
    try seedPage(&d, fds[1]);
    try testing.expectError(
        error.InvalidMouseType,
        d.dispatchMouseEvent("t1", "mouseWheel", 1, 1, "none", 0, null, null, 500),
    );
}

test "driver: click sends mousedown then mouseup" {
    const fds = try twoPipes();
    defer closePipes(fds);
    var d = Driver.init(testing.allocator, fds[2], fds[1], false);
    defer d.deinit();

    try seedPage(&d, fds[3]);

    var fw = FakeWire{};
    const replies = [_][]const u8{ "{\"id\":1,\"result\":{}}", "{\"id\":2,\"result\":{}}" };
    const thread = try std.Thread.spawn(.{}, FakeWire.run, .{ &fw, fds[0], fds[3], &replies });
    defer thread.join();

    try d.click("t1", 50, 60, 2000);
    try testing.expectEqualStrings(
        "{\"id\":1,\"sessionId\":\"s1\",\"method\":\"Page.dispatchMouseEvent\",\"params\":{\"type\":\"mousedown\",\"button\":0,\"x\":50,\"y\":60,\"modifiers\":0,\"clickCount\":1,\"buttons\":1}}",
        fw.got1Slice(),
    );
    try testing.expectEqualStrings(
        "{\"id\":2,\"sessionId\":\"s1\",\"method\":\"Page.dispatchMouseEvent\",\"params\":{\"type\":\"mouseup\",\"button\":0,\"x\":50,\"y\":60,\"modifiers\":0,\"clickCount\":1,\"buttons\":0}}",
        fw.got2Slice(),
    );
}

test "driver: dispatchWheelEvent wire" {
    const fds = try twoPipes();
    defer closePipes(fds);
    var d = Driver.init(testing.allocator, fds[2], fds[1], false);
    defer d.deinit();

    try seedPage(&d, fds[3]);

    var fw = FakeWire{};
    const thread = try std.Thread.spawn(.{}, FakeWire.run, .{ &fw, fds[0], fds[3], &[_][]const u8{"{\"id\":1,\"result\":{}}"} });
    defer thread.join();

    try d.dispatchWheelEvent("t1", 10, 20, 0, 120, 0, 2000);
    try testing.expectEqualStrings(
        "{\"id\":1,\"sessionId\":\"s1\",\"method\":\"Page.dispatchWheelEvent\",\"params\":{\"x\":10,\"y\":20,\"deltaX\":0,\"deltaY\":120,\"deltaZ\":0,\"modifiers\":0}}",
        fw.got1Slice(),
    );
}

test "driver: insertText wire" {
    const fds = try twoPipes();
    defer closePipes(fds);
    var d = Driver.init(testing.allocator, fds[2], fds[1], false);
    defer d.deinit();

    try seedPage(&d, fds[3]);

    var fw = FakeWire{};
    const thread = try std.Thread.spawn(.{}, FakeWire.run, .{ &fw, fds[0], fds[3], &[_][]const u8{"{\"id\":1,\"result\":{}}"} });
    defer thread.join();

    try d.insertText("t1", "hello", 2000);
    try testing.expectEqualStrings(
        "{\"id\":1,\"sessionId\":\"s1\",\"method\":\"Page.insertText\",\"params\":{\"text\":\"hello\"}}",
        fw.got1Slice(),
    );
}
