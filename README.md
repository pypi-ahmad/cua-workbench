# cua-workbench

## Overview

CUA Workbench is a local development environment for running computer-using agents inside a visible Linux sandbox. It combines a React frontend, a FastAPI orchestration backend, and a Dockerized Ubuntu desktop so you can start an agent session, watch what it does, inspect step-by-step logs, and compare multiple execution engines without leaving the same interface.

## Tech Stack

- Python (requirements.txt based)

## Repository Structure

- `.dockerignore`
- `.env.example`
- `.github/`
- `.gitignore`
- `audit/`
- `backend/`
- `CHANGELOG.md`
- `CODE_OF_CONDUCT.md`
- `constraints.txt`
- `CONTRIBUTING.md`
- `docker-compose.yml`
- `docker/`
- ... and 14 more entries

## Getting Started

### Prerequisites

- Git
- Runtime dependencies for this project's stack

### Installation

```bash
uv venv
uv pip install -r requirements.txt
```

## Usage

Use the project's documented entrypoint (CLI/app script) from this repository.

## Testing

Run tests with `uv run pytest` from repository root.

## Security

Please review [SECURITY.md](SECURITY.md) for reporting and handling security issues.

## Contributing

Please read [CONTRIBUTING.md](CONTRIBUTING.md) and [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) before opening issues or pull requests.

## Changelog

Ongoing changes are tracked in [CHANGELOG.md](CHANGELOG.md).

## License

This project is licensed under the terms described in [LICENSE](LICENSE).
