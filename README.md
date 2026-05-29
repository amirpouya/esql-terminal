# esql-terminal

Tiny psql-style ES|QL terminal for Elasticsearch.

- Interactive REPL: type/paste multiline ES|QL; complete statements execute on `;`.
- ES|QL `SET ...;` preambles stay attached to the query that follows.
- Arrow-key history (persisted to `~/.esql_history`).
- `Ctrl+C` aborts current input/query and clears buffer instead of exiting the app.
- Quit with `/q`, `\q`, `quit`, or `exit`.
- Default output is a psql-style table; `--format json|txt|csv|yaml` also supported.
- Friendly error formatting for Elasticsearch error responses, including query line/caret pointers for parse errors.
- Optional prompt coloring and autocomplete (via `prompt-toolkit` + `pygments`).
- Optional REST request mode for Elasticsearch Dev Tools-style `GET`/`PUT`/`POST` blocks, including `_bulk`.

## Install

This project is intentionally dependency-light and runs with the Python standard library.

```bash
chmod +x ./esql.py
python3 ./esql.py --help
```

## Usage

```bash
./esql.py                 # interactive REPL
./esql.py query.esql      # run a file
./esql.py < query.esql    # run from stdin
./esql.py --url http://localhost:9200 --user elastic --password secret
./esql.py --timing        # print elapsed query time
./esql.py --profile       # send {"profile": true} with each ES|QL request
./esql.py --no-auth       # talk to an unsecured local cluster without auth headers
./esql.py --no-auto-keywords  # disable keyword auto-uppercase (enabled by default)
./esql.py --rest requests.http  # run Elasticsearch REST request blocks
./esql.py --rest          # start the interactive REPL in REST request mode
```

## REST request mode

Use `--rest` when a file or stdin contains Elasticsearch API request blocks instead
of ES|QL statements. Request blocks start with an HTTP method and path; the body is
read until the next request line. Paths can be written with or without a leading
slash. `_bulk`, `_msearch`, and `_mget` bodies are sent as newline-delimited JSON.

```bash
./esql.py --rest < setup.http
```

In the interactive REPL, use `\rest` to open `$EDITOR`/`$VISUAL`, write or paste
one or more REST request blocks, and run them when the editor exits. The temp file
uses a `.http` suffix so editors can apply REST/HTTP syntax highlighting when
available. You can also start in inline REST mode with `./esql.py --rest`; in that
mode, paste request blocks directly into the prompt and run the buffer with `\g`.

Example:

```http
PUT sample_data
{
  "mappings": {
    "properties": {
      "client_ip": {
        "type": "ip"
      },
      "message": {
        "type": "keyword"
      }
    }
  }
}

PUT sample_data/_bulk
{"index": {}}
{"@timestamp": "2023-10-23T12:15:03.360Z", "client_ip": "172.21.2.162", "message": "Connected to 10.1.0.3", "event_duration": 3450233}
```

### Pasting `curl` commands

REST mode also recognizes `curl ...` invocations, so you can paste a command
straight from a docs page or shell history without rewriting it as a method/path
block. Multi-line bodies (single-quoted with embedded newlines) and trailing
backslash continuations are both supported.

The host portion of the URL and any auth-related flags (`-u`, `-H`, etc.) are
stripped — the request is forwarded to the cluster `esql.py` is configured to
talk to using its own credentials. Method is taken from `-X`, or inferred as
`POST` when `-d` is present, otherwise `GET`.

```bash
curl -u elastic:password -H "Content-Type: application/json" \
  "127.0.0.1:9200/_query?format=txt" -d '
{
  "query":
    "FROM test | EVAL c = CONCAT(x::keyword, \": \", message) | WHERE x > 1 AND c LIKE \"*G*\""
}
'
```

The example above is sent to the configured cluster as `POST /_query?format=txt`
with the JSON body intact.

## Load Wikipedia Sample Data

