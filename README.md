# esql-terminal

Tiny psql-style ES|QL terminal for Elasticsearch.

- Interactive REPL: type/paste multiline ES|QL, executes when terminated by `;`.
- Arrow-key history (persisted to `~/.esql_history`).
- `Ctrl+C` exits the REPL.
- Default output is a psql-style table; `--format json|txt|csv|yaml` also supported.
- Friendly error formatting for Elasticsearch error responses.

## Usage

```bash
./esql.py                 # interactive REPL
./esql.py query.esql      # run a file
./esql.py < query.esql    # run from stdin
```

## Environment

| Variable | Default | Purpose |
| --- | --- | --- |
| `ES_URL` | `http://127.0.0.1:9200` | Cluster base URL |
| `ES_USER` / `ES_PASSWORD` | `elastic` / `password` | Basic auth |
| `ES_API_KEY` | _(unset)_ | Use `ApiKey` auth instead of basic |
| `ES_FORMAT` | `psql` | Output format |
| `ES_INSECURE` | _(unset)_ | `1` to skip TLS verification |
| `ESQL_HISTORY` | `~/.esql_history` | Path to history file |
