---
title: Passwords & Logins
description: The agent signs into sites, pays and fills addresses for you without ever seeing a password.
---

# Passwords & Logins

Say **"log into GitHub"** and the agent signs in for you. The first time it
reaches a sign-in page it has no login for, it asks you, right there, in a
masked prompt. After that it just works. Passwords are encrypted on this
machine and injected straight into the page; the model never sees them.

There is nothing to set up.

## What it looks like

**CLI / TUI**

```
🔐 Save login for github.com
   The agent reached a sign-in page with no saved login for this site.
   Type the email / username you sign in with (shown), then Enter.
   ...
   Now the password (hidden). It is encrypted on this machine, bound to
   https://github.com, and filled into the page without the model ever seeing it.
```

**Desktop** — a "Save your github.com login?" card with an identifier field and
a masked password field. *Save & sign in* stores it and continues; *Don't save*
tells the agent to stop asking for this turn.

From then on the agent lists your saved logins, types the identifier itself and
fills the password through Hermes. The tool result it sees is
`{filled_fields: 1, origin: "https://github.com"}`; the password is also
registered with the redactor so a later page read cannot echo it back.

## Two-factor codes

Sites that ask for a code after the password are handled the same way:

- **Authenticator key saved with the login** (the "setup key" or `otpauth://`
  link a site shows when you enable 2FA; 1Password and Bitwarden items that
  hold a TOTP seed count too): Hermes generates the current code and enters
  it. Nobody is asked. Add the key in **Settings → Passwords & Logins → Add**
  or `hermes vault add`; the item shows a *2FA auto* badge.
- **Code sent to your phone or email**: a small prompt appears in your
  surface ("Verification code for github.com"), you type the code, Hermes
  enters it into the page. The code never enters the conversation either.
- **Passkeys, hardware keys, app approvals** ("tap Approve in Duo"): nothing
  to type. The agent tells you to complete it on your device and waits for
  the page to move on.

## Already using 1Password or Bitwarden?

