# Agent Note: Keep docs/sourceReader a Chinese-only library

Status: implemented

English | [中文](2026-11-19-sourcereader-chinese-only-library.zh.md)

## Problem

The four-layer source-reading library under `docs/sourceReader/` was authored Chinese-first, then converted into the repository-wide bilingual form: one pass added a mirrored `.zh.md` page and an `.i18n.yaml` consistency record for each of its sixteen pages, and a later refresh carried that form forward. Both conversions are already in published history, so the reversal had to be a deliberate undoing of shipped content rather than a continuation of it.

The reversal on 2026-11-12 came with three defects, found while auditing why earlier commits on this library had gone wrong:

- **Bilingual form doubles refresh cost.** The library guides readers who already read this repository's English source. A mirrored English prose copy means every code refresh rewrites three files per page instead of one: the bilingual refresh of the library touched forty-one files, the Chinese-only form touches nineteen. The mirror also stays unenforced unless the pairing gate keeps both sides in step.
- **A committed document pointed at an untracked tool.** `.progress/UPDATE-SPEC.md` and `.progress/state.json` name `.progress/sr_tools.py` as the way to verify a page, while `git ls-files --error-unmatch` reported that path as unknown to Git and `git check-ignore` found no rule covering it. The reference described a local-only file that a fresh clone does not have, so the documented verification command could not run anywhere but the author's machine.
- **Twelve stash snapshots were dropped.** A rewrite in progress was saved repeatedly with `git stash`, then the snapshots were discarded, leaving dangling commits in the object database that `git stash list` no longer shows. Comparing each one against the commits that followed showed every changed file byte-identical to what shipped, so nothing was lost — but the working state was unrecoverable from the normal stash interface.

## Decision

`docs/sourceReader/` is a Chinese-only library, and the exemption is enforced by the pairing gate rather than by prose alone:

- **Exclusion**: `docs/sourceReader/` is listed in the `excluded` array of [translation-pairing.manifest.json](../../../../scripts/translation-pairing.manifest.json), alongside `docs/topics/`. The gate therefore rejects a reintroduced `.zh.md` or `.i18n.yaml` under the library. The library's own maintenance document states the same rule, so the library and the gate agree.
- **No language switcher**: the library's pages carry no language-switcher line and no `.zh.md` link. A page that regains either fails the gate.
- **Deletion, not an in-library archive**: the sixteen `.zh.md` pages and their sixteen `.i18n.yaml` records are deleted. Git history retains them, so the library does not carry a second copy of its own former form.
- **Tracked verification tool**: `.progress/sr_tools.py` is committed next to the documents that name it, so the verification commands recorded in `.progress/UPDATE-SPEC.md` resolve in a fresh clone. A committed document must not point at a file the repository does not track.
- **Local exclusion of `.codegraph/`**: the codegraph index directory is excluded through `.git/info/exclude` instead of an entry in the shared `.gitignore`, so the decision stays on this machine rather than changing ignore rules for every contributor.

## Alternatives considered

**Keep the mirrored English copy.** The repository-wide convention favours a pair, and the two earlier conversions followed it. Every code refresh then rewrites three files per page, and the mirror drifts unless the pairing gate is trusted to catch it; the library is read by people who already read the English source, so the added audience is small.

**Keep the pages paired but exclude them from the gate.** Excluding a directory that still contains pairs invites the next pass to re-add the missing English page without the gate noticing, which is how a bilingual corpus quietly becomes a half-updated one. The pages are deleted instead.

**Write the exemption only into the library's maintenance document.** Prose cannot reject a reintroduced pair. The library's documents already say the rule, but the enforcement has to live in the manifest so a mistake fails a gate rather than a review.

**Delete the tool references instead of tracking the tool.** The verification commands in `.progress/UPDATE-SPEC.md` and `.progress/state.json` are the library's documented way of checking path and symbol references. Rewriting them to name no tool makes the documents unverifiable; tracking the script costs one file and keeps the documented command runnable.

