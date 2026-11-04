/* cfhost — Cloudflare challenge runner on QuickJS (no browser).
 *
 * Cloudflare's interstitial ships an obfuscated "orchestrate" bundle that
 * solves the challenge inside the page. This host runs that bundle in the
 * same embedded engine the nox add-on uses (kahin/addons/nox), so a
 * cf_clearance can be minted without opening a browser.
 *
 * The host owns no network stack: every request the page makes is handed to
 * the Python driver over a length-prefixed pipe and the driver performs it
 * with the same TLS profile the scraper will replay with.
 *
 * Modes
 *   cfhost --solve            run the challenge, print a "D" frame and exit
 *
 * Frames (host -> driver, and driver -> host)
 *   R <len> <payload>   request  payload: METHOD\nURL\nheader\n...\n\nBODY
 *   S <len> <payload>   response payload: STATUS\nheader\n...\n\nBODY
 *   D <len> <payload>   done     payload: JSON state from the page
 *
 * Exit codes: 0 ok, 2 usage, 3 unreadable file, 4 driver pipe closed,
 *             5 page threw before it finished.
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <unistd.h>

#include "quickjs.h"

#define MAX_FRAME (16 * 1024 * 1024)
#define MAX_SCRIPTS 8

static void report_exception(JSContext *ctx, const char *what) {
    JSValue e = JS_GetException(ctx);
    const char *m = JS_ToCString(ctx, e);
    fprintf(stderr, "!error %s %s\n", what, m ? m : "unknown");
    if (m) JS_FreeCString(ctx, m);
    JS_FreeValue(ctx, e);
    fflush(stderr);
}

static char *read_file(const char *path) {
    FILE *f = fopen(path, "rb");
    if (!f) return NULL;
    if (fseek(f, 0, SEEK_END) != 0) { fclose(f); return NULL; }
    long n = ftell(f);
    if (n < 0 || n > 64L * 1024L * 1024L) { fclose(f); return NULL; }
    if (fseek(f, 0, SEEK_SET) != 0) { fclose(f); return NULL; }
    char *buf = (char *)malloc((size_t)n + 1);
    if (!buf) { fclose(f); return NULL; }
    size_t got = fread(buf, 1, (size_t)n, f);
    fclose(f);
    if (got != (size_t)n) { free(buf); return NULL; }
    buf[n] = '\0';
    return buf;
}

/* ---------------------------------------------------------------- framing */

static int write_all(int fd, const void *buf, size_t n) {
    const char *p = (const char *)buf;
    while (n) {
        ssize_t w = write(fd, p, n);
        if (w <= 0) return -1;
        p += w;
        n -= (size_t)w;
    }
    return 0;
}

static int write_frame(int fd, char type, const void *buf, size_t len) {
    unsigned char hdr[5];
    hdr[0] = (unsigned char)type;
    hdr[1] = (unsigned char)(len & 0xffu);
    hdr[2] = (unsigned char)((len >> 8) & 0xffu);
    hdr[3] = (unsigned char)((len >> 16) & 0xffu);
    hdr[4] = (unsigned char)((len >> 24) & 0xffu);
    if (write_all(fd, hdr, 5) != 0) return -1;
    if (len && write_all(fd, buf, len) != 0) return -1;
    return 0;
}

static int read_exact(int fd, void *buf, size_t n) {
    char *p = (char *)buf;
    while (n) {
        ssize_t r = read(fd, p, n);
        if (r <= 0) return -1;
        p += r;
        n -= (size_t)r;
    }
    return 0;
}

static int read_frame(int fd, char *type, char **out, size_t *out_len) {
    unsigned char hdr[5];
    if (read_exact(fd, hdr, 5) != 0) return -1;
    uint32_t len = (uint32_t)hdr[1] | ((uint32_t)hdr[2] << 8)
                 | ((uint32_t)hdr[3] << 16) | ((uint32_t)hdr[4] << 24);
    if (len > MAX_FRAME) return -1;
    char *buf = (char *)malloc((size_t)len + 1);
    if (!buf) return -1;
    if (len && read_exact(fd, buf, len) != 0) { free(buf); return -1; }
    buf[len] = '\0';
    *type = (char)hdr[0];
    *out = buf;
    *out_len = (size_t)len;
    return 0;
}

/* ---------------------------------------------------------------- http rpc */

