# Contributing to open-rmf-adapters-quiksync

Thanks for your interest. This document covers the contribution discipline
the project follows. Read it before opening a pull request.

## License

This project is licensed under [Apache-2.0](LICENSE). By submitting a
contribution, you agree to license your work under the same terms.

## Developer Certificate of Origin (DCO) sign-off

Every commit must carry a `Signed-off-by:` trailer attesting that you have
the right to submit the contribution under the project's license. We follow
the standard [Developer Certificate of Origin](https://developercertificate.org/)
convention.

Practical recipe: add the trailer automatically when you commit:

```bash
git commit --signoff -m "your message"
```

The trailer looks like:

```
Signed-off-by: Your Name <your.email@example.org>
```

Use the same name + email as your `git config user.name` / `user.email`.

## Generative AI policy

This project **adopts the OSRF policy on the use of generative tools in
contributions** verbatim. The canonical text is at:

<https://github.com/openrobotics/osrf-policies-and-procedures/blob/main/OSRF%20Policy%20on%20the%20Use%20of%20Generative%20Tools%20(%E2%80%9CGenerative%20AI%E2%80%9D)%20in%20Contributions.md>

Read it before submitting any contribution that used a generative tool in
whole or in part. The short version:

1. Generative tools are **permitted** in contributions.
2. Their use **must be disclosed** at the time of the contribution.
3. The disclosure must live as long as the contribution (i.e. in the commit
   message, not just the PR description).
4. The **contributor is responsible** for verifying accuracy, ensuring no
   IP / license issues, running tests, and applying the project's normal
   review discipline to the generated output.

### How to disclose

Add a `Generated-by:` trailer to the commit message of every commit where a
generative tool produced any portion of the changes. List the fully-qualified
tool name including the provider and version / release information.

The canonical commit shape stacks three trailers (see [§ Authorship](#authorship)
for the role each one plays):

```
feat(fleet_adapter_quiksync): add reconnect-on-token-expiry to the WSS pump

The pump now preemptively closes-and-reopens the WebSocket at 80% of the
JWT TTL ± 10 min jitter so the gateway doesn't see a synchronised reconnect
storm across multi-adapter customers every TTL cycle.

Generated-by: Anthropic Claude Opus 4.7 (1M context)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>

Signed-off-by: Jane Doe <jane@example.org>
```

Multiple tools used in the same commit can be listed semicolon-separated on
the same line:

```
Generated-by: Anthropic Claude Opus 4.7 (1M context); GitHub Copilot v3.2
```

Pull request descriptions should also restate the disclosure (the commit
trailer is the durable record; the PR description is for reviewer
visibility).

### Authorship

When a generative tool produced material portions of the work, also add a
`Co-Authored-By:` trailer naming the tool, in addition to the
`Generated-by:` disclosure trailer. The `Co-Authored-By:` trailer surfaces
the assist in GitHub's authorship UI; `Generated-by:` is the machine-
readable disclosure record per the OSRF policy format.

Example:

```
Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
```

### Scope

The policy applies to source code, documentation, tests, configuration,
graphical works, and any other content in this repository.

## Code style

- **Python**: follow [PEP 8](https://peps.python.org/pep-0008/). Format with
  `black` if you have it; `ruff` if you don't. Type hints are encouraged.
- **Docstrings**: include a short summary line + relevant detail. Module
  docstrings should explain the module's role in the package.
- **Tests**: every new module / function should be testable. CI runs
  `pytest` against each package independently.

## Pull request flow

- Branch from `main`. Branch names follow `<type>/<short-slug>` —
  `feat/`, `fix/`, `refactor/`, `docs/`, `chore/`, `ci/`, `test/`, `perf/`.
- Conventional Commits subject line: `<type>(<scope>): <description>`.
- Open the PR against `main`. Fill in the PR body with what changed +
  test plan + generative-AI disclosure (if applicable).
- Squash-merge is the default merge method.

## Review process

The `main` branch is protected by a repository ruleset. To merge a PR you need:

1. **CI green** — the `colcon build + test` workflow must pass.
2. **One approving review from a code owner.** Code owners are listed in
   [`.github/CODEOWNERS`](.github/CODEOWNERS); GitHub auto-requests review
   from them when a PR touches files they own.
3. **Signed commits.** Every commit on the PR branch must be signed (GPG
   or SSH). See [GitHub's guide on commit signing](https://docs.github.com/en/authentication/managing-commit-signature-verification/about-commit-signature-verification).
4. **DCO sign-off** on every commit (`Signed-off-by:` trailer, see above).
5. **Generative-AI disclosure** on every commit that used one
   (`Generated-by:` trailer, see above).
6. **All review threads resolved** before merging.

Force pushes to `main` and branch deletion are blocked. The ruleset is
enforced for all contributors; the project owner may bypass the review
requirement when no second maintainer is available, but CI + signing +
DCO requirements are non-negotiable.

## Testing

Per-package locally:

```bash
python3 -m venv venv && source venv/bin/activate
pip install -e packages/quiksync_client -e packages/fleet_adapter_quiksync \
            -e packages/door_adapter_quiksync -e packages/lift_adapter_quiksync \
            httpx pytest pytest-asyncio websockets pyyaml
for p in packages/*/; do pytest "$p/test" -q; done
```

CI exercises the dry-run path (no `rmf_adapter`); live-Open-RMF wire-up
is exercised against staging per [`docs/smoke.md`](docs/smoke.md).
