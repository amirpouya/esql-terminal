#!/usr/bin/env python3
"""Self-contained tests for esql.py. Run: python3 test_esql.py"""
from __future__ import annotations

import io
import json
import os
import socket
import subprocess
import sys
import threading
import time
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import esql  # noqa: E402


PASSED = 0
FAILED = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  ok  {name}")
    else:
        FAILED += 1
        print(f"  FAIL {name}{(': ' + detail) if detail else ''}")


def section(title: str) -> None:
    print(f"\n[{title}]")


def test_splitter() -> None:
    section("split_complete_statements")

    s, rem = esql.split_complete_statements("FROM a; FROM b;")
    check("two simple statements", s == ["FROM a", "FROM b"] and rem == "", f"s={s} rem={rem!r}")

    s, rem = esql.split_complete_statements("FROM a; FROM b")
    check("trailing remainder kept", s == ["FROM a"] and rem == " FROM b", f"s={s} rem={rem!r}")

    s, rem = esql.split_complete_statements("FROM a // ignore ; me\n;")
    check("line comment // hides ;", s == ["FROM a // ignore ; me"], f"s={s}")

    s, rem = esql.split_complete_statements("FROM a /* ; nope */;")
    check("block comment hides ;", s == ["FROM a /* ; nope */"], f"s={s}")

    s, rem = esql.split_complete_statements('FROM a | WHERE x = """;not;""";')
    check("triple-quoted string hides ;", s == ['FROM a | WHERE x = """;not;"""'], f"s={s}")

    s, rem = esql.split_complete_statements('FROM a | KEEP `weird;name`;')
    check("backtick identifier hides ;", s == ['FROM a | KEEP `weird;name`'], f"s={s}")

    s, rem = esql.split_complete_statements(r'FROM a | WHERE x = "a\";b";')
    check("backslash escape inside string", s == [r'FROM a | WHERE x = "a\";b"'], f"s={s}")

    s, rem = esql.split_complete_statements("FROM a | WHERE x = 'it''s' OR y = ';'; FROM b;")
    check(
        "single quotes treated as strings",
        s == ["FROM a | WHERE x = 'it''s' OR y = ';'", "FROM b"],
        f"s={s}",
    )

    s, rem = esql.split_complete_statements(";; ; FROM a;")
    check("empty statements are skipped", s == ["FROM a"] and rem == "", f"s={s} rem={rem!r}")

    s, rem = esql.split_complete_statements(
        "// header comment\nFROM a; /* mid */ FROM b /* tail */;"
    )
    check(
        "leading/middle comments don't break splitting",
        s == ["// header comment\nFROM a", "/* mid */ FROM b /* tail */"],
        f"s={s}",
    )

    s, rem = esql.split_complete_statements('FROM a | WHERE r = """multi\nline ; ok"""; FROM b;')
    check(
        "triple-quoted string spans lines",
        s == ['FROM a | WHERE r = """multi\nline ; ok"""', "FROM b"],
        f"s={s}",
    )

    s, rem = esql.split_complete_statements(
        'SET project_routing="_alias:my-project";\nFROM logs*\n| STATS COUNT(*);'
    )
    check(
        "leading SET clause stays with following query",
        s == ['SET project_routing="_alias:my-project";\nFROM logs*\n| STATS COUNT(*)'] and rem == "",
        f"s={s} rem={rem!r}",
    )

    s, rem = esql.split_complete_statements(
        'SET a=1;\nFROM first;\nSET b=2;\nFROM second;'
    )
    check(
        "multiple SET-prefixed statements split after query terminators",
        s == ['SET a=1;\nFROM first', 'SET b=2;\nFROM second'] and rem == "",
        f"s={s} rem={rem!r}",
    )

    s, rem = esql.split_complete_statements(
        '// route hint\nSET project_routing="_alias:my-project";\nFROM logs*;'
    )
    check(
        "comments before leading SET are preserved",
        s == ['// route hint\nSET project_routing="_alias:my-project";\nFROM logs*'] and rem == "",
        f"s={s} rem={rem!r}",
    )

    s, rem = esql.split_complete_statements(
        'SET time_zone = "+05:00";\n'
        'TS k8s\n'
        '| WHERE @timestamp == "2024-05-10T00:04:49.000Z"\n'
        '| STATS by @timestamp, bucket = TBUCKET(3 hours)\n'
        '| SORT @timestamp\n'
        '| LIMIT 2;'
    )
    check(
        "leading SET clause stays with following TS query",
        s == [
            'SET time_zone = "+05:00";\n'
            'TS k8s\n'
            '| WHERE @timestamp == "2024-05-10T00:04:49.000Z"\n'
            '| STATS by @timestamp, bucket = TBUCKET(3 hours)\n'
            '| SORT @timestamp\n'
            '| LIMIT 2'
        ] and rem == "",
        f"s={s} rem={rem!r}",
    )