Nothing to enable. If the `op` or `bw` command-line tool is installed and signed
in, Hermes picks it up automatically and its website logins become fillable
alongside the local ones. The first time the agent needs one of those logins it
asks you to unlock the manager with your master password (masked prompt; once
per session, 30 minutes idle). Hermes hands the master password to the manager's
CLI through its non-interactive channel (`op signin` on stdin, `bw unlock
--passwordenv` in the child's environment) and keeps only the session token in
memory. The agent never sees the master password, the token, or any login.
A manager item that lists several websites (say `amazon.co.uk`,
`www.amazon.co.uk` and `eu.account.amazon.com`) fills on each of those exact
origins; nothing is inferred beyond the URLs saved on the item.

Prefer not to use a detected manager? `hermes vault sources --disable bitwarden`,
or the switch in **Settings → Passwords & Logins**.

### Dedicated Bitwarden bot account in unattended sessions

The normal Bitwarden integration asks for an unlock through a masked CLI/Desktop
prompt. To use an **agent-only** Bitwarden Password Manager account in Telegram,
cron or another unattended session without keeping a second Hermes login item,
an operator may opt in to a protected, profile-local unlock file:

```yaml
vault:
  bitwarden:
    enabled: true
    binary_path: /home/agent/.local/bin/bw
    account_email: bot@example.com
    unattended_password_file: /home/agent/.hermes/secrets/bitwarden-unlock.env
    appdata_dir: /home/agent/.hermes/vault/bitwarden-cli
```

Sign in the `bw` CLI once as **that exact account** with
`BITWARDENCLI_APPDATA_DIR=/home/agent/.hermes/vault/bitwarden-cli` in its
environment; verify `bw status` with that same appdata path. Create the
appdata directory with mode `0700` inside the selected profile's `vault/`
directory. Never point it to the operator's default `bw` state or to another
Hermes profile. The master-password file must be inside that profile's
`secrets/` directory, owned by the process user, a regular non-symlink file
with mode `0600`, and contain exactly one line:
the raw master password or `BW_PASSWORD=<master password>`. Never place that
value in `config.yaml`, a shell command, or the conversation. Both directories
and every checked parent must be owner-controlled (not group/world-writable),
and symlink path components below the resolved profile root are rejected. The
browser backend checks the signed-in account against the configured email before
each CLI operation and unlocks with `bw --passwordenv` using the CLI child's
environment, retains the session token only in process memory, and syncs before
listing logins so newly shared or rotated items are used. It
returns only handles and login identifiers to the agent; actual passwords are
resolved at fill time and injected only on an exact website origin from the
Bitwarden item's URI list. The `bw list items` CLI returns full decrypted item
JSON *internally* to the trusted backend before it strips the password fields
for the agent; this design does not provide per-item decryption or prevent a
compromised host user from accessing the dedicated account. Add the **real
sign-in origin**, not just a site's marketing homepage, to each Bitwarden
Login. Automatic codes generated from a saved TOTP seed are likewise refused
unless the code page has one of the login's bound origins. When this mode is
configured, browser onboarding refuses to create another Hermes-local copy;
add new logins to the dedicated Bitwarden account.

This is a deliberate security tradeoff: an attacker who controls the host user
and that file can unlock the bot account. Restrict the account to collections
of credentials the agent is authorized to use, protect and recover its bootstrap
separately, and never use this option for your personal full-access vault. The
setting is off by default; without it, unattended sessions still cannot unlock
Bitwarden and fail closed.

## Paying and filling addresses

Cards and addresses work the same way as logins: saved once (**Settings →
Passwords & Logins → Add**, or `hermes vault add`), bound to the checkout site,
and filled by the agent on that site only. **Every card fill asks you first**,
with the same approval prompt as a dangerous command; declining writes nothing.
Headless sessions (cron, webhooks, the API server) cannot confirm and are
refused, so a prompt injection that reaches a checkout page can ask, but it
cannot spend. Address fills need no confirmation.

## Managing what's saved

- **Desktop → Settings → Passwords & Logins**: everything saved, the detected
  password managers with Unlock/Lock, Add, Remove.
- **CLI**: `hermes vault list`, `hermes vault add`, `hermes vault rm <handle>`,
  `hermes vault sources`.

Items live encrypted under `~/.hermes/vault/` (Fernet key + vault file, both
`0600`), scoped to the profile. Labels, site origins and login identifiers are
visible metadata; passwords and card values never leave the vault except into
the page.

## Headless sessions

Cron jobs, webhooks, the API server and `hermes chat -q` have nobody to answer a
prompt. Saved local logins keep working there; by default, a locked password
manager reports `unavailable_in_this_session` and a missing login reports
`prompt_unavailable`. The opt-in dedicated Bitwarden bot configuration above
can unlock without a prompt; 1Password can use a service account token
(`OP_SERVICE_ACCOUNT_TOKEN`).

```yaml
vault:
  onepassword:
    enabled: false          # opt OUT of a detected manager (default: on when installed)
    account: ""             # `op --account` shorthand; empty = default
    service_account_token_env: OP_SERVICE_ACCOUNT_TOKEN
  bitwarden:
    enabled: false
    account_email: ""                # required for unattended_password_file
    unattended_password_file: ""      # disabled by default; profile-local 0600 file
```

## What this does and does not guarantee

**Does:** the password never enters the model's context through Hermes: not in
tool results, logs, the session database, or the CLI arguments of any process.
Fills happen over the supervised browser session's direct CDP socket and are
refused unless the page origin exactly matches the saved origin, checked again
inside the page immediately before the write.

**Does not:** protect against the page itself. Once a password is typed into a
site, that site (and any script it runs) has it, exactly as when you type it
yourself. On a cloud browser backend the vendor's browser sees the page like any
other. The origin binding is the guard against filling on the wrong site, not
against a compromised right one.
