# patches/ — manual upstream patches (NOT applied automatically)

Audit L-14 note: these `.patch` files are **orphaned by design** — no
Dockerfile step, entrypoint, install script, or proxy code references them.
They preserve hard-won workarounds for the third-party
[`holger81/email-mcp`](https://github.com/holger81/email-mcp) stdio server and
are kept here for manual rebasing only.

## Baseline and conflict

Both patches target `src/services/imap.service.ts` at upstream commit
`17be138` and their hunks **overlap** (same region of `ImapService`), so they
cannot be applied on top of each other:

| File | Purpose |
| --- | --- |
| `holger81-email-mcp-resolve-mailbox-path.patch` | Maps user-facing mailbox names to the exact path returned by IMAP `LIST` (Exchange/Outlook stall on UID `COPY` when the destination does not match the server's canonical folder path, e.g. `INBOX` vs `Inbox`). |
| `holger81-email-mcp-bulk-per-uid.patch` | Replaces UID-range `COPY` with per-UID copy-then-delete steps (bulk range `COPY` stalls on some hosts, e.g. Exchange Online; single-UID steps match `moveEmail`). |

Pick one, or rebase the second onto the first manually.

## How to apply (if you really need them)

```sh
git clone https://github.com/holger81/email-mcp
cd email-mcp
git checkout 17be138
git apply /path/to/mcp-proxy-server/patches/holger81-email-mcp-resolve-mailbox-path.patch
npm ci && npm run build
```

Then register the built entry point as a stdio server in the admin UI — do
**not** expect `@codefuturist/email-mcp` npm installs to carry these fixes.

There are no tests for these patches in this repository and no CI applies
them; if upstream merges equivalent fixes, delete this directory.