def test_parse_rest_requests() -> None:
    section("parse_rest_requests")

    script = """
PUT sample_data
{
  "mappings": {
    "properties": {
      "message": {
        "type": "keyword"
      }
    }
  }
}

PUT sample_data/_bulk
{"index": {}}
{"message": "hello"}
"""
    requests = esql.parse_rest_requests(script)
    check("two REST requests parsed", len(requests) == 2, f"requests={requests}")
    check("first request is PUT index", requests[0].method == "PUT" and requests[0].path == "sample_data")
    check("first request body preserved", '"mappings"' in (requests[0].body or ""), f"body={requests[0].body!r}")
    check("bulk body ends with newline", (requests[1].body or "").endswith("\n"), f"body={requests[1].body!r}")
    check(
        "bulk content type is ndjson",
        esql.content_type_for_rest_request(requests[1]).startswith("application/x-ndjson"),
    )

    try:
        esql.parse_rest_requests("FROM logs | LIMIT 1")
    except ValueError as exc:
        check("invalid REST script reports bad first line", "Expected REST request line" in str(exc), str(exc))
    else:
        check("invalid REST script reports bad first line", False)


def test_extract_line_col_errors() -> None:
    section("extract_line_col_errors")

    out = esql.extract_line_col_errors("line 1:8: token recognition error at: '>>='")
    check("single position parsed", out == [(1, 8, "token recognition error at: '>>='")], f"out={out}")

    out = esql.extract_line_col_errors(
        "Found 2 problems\nline 1:13: Unknown column [foo]\nline 2:1: missing FROM"
    )
    check(
        "verification multi-error parsed",
        out == [(1, 13, "Unknown column [foo]"), (2, 1, "missing FROM")],
        f"out={out}",
    )

    check("empty reason returns []", esql.extract_line_col_errors("") == [])
    check(
        "no positions returns []",
        esql.extract_line_col_errors("plain reason without positions") == [],
    )


def test_render_query_pointer() -> None:
    section("render_query_pointer")

    src = "FROM logs | WHERE foo >>= 1"
    out = esql.render_query_pointer(src, 1, 23)
    expected = "  FROM logs | WHERE foo >>= 1\n  " + " " * 22 + "^"
    check("caret aligns under column", out == expected, f"out={out!r}")

    out = esql.render_query_pointer("FROM a\nFROM b\nFROM c", 2, 6)
    check(
        "selects correct line in multi-line",
        out == "  FROM b\n  " + " " * 5 + "^",
        f"out={out!r}",
    )

    long_src = "x" * 200 + "BUG" + "y" * 200
    col = 201
    out = esql.render_query_pointer(long_src, 1, col, max_width=40)
    first_line = out.split("\n")[0].lstrip(" ")
    second_line = out.split("\n")[1]
    check("long line truncated with ellipses", first_line.startswith("…") and first_line.endswith("…"), f"first={first_line!r}")
    caret_index = second_line.index("^") - 2
    char_under = first_line[caret_index]
    check("caret points at expected character on truncated line", char_under == "x" or char_under == "B", f"char={char_under!r}")

    check("out-of-range line returns empty", esql.render_query_pointer("FROM a", 5, 1) == "")
    check("zero/negative line returns empty", esql.render_query_pointer("FROM a", 0, 1) == "")


def test_print_error_caret() -> None:
    section("print_error caret integration")

    raw = json.dumps(
        {
            "error": {
                "type": "parsing_exception",
                "reason": "line 1:23: token recognition error at: '>>='",
                "root_cause": [
                    {"type": "parsing_exception", "reason": "line 1:23: token recognition error at: '>>='"}
                ],
            },
            "status": 400,
        }
    )
    buf = io.StringIO()
    with redirect_stderr(buf):
        esql.print_error(raw, 400, "psql", query="FROM logs | WHERE foo >>= 1")
    output = buf.getvalue()
    check("ERROR line present", "ERROR: parsing_exception" in output, output)
    check("source line present", "FROM logs | WHERE foo >>= 1" in output, output)
    check("caret on its own line", any(line.strip() == "^" for line in output.splitlines()), output)
    check("STATUS line present", "STATUS: 400" in output, output)


def test_json_coloring_forced() -> None:
    section("JSON coloring")
    old_color = os.environ.get("ESQL_COLOR")
    os.environ["ESQL_COLOR"] = "1"
    try:
        buf = io.StringIO()
        with redirect_stdout(buf):
            esql.print_json({"ok": True})
        out = buf.getvalue()
        if esql.highlight is None:
            check("color unavailable without pygments", "\x1b[" not in out and '"ok"' in out, out)
        else:
            check("ANSI color emitted when forced", "\x1b[" in out and '"ok"' in out, out)
    finally:
        if old_color is None:
            os.environ.pop("ESQL_COLOR", None)
        else:
            os.environ["ESQL_COLOR"] = old_color


# ----------------------------------------------------------------------------
# End-to-end: spin up a fake ES, run esql.py against it, check output & timing.
# ----------------------------------------------------------------------------

