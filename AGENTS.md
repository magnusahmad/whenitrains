# Agent Safety Rules

This repo has an intentionally untracked production-like SQLite database under `data/`.

## Engineering Workflow Rules

- In this codebase, everything is testable. Whenever you add a new feature or functionality, cover it with tests, including tests that explicitly verify the intended business logic is observed.
- Use red/green TDD. Before starting implementation work, specify the failing tests you expect to write or run first.
- When making a change to the web dashboard, always perform visual checks with Browser Use and do not call the work done until those visual checks look good.
- Track the state of the codebase with detailed specs and status files. Check an existing status file such as `status.md` or a feature-specific `*-status.md` file before updating or creating one, and follow the established structure for current state, decisions, milestones, tests, and exit criteria.
- Follow `docs/specs.md` for specification and status-file discipline; follow `docs/milestone-files.md` for milestone file structure and session updates.
- For python environment management we use venv

Hard rules:

- Never delete `data/`, `data/whenitrains.sqlite3`, `data/backups/`, or any `*.sqlite3` file unless the user explicitly asks for that exact destructive action.
- Never run broad cleanup commands such as `rm -rf data`, `find . -name '*.sqlite3' -delete`, or `git clean -fdx` in this repo.
- Before any command that clears state, migrates data in a non-additive way, or rewrites storage logic, create a backup:

```bash
PYTHONPATH=src python3 -m whenitrains.cli --db data/whenitrains.sqlite3 backup-db
```

- Use `reset-paper --yes` for paper-trading cleanup. It clears only paper orders, positions, decisions, and signals, and it creates a backup first by default.
- Use `/private/tmp/*.sqlite3` for smoke tests and destructive experiments.

## Live Log Access

The live machine publishes scheduler logs over the LAN from `~/whenitrains-live-logs` on port `8765`.

Commands for this machine to view the live logs:

```bash
curl -L http://192.168.1.23:8765/
curl -L -o /private/tmp/live-scheduler-latest.log http://192.168.1.23:8765/<log-file-name>
rg -n -i "LIVE|preflight|failed|error|not enough|insufficient|balance|allowance|block_new_entries|buy|sell|filled|submitted|request error|live-scheduler actions" /private/tmp/live-scheduler-latest.log
tail -n 120 /private/tmp/live-scheduler-latest.log
```

If sandboxed network access blocks the LAN request, rerun the `curl` with approved network access rather than changing the live machine.

Rationale:

- `data/` is ignored by git, so git cannot recover the live database.
- The DB contains HKO snapshots, Polymarket orderbooks, decisions, and paper trade history that cannot be fully reconstructed from source code.
- SQLite backups should use the CLI backup command because it uses SQLite's online backup API and runs an integrity check.