/* Build "METHOD\nURL\nheader: value\n...\n\nBODY" from the JS arguments. */
static char *build_request(const char *method, const char *url,
                           const char *headers, const char *body, size_t *out_len) {
    size_t m = strlen(method), u = strlen(url), h = headers ? strlen(headers) : 0;
    size_t b = body ? strlen(body) : 0;
    size_t total = m + u + h + b + 8;
    char *buf = (char *)malloc(total + 1);
    if (!buf) return NULL;
    size_t n = 0;
    memcpy(buf + n, method, m); n += m; buf[n++] = '\n';
    memcpy(buf + n, url, u); n += u; buf[n++] = '\n';
    if (h) { memcpy(buf + n, headers, h); n += h; }
    buf[n++] = '\n';
    if (b) { memcpy(buf + n, body, b); n += b; }
    buf[n] = '\0';
    *out_len = n;
    return buf;
}

/* JS entry: __ka_http(method, url, headers, body) -> response object. */
static JSValue js_ka_http(JSContext *ctx, JSValueConst this_val,
                           int argc, JSValueConst *argv) {
    (void)this_val;
    const char *method = argc > 0 ? JS_ToCString(ctx, argv[0]) : NULL;
    const char *url = argc > 1 ? JS_ToCString(ctx, argv[1]) : NULL;
    const char *headers = argc > 2 ? JS_ToCString(ctx, argv[2]) : NULL;
    const char *body = argc > 3 ? JS_ToCString(ctx, argv[3]) : NULL;
    if (!method || !url) {
        if (method) JS_FreeCString(ctx, method);
        if (url) JS_FreeCString(ctx, url);
        if (headers) JS_FreeCString(ctx, headers);
        if (body) JS_FreeCString(ctx, body);
        return JS_ThrowTypeError(ctx, "__ka_http needs a method and a url");
    }

    size_t req_len = 0;
    char *req = build_request(method, url, headers, body, &req_len);
    JS_FreeCString(ctx, method);
    JS_FreeCString(ctx, url);
    if (headers) JS_FreeCString(ctx, headers);
    if (body) JS_FreeCString(ctx, body);
    if (!req) return JS_ThrowOutOfMemory(ctx);

    if (write_frame(STDOUT_FILENO, 'R', req, req_len) != 0) {
        free(req);
        return JS_ThrowInternalError(ctx, "driver pipe write failed");
    }
    free(req);

    char type = 0;
    char *resp = NULL;
    size_t resp_len = 0;
    if (read_frame(STDIN_FILENO, &type, &resp, &resp_len) != 0) {
        return JS_ThrowInternalError(ctx, "driver pipe closed");
    }
    if (type != 'S') {
        free(resp);
        return JS_ThrowInternalError(ctx, "unexpected driver frame");
    }

    /* STATUS\nheader\n...\n\nBODY */
    long status = 0;
    char *p = resp;
    char *eol = strchr(p, '\n');
    if (!eol) { free(resp); return JS_ThrowInternalError(ctx, "malformed response"); }
    *eol = '\0';
    status = strtol(p, NULL, 10);
    p = eol + 1;

    JSValue result = JS_NewObject(ctx);
    JS_SetPropertyStr(ctx, result, "status", JS_NewInt32(ctx, (int32_t)status));

    JSValue hdrs = JS_NewObject(ctx);
    char *header_buf = (char *)malloc(resp_len + 1);
    size_t header_len = 0;
    if (header_buf) header_buf[0] = '\0';

    while (p < resp + resp_len) {
        char *nl = strchr(p, '\n');
        size_t line_len = nl ? (size_t)(nl - p) : (size_t)(resp + resp_len - p);
        if (line_len == 0) { p = nl ? nl + 1 : resp + resp_len; break; }
        char *line = (char *)malloc(line_len + 1);
        if (!line) break;
        memcpy(line, p, line_len);
        line[line_len] = '\0';
        char *colon = strchr(line, ':');
        if (colon) {
            *colon = '\0';
            char *value = colon + 1;
            while (*value == ' ') value++;
            JS_SetPropertyStr(ctx, hdrs, line, JS_NewString(ctx, value));
        }
        free(line);
        if (header_buf) {
            memcpy(header_buf + header_len, p, line_len);
            header_len += line_len;
            header_buf[header_len++] = '\n';
            header_buf[header_len] = '\0';
        }
        if (!nl) break;
        p = nl + 1;
    }
    JS_SetPropertyStr(ctx, result, "headers", hdrs);
    if (header_buf) {
        JS_SetPropertyStr(ctx, result, "headerText", JS_NewString(ctx, header_buf));
        free(header_buf);
    }

    const char *body_text = (p <= resp + resp_len) ? p : "";
    size_t body_len = (size_t)(resp + resp_len - body_text);
    JS_SetPropertyStr(ctx, result, "body", JS_NewStringLen(ctx, body_text, body_len));
    JS_SetPropertyStr(ctx, result, "ok", JS_NewBool(ctx, status >= 200 && status < 400));
    free(resp);
    return result;
}