class FakeESHandler(BaseHTTPRequestHandler):
    routes: dict[str, tuple[int, dict | str]] = {}

    def log_message(self, *_args, **_kw):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)  # discard
        path = self.path.split("?")[0]
        status, body = self.routes.get(path, (404, {"error": {"type": "not_found", "reason": path}}))
        encoded = json.dumps(body).encode("utf-8") if isinstance(body, dict) else body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self):
        path = self.path.split("?")[0]
        status, body = self.routes.get(path, (404, {"error": {"type": "not_found", "reason": path}}))
        encoded = json.dumps(body).encode("utf-8") if isinstance(body, dict) else body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def start_fake_es(routes: dict[str, tuple[int, dict | str]]) -> tuple[HTTPServer, str]:
    FakeESHandler.routes = routes
    httpd = HTTPServer(("127.0.0.1", 0), FakeESHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address
    return httpd, f"http://{host}:{port}"


def run_cli(url: str, query: str, *extra: str, timing_env: str = "") -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["ES_URL"] = url
    env["ES_USER"] = "u"
    env["ES_PASSWORD"] = "p"
    if timing_env:
        env["ES_TIMING"] = timing_env
    return subprocess.run(
        [sys.executable, os.path.join(HERE, "esql.py"), *extra],
        input=query.encode("utf-8"),
        capture_output=True,
        env=env,
        timeout=10,
    )


def test_e2e_success() -> None:
    section("end-to-end: success path")
    routes = {
        "/_query": (
            200,
            {
                "columns": [{"name": "id", "type": "long"}, {"name": "name", "type": "keyword"}],
                "values": [[1, "a"], [2, "b"]],
            },
        )
    }
    httpd, url = start_fake_es(routes)
    try:
        proc = run_cli(url, "FROM x;\n")
        out = proc.stdout.decode()
        err = proc.stderr.decode()
        check("exit 0 on success", proc.returncode == 0, f"rc={proc.returncode} err={err}")
        check("psql header rendered", "id" in out and "name" in out, out)
        check("row count footer", "(2 rows)" in out, out)
        check("no timing line by default", "Time:" not in err, err)
    finally:
        httpd.shutdown()


def test_e2e_timing_env() -> None:
    section("end-to-end: ES_TIMING=1")
    routes = {
        "/_query": (
            200,
            {"columns": [{"name": "id", "type": "long"}], "values": [[1]]},
        )
    }
    httpd, url = start_fake_es(routes)
    try:
        proc = run_cli(url, "FROM x;\n", timing_env="1")
        err = proc.stderr.decode()
        check("Time line present in stderr", "Time:" in err, err)
        check("timing reports ms", "ms" in err, err)
    finally:
        httpd.shutdown()


def test_e2e_profile_cli() -> None:
    section("end-to-end: --profile")
    seen: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_a, **_k): pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("utf-8")
            seen.append(json.loads(body))
            payload = json.dumps({"columns": [{"name": "n", "type": "long"}], "values": [[1]]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    host, port = httpd.server_address
    url = f"http://{host}:{port}"
    try:
        proc = run_cli(url, "FROM x;\n", "--profile")
        check("exit 0", proc.returncode == 0, proc.stderr.decode())
        check("server saw profile=true", seen == [{"query": "FROM x", "profile": True}], f"seen={seen}")
    finally:
        httpd.shutdown()


def test_e2e_indices_command() -> None:
    section("end-to-end: \\indices")
    routes = {
        "/_cat/indices": (
            200,
            "health status index    uuid pri rep docs.count\n"
            "green  open   my_index abc  1   0   42\n",
        )
    }
    httpd, url = start_fake_es(routes)
    try:
        proc = run_cli(url, "\\indices\n")
        out = proc.stdout.decode()
        err = proc.stderr.decode()
        check("exit 0", proc.returncode == 0, f"rc={proc.returncode} err={err}")
        check("_cat indices output printed", "my_index" in out and "docs.count" in out, out)
    finally:
        httpd.shutdown()


def test_e2e_indices_connection_reset() -> None:
    section("end-to-end: \\indices connection reset")

    class ResetHandler(BaseHTTPRequestHandler):
        def log_message(self, *_a, **_k): pass

        def do_GET(self):
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.connection.close()

    httpd = HTTPServer(("127.0.0.1", 0), ResetHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    host, port = httpd.server_address
    url = f"http://{host}:{port}"
    try:
        proc = run_cli(url, "\\indices\n")
        err = proc.stderr.decode()
        check("connection reset exits nonzero without traceback", proc.returncode != 0, f"rc={proc.returncode}")
        check("connection reset rendered", "Connection reset" in err or "Remote end closed" in err, err)
        check("traceback suppressed", "Traceback" not in err, err)
        check("port-forward hint shown", "port-forward" in err, err)
    finally:
        httpd.shutdown()


def test_connection_check() -> None:
    section("connection check")

    seen_auth: list[str | None] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_a, **_k): pass

        def do_GET(self):
            seen_auth.append(self.headers.get("Authorization"))
            if self.path == "/unauthorized/":
                payload = json.dumps(
                    {
                        "error": {
                            "type": "security_exception",
                            "reason": "unable to authenticate user [elastic]",
                        },
                        "status": 401,
                    }
                ).encode()
                self.send_response(401)
            else:
                payload = json.dumps({"tagline": "You Know, for Search"}).encode()
                self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    host, port = httpd.server_address
    url = f"http://{host}:{port}"
    try:
        ok_client = esql.ESQLClient(url, "psql", 1.0, False, user="u", password="p")
        check("connection check succeeds", ok_client.check_connection() == 0)
        check("connection check sends configured auth", seen_auth == ["Basic dTpw"], f"seen_auth={seen_auth}")

        err_client = esql.ESQLClient(f"{url}/unauthorized", "psql", 1.0, False)
        err = io.StringIO()
        with redirect_stderr(err):
            status = err_client.check_connection()
        output = err.getvalue()
        check("connection check returns HTTP status", status == 401, f"status={status} output={output}")
        check("connection check prints auth error", "security_exception" in output, output)
    finally:
        httpd.shutdown()


def test_e2e_common_api_commands() -> None:
    section("end-to-end: common API commands")
    seen: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_a, **_k): pass

        def do_GET(self):
            seen.append(self.path)
            if self.path.startswith("/_cat/"):
                payload = "ok\n".encode()
                content_type = "text/plain"
            else:
                payload = json.dumps({"ok": True}).encode()
                content_type = "application/json"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    host, port = httpd.server_address
    url = f"http://{host}:{port}"
    try:
        script = (
            "\\health\n"
            "\\nodes\n"
            "\\shards wikipedia\n"
            "\\aliases\n"
            "\\templates\n"
            "\\datastreams\n"
            "\\tasks\n"
            "\\count wikipedia\n"
            "\\mapping wikipedia\n"
            "\\get /_cluster/settings?pretty\n"
        )
        proc = run_cli(url, script)
        out = proc.stdout.decode()
        check("exit 0", proc.returncode == 0, proc.stderr.decode())
        check("json response pretty printed", '"ok": true' in out, out)
        check(
            "expected REST paths used",
            seen == [
                "/_cluster/health?pretty",
                "/_cat/nodes?v",
                "/_cat/shards/wikipedia?v",
                "/_cat/aliases?v",
                "/_cat/templates?v",
                "/_cat/data_streams?v",
                "/_cat/tasks?v",
                "/wikipedia/_count?pretty=true",
                "/wikipedia/_mapping?pretty",
                "/_cluster/settings?pretty",
            ],
            f"seen={seen}",
        )
    finally:
        httpd.shutdown()


def test_e2e_format_command() -> None:
    section("end-to-end: \\format")
    seen: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_a, **_k): pass

        def do_POST(self):
            seen.append(self.path)
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            payload = json.dumps({"columns": [{"name": "n", "type": "long"}], "values": [[1]]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    host, port = httpd.server_address
    url = f"http://{host}:{port}"
    try:
        proc = run_cli(url, "\\format json\nFROM x;\n")
        out = proc.stdout.decode()
        check("exit 0", proc.returncode == 0, proc.stderr.decode())
        check("format command reported json", "Format is json." in out, out)
        check("query used json response format", seen == ["/_query?format=json"], f"seen={seen}")
        check("json output pretty printed", '"columns": [' in out and '"values": [' in out, out)
    finally:
        httpd.shutdown()


def test_e2e_parse_error_caret() -> None:
    section("end-to-end: parse error caret")
    routes = {
        "/_query": (
            400,
            {
                "error": {
                    "type": "parsing_exception",
                    "reason": "line 1:23: token recognition error at: '>>='",
                },
                "status": 400,
            },
        )
    }
    httpd, url = start_fake_es(routes)
    try:
        proc = run_cli(url, "FROM logs | WHERE foo >>= 1;\n")
        err = proc.stderr.decode()
        check("ERROR rendered", "ERROR: parsing_exception" in err, err)
        check("source line present", "FROM logs | WHERE foo >>= 1" in err, err)
        check("caret line present", any(s.strip() == "^" for s in err.splitlines()), err)
        check("STATUS rendered", "STATUS: 400" in err, err)
        check("nonzero exit", proc.returncode != 0, f"rc={proc.returncode}")
    finally:
        httpd.shutdown()


def test_e2e_multistatement_pipe() -> None:
    section("end-to-end: multi-statement with comments via pipe")
    calls: list[str] = []

    class CountingHandler(BaseHTTPRequestHandler):
        def log_message(self, *_a, **_k): pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            payload = self.rfile.read(length).decode("utf-8")
            try:
                calls.append(json.loads(payload).get("query", ""))
            except Exception:
                calls.append(payload)
            body = json.dumps({"columns": [{"name": "n", "type": "long"}], "values": [[1]]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    httpd = HTTPServer(("127.0.0.1", 0), CountingHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address
    url = f"http://{host}:{port}"
    try:
        script = '// header\nFROM a /* ; */;\nFROM b | WHERE x = """ ; """;\n'
        proc = run_cli(url, script)
        check("exit 0 on multi-statement script", proc.returncode == 0, proc.stderr.decode())
        check("server saw 2 statements", len(calls) == 2, f"calls={calls}")
        check("first statement preserved", "FROM a" in calls[0] and ";" not in calls[0].split("/*")[0], f"call0={calls[0]!r}")
        check("second statement triple-quoted intact", '"""' in calls[1] and "FROM b" in calls[1], f"call1={calls[1]!r}")
    finally:
        httpd.shutdown()


def test_e2e_set_clause_stays_with_query() -> None:
    section("end-to-end: SET clause stays with query")
    calls: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_a, **_k): pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            payload = self.rfile.read(length).decode("utf-8")
            calls.append(json.loads(payload).get("query", ""))
            body = json.dumps({"columns": [{"name": "count"}], "values": [[1]]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    host, port = httpd.server_address
    url = f"http://{host}:{port}"
    try:
        script = (
            'SET project_routing="_alias:my-project";\n'
            'FROM logs*\n'
            '| STATS COUNT(*);\n'
            'SET time_zone = "+05:00";\n'
            'TS k8s\n'
            '| WHERE @timestamp == "2024-05-10T00:04:49.000Z"\n'
            '| STATS by @timestamp, bucket = TBUCKET(3 hours)\n'
            '| SORT @timestamp\n'
            '| LIMIT 2\n'
        )
        proc = run_cli(url, script)
        check("exit 0", proc.returncode == 0, proc.stderr.decode())
        check("server saw two queries", len(calls) == 2, f"calls={calls}")
        check(
            "SET clause included in FROM query",
            len(calls) > 0 and calls[0] == 'SET project_routing="_alias:my-project";\nFROM logs*\n| STATS COUNT(*)',
            f"calls={calls}",
        )
        check(
            "SET clause included in TS query",
            len(calls) > 1 and calls[1] == (
                'SET time_zone = "+05:00";\n'
                'TS k8s\n'
                '| WHERE @timestamp == "2024-05-10T00:04:49.000Z"\n'
                '| STATS BY @timestamp, bucket = TBUCKET(3 hours)\n'
                '| SORT @timestamp\n'
                '| LIMIT 2'
            ),
            f"calls={calls}",
        )
    finally:
        httpd.shutdown()


def test_e2e_rest_request_mode() -> None:
    section("end-to-end: --rest request blocks")
    seen: list[tuple[str, str, str | None, str]] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_a, **_k): pass

        def do_PUT(self):
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("utf-8") if length else ""
            seen.append((self.command, self.path, self.headers.get("Content-Type"), body))
            if self.path.endswith("/_bulk"):
                payload = json.dumps({"errors": False, "items": []}).encode()
            else:
                payload = json.dumps({"acknowledged": True, "index": self.path.strip("/")}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    host, port = httpd.server_address
    url = f"http://{host}:{port}"
    try:
        script = """
PUT sample_data
{
  "mappings": {
    "properties": {
      "message": {
        "type": "keyword"
      }
    }
  }
}

PUT sample_data/_bulk
{"index": {}}
{"message": "hello"}
"""
        proc = run_cli(url, script, "--rest")
        out = proc.stdout.decode()
        check("exit 0", proc.returncode == 0, proc.stderr.decode())
        check("two REST calls sent", len(seen) == 2, f"seen={seen}")
        check("index path normalized", seen[0][0] == "PUT" and seen[0][1] == "/sample_data", f"seen={seen}")
        check("json content type used", (seen[0][2] or "").startswith("application/json"), f"seen={seen}")
        check("bulk path normalized", seen[1][1] == "/sample_data/_bulk", f"seen={seen}")
        check("bulk content type used", (seen[1][2] or "").startswith("application/x-ndjson"), f"seen={seen}")
        check("bulk body keeps trailing newline", seen[1][3].endswith("\n"), f"body={seen[1][3]!r}")
        check("json REST responses printed", '"acknowledged": true' in out and '"errors": false' in out, out)
    finally:
        httpd.shutdown()


def test_e2e_rest_repl_mode_and_go() -> None:
    section("end-to-end: --rest REPL mode + \\g")
    seen: list[tuple[str, str, str]] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_a, **_k): pass

        def do_PUT(self):
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("utf-8") if length else ""
            seen.append((self.command, self.path, body))
            payload = json.dumps({"ok": True}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    host, port = httpd.server_address
    client = esql.ESQLClient(f"http://{host}:{port}", "psql", 1.0, False, rest_mode=True)
    try:
        buffer = ""
        status = 0
        out = io.StringIO()
        with redirect_stdout(out):
            for line in ("PUT sample_data", "{", '  "settings": {}', "}", "\\g"):
                buffer, status, should_quit = esql._dispatch_line(client, line, buffer, status)
                if should_quit:
                    break
        check("exit 0", status == 0, f"status={status} out={out.getvalue()}")
        check("interactive REST request sent", seen == [("PUT", "/sample_data", '{\n  "settings": {}\n}')], f"seen={seen}")
        check("interactive REST response printed", '"ok": true' in out.getvalue(), out.getvalue())
    finally:
        httpd.shutdown()


def test_parse_repl_command() -> None:
    section("parse_repl_command")

    check("plain text -> (None, '')", esql.parse_repl_command("FROM logs") == (None, ""))

    check("/q canonicalized", esql.parse_repl_command("/q") == ("quit", ""))
    check("\\q canonicalized", esql.parse_repl_command("\\q") == ("quit", ""))
    check("/timing alias t", esql.parse_repl_command("/t") == ("timing", ""))
    check("/format alias f", esql.parse_repl_command("/f json") == ("format", "json"))

    check("/i path captured", esql.parse_repl_command("/i ./queries.esql") == ("include", "./queries.esql"))
    check("\\set with spaces", esql.parse_repl_command("\\set name foo bar") == ("set", "name foo bar"))
    check("/watch interval", esql.parse_repl_command("/watch 5") == ("watch", "5"))
    check("/! shell command", esql.parse_repl_command("/! echo hi") == ("shell", "echo hi"))
    check("\\unset arg", esql.parse_repl_command("\\unset foo") == ("unset", "foo"))
    check("/conninfo no args", esql.parse_repl_command("/conninfo") == ("conninfo", ""))
    check("\\autokeywords command", esql.parse_repl_command("\\autokeywords") == ("autokeywords", ""))
    check("\\profile command", esql.parse_repl_command("\\profile") == ("profile", ""))
    check("\\rest command", esql.parse_repl_command("\\rest") == ("rest", ""))
    check("\\indices command", esql.parse_repl_command("\\indices") == ("indices", ""))
    check("\\di indices alias", esql.parse_repl_command("\\di") == ("indices", ""))
    check("\\health command", esql.parse_repl_command("\\health") == ("health", ""))
    check("\\nodes command", esql.parse_repl_command("\\nodes") == ("nodes", ""))
    check("\\ds datastreams alias", esql.parse_repl_command("\\ds") == ("datastreams", ""))
    check("\\get command", esql.parse_repl_command("\\get /_cluster/health") == ("get", "/_cluster/health"))

    check("unknown slash kept verbatim", esql.parse_repl_command("/whatever") == ("whatever", ""))


def test_dispatch_clear_command() -> None:
    section("dispatch: \\clear")
    client = esql.ESQLClient("http://127.0.0.1:9200", "psql", 1.0, False)
    buf = io.StringIO()
    with redirect_stdout(buf):
        new_buffer, status, should_quit = esql._dispatch_line(client, "\\clear", "FROM logs\n", 0)
    check("buffer reset", new_buffer == "", f"buffer={new_buffer!r}")
    check("status preserved", status == 0, f"status={status}")
    check("does not quit", should_quit is False, f"should_quit={should_quit}")
    check("clear screen sequence emitted", buf.getvalue() == "\x1b[2J\x1b[H", f"out={buf.getvalue()!r}")


def test_dispatch_format_command() -> None:
    section("dispatch: \\format")
    client = esql.ESQLClient("http://127.0.0.1:9200", "psql", 1.0, False)
    buf = io.StringIO()
    with redirect_stdout(buf):
        new_buffer, status, should_quit = esql._dispatch_line(client, "\\format json", "FROM logs\n", 0)
    check("buffer preserved", new_buffer == "FROM logs\n", f"buffer={new_buffer!r}")
    check("status preserved", status == 0, f"status={status}")
    check("does not quit", should_quit is False, f"should_quit={should_quit}")
    check("client format changed", client.fmt == "json" and "format=json" in client.url, f"fmt={client.fmt} url={client.url}")


def test_dispatch_rest_opens_editor_and_runs() -> None:
    section("dispatch: \\rest editor")
    seen: list[tuple[str, str, str]] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_a, **_k): pass

        def do_PUT(self):
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("utf-8") if length else ""
            seen.append((self.command, self.path, body))
            payload = json.dumps({"ok": True}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    host, port = httpd.server_address
    client = esql.ESQLClient(f"http://{host}:{port}", "psql", 1.0, False)
    old_editor = esql._cmd_editor
    editor_calls: list[tuple[str, str]] = []

    def fake_editor(buffer: str, suffix: str = ".esql") -> str:
        editor_calls.append((buffer, suffix))
        return 'PUT sample_data\n{\n  "settings": {}\n}\n'

    esql._cmd_editor = fake_editor
    try:
        buf = io.StringIO()
        with redirect_stdout(buf):
            new_buffer, status, should_quit = esql._dispatch_line(client, "\\rest", "", 0)
        check("editor REST exits 0", status == 0, f"status={status} out={buf.getvalue()}")
        check("editor REST clears buffer", new_buffer == "", f"buffer={new_buffer!r}")
        check("editor REST does not quit", should_quit is False, f"should_quit={should_quit}")
        check("editor REST uses HTTP suffix", editor_calls == [("", ".http")], f"editor_calls={editor_calls}")
        check("editor REST request sent", seen == [("PUT", "/sample_data", '{\n  "settings": {}\n}')], f"seen={seen}")
        check("editor REST response printed", '"ok": true' in buf.getvalue(), buf.getvalue())
    finally:
        esql._cmd_editor = old_editor
        httpd.shutdown()


def test_history_keeps_commands_out() -> None:
    section("history")

    class FakeHistory:
        def __init__(self):
            self.entries: list[str] = []

        def append_string(self, value: str) -> None:
            self.entries.append(value)

    client = esql.ESQLClient("http://127.0.0.1:9200", "psql", 1.0, False)
    history = FakeHistory()
    client.prompt_history = history

    with redirect_stdout(io.StringIO()):
        esql._dispatch_line(client, "\\h", "", 0)
        esql._dispatch_line(client, "\\format json", "", 0)

    check("slash commands are not added to history", history.entries == [], f"history={history.entries}")

    original_execute = client.execute
    client.execute = lambda _statement: 0
    try:
        status, remainder = esql._run_statements(client, "FROM logs | LIMIT 1;")
    finally:
        client.execute = original_execute
    check("executed query added to history", history.entries == ["FROM logs | LIMIT 1"], f"history={history.entries}")
    check("query execution status preserved", status == 0 and remainder == "", f"status={status} remainder={remainder!r}")


def test_prompt_history_suppresses_auto_adds() -> None:
    section("prompt history")
    if esql.QueryHistory is None:
        check("prompt-toolkit unavailable", True)
        return

    import tempfile
    with tempfile.NamedTemporaryFile("w+", delete=False, encoding="utf-8") as tf:
        path = tf.name
    try:
        history = esql.QueryHistory(path)
        history.append_string("\\h")
        history.store_string("\\q")
        client = esql.ESQLClient("http://127.0.0.1:9200", "psql", 1.0, False)
        client.prompt_history = history
        esql.add_query_to_history(client, "FROM logs | LIMIT 1")
        with open(path, encoding="utf-8") as handle:
            contents = handle.read()
        check("auto-added slash commands ignored", "\\h" not in contents and "\\q" not in contents, contents)
        check("manual query persisted", "FROM logs | LIMIT 1" in contents, contents)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def test_build_auth_headers() -> None:
    section("build_auth_headers")

    saved = {name: os.environ.get(name) for name in ("ES_NO_AUTH", "ES_API_KEY", "ES_USER", "ES_PASSWORD")}
    try:
        os.environ["ES_NO_AUTH"] = "1"
        os.environ.pop("ES_API_KEY", None)
        os.environ.pop("ES_USER", None)
        os.environ.pop("ES_PASSWORD", None)
        check("ES_NO_AUTH disables auth headers", esql.build_auth_headers() == [])

        os.environ.pop("ES_NO_AUTH", None)
        os.environ["ES_API_KEY"] = "id:secret"
        headers = esql.build_auth_headers()
        check(
            "api key form is base64 encoded",
            headers == [("Authorization", "ApiKey aWQ6c2VjcmV0")],
            f"headers={headers}",
        )
        headers = esql.build_auth_headers(user="cli_user", password="cli_password", prefer_basic=True)
        check(
            "explicit basic auth overrides api key",
            headers == [("Authorization", "Basic Y2xpX3VzZXI6Y2xpX3Bhc3N3b3Jk")],
            f"headers={headers}",
        )
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def test_apply_variables() -> None:
    section("apply_variables")

    out = esql.apply_variables("FROM ${idx} | LIMIT ${n}", {"idx": "logs", "n": "10"})
    check("simple substitution", out == "FROM logs | LIMIT 10", f"out={out!r}")

    out = esql.apply_variables("FROM ${idx}", {})
    check("empty variables -> identity", out == "FROM ${idx}", f"out={out!r}")

    out = esql.apply_variables("FROM ${unknown}", {"x": "y"})
    check("unknown name kept literal", out == "FROM ${unknown}", f"out={out!r}")

    out = esql.apply_variables("FROM a | WHERE x = '${name}'", {"name": "bob"})
    check("substitution inside string", out == "FROM a | WHERE x = 'bob'", f"out={out!r}")


def test_uppercase_esql_keywords() -> None:
    section("uppercase_esql_keywords")

    out = esql.uppercase_esql_keywords("from logs | where level == 'info' | limit 5")
    check(
        "mixed/lower case keywords capitalized",
        out == "FROM logs | WHERE level == 'info' | LIMIT 5",
        f"out={out!r}",
    )

    out = esql.uppercase_esql_keywords("FrOm logs | wHeRe a in (1,2)")
    check("in-between case keywords capitalized", out == "FROM logs | WHERE a IN (1,2)", f"out={out!r}")

    out = esql.uppercase_esql_keywords(
        "from logs // where limit\n| eval s = 'from where' | keep `from`; /* limit */"
    )
    check(
        "strings/comments/backticks preserved",
        out == "FROM logs // where limit\n| EVAL s = 'from where' | KEEP `from`; /* limit */",
        f"out={out!r}",
    )

    out = esql.uppercase_esql_keywords('from logs | eval r = """from | where""" | limit 1')
    check(
        "triple-quoted strings preserved",
        out == 'FROM logs | EVAL r = """from | where""" | LIMIT 1',
        f"out={out!r}",
    )


def test_e2e_set_and_substitute() -> None:
    section("end-to-end: \\set + variable substitution via \\g")
    seen: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_a, **_k): pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("utf-8")
            try:
                seen.append(json.loads(body).get("query", ""))
            except Exception:
                seen.append(body)
            payload = json.dumps({"columns": [{"name": "n"}], "values": [[1]]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    host, port = httpd.server_address
    url = f"http://{host}:{port}"
    try:
        script = "\\set idx logs\nFROM ${idx} | LIMIT 1;\n"
        proc = run_cli(url, script)
        check("exit 0", proc.returncode == 0, proc.stderr.decode())
        check("server saw substituted index", any("FROM logs" in q for q in seen), f"seen={seen}")
        check("no raw ${idx} reached server", all("${idx}" not in q for q in seen), f"seen={seen}")
    finally:
        httpd.shutdown()


def test_e2e_show_functions_via_slash_df() -> None:
    section("end-to-end: \\df runs SHOW FUNCTIONS")
    seen: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_a, **_k): pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("utf-8")
            try:
                seen.append(json.loads(body).get("query", ""))
            except Exception:
                seen.append(body)
            payload = json.dumps({"columns": [{"name": "name"}], "values": [["abs"]]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    host, port = httpd.server_address
    url = f"http://{host}:{port}"
    try:
        proc = run_cli(url, "\\df\n")
        check("exit 0", proc.returncode == 0, proc.stderr.decode())
        check("server saw SHOW FUNCTIONS", seen == ["SHOW FUNCTIONS"], f"seen={seen}")
    finally:
        httpd.shutdown()


def test_e2e_auto_keywords_env() -> None:
    section("end-to-end: ES_AUTO_KEYWORDS=1")
    seen: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_a, **_k): pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("utf-8")
            try:
                seen.append(json.loads(body).get("query", ""))
            except Exception:
                seen.append(body)
            payload = json.dumps({"columns": [{"name": "n"}], "values": [[1]]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    host, port = httpd.server_address
    url = f"http://{host}:{port}"
    try:
        env = dict(os.environ)
        env["ES_URL"] = url
        env["ES_USER"] = "u"
        env["ES_PASSWORD"] = "p"
        env["ES_AUTO_KEYWORDS"] = "1"
        proc = subprocess.run(
            [sys.executable, os.path.join(HERE, "esql.py")],
            input=b"from logs | where level == 'info' | limit 1;\n",
            capture_output=True,
            env=env,
            timeout=10,
        )
        check("exit 0", proc.returncode == 0, proc.stderr.decode())
        check("query was normalized", seen == ["FROM logs | WHERE level == 'info' | LIMIT 1"], f"seen={seen}")
    finally:
        httpd.shutdown()


def test_e2e_no_auth_env() -> None:
    section("end-to-end: ES_NO_AUTH=1")
    seen: list[str | None] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_a, **_k): pass

        def do_POST(self):
            seen.append(self.headers.get("Authorization"))
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            payload = json.dumps({"columns": [{"name": "n"}], "values": [[1]]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    host, port = httpd.server_address
    url = f"http://{host}:{port}"
    try:
        env = dict(os.environ)
        env["ES_URL"] = url
        env["ES_NO_AUTH"] = "1"
        env.pop("ES_API_KEY", None)
        env.pop("ES_USER", None)
        env.pop("ES_PASSWORD", None)
        proc = subprocess.run(
            [sys.executable, os.path.join(HERE, "esql.py")],
            input=b"FROM logs | LIMIT 1;\n",
            capture_output=True,
            env=env,
            timeout=10,
        )
        check("exit 0", proc.returncode == 0, proc.stderr.decode())
        check("authorization header omitted", seen == [None], f"seen={seen}")
    finally:
        httpd.shutdown()


def test_e2e_cli_connection_options() -> None:
    section("end-to-end: CLI connection options")
    seen: list[str | None] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_a, **_k): pass

        def do_POST(self):
            seen.append(self.headers.get("Authorization"))
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            payload = json.dumps({"columns": [{"name": "n"}], "values": [[1]]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    host, port = httpd.server_address
    url = f"http://{host}:{port}"
    try:
        env = dict(os.environ)
        env["ES_URL"] = "http://127.0.0.1:1"
        env["ES_USER"] = "env_user"
        env["ES_PASSWORD"] = "env_password"
        env["ES_API_KEY"] = "env_id:env_secret"
        proc = subprocess.run(
            [
                sys.executable,
                os.path.join(HERE, "esql.py"),
                "--url",
                url,
                "--user",
                "cli_user",
                "--password",
                "cli_password",
            ],
            input=b"FROM logs | LIMIT 1;\n",
            capture_output=True,
            env=env,
            timeout=10,
        )
        check("exit 0", proc.returncode == 0, proc.stderr.decode())
        check(
            "CLI credentials override environment",
            seen == ["Basic Y2xpX3VzZXI6Y2xpX3Bhc3N3b3Jk"],
            f"seen={seen}",
        )
    finally:
        httpd.shutdown()


def main() -> int:
    test_splitter()
    test_parse_rest_requests()
    test_extract_line_col_errors()
    test_render_query_pointer()
    test_print_error_caret()
    test_json_coloring_forced()
    test_parse_repl_command()
    test_dispatch_clear_command()
    test_dispatch_format_command()
    test_dispatch_rest_opens_editor_and_runs()
    test_history_keeps_commands_out()
    test_prompt_history_suppresses_auto_adds()
    test_build_auth_headers()
    test_apply_variables()
    test_uppercase_esql_keywords()
    test_e2e_success()
    test_e2e_timing_env()
    test_e2e_profile_cli()
    test_e2e_indices_command()
    test_e2e_indices_connection_reset()
    test_connection_check()
    test_e2e_common_api_commands()
    test_e2e_format_command()
    test_e2e_parse_error_caret()
    test_e2e_multistatement_pipe()
    test_e2e_set_clause_stays_with_query()
    test_e2e_rest_request_mode()
    test_e2e_rest_repl_mode_and_go()
    test_e2e_set_and_substitute()
    test_e2e_show_functions_via_slash_df()
    test_e2e_auto_keywords_env()
    test_e2e_no_auth_env()
    test_e2e_cli_connection_options()
    print(f"\n{PASSED} passed, {FAILED} failed")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