Load random Wikipedia page summaries into a local Elasticsearch index:

```bash
./load_wikipedia.sh
```

Useful options:

```bash
WIKI_INDEX=wikipedia WIKI_PAGES=500 ./load_wikipedia.sh
ES_URL=https://example.es:9200 ES_API_KEY=id:secret ./load_wikipedia.sh
ES_NO_AUTH=1 ./load_wikipedia.sh  # for unsecured local clusters
```

The loader uses `ES_USER=elastic` and `ES_PASSWORD=password` by default, matching
`esql.py`. If Python has trouble with local CA certificates, the loader still fetches
Wikipedia with `curl`; set `WIKI_INSECURE=1` only if your environment requires it.

## Development

Run the self-contained test suite:

```bash
python3 test_esql.py
```

## Optional REPL UX dependencies

Install these for inline syntax highlighting and autocomplete while typing:

```bash
python3 -m pip install prompt-toolkit pygments
```

## Environment

| Variable | Default | Purpose |
| --- | --- | --- |
| `ES_URL` | `http://127.0.0.1:9200` | Cluster base URL |
| `ES_USER` / `ES_PASSWORD` | `elastic` / `password` | Basic auth |
| `ES_API_KEY` | _(unset)_ | Use `ApiKey` auth instead of basic |
| `ES_NO_AUTH` | _(unset)_ | Skip auth headers for unsecured local clusters |
| `ES_FORMAT` | `psql` | Output format |
| `ES_INSECURE` | _(unset)_ | `1` to skip TLS verification |
| `ES_TIMING` | _(unset)_ | `1` to print elapsed time per query |
| `ES_PROFILE` | _(unset)_ | `1` to send `profile: true` in ES|QL request bodies |
| `ESQL_COLOR` | `auto` | `1`/`always` to force JSON color, `0`/`never` to disable |
| `ES_AUTO_KEYWORDS` | `1` | Auto-uppercase ES|QL keywords before execution (`0` disables) |
| `ESQL_HISTORY` | `~/.esql_history` | Path to history file |

Connection options can also be passed as CLI flags: `--url`, `--user`, and
`--password`. CLI values override the corresponding environment variables for the
current run.

## REPL commands

Slash commands work with both `\` and `/` prefixes.

- `\q`/`/q`, `quit`, `exit`: quit REPL
- `\h`/`/?`: help
- `\clear`: clear the screen and current multiline buffer
- `\format [fmt]` (`\f`): show or change output format, for example `\format json`
- `\timing`: toggle elapsed time printing
- `\autokeywords` (`\ak`): toggle keyword auto-uppercase
- `\profile` (`\p`): toggle `profile: true` in ES|QL request bodies
- `\rest`: edit REST request blocks in `$EDITOR`/`$VISUAL`, then run them
- `\g`: run current buffer (or rerun last ES|QL/REST input)
- `\i <path>`: run statements from a file; in REST mode, run REST request blocks
- `\e`: edit current buffer in `$EDITOR`/`$VISUAL`, then run
- `\o [<path>]`: redirect output to file (blank to restore stdout)
- `\watch [secs]`: rerun last query every N seconds
- `\set [name value]` / `\unset <name>`: manage `${name}` substitutions
- `\conninfo`: show current session settings
- `\indices` (`\di`): list Elasticsearch indexes via `_cat/indices`
- `\health`: show cluster health
- `\nodes`: list cluster nodes
- `\shards [index]`: list shard allocation
- `\aliases`: list aliases
- `\templates`: list index templates
- `\datastreams` (`\ds`): list data streams
- `\tasks`: list running tasks
- `\count [index]`: count documents
- `\mapping <index>`: show index mappings
- `\get /_path`: run a read-only Elasticsearch GET API request
- `\df`: run `SHOW FUNCTIONS`
- `\info`: run `SHOW INFO`
- `\! <cmd>`: run a shell command via your configured shell (trusted input only)
