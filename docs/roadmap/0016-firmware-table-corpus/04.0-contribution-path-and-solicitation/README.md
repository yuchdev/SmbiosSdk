# Story 04.0 - Contribution Path and Solicitation

**Parent milestone:** [plan.md](../plan.md)
**Status:** ⬜ Not started
**Depends on:** Stories 01.0, 02.0 and 03.0 - **and** [ADR 0007](/docs/adr/0007-ci-compute-architecture-coverage-and-self-hosted-surface.md) D9's readiness bar

## Scope

Open the corpus to contributors: a one-command recipe, a submission
template, review guidance, and the public call for dumps.

**This story is gated twice, and both gates are real.** Stories 01.0-03.0
must land because soliciting dumps before the scrubber, the format and the
harness exist would collect data this project cannot safely handle. ADR
0007 D9's readiness bar must be met because inbound contributions outpace
a single maintainer unless the automated suite already catches regressions
on its own.

## Tasks

| #  | Document                                                  | Status         | Blocks |
|----|-----------------------------------------------------------|----------------|--------|
| 01 | [Contribution recipe and submission template](01-contribution-recipe-and-template.md) | ⬜ Not started | 02     |
| 02 | [Review checklist and the public call](02-review-checklist-and-call.md) | ⬜ Not started | 03     |
| 03 | [Tests](03-tests.md)                                      | ⬜ Not started | -      |

## Key constraints

- **Nothing in this story runs before both gates are met.** Record the bar
  as met in `status.md`, with evidence, before the call goes out.
- **A contributor must never be asked to send an unscrubbed dump**, not
  "just this once", not by email, not to a maintainer directly. The recipe
  scrubs locally; only scrubbed bytes ever leave their machine.
- **Every submission is re-verified by the maintainer**, never trusted.
  `smbios_scrub --verify` runs on every incoming fixture as a review step,
  because a contributor may have run an older scrubber version or skipped
  the step.
- **Consent is recorded per fixture**, with a removal procedure that
  actually works - a contributor who later objects must be able to get
  their fixture deleted.
- **No automated intake.** Contribution is a pull request or an issue
  attachment reviewed by a person. This milestone builds no endpoint that
  receives data unattended.
- **The call must set expectations about privacy honestly**: say what is
  destroyed, what is preserved, and that the result is public and
  permanent.

## Files or directories created or modified by this story

- `CONTRIBUTING.md` (new or modified) - a corpus contribution section.
- `.github/ISSUE_TEMPLATE/` and/or `.github/PULL_REQUEST_TEMPLATE/` - the
  submission template, including the consent statement.
- `docs/dev/corpus-capture-recipe.md` - promoted from maintainer notes to
  contributor-facing instructions.
- `docs/dev/corpus-review-checklist.md` (new) - what a maintainer checks
  before merging a donated fixture.
- `docs/roadmap/0016-firmware-table-corpus/status.md` - the gate record.
