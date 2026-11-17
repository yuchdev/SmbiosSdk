# Milestone 0016 - Real-Firmware Table Corpus - Status

Tracks progress against [plan.md](plan.md). Updated as each story lands.

## Current status

| Story | Name                                        | Status         | Tests |
|-------|---------------------------------------------|----------------|-------|
| 01.0  | Scrubbing tool and the privacy contract     | ⬜ Not started | - |
| 02.0  | Corpus format, storage and replay harness   | ⬜ Not started | - |
| 03.0  | Seed corpus and CI integration - the endian proof needs ADR 0007 D3's `linux-s390x` leg | ⬜ Not started | - |
| 04.0  | Contribution path and solicitation - gated on 01.0-03.0 **and** ADR 0007 D9's readiness bar | ⬜ Not started | - |

**Legend:** ✅ Complete · 🔶 In progress / partial · ⬜ Not started

## Notes & decisions

- 2026-11-16 Milestone created from [ADR 0007](/docs/adr/0007-ci-compute-architecture-coverage-and-self-hosted-surface.md)
  D8. Purpose: shrink the self-hosted firmware lab (ADR 0007 D7 node
  **S1**) by collecting real firmware tables as **data** rather than
  acquiring machines.
- 2026-11-16 **The one hard rule: scrub before solicit.** Story 01.0
  lands before any third party is asked for a dump. Raw SMBIOS tables
  carry serial numbers, UUIDs and asset tags; once one is in git history
  it is effectively permanent. The story order is a safety ordering, not
  a preference.
- 2026-11-16 **Scope boundary recorded explicitly:** a corpus covers
  *table content* diversity and **cannot** cover *source acquisition*
  (sysfs vs EFI vs IOKit vs legacy scan). Node S1 shrinks from one
  machine per vendor to **one machine per OS** - it does not disappear.
  Any claim that the corpus "removes the need for hardware" is wrong and
  should be corrected wherever it appears.
- 2026-11-16 **Independent of the distribution milestones.** This
  milestone touches no packaging, no install rule and no workflow file,
  so it runs in parallel with 0012/0014/0015 rather than queueing behind
  them. Its only cross-milestone dependency is ADR 0007 D3's
  `linux-s390x` leg, and only for Story 03.0's endian proof - Story 03.0
  still lands without it, minus that one deliverable.

## Still open for the maintainer

- **The exhaustive identifying-field list** (contract C1) needs a
  decision on borderline fields: OEM strings (Type 11) and system
  configuration options (Type 12) are free-text and have been observed
  carrying site-specific data in the wild. Destroying them loses real
  parser coverage; preserving them risks leaking. Story 01.0 Task 01 must
  put a recommendation to the maintainer rather than deciding silently.
- **Consent terms** for contributed fixtures (contract C2's `meta.json`):
  what a contributor is agreeing to, and the removal procedure if they
  later object.
- **Whether the seed corpus is published at all before Story 04.0.** A
  maintainer-only corpus proves the harness works without yet creating
  any third-party privacy exposure; publishing it early is a separate,
  reversible-only-once decision.
- **What S1 hardware exists** (ADR 0007 open question 6). The seed corpus
  in Story 03.0 can only be as diverse as the machines available, and
  that inventory bounds this milestone's starting point.