/* JS entry: __ka_log(message) -> undefined */
static JSValue js_ka_log(JSContext *ctx, JSValueConst this_val,
                         int argc, JSValueConst *argv) {
    (void)this_val;
    if (argc > 0) {
        const char *m = JS_ToCString(ctx, argv[0]);
        if (m) {
            fprintf(stderr, "[cf] %s\n", m);
            fflush(stderr);
            JS_FreeCString(ctx, m);
        }
    }
    return JS_UNDEFINED;
}

/* --------------------------------------------------------------- execution */

static int eval_script(JSContext *ctx, const char *code, const char *label) {
    JSValue v = JS_Eval(ctx, code, strlen(code), label, JS_EVAL_TYPE_GLOBAL);
    if (JS_IsException(v)) {
        report_exception(ctx, "script_threw");
        JS_FreeValue(ctx, v);
        return -1;
    }
    JS_FreeValue(ctx, v);
    return 0;
}

/* Run the timers the page queued and the microtasks they released. Bounded so
 * a self-rescheduling interval cannot spin forever. */
static void drain_timers(JSContext *ctx, int rounds) {
    static const char tick[] =
        "(function(){var f=__ka_timers.shift();if(!f)return 0;try{f({});}catch(e){"
        "__ka_note('tick-error', (e && e.stack) ? e.stack : String(e));}return 1;})()";
    JSContext *pctx = NULL;
    for (int i = 0; i < rounds; i++) {
        JSValue v = JS_Eval(ctx, tick, strlen(tick), "<tick>", JS_EVAL_TYPE_GLOBAL);
        if (JS_IsException(v)) { JS_FreeValue(ctx, v); return; }
        int fired = JS_VALUE_GET_INT(v);
        JS_FreeValue(ctx, v);
        while (JS_ExecutePendingJob(JS_GetRuntime(ctx), &pctx) > 0) {
            if (pctx) pctx = NULL;
        }
        if (!fired) return;
    }
}

/* JS entry: __ka_load(path) -> file content as a string. */
static JSValue js_ka_load(JSContext *ctx, JSValueConst this_val,
                          int argc, JSValueConst *argv) {
    (void)this_val;
    if (argc < 1) return JS_ThrowTypeError(ctx, "__ka_load needs a path");
    const char *path = JS_ToCString(ctx, argv[0]);
    if (!path) return JS_EXCEPTION;
    char *content = read_file(path);
    JS_FreeCString(ctx, path);
    if (!content) return JS_ThrowInternalError(ctx, "cannot read file");
    JSValue value = JS_NewStringLen(ctx, content, strlen(content));
    free(content);
    return value;
}

static void install_globals(JSContext *ctx) {
    JSValue http = JS_NewCFunction(ctx, js_ka_http, "http", 4);
    JSValue global = JS_GetGlobalObject(ctx);
    JS_SetPropertyStr(ctx, global, "__ka_http", http);
    JSValue log = JS_NewCFunction(ctx, js_ka_log, "log", 1);
    JSValue load = JS_NewCFunction(ctx, js_ka_load, "load", 1);
    JS_SetPropertyStr(ctx, global, "__ka_load", load);
    JS_SetPropertyStr(ctx, global, "__ka_log", log);
    JS_FreeValue(ctx, global);
}

static void usage(void) {
    fprintf(stderr,
        "usage: cfhost --solve\n"
        "env: CF_SHIM, CF_SCRIPTS (colon-separated),\n"
        "     CF_USER_AGENT, CF_LOCATION\n");
}

/* Kahin-owned knobs, read by the shim before anything else runs. */
static void set_string_global(JSContext *ctx, const char *name, const char *value) {
    JSValue global = JS_GetGlobalObject(ctx);
    JS_SetPropertyStr(ctx, global, name, JS_NewString(ctx, value ? value : ""));
    JS_FreeValue(ctx, global);
}

