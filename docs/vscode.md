# VS Code settings and extensions

Agentbot reconciles an explicitly selected VS Code configuration. It owns only
what `vscode.yaml` names, and reports everything else rather than changing it.

```bash
agentbot vscode status   # preview; writes nothing
agentbot vscode seed     # record the installed extensions into vscode.yaml
agentbot vscode apply    # install missing extensions, merge owned settings
```

It is also the `vscode` install component, and one row of `agentbot status`.
This surface reached the operator only through a Platform submenu until that
submenu was folded into install and status: a separate menu for three read-and-
apply surfaces made "is this machine set up?" a question with two answers in two
places.

## Two hosts, not one editor

Remote-WSL splits one editor across two hosts, and they are not
interchangeable.

| | WSL host | Windows host |
|---|---|---|
| Extensions | `~/.vscode-server/extensions` | `%USERPROFILE%\.vscode\extensions` |
| Settings | `~/.vscode-server/data/Machine/settings.json` (machine scope) | `…\Code\User\settings.json` (user scope) |
| CLI | `~/.vscode-server/bin/*/bin/remote-cli/code` | `…\Microsoft VS Code\bin\code` |

An extension installed in WSL is not evidence that its Windows counterpart is
installed, so the manifest keeps a list per host.

`vscode/settings.universal.json` is written to **every** available host's
settings file, and `vscode/settings.wsl.json` / `vscode/settings.windows.json`
layer per-host overrides on top of it.

Writing universal keys into both files, rather than only into the Windows
user-scope file both hosts read, is deliberate. User-scope settings do reach a
remote window, but machine-scoped keys do not — an interpreter path is
machine-specific whether or not the editor is. A single shared file would
therefore work for some settings and silently fail for exactly the ones the
per-host file exists to hold.

**`code` on `PATH` inside WSL is the Windows executable**, reached through
interop. It is the correct CLI for the Windows host and the wrong one for the
WSL host — driving WSL with it installs into the other host while reporting
success for this one. Agentbot resolves each host's own CLI rather than taking
whatever `PATH` offers.

## The manifest

```yaml
version: 1
extensions:
  wsl:
    - charliermarsh.ruff
  windows:
    - ms-vscode-remote.remote-wsl
```

Settings are authored as JSON, in the same JSONC syntax as `settings.json`
itself, so a block can be pasted straight across:

```
vscode/settings.universal.json   # applied to both hosts
vscode/settings.wsl.json         # overrides, WSL only
vscode/settings.windows.json     # overrides, Windows only
```

```jsonc
{
  // Comments are fine here too.
  "editor.fontSize": 13,
  "files.autoSave": "off"
}
```

**They are JSON rather than a YAML block for a concrete reason.** YAML's
implicit typing rewrites real VS Code values: `files.autoSave: off` is the
string `"off"` to VS Code and the boolean `false` to YAML, and
`editor.wordWrap: on` is the string `"on"`. Authoring settings in YAML would
silently write values VS Code does not accept. Extensions stay in
`vscode.yaml`, where the values are plain identifier strings that YAML cannot
misread.

`agentbot vscode seed` fills in `extensions` from what is currently installed,
so an existing setup becomes the baseline. It never seeds `settings`: copying a
settings file wholesale would claim ownership of every key in it, and explicit
ownership is the point of the manifest.

## What it will not do

- **Remove an extension.** Absence from the manifest is reported as `unmanaged`,
  never uninstalled. Removal needs its own explicit selection.
- **Guess a Windows profile.** If several Windows accounts have VS Code
  installed, the host is reported unresolvable rather than written to on a coin
  flip.
- **Write a file it could not read.** An unparseable `settings.json` stops that
  scope and leaves the file untouched.
- **Touch credentials, Settings Sync, profiles, keybindings, or snippets.**

## How settings are merged

Owned keys are edited into the file's existing text: values are replaced in
place and new keys appended. The file is not re-serialised from a parsed object,
because that would delete every comment the operator wrote and reformat settings
maintained by hand.

Two shapes matter and are covered by tests, both found by running the merge
against a real `settings.json` rather than a fixture:

- A **trailing comma** before the closing brace is legal JSONC and common in
  hand-written settings. A second one is legal nowhere.
- **CRLF line endings** on the Windows side. Appending an LF line would leave
  one mixed line in an otherwise consistent file.

The original is copied to `settings.json.agentbot-backup` immediately before the
replace, and the write itself is atomic.
