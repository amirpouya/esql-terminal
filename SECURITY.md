# Security Policy

## Supported use

- This repository is intended for local development and experimentation with Elasticsearch ES|QL.
- Example credentials in the documentation (`elastic` / `password`) are for disposable local clusters only.
- Do not reuse sample credentials in shared, staged, or production environments.

## Secret handling

- Prefer `ES_API_KEY` over basic auth when connecting to secured clusters.
- Keep credentials in local environment files or shell profiles that are **not** committed to git.
- `.env`-style files are ignored by default in this repository.

## Reporting a vulnerability

If you find a security issue, please open a private security advisory or contact the maintainer privately before publishing details.
