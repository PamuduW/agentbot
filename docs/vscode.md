# VS Code settings and extensions

Agentbot reconciles an explicitly selected VS Code configuration. It owns only
what `vscode.yaml` names, and reports everything else rather than changing it.

```bash
agentbot vscode status   # preview; writes nothing
agentbot vscode seed     # record the installed extensions into vscode.yaml
agentbot vscode apply    # install missing extensions, merge owned settings
```

Apply installs Windows and WSL extensions in groups of up to eight, using repeated
`--install-extension` flags in one CLI invocation to share startup work. It
prints the requested names, then shows a short progress message with the host
and group position. It checks the host's extensions directory to report
individual results, including partial successes. If a successful command leaves
none of the requested extensions in that directory, it reports the CLI output
and expected directory, then stops later groups for that host. Each host uses
its own CLI and extensions directory.

Each command allows five minutes per requested extension (up to 40 minutes for
a full group). A timeout stops the remaining groups on that host and
marks them as not attempted. Other hosts can continue. Retrying installs only
the extensions still missing. Slow downloads can still take time despite batching.
Ctrl+C cancels the CLI with status 130 and a short message.

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
| CLI | `~/.vscode-server/bin/*/bin/code-server` | `…\Microsoft VS Code\bin\code.cmd` via PowerShell |

An extension installed in WSL is not evidence that its Windows counterpart is
installed, so the manifest keeps a list per host.

WSL installs use the server's `bin/code-server` with an explicit
`--extensions-dir`. Agentbot also discovers CLI-managed builds under
`~/.vscode-server/cli/servers/*/server/bin/code-server`. The `remote-cli/code`
terminal launcher requires a VS Code connection and can exit successfully
without installing anything in an ordinary terminal, so it is not used for
extension management.

For Windows, Agentbot checks the selected profile's
`AppData/Local/Programs/Microsoft VS Code/bin/code` first, then the system
install under `Program Files` or `Program Files (x86)`. It does not use another
Windows account's user installation.

Windows installs use the native `code.cmd` entrypoint through `powershell.exe`,
with an explicit Windows `--extensions-dir`. Agentbot translates both paths
with `wslpath` and clears inherited remote CLI routing variables in the child
process. The neighboring `bin/code` shell launcher can delegate to Remote WSL
and install into the Linux host even when its executable lives on Windows.
Path translation or native launch failures are reported without falling back
to that shell launcher.

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

The managed font-family settings use Ubuntu Sans Mono throughout, including
editor text, chat, hints, the debug console, notebooks, source control input,
REST Client and Markdown preview. Ligatures and variable-font features are
disabled for the static font build. Dotfiles installs the fonts for WSL and,
when interop is available, the current Windows user. Windows desktop VS Code
needs the Windows installation even when editing files through Remote–WSL.
Apply the declared settings with `agentbot vscode apply`.

On native Linux, Agentbot also detects a portable desktop installation at
`~/.local/share/code/bin/code` when no remote server CLI is available. It uses
`~/.vscode/extensions` and `~/.config/Code/User/settings.json`, with the existing
`wsl` manifest selection for Linux extensions. This fallback is disabled on WSL.
Catalog qualification uses the full Linux client because the standalone server
omits client-side built-in language extensions. This does not certify WSL or
Windows integration.
