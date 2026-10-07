# Releasing

Merging the release pull request publishes it. [release-please](https://github.com/googleapis/release-please)
keeps that pull request open and up to date from the commits on `main`, and there is no npm token
and no one-time password involved: npm trusts this repository's `release.yml` workflow directly
(trusted publishing, OIDC).

Nobody bumps the version or edits the changelog by hand in an ordinary pull request. Both are
derived from commit messages, which is why those follow
[Conventional Commits](../CONTRIBUTING.md#commit-messages-and-pull-request-titles).

## Cutting a release

1. Merge work into `main` as usual. After each merge, release-please opens a pull request named
   `chore(main): release X.Y.Z`, or updates the one that is already open. It holds the version
   bump in `package.json` and the new section of `CHANGELOG.md`.
2. When you want to release, read that pull request. Check the version and the changelog, and
   edit the changelog if it needs it (see [The changelog](#the-changelog)).
3. Make sure `main` is green in CI, and merge the release pull request.

That merge is the release. Within a minute or two the version is tagged, on GitHub's release page,
and on npm. To watch it, and then check the result (see [Checking a release](#checking-a-release)):

```bash
gh run watch "$(gh run list --workflow release.yml --limit 1 --json databaseId --jq '.[0].databaseId')"
```

There is no schedule. The release pull request can stay open for as long as you like while it
collects changes.

To run everything short of publishing against an existing tag, or to finish a release that
stopped half way:

```bash
gh workflow run release.yml -f tag=v0.3.1 -f dry_run=true
gh workflow run release.yml -f tag=v0.3.1
```

A dry run does the checks, the build and `npm publish --dry-run`, which prints the list of files
that would ship. It creates no GitHub release and does not call the Homebrew workflow.

## What the workflow does

`release.yml` runs on every push to `main`, and by hand with a `tag` input. Most pushes only
reach the first stage.

| Stage | What happens | Stops here when |
| --- | --- | --- |
| release-please | Reads the commits since the last release and opens or updates the release pull request. If the push *was* the merge of that pull request, creates the tag `vX.Y.Z` and the GitHub release instead, with the changelog section as its notes | The push was anything other than the release pull request being merged: there is nothing to publish |
| Check the tag | Compares the tag with `package.json` and with `main` | The tag is not `vX.Y.Z`, differs from the version in `package.json`, or its commit is not on `main` |
| Check the code | `pnpm install --frozen-lockfile`, `pnpm typecheck`, `pnpm test`, `pnpm build`, on Node 24 | Any of them fails |
| Publish to npm | `npm publish --access public --ignore-scripts`, with provenance | npm refuses the publish. A version that is already on npm is skipped, not an error |
| GitHub release | Only for a tag made by hand: creates the release from the changelog section | The release already exists, which is the normal case |
| Homebrew | Calls `release-homebrew.yml`, which rewrites the formula in the tap | Does nothing, and stays green, until a tap is set up. See [homebrew-tap.md](homebrew-tap.md) |

Homebrew goes last because the formula needs the SHA256 of the tarball npm serves.

The GitHub release exists a minute before the npm version does, because release-please creates
it. If the later stages fail, the release page names a version npm does not have yet: fix the
cause and run the workflow by hand for that tag. Every stage can be re-run, and the ones that
already succeeded are skipped.

## How the version is chosen

release-please reads the commits on `main` since the last tag. The package follows
[semantic versioning](https://semver.org), and it is still `0.x`, where the minor number does the
work of a major one:

| Commits since the last release | Bump | Example |
| --- | --- | --- |
| Only `fix:` and `perf:` | patch | 0.2.0 to 0.2.1: token metering fixed for Codex on a ChatGPT subscription |
| At least one `feat:` | minor | 0.2.2 to 0.3.0: OpenCode and Gemini support |
| A breaking change (`feat!:`, `fix!:`, or a `BREAKING CHANGE:` footer) | minor while `0.x`, major after 1.0 | 0.1.0 to 0.2.0: listening on loopback only, renamed flags |
| Only `docs:`, `test:`, `ci:`, `chore:`, `refactor:`, `build:`, `style:` | none: no release pull request is opened | |

Commits without a conventional prefix are invisible to release-please. They neither trigger a
release nor appear in the changelog.

A pull request's title counts however it is merged. A squash makes the title the commit. A merge
commit gets the title in its body, where GitHub puts it, and release-please reads conventional
lines there too: that is how #6, whose only commit had no prefix, reached 0.4.0. The difference is
in what else counts. A merge commit also brings the pull request's own commits onto `main`, so
each conventional one among them gets a changelog line next to the title's. A squash leaves the
title alone, one line per pull request.

To force a particular version, for example to go to 1.0.0, put a `Release-As: 1.0.0` footer in
the body of a commit on `main`:

```bash
git commit --allow-empty -m "chore: release 1.0.0" -m "Release-As: 1.0.0"
```

**A version number is spent the moment it is tagged.** npm never lets a version be published
twice, and a tag others may have fetched should not move. If a released version turns out to be
wrong, fix it with a `fix:` commit and release the next patch. That is how 0.3.0 went: it was
tagged, never reached npm, and 0.3.1 carried its changes.

Pre-release versions (`1.0.0-beta.1`) are not supported yet: npm refuses to publish a pre-release
without a `--tag`, and the workflow does not pass one.

## The changelog

release-please writes each section of `CHANGELOG.md` from commit subjects: `feat:` commits go
under **Added**, `fix:` under **Fixed**, `perf:` and reverts under **Changed**, and breaking
changes get a section of their own at the top. The other types are left out. The section names are
set in `release-please-config.json`. Sections up to 0.3.1 were written by hand and stay as they
are.

A commit subject is rarely the best sentence for someone who uses the gateway. Two ways to do
better, both optional:

- **Before merging, edit `CHANGELOG.md` on the release pull request's branch.** Rewrite entries
  for users, group them, and credit outside contributors (`Thanks to @someone (#12).`). Do this
  last: release-please rebuilds the branch, and discards such edits, whenever something new lands
  on `main`.
- **Fix the source instead.** Edit the description of the *merged* pull request the entry came
  from and add an override block. release-please uses it in place of the original commit message
  the next time it runs. This works for squash-merged pull requests only, which is one reason to
  prefer squashing:

  ```
  BEGIN_COMMIT_OVERRIDE
  feat: open the dashboard from any launcher with --dashboard
  END_COMMIT_OVERRIDE
  ```

The GitHub release notes are taken when the release pull request is merged, from what release-please
generated. To change them afterwards: `gh release edit vX.Y.Z --notes-file notes.md`.

## Checking a release

```bash
npm view jev-gateway version                 # the new version
npm view jev-gateway dist.attestations      # provenance is attached
gh release view v0.3.1                       # the notes
npm install -g jev-gateway && jev-codex --status
```

The package page on npmjs.com shows a provenance badge that links to the commit and to the
workflow run that built it.

## When something goes wrong

| Symptom | Cause | Fix |
| --- | --- | --- |
| No release pull request appears after a merge | Nothing releasable landed: the commits are `docs:`, `chore:` and the like, or neither the pull request's title nor any of its commits has a conventional prefix | Nothing is wrong if there is nothing to release. Otherwise add an override block to the merged pull request (see [The changelog](#the-changelog)) and re-run the workflow |
| The release-please job fails with `GitHub Actions is not permitted to create or approve pull requests` | The repository setting is off | Turn it on: see [One-time setup on GitHub](#one-time-setup-on-github) |
| CI does not run on the release pull request | A pull request opened with `GITHUB_TOKEN` starts no workflows | Expected. It only changes `package.json` and `CHANGELOG.md`, and the publish job runs the full checks before anything reaches npm. To run CI anyway, close and reopen the pull request |
| Released changes sit under a `## Unreleased` heading in `CHANGELOG.md` | Someone wrote changelog entries by hand in a pull request. release-please does not read them: it adds its own section below and leaves that heading where it was (this happened with 0.4.0) | Move the text into the version's section, in a `docs:` commit, and delete the heading. Changelog entries come from commit messages; to word one better, see [The changelog](#the-changelog) |
| The release pull request has merge conflicts | `package.json` or `CHANGELOG.md` changed on `main` by hand | Push anything to `main`, or re-run the workflow: release-please rebuilds the branch from `main` |
| The version in the release pull request is not what you expected | A commit has the wrong type, or a `!` that should not be there | Correct it with an override block, or force the version with `Release-As:` |
| `Tag vX.Y.Z does not match package.json version` | Only with a tag made by hand, pushed without the version bump | Nothing was published. Delete the tag (`git push origin :vX.Y.Z`, `git tag -d vX.Y.Z`) and release through the pull request |
| Type check, tests or build fail in the publish job | `main` was not green when the release pull request was merged | npm has nothing, but the tag and the GitHub release exist. Fix on `main` with a `fix:` commit and merge the next release pull request. Delete the orphaned GitHub release, or mark it as superseded |
| `npm error 403 ... OIDC permission denied for this action` | npm's trusted publisher setting does not match this repository and workflow file | Compare the setting with [the table below](#one-time-setup-on-npmjscom), save it, and run the workflow by hand for the same tag |
| Publish fails asking for a login or a token (`ENEEDAUTH`) | The job's npm is too old for trusted publishing, which needs 11.5.1 or later | Keep `node-version` in `release.yml` on a Node that ships such an npm (Node 24 does) |
| The GitHub release exists but npm does not have the version | The run stopped after release-please | Run the workflow by hand for the same tag |
| The Homebrew stage fails | See the troubleshooting table in [homebrew-tap.md](homebrew-tap.md) | npm and the GitHub release are already done. Re-run only that part with `gh workflow run release-homebrew.yml -f tag=vX.Y.Z` |
| A broken version reached npm | | Release a fixed patch version. To warn people off the broken one: `npm deprecate jev-gateway@X.Y.Z "reason"` (this one needs an owner's npm login, not the workflow) |

release-please tracks its own state in two places: `.release-please-manifest.json` holds the last
released version, and the merged release pull request carries an `autorelease: pending` label
until it is tagged, then `autorelease: tagged`. If a release was somehow made outside this flow,
set the manifest to that version in a `chore:` commit so the next release pull request starts from
the right place.

## One-time setup on GitHub

release-please opens pull requests with the workflow's own `GITHUB_TOKEN`, which GitHub does not
allow by default. In the repository, open **Settings → Actions → General → Workflow
permissions** and tick **Allow GitHub Actions to create and approve pull requests**. Or:

```bash
gh api -X PUT repos/vinilana/jev-gateway/actions/permissions/workflow \
  -f default_workflow_permissions=read -F can_approve_pull_request_reviews=true
```

The default token permission stays read-only. `release.yml` asks for the write permissions it
needs job by job.

## One-time setup on npmjs.com

Trusted publishing is switched on per package, by an owner, in the browser:

1. Open https://www.npmjs.com/package/jev-gateway/access and find **Trusted Publisher**.
2. Choose **GitHub Actions** and fill in:

   | Field | Value |
   | --- | --- |
   | Organization or user | `vinilana` |
   | Repository | `jev-gateway` |
   | Workflow filename | `release.yml` |
   | Environment name | leave empty |

3. Save. Then, on the same page under **Publishing access**, pick **Require two-factor
   authentication and disallow tokens**. After that the workflow is the only thing that can
   publish, and a leaked token is worth nothing.

npm matches the repository and the workflow file name exactly. Renaming `release.yml`, or moving
the `npm publish` step into a reusable workflow, breaks publishing until the setting is updated.

## Why it is built this way

- **No secrets to leak.** GitHub hands the job a short-lived identity token (`id-token: write`)
  and npm exchanges it for a one-off publish credential. Nothing long-lived exists to steal.
  release-please runs on the built-in `GITHUB_TOKEN` for the same reason, not on a personal
  access token.
- **One workflow file.** A tag or release created with `GITHUB_TOKEN` does not start other
  workflows, and npm only trusts a publish that comes from `release.yml`. So release-please is a
  job inside `release.yml` and hands its tag to the publish job directly, instead of living in a
  file of its own and triggering or calling this one.
- **Provenance for free.** Packages published this way carry a signed statement of which commit
  and which workflow run built them, shown on the npm page.
- **Scripts stay out of the publish step.** The checks and the build run first, and the publish
  itself uses `--ignore-scripts`, so no package script runs while the job holds the identity.
- **Actions are pinned to commit hashes**, so a moved tag in someone else's action cannot change
  what runs here. When updating one, replace the hash and keep the version in the trailing comment.
- **Only tags on `main` publish.** A tag on any other branch is refused.
- **Homebrew is called, not triggered**, for the same reason release-please lives in this file:
  `release-homebrew.yml` cannot listen for a release that a workflow created.
- **A person still decides when.** Merging the release pull request is a deliberate act, and the
  pull request shows exactly what will ship before it does.
