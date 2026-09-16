"""Integration tests for oracle.py — MCP Server."""

import json
import subprocess
import sys
import time


def _start_server() -> subprocess.Popen:
    proc = subprocess.Popen(
        [sys.executable, "-c", "from kahin.oracle import main; main()"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, cwd=".",
    )
    proc.stdin.write('{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"0.1.0","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}}\n')
    proc.stdin.flush()
    json.loads(proc.stdout.readline())
    proc.stdin.write('{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
    proc.stdin.flush()
    time.sleep(0.1)
    return proc


def test_server_initialize_and_list_tools() -> None:
    proc = _start_server()
    proc.stdin.write('{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}\n')
    proc.stdin.flush()
    resp = json.loads(proc.stdout.readline())
    tools = resp["result"]["tools"]
    tool_names = [t["name"] for t in tools]
    assert "kahin_list_domains" in tool_names
    assert "kahin_get_command" in tool_names
    assert "kahin_validate_command" in tool_names
    assert "kahin_error_decode" in tool_names
    assert "kahin_find_concept" in tool_names
    assert "kahin_browser_start" in tool_names
    assert "kahin_navigate" in tool_names
    assert "kahin_screenshot" in tool_names
    assert "kahin_ocr" in tool_names
    assert "kahin_execute_cdp" in tool_names
    assert "kahin_list_sessions" in tool_names
    assert "kahin_event_history" in tool_names
    assert "kahin_pattern_learn" in tool_names
    assert "kahin_pattern_query" in tool_names
    assert "kahin_pattern_suggest" in tool_names
    assert "kahin_pattern_forget" in tool_names
    assert "kahin_pattern_stats" in tool_names
    assert "kahin_crawl_events" in tool_names
    assert "kahin_visualize_data" in tool_names
    assert "kahin_extension_prepare" in tool_names
    # Faz 9 Task 3: 32 existing + 65 new Juggler-native tools = 97.
    # Gap C: +2 upload tools (set_file_chooser_intercept, upload_files) = 99.
    # Gap D: +4 screencast tools (start, frame, stop, pending) = 103.
    # Gap E: +1 accessibility tool (kahin_mirage_accessibility_tree) = 104.
    # Faz 11: +5 adaptive DOM stream tools + context cleanup = 110.
    # Faz 1 reliability: +8 expect/check/select/dblclick/drag/wait tools = 118.
    # Task 11 route adds one native network tool = 119.
    # Faz 2 Task 2: +1 agent-native snapshot tool = 120.
    # Faz 2 Task 3: +1 fill_form tool = 121.
    # Faz 2 Task 4: +2 session state save/load tools = 123.
    # Faz 2 Task 5: +5 identity tools (new/save/list/delete/report) = 128.
    # Faz 2 Task 6: +1 agent status overview tool = 129.
    # Faz 3 Task 1: +1 stealth audit tool = 130.
    # Faz 3 Task 3: +2 humanized input tools (mouse_trajectory, click_humanized) = 132.
    #   (kahin_mirage_key_text already exists — cadence params replaced the
    #   fast-path registration in place, so it is NOT double-counted.)
    # Faz 3 Task 4: +4 identity pin tools (pin/unpin/pins/for_domain) = 136.
    # Faz 3 Task 5: +2 proxy/geo + fingerprint report tools = 138.
    # Faz 4 Task 3: +1 engine stats tool (kahin_engine_stats) = 139.
    # Faz 4 crawler contract: +1 challenge status + 6 single-engine crawler
    # lifecycle tools = 146. Live crawl event deltas and visualization add
    # Extension staging adds 1 = 149. OCR adds 1 = 150. Live watch adds 2 = 152;
    # cf_clear/status add 2 = 154.
    assert len(tools) == 154
    assert "kahin_mirage_watch_start" in tool_names
    assert "kahin_mirage_watch_stop" in tool_names
    assert "kahin_mirage_dom_start" in tool_names
    assert "kahin_mirage_dom_snapshot" in tool_names
    assert "kahin_mirage_dom_events" in tool_names
    assert "kahin_mirage_dom_action" in tool_names
    assert "kahin_mirage_dom_stop" in tool_names
    assert "kahin_mirage_snapshot" in tool_names
    assert "kahin_mirage_fill_form" in tool_names
    assert "kahin_mirage_state_save" in tool_names
    assert "kahin_mirage_state_load" in tool_names
    assert "kahin_identity_new" in tool_names
    assert "kahin_identity_save" in tool_names
    assert "kahin_identity_list" in tool_names
    assert "kahin_identity_delete" in tool_names
    assert "kahin_identity_report" in tool_names
    assert "kahin_agent_status" in tool_names
    assert "kahin_engine_stats" in tool_names
    assert "kahin_stealth_audit" in tool_names
    assert "kahin_mirage_mouse_trajectory" in tool_names
    assert "kahin_mirage_click_humanized" in tool_names
    assert "kahin_identity_pin" in tool_names
    assert "kahin_identity_unpin" in tool_names
    assert "kahin_identity_pins" in tool_names
    assert "kahin_identity_for_domain" in tool_names
    assert "kahin_fingerprint_report" in tool_names
    assert "kahin_proxy_resolve" in tool_names
    assert "kahin_challenge_status" in tool_names
    for name in [
        "kahin_crawl_start",
        "kahin_crawl_status",
        "kahin_crawl_results",
        "kahin_crawl_pause",
        "kahin_crawl_resume",
        "kahin_crawl_stop",
    ]:
        assert name in tool_names
    for name in [
        "kahin_mirage_query",
        "kahin_mirage_click",
        "kahin_mirage_type",
        "kahin_mirage_key_press",
        "kahin_mirage_reload",
        "kahin_mirage_tab_new",
        "kahin_mirage_tab_list",
        "kahin_mirage_context_close",
        "kahin_mirage_network_requests",
        "kahin_mirage_get_response_body",
        "kahin_mirage_console_log",
        "kahin_mirage_errors_list",
        "kahin_mirage_cookie_get",
        "kahin_mirage_storage_local_get",
        "kahin_mirage_set_user_agent",
        "kahin_mirage_set_viewport",
        "kahin_mirage_dialog_accept",
        "kahin_mirage_download_list",
        "kahin_mirage_worker_list",
        "kahin_mirage_websocket_list",
        "kahin_mirage_set_file_chooser_intercept",
        "kahin_mirage_upload_files",
        "kahin_mirage_screencast_start",
        "kahin_mirage_screencast_frame",
        "kahin_mirage_screencast_stop",
        "kahin_mirage_screencast_pending",
        "kahin_engine_health",
        "kahin_mirage_expect",
        "kahin_mirage_check",
        "kahin_mirage_uncheck",
        "kahin_mirage_select_option",
        "kahin_mirage_dblclick",
        "kahin_mirage_drag",
        "kahin_mirage_wait_for_text",
        "kahin_mirage_wait_for_timeout",
        "kahin_mirage_route",
    ]:
        assert name in tool_names, f"missing tool {name}"
    proc.terminate()


def test_kahin_list_domains() -> None:
    proc = _start_server()
    proc.stdin.write('{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"kahin_list_domains","arguments":{}}}\n')
    proc.stdin.flush()
    resp = json.loads(proc.stdout.readline())
    text = resp["result"]["content"][0]["text"]
    data = json.loads(text)
    assert len(data) == 56
    assert data[0]["domain"] == "Accessibility"
    proc.terminate()


def test_kahin_get_command() -> None:
    proc = _start_server()
    proc.stdin.write('{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"kahin_get_command","arguments":{"domain":"Page","command":"navigate"}}}\n')
    proc.stdin.flush()
    resp = json.loads(proc.stdout.readline())
    cmd = json.loads(resp["result"]["content"][0]["text"])
    assert cmd["name"] == "navigate"
    assert len(cmd["parameters"]) == 5
    proc.terminate()


def test_kahin_validate_command() -> None:
    proc = _start_server()
    proc.stdin.write('{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"kahin_validate_command","arguments":{"domain":"Page","command":"navigate","parameters":{"urll":"test"}}}}\n')
    proc.stdin.flush()
    resp = json.loads(proc.stdout.readline())
    val = json.loads(resp["result"]["content"][0]["text"])
    assert val["valid"] is False
    proc.terminate()


def test_kahin_error_decode() -> None:
    proc = _start_server()
    proc.stdin.write('{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"kahin_error_decode","arguments":{"error_code":-32601,"error_message":"Method not found: Page.navigat"}}}\n')
    proc.stdin.flush()
    resp = json.loads(proc.stdout.readline())
    err = json.loads(resp["result"]["content"][0]["text"])
    assert err["code"] == -32601
    assert "Method not found" in err["name"]
    proc.terminate()
