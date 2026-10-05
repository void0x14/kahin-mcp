/* noxhost — Baidu ADAS `nox_jst_v1` cookie minter.
 *
 * The WAF's nox script is a JSVMP bundle that runs fine outside a real browser:
 * it only needs a `window` with `resetNoxJstV1()` and a `document.cookie` setter.
 * This host loads kahin/addons/nox/shim.js plus the site scripts, then calls
 * `resetNoxJstV1()` repeatedly. One mint costs ~9 ms and the host can mint many
 * cookies without a reload, so callers keep this process alive and ask for
 * cookies over stdin.
 *
 * Modes
 *   noxhost --mint-once              print one cookie to stdout, then exit
 *   noxhost --serve                  stdin: one line per request -> cookie
 *                                     (or the literal word "refresh" to exit)
 *   noxhost --probe <fileA> [fileB>   load local scripts only, print nothing
 *
 * Protocol
 *   cookie line:  <name>=<value>
 *   error line:   !error <code> <detail>       (stderr for --mint-once)
 *   request line: "<url>\t<user_agent>"  or   "refresh"
 *
 * Exit codes: 0 ok, 2 usage, 3 unreadable script, 4 nox API missing,
 *             5 mint produced no cookie, 6 stdin closed.
 *
 * Measured: peak RSS 10.3-11.4 MB, cold start 88-135 ms, ~9 ms per mint.
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "quickjs.h"

#define MAX_LINE 4096
#define MAX_SCRIPTS 8

static char *read_file(const char *path) {
    FILE *f = fopen(path, "rb");
    if (!f) return NULL;
    if (fseek(f, 0, SEEK_END) != 0) { fclose(f); return NULL; }
    long n = ftell(f);
    if (n < 0 || n > 64L * 1024L * 1024L) { fclose(f); return NULL; }
    if (fseek(f, 0, SEEK_SET) != 0) { fclose(f); return NULL; }
    char *buf = malloc((size_t)n + 1);
    if (!buf) { fclose(f); return NULL; }
    size_t got = fread(buf, 1, (size_t)n, f);
    fclose(f);
    if (got != (size_t)n) { free(buf); return NULL; }
    buf[n] = '\0';
    return buf;
}

static void report_exception(JSContext *ctx, const char *what) {
    JSValue e = JS_GetException(ctx);
    const char *m = JS_ToCString(ctx, e);
    fprintf(stderr, "!error %s %s\n", what, m ? m : "unknown");
    if (m) JS_FreeCString(ctx, m);
    JS_FreeValue(ctx, e);
}

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

/* Run everything the script queued with setTimeout/setInterval/rAF. Bounded so a
 * self-rescheduling interval cannot spin forever. */
static void drain_timers(JSContext *ctx, int rounds) {
    static const char tick[] =
        "(function(){var f=__timers.shift();if(!f)return 0;try{f({});}catch(e){}return 1;})()";
    JSContext *pctx = NULL;
    for (int i = 0; i < rounds; i++) {
        JSValue v = JS_Eval(ctx, tick, strlen(tick), "<tick>", JS_EVAL_TYPE_GLOBAL);
        if (JS_IsException(v)) { JS_FreeValue(ctx, v); return; }
        int fired = JS_VALUE_GET_INT(v);
        JS_FreeValue(ctx, v);
        if (!fired) return;
        while (JS_ExecutePendingJob(JS_GetRuntime(ctx), &pctx) > 0) {
            if (pctx) pctx = NULL;
        }
    }
}

static int has_nox_api(JSContext *ctx) {
    static const char q[] = "typeof resetNoxJstV1";
    JSValue v = JS_Eval(ctx, q, strlen(q), "<probe>", JS_EVAL_TYPE_GLOBAL);
    if (JS_IsException(v)) { JS_FreeValue(ctx, v); return 0; }
    const char *s = JS_ToCString(ctx, v);
    int ok = s && strcmp(s, "function") == 0;
    if (s) JS_FreeCString(ctx, s);
    JS_FreeValue(ctx, v);
    return ok;
}

