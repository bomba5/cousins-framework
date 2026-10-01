# Claude logins and Anthropic's terms

Read this before you run a [cousin](glossary.md#cousin) on a Claude subscription login. The short
version: **the risk is yours**. Anthropic's terms do not clearly allow what a
cousin on a subscription does, Anthropic may enforce them without notice, and
the account you can lose is the one your whole fleet runs on. This project is
not affiliated with or endorsed by Anthropic, and this page is not legal
advice.

If you want no doubt at all, run your cousins on an
[Anthropic API key](install.md#a-cousin-on-a-claude-key-or-login) or on
[opencode](install.md#a-first-cousin-on-opencodes-free-model), not on a
subscription login.

## What Anthropic says

Checked on 2026-10-01. The terms change; read the current text yourself.

The [Claude Code legal and compliance page](https://code.claude.com/docs/en/legal-and-compliance),
"Authentication and credential use":

- OAuth authentication (signing in with a Claude account) "is intended
  exclusively for purchasers of Claude Free, Pro, Max, Team, and Enterprise
  subscription plans and is designed to support ordinary use of Claude Code
  and other native Anthropic applications."
- "Developers building products or services that interact with Claude's
  capabilities, including those using the Agent SDK, should use API key
  authentication." Anthropic "does not permit third-party developers to offer
  Claude.ai login into their own applications, or to route requests through
  Free, Pro, or Max plan credentials on behalf of their users", and
  developers "may not collect, store, or intermediate Claude.ai credentials
  or session tokens".
- It does not prevent "an end user from signing in to the unmodified Claude
  Code binary with their own Claude subscription".
- "Anthropic reserves the right to take measures to enforce these
  restrictions and may do so without prior notice."
- Under "Acceptable use": the advertised usage limits for Pro and Max "assume
  ordinary, individual usage of Claude Code and the Agent SDK."

The [Help Center article on the Agent SDK and subscription plans](https://support.claude.com/en/articles/15036540)
(updated 2026-06-16) says that for now the Agent SDK, `claude -p` and
third-party app usage "still draw from your subscription's usage limits".
That is a statement about billing, not a permission.

Enforcement is not hypothetical: between February and April 2026 Anthropic
blocked subscription logins in third-party agent tools (widely reported at
the time).

## What a cousin on a subscription does

The login lane is every `claude-login` account (`host` and each named one)
and every `claude-token` account. See [accounts.toml](configuration.md#accountstoml).

- **The binary is unmodified.** The `sdk` [runner](glossary.md#runner) drives the Claude Code CLI
  that the Claude Agent SDK bundles, through the SDK; the `tmux` runner types
  into the Claude Code CLI installed on the host, in a terminal. The
  framework does not patch the CLI or call Anthropic's API itself.
- **Your own account, for you.** A cousin runs on credentials you put there,
  for you. The framework has no hosted service, resells nothing and never
  sees another user's account.
- **But it is a product built on the Agent SDK**, which is the case
  Anthropic says should use an API key.
- **`cousin-account token` stores a subscription token** (from
  `claude setup-token`) in a secret file, so the framework stores a
  subscription credential.
- **`cousin-account login --via <cousin>` relays the sign-in through chat**:
  the CLI's own sign-in URL is shown in that cousin's chat and the code you
  paste back is handed to the CLI. The sign-in itself is Anthropic's flow,
  but the framework passes its URL and code along.
- **A fleet is not one person at a keyboard.** Several cousins, woken by
  heartbeats and [flips](glossary.md#flip) around the clock, are hard to call "ordinary,
  individual usage".

Each of these can be read against the terms above. Whether Anthropic does,
and what it does about it, is not something this project can promise you.

## Your choices

| [Lane](glossary.md#lane) | Terms position | Cost |
|---|---|---|
| Anthropic API key (`anthropic-key` account) | what Anthropic tells Agent SDK developers to use | metered |
| opencode (`opencode` account) | no Claude credentials involved; check the terms of the model you pick | free models, or your provider's prices |
| Claude login or token (`claude-login`, `claude-token`) | not clearly allowed; may be enforced without notice | your subscription |

If you run on a subscription anyway, keep it to your own use, keep the fleet
small, and have an API key or an opencode account ready to switch to (a
cousin's account is one line in its `cousin.toml`, see
[accounts.toml](configuration.md#accountstoml)).
