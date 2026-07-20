---
name: "publish-change"
description: "Preflight and perform Commit, Push, or pull-request actions when the user explicitly authorizes those exact external Git or GitHub actions and their scope. Trigger on the explicit publication request even if readiness still needs verification; stop and report if review, validation, branch, or scope evidence is insufficient. Never trigger from implementation completion alone. Do not use to implement code, merge, release, tag, or publish unrelated work; force-push requires separate explicit authorization after its overwrite risk is explained."
---

# Publish Change

Publish an intentional, reviewed change without broadening the user's
authorization or losing local work.

## Workflow

1. Confirm the exact requested actions: commit, push, and/or pull request. Each
   authorization is independent: a PR-only request does not authorize a commit
   or push, and a push request does not authorize a commit. If a requested
   action lacks a prerequisite, stop and request the exact additional authority
   instead of inferring it. Treat prior implementation approval, mentioning this
   Skill, or asking how it works as insufficient authority for external writes.
2. Inspect the repository root, current branch, remotes, status, staged and
   unstaged Diffs, untracked files, and available validation results. Identify
   unrelated user changes before staging anything. If the final actions, branch,
   or file scope materially differ from what the user authorized, stop and
   reconfirm the changed publication scope.
3. Review the final scope for secrets, generated files, compatibility risks,
   accidental public APIs, and incomplete validation. Stop and report any
   material issue that would make publication misleading or unsafe.
4. For a commit:
   - stage only files belonging to the explicitly authorized publication scope
   - do not use a broad staging command when unrelated files exist
   - write a concise message that describes the actual change
   - do not amend or rewrite history unless explicitly requested
5. For a push, push only the intended branch. A force-push requires separate
   exact-branch authorization after its overwrite risk is explained. Record the
   current remote ref for recovery and prefer `--force-with-lease`; stop if the
   lease has moved. Never infer force-push authority from an ordinary push.
6. For a pull request, verify the head and base branches, then write a title and
   body containing the change summary, validation, and remaining risks. Respect
   an explicit draft or ready-for-review request; do not merge it.
7. If a network or API call times out, preserve the local commit and branch.
   Check whether the remote branch or pull request was created before retrying
   so that a transient failure does not produce duplicates.
8. Report each action that actually succeeded, its branch or URL, and any action
   that remains incomplete. Never infer remote success from a local command
   alone.

## Boundaries

- Do not modify implementation merely to make publication easier.
- Do not include credentials, private endpoints, unrelated changes, caches,
  runtime data, or generated artifacts.
- This Skill never merges, releases, tags, deletes branches, closes issues, or
  notifies third parties. Those actions require separate authorization and a
  separately applicable workflow.
- Prefer non-destructive Git operations and preserve recoverability; an
  explicitly authorized force-push remains an exceptional, separately scoped
  action.

## Completion Evidence

Report the committed files and commit identifier when created, the pushed branch
when confirmed, the pull-request URL when created, validation already performed,
and any remaining publication step.