int main(int argc, char **argv) {
    if (argc < 2) { usage(); return 2; }
    if (strcmp(argv[1], "--solve") != 0) { usage(); return 2; }

    const char *shim_path = getenv("CF_SHIM");
    const char *script_list = getenv("CF_SCRIPTS");
    const char *html_file = getenv("CF_HTML_FILE");
    const char *ua = getenv("CF_USER_AGENT");
    const char *loc = getenv("CF_LOCATION");
    if (!shim_path || !*shim_path || !script_list || !*script_list) {
        usage();
        return 2;
    }

    JSRuntime *rt = JS_NewRuntime();
    if (!rt) { fprintf(stderr, "!error runtime_failed\n"); return 3; }
    JSContext *ctx = JS_NewContext(rt);
    if (!ctx) { JS_FreeRuntime(rt); return 3; }

    install_globals(ctx);

    set_string_global(ctx, "__KA_USER_AGENT", ua);
    set_string_global(ctx, "__KA_LOCATION", loc);
    set_string_global(ctx, "__KA_HTML_FILE", html_file);

    char *shim = read_file(shim_path);
    if (!shim) { fprintf(stderr, "!error shim_unreadable %s\n", shim_path); return 3; }
    eval_script(ctx, shim, shim_path);
    free(shim);
    drain_timers(ctx, 64);

    char *copy = strdup(script_list);
    if (!copy) return 3;
    char *scripts[MAX_SCRIPTS];
    int script_count = 0;
    for (char *tok = strtok(copy, ":"); tok && script_count < MAX_SCRIPTS; tok = strtok(NULL, ":")) {
        if (*tok) scripts[script_count++] = tok;
    }
    for (int i = 0; i < script_count; i++) {
        char *code = read_file(scripts[i]);
        if (!code) {
            fprintf(stderr, "!error script_unreadable %s\n", scripts[i]);
            free(copy);
            return 3;
        }
        eval_script(ctx, code, scripts[i]);
        free(code);
        drain_timers(ctx, 128);
    }
    free(copy);

    /* The page's lifecycle, in order: the challenge bundle is fetched and
     * evaluated while the document is still 'loading', then the document
     * becomes 'interactive' (DOMContentLoaded), then 'active' and finally
     * 'complete' with the load event. Each bundle decides when to start
     * from document.readyState, so the sequence has to be the browser's. */
    /* The page's lifecycle, in order. Each bundle decides when to start from
     * document.readyState:
     *   the challenge bundle waits for DOMContentLoaded (state 'loading'),
     *   the Turnstile bundle starts at once once the state is 'interactive'.
     * DOMContentLoaded is therefore fired twice: once for the challenge
     * bundle, once more after the Turnstile bundle has registered its own
     * listener. That mirrors a browser, where a script injected during
     * DOMContentLoaded still sees the event. */
    eval_script(ctx,
        "(function(){"
        "try{document.dispatchEvent(new Event('DOMContentLoaded'));}catch(e){}"
        "try{document.readyState='interactive';}catch(e){}"
        "try{document.dispatchEvent(new Event('readystatechange'));}catch(e){}"
        "try{if(typeof document.onreadystatechange==='function')document.onreadystatechange();}catch(e){}})()",
        "<domcontentloaded>");
    drain_timers(ctx, 64);
    eval_script(ctx,
        "(function(){"
        "try{document.dispatchEvent(new Event('DOMContentLoaded'));}catch(e){}"
        "try{document.readyState='active';}catch(e){}"
        "try{document.dispatchEvent(new Event('readystatechange'));}catch(e){}"
        "try{document.readyState='complete';}catch(e){}"
        "try{document.dispatchEvent(new Event('readystatechange'));}catch(e){}"
        "try{window.dispatchEvent(new Event('load'));}catch(e){}"
        "try{document.dispatchEvent(new Event('load'));}catch(e){}"
        "try{if(typeof window.onload==='function')window.onload();}catch(e){}})()",
        "<load>");

    /* Give the page a few more rounds: the challenge finishes with a
     * navigation or a cookie write, both of which land in a timer. */
    drain_timers(ctx, 512);


    static const char state[] =
        "JSON.stringify({"
        "cookie: document.cookie, "
        "href: location.href, "
        "title: document.title, "
        "events: __ka_events, "
        "wlog: __ka_wlog, "
        "turn: (typeof globalThis.turnstile), "
        "cfopt: (function(){var o=window._cf_chl_opt||{};var r={};"
        "for(var k in o){var v=o[k];"
        "if(typeof v==='boolean'||typeof v==='number'||typeof v==='string')r[k]=String(v).slice(0,80);}"
        "return r;})(), "
        "dom: (function(){var out=[];"
        "for(var i=0;i<__ka_all.length;i++){"
        "var e=__ka_all[i];var t=e.textContent||'';"
        "if(t&&t.trim())out.push(e.tagName+':'+t.trim().slice(0,300));"
        "}return out.slice(0,60);})()"
        "})";
    JSValue sv = JS_Eval(ctx, state, strlen(state), "<state>", JS_EVAL_TYPE_GLOBAL);
    if (JS_IsException(sv)) {
        report_exception(ctx, "state_threw");
        JS_FreeValue(ctx, sv);
        JS_FreeContext(ctx);
        JS_FreeRuntime(rt);
        return 5;
    }
    size_t state_len = 0;
    const char *state_str = JS_ToCStringLen(ctx, &state_len, sv);
    if (state_str) {
        write_frame(STDOUT_FILENO, 'D', state_str, state_len);
        JS_FreeCString(ctx, state_str);
    }
    JS_FreeValue(ctx, sv);

    JS_FreeContext(ctx);
    JS_FreeRuntime(rt);
    return 0;
}
