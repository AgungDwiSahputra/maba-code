---
description: Reverse engineer a file, folder, app, or URL with REA (Reverse Engineer Anything) and explain the findings
argument-hint: <path | URL | pasted code> [question about the target]
allowed-tools: Bash(rea:*), Bash(npm root:*), Read, Glob, Grep, Write
---

# Reverse engineer with REA

Target and question supplied by the user:

```
$ARGUMENTS
```

Installed REA version: !`rea --version`

## Ground rules

- If the target above is empty, ask the user for a target (path, URL, or code) and stop.
- The first token(s) are the target; any remaining text is the user's question. With no question,
  explain what the target is, how it is built, and its notable behavior.
- Only analyze software the user owns or is authorized to examine (see `tools/rea/DISCLAIMER.md`).
- Static commands (`inspect-*`, `analyze-javascript-application`, `search`, `decompile`) only read files.
  Runtime commands (`capture-*`, `observe-*`, `instrument-with-frida`, `debug-with-*`, any `*-adb-*`
  command that installs/starts/stops apps) execute or change something: confirm with the user first.
- Never run `rea setup`, `rea update`, or `rea uninstall` without explicit approval; they change agent config.
- If the target is ordinary readable source code in a repository, REA adds little: say so and analyze it
  with normal tools instead.

## 1. Identify the target

- **Local path**: resolve it to an absolute path with forward slashes and quote it
  (e.g. `"D:/apps/example/app.asar"`). Confirm it exists. Check the extension and, if unclear,
  start with `rea inspect-artifact "<path>"`, which returns the format plus suggested next probes.
- **URL**: use the browser workflow in step 2.
- **Pasted code**: REA analyzes files, not text. For JavaScript, write it into a new empty folder in
  your session scratchpad (or the system temp dir) and analyze that folder; for other languages,
  explain the code directly.

## 2. Pick the first REA command

Run `rea <command> --help` (or `--schema`) before using options you are unsure about.

| Target | First command | Follow-ups |
| --- | --- | --- |
| Unknown file, archive, installer, MSIX/ZIP/APK/IPA/DMG | `inspect-artifact` | the `next probes` it returns, `extract-artifact` |
| `.asar` or extracted JavaScript/Electron folder | `analyze-javascript-application` | `recover-javascript-sources`, `trace-application-feature`, `inspect-analysis-view`, `inspect-electron-fuses` |
| .NET assembly (`.dll`/`.exe` with a CLI header) | `inspect-managed-artifact` | `inspect-managed-members [--type Full.Name]`, `inspect-managed-native-boundaries` |
| Go binary | `inspect-go-binary` | |
| Rust binary | `inspect-rust-binary` | |
| Windows PE resources/icons/version info | `inspect-pe-resources` | |
| SQLite database | `inspect-sqlite-database` | |
| HAR / Fiddler `.saz` capture | `inspect-web-network-capture` | |
| pcap / pcapng | `inspect-pcap-capture` | needs tshark + capinfos |
| WASM | `inspect-wasm-artifact` | needs WABT via `REA_WABT_BIN_DIRECTORY` |
| Android APK | `inspect-android-package`, `identify-flutter-build` | class/method decompile needs JADX (`REA_JADX_MCP_JAR`) |
| Native binary (pseudocode, assembly, xrefs) | `analyze` | `search`, `decompile`, `function`, `xrefs`, `trace` |
| URL / website | `capture-browser-scenario` | `export-web-scripts` → `analyze-javascript-application`, `inspect-web-network-capture` |

Notes for this machine (Windows x64, Node 24):

- **Native decompilation** (`analyze`, `decompile`, `function`, ...) needs Ghidra, Hopper, or IDA, and none
  is configured yet. Check with `rea providers`. If none is available, do what static-only commands
  allow (`inspect-artifact`, `inspect-pe-resources`, `inspect-go-binary`, `inspect-rust-binary`,
  `inspect-managed-*`) and tell the user that deeper analysis needs e.g. `GHIDRA_INSTALL_DIR` set to an
  extracted Ghidra 12.1.x directory.
- **URL**: Chrome and Edge are installed. Get the input format with `rea capture-browser-scenario --schema`,
  build the smallest scenario that opens the URL, and run it. For an already-open browser started with
  `--remote-debugging-port`, use `list-browser-targets` → `inspect-web-page` / `analyze-web-bundle`.

## 3. Run it without flooding the context

- Start bounded: `rea <command> "<target>" --token-limit 4000`. Page with `--token-offset`, or narrow with
  `--filter-output key.path,other.key`.
- For large results, save the full output to a file in your scratchpad
  (`rea <command> "<target>" --format json > <scratchpad>/rea-<command>.json`) and read only the parts you need.
- For a large JavaScript application, analyze first, then drill down with `inspect-analysis-view` on the
  returned Evidence instead of repeating the analysis.
- Don't repeat an identical command that already succeeded or failed; change the input or fix the blocker first.
- If a command reports a missing provider or tool, run `rea doctor` once, report the specific remediation,
  and continue with whatever static analysis still works.

For detailed workflows, read only the relevant guide from REA's bundled skill
(`<npm root -g>/rea-agents/skills/reverse-engineer-anything/references/`) or the docs in `tools/rea/docs/`
(e.g. `cli.md`, `javascript-application-workflows.md`, `managed-code-analysis.md`).

## 4. Analyze and report

Report in this structure:

1. **Target**: name, format, architecture, size, SHA-256 (from the Evidence `subject`).
2. **Answer / how it works**: plain-language explanation of what the target does or how the requested
   feature works, tracing entry point → data/state → result where the evidence allows.
3. **Evidence**: the key facts behind each conclusion, citing `evidence_id` values and concrete locations
   (module/file path, type/member, address, offset).
4. **Observations vs inferences vs unknowns**: keep these separate. Static analysis never proves runtime
   behavior; carry over REA's own limitations and incomplete-coverage notes.
5. **Next steps**: the most useful follow-up REA commands or missing providers that would close the gaps.

If the user asked to build something similar, separate the observed behavior from your own design choices
and implement it with the normal coding tools.
