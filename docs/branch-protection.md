# Branch protection

`main` is protected on the two repos the runner executes code from:

| Repo | Why it matters |
|---|---|
| `ai-curriculum-project/ai-infra-content-generator` | pi-rook pulls and runs it every job |
| `ai-curriculum-project/ai-curriculum-project.github.io` | the `publish` job runs its build scripts |

Rules (both repos, set 2026-11-03):

- Every change reaches `main` through a pull request, **admins included**, so a leaked token for
  the maintainer account still can't push straight to `main`.
- No force pushes and no branch deletion.
- Zero required approvals, so the sole maintainer (and the runner, which uses the same account)
  can merge their own PRs.

What this means for automation:

- Approved new roles land through a config PR that `aicg org approvals` opens and merges.
- The `publish` job refreshes the umbrella site through a PR it opens and merges.
- The six `<org>.github.io` repos hold only generated pages that nothing executes, so they stay
  unprotected and the publish job pushes to them directly.

The stronger next step is a separate GitHub account (or fine-grained token) for pi-rook with
access only to the repos it writes, so the runner's credentials and the maintainer's are no
longer the same.
