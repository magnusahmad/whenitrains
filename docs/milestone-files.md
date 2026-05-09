# Milestone Files Specification

## Overview

Milestone files track the implementation progress of whenitrains initiatives. They serve as the primary coordination mechanism between AI agents and human reviewers. This specification defines the structure, mandatory properties, update patterns, and linting requirements for milestone files.

Milestone files use markdown with the filename convention `initiative.status.md`.

## Goals

- Provide a machine-parseable format for tracking implementation progress.
- Enable automated verification of cited test names and file paths.
- Establish clear patterns for agent session workflows.

## Template

```text
#+TITLE: <Initiative Name> Milestones

* Document
  :PROPERTIES:
  :next_steps: <current next steps - MANDATORY>
  :last_updated: <date YYYY-MM-DD>
  :current_milestone: <milestone ID>
  :executive_summary: <one-line high-level status - MANDATORY>
  :END:

* Introduction
  :PROPERTIES:
  :initiative_goals: <broad goals>
  :related_specs: <links to related specification files>
  :END:

  Brief context and scope of the initiative.

* Milestones

** M0: <Milestone Name>
   :PROPERTIES:
   :status: planned|in_progress|partially_completed|completed|blocked
   :depends_on:
   :END:

*** Deliverables
    - [ ] Deliverable 1
    - [ ] Deliverable 2

*** Verification
    - test_name: <greppable_test_name>
      description: <what the test verifies>
      status: pending|passing|failing

*** Implementation Details
    (Populated when milestone work begins)

*** Key Source Files
    - path/to/file.rs - Description of what this file does

*** Outstanding Tasks
    - [ ] Task 1
    - [ ] Task 2

** M1: <Next Milestone>
   ...
```

## Document-Level Properties

Document-level properties are placed in a `* Document` section at the top of the file, after `#+TITLE`.

| Property | Required | Description |
| --- | --- | --- |
| `#+TITLE` | Yes | Initiative name followed by `Milestones`. |
| `:next_steps:` | Yes | Current actionable next steps, updated each session. |
| `:last_updated:` | Yes | Date of last update, formatted as `YYYY-MM-DD`. |
| `:current_milestone:` | Yes | ID of milestone currently being worked on. |
| `:executive_summary:` | Yes | One-line, approximately 120-character high-level status for dashboard display. Updated by the Manager Agent. Includes any compromises or deviations. |

## Introduction Section Properties

| Property | Required | Description |
| --- | --- | --- |
| `:initiative_goals:` | Yes | Broad goals of the initiative. |
| `:related_specs:` | Yes | Links to related specification files. |

## Milestone Properties

| Property | Required | Description |
| --- | --- | --- |
| `:status:` | Yes | One of `planned`, `in_progress`, `partially_completed`, `completed`, or `blocked`. |
| `:depends_on:` | Yes | Space-separated milestone IDs that must already be completed, or empty if there are no dependencies. |
| `:external_prereqs:` | No | Space-separated references to external specs or initiatives that must already be green. |

## Dependency Validation Rules

- Every ID in `:depends_on:` must refer to an earlier milestone in the same file.
- A milestone must not be marked `in_progress` until all `:depends_on:` milestones are completed.
- `:external_prereqs:` entries must be mentioned in the Introduction `:related_specs:` property.
- The linter verifies these constraints and reports violations as errors.

## Verification Entry Properties

Each verification entry must have:

| Property | Required | Description |
| --- | --- | --- |
| `test_name` | Yes | Test function name. Must exist in the file when status is not `pending`. |
| `file` | Conditional | Path to the file containing the test. Required when status is `passing` or `failing`. Optional when status is `pending`, because the test may not exist yet. |
| `description` | Yes | Human-readable description of what is verified. |
| `status` | Yes | One of `pending`, `passing`, or `failing`. |

## Test Name Requirements

Test names must be:

- Existing: When `file` is provided, the linter verifies the test name exists in that file.
- Unique: No two tests should have the same name within a milestone file.
- Descriptive: Indicate what is being tested.
- Prefixed: Use consistent prefixes by category.

Prefix examples:

- `test_` for unit tests.
- `e2e_` for end-to-end integration tests.
- `smoke_` for smoke tests.
- `verify_` for verification scripts.
- `just` for Justfile targets, such as `just test-fuse-basic-ops`.

Example:

