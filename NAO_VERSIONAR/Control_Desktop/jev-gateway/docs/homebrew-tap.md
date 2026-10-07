# Homebrew tap for jev-gateway

The brew install path ships the published npm tarball through a separate tap,
`vinilana/homebrew-tap`. This repo owns the release automation
(`.github/workflows/release-homebrew.yml`); the tap repo only holds the
formula. No gateway behavior changes here: npm stays the primary flow.

## Status

Not live yet. The workflow is in place and does nothing until the tap repository and the
`HOMEBREW_TAP_TOKEN` secret exist. Once the first formula has been pushed and `brew install` has
been checked on a real machine, add the install instructions to the README.

## Quick path

1. Create the tap repo `vinilana/homebrew-tap` with a `Formula/` directory (below).
2. Add the `HOMEBREW_TAP_TOKEN` secret to this repo (below).
3. Publish a release by merging the release pull request (see [releasing.md](releasing.md)):
   tag → npm publish → formula bump, all automatic.
4. Verify with `brew install` + `brew test` (below).

## Tap bootstrap

The tap repo does not exist yet: create it once, deterministically:

```bash
gh repo create vinilana/homebrew-tap --public --description "Homebrew tap for jev-gateway"
git clone git@github.com:vinilana/homebrew-tap.git
cd homebrew-tap
mkdir -p Formula
touch Formula/.gitkeep
git add Formula/.gitkeep
git commit -m "chore: bootstrap tap with Formula dir"
git push -u origin HEAD
```

Expected layout after the first automated bump:

```text
homebrew-tap/
  Formula/
    jev-gateway.rb
  README.md   # optional: point back to https://github.com/vinilana/jev-gateway
```

## Token setup

| Topic | Decision |
| --- | --- |
| Secret name | `HOMEBREW_TAP_TOKEN` in this repo (`Settings → Secrets → Actions`) |
| Token type | Fine-grained PAT with `contents:write` on `vinilana/homebrew-tap` only |
| Why a PAT | The workflow pushes to a *different* repo; `GITHUB_TOKEN` cannot do that |
| Never | Hardcode credentials in the workflow or the formula |

## Release ordering

The tag and the GitHub release, then npm, then the formula. `release.yml` publishes the tag to npm
and calls this workflow afterwards, so the tarball normally exists by the time it runs. It still
polls for the tarball before computing the SHA256, because the registry can take a moment to serve
a new version. If the tarball never appears, the run fails with an error naming the version.

Manual re-run for one tag (either mode):

```bash
gh workflow run release-homebrew.yml -f tag=v0.2.2 -f push_mode=direct-push
gh workflow run release-homebrew.yml -f tag=v0.2.2 -f push_mode=pr
```

| Push mode | Behavior |
| --- | --- |
| `direct-push` (default) | Commit straight to the tap's default branch |
| `pr` | Open a bump PR in the tap repo for review before merge |

## Node version note

The formula uses unversioned `depends_on "node"` and the gateway requires
Node.js 22.15 or newer (`engines` in `package.json`). Homebrew's current
`node` satisfies that today. Only pin to `node@22` if the tap maintainer
prefers a versioned dependency: the default stays unversioned.

## Canonical formula

This is exactly what the workflow writes to `Formula/jev-gateway.rb`
(`<VERSION>`, `<TARBALL_URL>`, `<SHA256>` filled in per release):

```ruby
class JevGateway < Formula
  desc "Local LLM gateway that routes tool selection through Jev"
  homepage "https://github.com/vinilana/jev-gateway"
  url "https://registry.npmjs.org/jev-gateway/jev-gateway-<VERSION>.tgz"
  sha256 "<SHA256>"
  license "MIT"

  depends_on "node"

  def install
    system "npm", "install", *std_npm_args
    bin.install_symlink libexec.glob("bin/*")
  end

  test do
    assert_match "jev-codex", shell_output("#{bin}/jev-codex --status")
    assert_match "jev-claude", shell_output("#{bin}/jev-claude --status")
  end
end
```

Why this shape: the npm tarball contains `dist/` beside `bin/`, and the
launcher runs `dist/index.js` when no `src/` checkout is present: so the
whole tarball goes into `libexec` and both `bin/jev-*` launchers are
symlinked. The `test` block uses `--status`, which needs no API keys.

## Verification checklist

- [ ] `brew tap vinilana/tap && brew install --build-from-source --verbose jev-gateway`
- [ ] `brew test jev-gateway` (runs the `--status` checks, no keys needed)
- [ ] `brew audit --strict --online vinilana/tap/jev-gateway` (optional but recommended)
- [ ] `jev-codex --status` and `jev-claude --status` print status lines

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| Workflow fails: "npm tarball still missing" | npm publish lagged behind the GitHub release (the 0.2.2 case) | Publish `jev-gateway@<VERSION>` to npm, then re-run the workflow |
| Workflow fails: "Missing HOMEBREW_TAP_TOKEN" | Secret not set | Add the PAT as described in Token setup |
| Workflow fails: "Could not clone tap repo" | Tap repo not created yet | Follow Tap bootstrap, then re-run |
| `brew test` fails on `--status` | Gateway or Node issue in the bottled env | Run `jev-codex --status` directly for the error, check `depends_on "node"` resolved to Node ≥ 22.15 |

## Next step

User-facing install/update steps live in the [README](../README.md#install-with-homebrew-macos-and-linux).