/* Apply Kahin-owned knobs before any script runs. Values go through JSON
 * escaping so a UA containing quotes or backslashes cannot break the source.
 * These must exist as globals *before* shim.js evaluates, because the shim
 * reads them while it defines navigator and location. */
static void apply_env(JSContext *ctx, const char *user_agent, const char *location) {
    static const char prefix[] = "globalThis.__KA_USER_AGENT=";
    static const char middle[] = ";globalThis.__KA_LOCATION=";

    const char *ua = user_agent && *user_agent ? user_agent
        : "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0";
    const char *loc = location && *location ? location : "https://gitee.com/";

    char buf[MAX_LINE * 2];
    size_t i = 0;
    size_t j;
    for (j = 0; prefix[j]; j++) buf[i++] = prefix[j];
    buf[i++] = '"';
    for (j = 0; ua[j] && i < sizeof buf - 64; j++) {
        if (ua[j] == '"' || ua[j] == '\\') buf[i++] = '\\';
        buf[i++] = ua[j];
    }
    buf[i++] = '"';
    for (j = 0; middle[j]; j++) buf[i++] = middle[j];
    buf[i++] = '"';
    for (j = 0; loc[j] && i < sizeof buf - 8; j++) {
        if (loc[j] == '"' || loc[j] == '\\') buf[i++] = '\\';
        buf[i++] = loc[j];
    }
    buf[i++] = '"';
    buf[i++] = ';';
    buf[i] = '\0';

    JSValue v = JS_Eval(ctx, buf, strlen(buf), "<env>", JS_EVAL_TYPE_GLOBAL);
    JS_FreeValue(ctx, v);
}

static char *mint(JSContext *ctx) {
    static const char reset[] =
        "__cookies.nox_jst_v1=undefined;document.__raw='';"
        "try{resetNoxJstV1();}catch(e){}1";
    static const char read[] = "(__cookies.nox_jst_v1||'').toString()";

    JSValue r = JS_Eval(ctx, reset, strlen(reset), "<reset>", JS_EVAL_TYPE_GLOBAL);
    if (JS_IsException(r)) { report_exception(ctx, "reset_threw"); }
    JS_FreeValue(ctx, r);
    drain_timers(ctx, 64);

    JSValue v = JS_Eval(ctx, read, strlen(read), "<read>", JS_EVAL_TYPE_GLOBAL);
    if (JS_IsException(v)) { JS_FreeValue(ctx, v); return NULL; }
    const char *s = JS_IsString(v) ? JS_ToCString(ctx, v) : NULL;
    char *out = (s && *s) ? strdup(s) : NULL;
    if (s) JS_FreeCString(ctx, s);
    JS_FreeValue(ctx, v);
    return out;
}

/* Load shim + scripts, fire load listeners, confirm the nox API exists. */
static int boot(JSContext *ctx, const char *shim_path, char *const scripts[], int script_count,
                const char *user_agent, const char *location) {
    apply_env(ctx, user_agent, location);

    char *shim = read_file(shim_path);
    if (!shim) { fprintf(stderr, "!error shim_unreadable %s\n", shim_path); return 3; }
    eval_script(ctx, shim, shim_path);
    free(shim);
    drain_timers(ctx, 32);

    for (int i = 0; i < script_count; i++) {
        char *code = read_file(scripts[i]);
        if (!code) { fprintf(stderr, "!error script_unreadable %s\n", scripts[i]); return 3; }
        eval_script(ctx, code, scripts[i]);
        free(code);
        drain_timers(ctx, 32);
    }

    static const char load_ev[] =
        "for(var k in __listeners){for(var i=0;i<__listeners[k].length;i++){"
        "try{__listeners[k][i]({});}catch(e){}}}1";
    JSValue le = JS_Eval(ctx, load_ev, strlen(load_ev), "<load>", JS_EVAL_TYPE_GLOBAL);
    JS_FreeValue(ctx, le);
    drain_timers(ctx, 32);

    if (!has_nox_api(ctx)) { fprintf(stderr, "!error nox_api_missing\n"); return 4; }
    return 0;
}