```text
*** Verification
    ;; Passing test - file is REQUIRED
    - test_name: test_zfs_snapshot_creation
      file: crates/ah-fs-snapshots-zfs/src/lib.rs
      description: Verifies ZFS snapshots are created with correct naming
      status: passing

    ;; Pending test - file is OPTIONAL (test may not exist yet)
    - test_name: e2e_session_branching_workflow
      description: End-to-end test of session branching from timeline
      status: pending

    ;; Failing test - file is REQUIRED
    - test_name: just test-fuse-basic-ops
      file: Justfile
      description: Runs FUSE basic filesystem operations test harness
      status: failing
```

## Section Lifecycle

### Initial State: Planning Phase

When a milestone file is first created, it contains:

- Introduction: Context, goals, and links to specs.
- Milestones, each with:
- Deliverables as checkbox items.
- Verification test specifications with `pending` status.
- Empty Implementation Details, Key Source Files, and Outstanding Tasks.

### Active Development Phase

As work progresses on a milestone:

- Deliverables: Checkboxes are marked as completed `[x]`.
- Verification: Test statuses are updated to `passing` or `failing`.
- Implementation Details: Populated with architectural decisions and technical insights.
- Key Source Files: Populated with paths and descriptions of key files.
- Outstanding Tasks: Populated with remaining work items.

### Partial Completion Phase

When a milestone has significant implementation work done but has remaining tasks, such as CI verification pending, platform-specific testing needed, or manual steps remaining:

- Some deliverable checkboxes are marked `[x]`, others remain `[ ]`.
- Some verification tests have status `passing`, others may be `pending`.
- Implementation Details document what has been built.
- Key Source Files are populated for implemented components.
- Outstanding Tasks lists the remaining work.
- Milestone `:status:` property is set to `partially_completed`.

This status is appropriate when:

- The core implementation is done but cross-platform verification is pending.
- Automated infrastructure is in place but manual steps, such as Apple entitlement approval, remain.
- The milestone depends on external processes or different build environments, such as Linux CI.

### Completion Phase

When a milestone is completed:

- All deliverable checkboxes are marked `[x]`.
- All verification tests have status `passing`.
- Implementation Details are comprehensive.
- Key Source Files are fully documented.
- Outstanding Tasks are either completed or moved to a new milestone.
- Milestone `:status:` property is set to `completed`.

## Session Update Pattern

Every agent session working on a milestone file must follow this pattern:

### 1. Session Start

- Read the current `next_steps` property.
- Identify the `current_milestone`.
- Review the milestone's deliverables and outstanding tasks.
- Begin work on the immediate next task.

### 2. During Session

- Update deliverable checkboxes as items are completed.
- Run verification tests and update their status.
- Add implementation details as decisions are made.
- Add key source files as code is written.

### 3. Session End: Mandatory

Every session must end with a milestone file update that includes:

- Update `next_steps`: What should be done next.
- Update `last_updated`: Current date.
- Update verification statuses: Run tests and record results.
- Add any new outstanding tasks: Issues discovered during session.
- Update milestone status: If blocked or completed.

Example session-end commit:

```text
* Document
  :PROPERTIES:
  :next_steps: Implement CommandChunk IPC message handling; fix flaky e2e_spawn_tree test
  :last_updated: 2025-12-13
  :current_milestone: M2
  :END:

** M2: Stdout/Stderr Chunk Capture
   :PROPERTIES:
   :status: in_progress
   :depends_on: M1
   :END:

*** Deliverables
    - [x] Helper program with mixed output patterns
    - [x] FD tracker for dup/dup2/dup3
    - [ ] CommandChunk IPC message implementation
    - [ ] Integration with recorder pipeline

*** Verification
    - test_name: test_fd_tracker_dup_chain
      description: Verifies FD tracking across dup/dup2/dup3 calls
      status: passing

    - test_name: e2e_mixed_output_capture
      description: End-to-end capture of interleaved stdout/stderr
      status: pending

*** Outstanding Tasks
    - [ ] Fix race condition in e2e_spawn_tree when process exits quickly
    - [ ] Add timeout handling for blocking write syscalls
```

## Allowed Sections

The linter enforces that only these sections are present.

Top-level sections:

- `* Document` (required): Contains document-level properties.
- `* Introduction` (required).
- `* Milestones` (required).
- `* References` (optional): Links to related documentation.

Milestone subsections under each `** M<N>: <Name>`:

- `*** Deliverables` (required).
- `*** Verification` (required).
- `*** Implementation Details` (optional, required when milestone has started).
- `*** Key Source Files` (optional, required when milestone has implementation).
- `*** Outstanding Tasks` (optional).
