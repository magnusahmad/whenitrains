# Specifications And Status Files

Your goal is to build a very comprehensive specification, meeting the goals stated in the initial developer input and expanding upon them with solid research and engineering. Where necessary, verify unknowns and risky assumptions by building small proof-of-concept programs, or preferably, using deep web research to review previous approaches.

All existing current specs live in the `docs/` folder. Anything that looks like a spec outside of that folder should be treated as draft. Status files matching the `.status.md` pattern are found anywhere in the main project directory.

## Milestone And Plan Files

Milestone and plan files track implementation progress and coordinate work between agents and humans. Refer to [milestone-files.md](milestone-files.md) for the detailed requirements of milestone files.

## Key Principles

- Granular milestones: Break work into small, testable tasks.
- Automated verification: Every deliverable must have automated tests.
- Session discipline: Every agent session must end with a status file update.
- No deviation: When blocked, document the problem rather than changing goals.
- Test-first planning: Define verification criteria before implementation.

When a task proves too difficult to complete according to the plan, you should never deviate significantly from the original goal. Instead, you must:

- Update the milestone's Outstanding Tasks with what was tried.
- Create a detailed problem report if the issue is complex.
- Update the `next_steps` property with the recommended approach.
- Set milestone status to `blocked` if unable to proceed.

These reports will be forwarded to senior developers and management who may adjust the plan in response. The reports should describe the context of the problem in extreme detail, as they may be shared with online AI agents who are not familiar with this project.

## Testing Strategy

It is extremely important that the tasks are very granular and that they can be verified with automated tests. Prefer integration tests over unit tests, but apply reasonable judgment on a case-by-case basis.

All implementation plans and testing strategies will be reviewed before implementation starts. Ideally, the plans will identify development tracks that can be started in parallel without interfering with each other.