**Add `.codegraph/` to the shared `.gitignore`.** The directory is machine-local generated state. Putting its rule in the shared ignore file changes the repository for every contributor, including machines where the directory is absent; `.git/info/exclude` reaches the same effect locally without a committed decision.

**Keep the dropped stash snapshots.** Comparing each snapshot against the commits that followed it showed byte-identical content, so recovering them would add no information. They are removed with `git prune` after that comparison.

## Consequences

A code refresh now touches one page instead of three per document, and the pairing gate no longer scans the library, so nothing in the corpus catches a stale page on its own. Correctness of the library's path and symbol references therefore rests entirely on the `verify` and `symbols` subcommands of `.progress/sr_tools.py` plus `pnpm run test:docs`.

Anyone who wants an English rendering of a page authors it as a separate document. There is no generator and no commitment to one, and the library does not promise English coverage.

Reintroducing a `.zh.md` or `.i18n.yaml` under `docs/sourceReader/` fails `verify-translation-pairing` with the library named, which is the enforcement the library's prose alone did not provide.

The dangling stash commits are gone from the object database after the comparison above, so the audit trail for that rewrite exists only in the commits that followed it and in this note.

## Verification ledger

| Block | Record |
|---|---|
| Date | 2026-11-19 |
| Environment | Local working copy of this fork at `master`, no remote deployment involved |
| Repository | `docs/sourceReader/`, `docs/topics/`, `scripts/translation-pairing.manifest.json`, `.agents/notes/implemented/process/` |
| One-line conclusion | Earlier commits were not malformed Git objects; one had an off-convention message, two had shipped a form the library later abandoned, and the reversal plus its tooling were left uncommitted |
| State before | Working tree held 32 deletions, 19 modifications, and 3 untracked paths; the three library commits were ancestors of `origin/master`; 12 dangling commits were present, all of them stash snapshots |
| Preconditions and confirmation | Confirmed by ancestry check that all three library commits were published, confirmed by `git check-ignore` that no rule covered the verification script, confirmed by `git grep` that no committed file named the script before this change |
| Manual steps before work | None required; the audit is read-only until the commits are made |
| Scripts and results | `sr_tools.py` (11.8 KB, committed in this change) supplies `verify` and `symbols`; `pnpm run test:docs` ran the documentation aggregate, 21 gates passed and 0 failed, including translation pairing, markdown links, markdown wrap, and repository references |
| Commands as run | See the command block below |
| Final state | Three commits: the Chinese-only conversion with the tracked tool, the `docs/topics/` addition with its exclusion, and this note; `.codegraph/` excluded locally; dangling objects pruned |
| Unique finding | The stash snapshots were byte-identical to the commits that followed them, so the repeated stashing produced noise, not data loss; each carried exactly sixteen changed files, all under `docs/sourceReader/`. `git prune` removed none of them because they were already packed, so dropping them took a repack |
| Idempotence | Re-running `git fsck --no-reflogs --dangling` reports no dangling commits; re-running the documentation aggregate reports the same 21 gates passing; re-adding an excluded pair fails the pairing gate again |
| Reusable check | `git merge-base --is-ancestor <commit> origin/master` to tell pushed work from local work, and `git diff <stash> <commit> -- <path>` to tell a snapshot from shipped content |
| Remaining work | No remote push was performed; publishing these commits is the user's action |

```sh
git reflog --date=iso -40                       # commit, stash, and pull history with timestamps
git fsck --no-reflogs --dangling                # locate dropped stash snapshots
git ls-files --error-unmatch docs/sourceReader/.progress/sr_tools.py   # untracked: no rule and no index entry
git check-ignore -v docs/sourceReader/.progress/sr_tools.py            # exit 1: not ignored either
for s in <stash commits>; do                    # prove each snapshot matches shipped content
  git diff "$s" <following commit> -- docs/sourceReader/ | wc -l
done
pnpm run test:docs                              # 21 gates, 0 failures
git prune                                       # remove only the proven-redundant dangling objects
```