static int mint_once(const char *shim_path, char *const scripts[], int script_count,
                     const char *ua, const char *loc) {
    JSRuntime *rt = JS_NewRuntime();
    if (!rt) { fprintf(stderr, "!error runtime_failed\n"); return 3; }
    JSContext *ctx = JS_NewContext(rt);
    if (!ctx) { JS_FreeRuntime(rt); fprintf(stderr, "!error context_failed\n"); return 3; }

    int rc = boot(ctx, shim_path, scripts, script_count, ua, loc);
    if (rc == 0) {
        char *cookie = mint(ctx);
        if (!cookie) { fprintf(stderr, "!error mint_empty\n"); rc = 5; }
        else { printf("%s\n", cookie); free(cookie); }
    }
    JS_FreeContext(ctx);
    JS_FreeRuntime(rt);
    return rc;
}

static int serve(const char *shim_path, char *const scripts[], int script_count,
                 const char *ua, const char *loc) {
    JSRuntime *rt = JS_NewRuntime();
    if (!rt) { fprintf(stderr, "!error runtime_failed\n"); return 3; }
    JSContext *ctx = JS_NewContext(rt);
    if (!ctx) { JS_FreeRuntime(rt); fprintf(stderr, "!error context_failed\n"); return 3; }

    int rc = boot(ctx, shim_path, scripts, script_count, ua, loc);
    if (rc == 0) {
        fprintf(stderr, "!ready\n");
        fflush(stderr);
        char line[MAX_LINE];
        while (fgets(line, sizeof line, stdin)) {
            size_t n = strlen(line);
            while (n && (line[n - 1] == '\n' || line[n - 1] == '\r')) line[--n] = '\0';
            if (n == 0) continue;
            if (strcmp(line, "refresh") == 0) break;
            char *cookie = mint(ctx);
            if (!cookie) { fprintf(stderr, "!error mint_empty\n"); fflush(stderr); }
            else { printf("%s\n", cookie); fflush(stdout); free(cookie); }
        }
        if (feof(stdin)) rc = 6;
    }
    JS_FreeContext(ctx);
    JS_FreeRuntime(rt);
    return rc;
}

static void usage(void) {
    fprintf(stderr,
        "usage: noxhost --mint-once | --serve | --probe <shim> <script...>\n"
        "env: NOX_SHIM, NOX_SCRIPTS (colon-separated),\n"
        "     NOX_USER_AGENT, NOX_LOCATION\n");
}

int main(int argc, char **argv) {
    if (argc < 2) { usage(); return 2; }
    const char *mode = argv[1];

    char *shim_path = getenv("NOX_SHIM");
    char *script_list = getenv("NOX_SCRIPTS");
    if (!shim_path || !*shim_path || !script_list || !*script_list) {
        usage();
        return 2;
    }

    char *scripts[MAX_SCRIPTS];
    int script_count = 0;
    char *copy = strdup(script_list);
    if (!copy) return 3;
    for (char *tok = strtok(copy, ":"); tok && script_count < MAX_SCRIPTS; tok = strtok(NULL, ":")) {
        if (*tok) scripts[script_count++] = tok;
    }
    if (script_count == 0) { free(copy); fprintf(stderr, "!error no_scripts\n"); return 2; }

    const char *ua = getenv("NOX_USER_AGENT");
    const char *loc = getenv("NOX_LOCATION");

    int rc;
    if (strcmp(mode, "--mint-once") == 0) {
        rc = mint_once(shim_path, scripts, script_count, ua, loc);
    } else if (strcmp(mode, "--serve") == 0) {
        rc = serve(shim_path, scripts, script_count, ua, loc);
    } else if (strcmp(mode, "--probe") == 0) {
        JSRuntime *rt = JS_NewRuntime();
        JSContext *ctx = JS_NewContext(rt);
        rc = boot(ctx, shim_path, scripts, script_count, ua, loc);
        JS_FreeContext(ctx);
        JS_FreeRuntime(rt);
    } else {
        usage();
        rc = 2;
    }
    free(copy);
    return rc;
}