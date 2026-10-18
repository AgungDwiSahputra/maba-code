# Laporan REA: cara kerja Claude Code → desain MABA Code

Dokumen ini adalah **bukti** di balik desain MABA Code. Setiap klaim diberi status:

- **[V]** terverifikasi oleh sumber yang disebut,
- **[V-saya]** saya verifikasi sendiri (kode/uji di repo ini atau panggilan Bedrock nyata),
- **[I]** inferensi.

## Metode dan batasan (penting)

Analisis dilakukan secara **clean-room**: hanya dokumentasi publik dan perilaku yang dapat diamati.
Bundle/binary Claude Code yang terpasang **tidak** didekompilasi atau dibongkar (itu melanggar ketentuan
layanan), dan `.credentials.json` serta token tidak dibuka. Dua agen REA dijalankan paralel:

| Agen | Sumber yang dipakai | Hasil |
|---|---|---|
| REA-1 (Claude Code) | Dokumentasi resmi `code.claude.com/docs/en/*` lewat WebFetch/WebSearch; file milik pengguna di `~/.claude` (settings, skills, memory — read-only; dari transkrip `.jsonl` hanya nama kunci JSON yang dihitung) | Tabel fitur + 10 fitur MVP |
| REA-2 (Strands) | Source Strands Agents 1.54.0 yang terpasang (Apache-2.0) + 15 skrip uji offline dengan model palsu | Peta API terverifikasi + gotcha Windows |

Catatan kejujuran: ringkasan halaman dokumentasi diambil lewat WebFetch (bersifat *lossy*) dan **tidak saya
ambil ulang satu per satu**; detail kecil (angka batas, nama event) sebaiknya dicek ulang ke halaman aslinya
sebelum dijadikan acuan kontrak.

---

## A. Temuan tentang Claude Code → implementasi di MABA Code

| # | Fitur Claude Code | Bukti | Status | Implementasi MABA Code | Uji |
|---|---|---|---|---|---|
| 1 | Agent loop: ulangi *prompt → tool_use → tool_result* sampai respons tanpa tool call; ada batas turn/budget dengan hasil bertipe (`success`, `error_max_turns`, …) | `agent-sdk/agent-loop`: "repeats until the task is complete" | [V] | Loop bawaan Strands + `limits={"turns": N}` → `stop_reason == "limit_turns"` | `test_turn_limit_stops_runaway_loop` |
| 2 | Tool read-only boleh paralel; Edit/Write/Bash berurutan | `agent-sdk/agent-loop`: "Read-only tools… can run concurrently" | [V] | `ConcurrentToolExecutor` bawaan Strands untuk semua tool + `Workspace.write_lock` yang menserialkan tool tulis/shell | `test_parallel_reads_all_succeed` |
| 3 | Tool bawaan: Read, Edit, Write, Bash, Glob, Grep, Agent, TodoWrite, ExitPlanMode … ; tanpa izin: baca/cari/todo; butuh izin: Bash/Edit/Write | `tools-reference` | [V] | `maba_code/tools.py`: `read_file`, `edit_file`, `write_file`, `glob_files`, `grep_files`, `run_command`, `todo_write`, `exit_plan_mode`, `explore` | `tests/test_agent.py` |
| 4 | Edit = `old_string`/`new_string`/`replace_all`; wajib Read dulu; string harus cocok persis & unik | `tools-reference` | [V] | `fsops.plan_edit` + pelacak `Workspace.was_read_fresh` (juga menolak bila file berubah setelah dibaca) | `test_edit_without_read…`, `test_edit_fails_if_file_changed…`, `test_plan_edit_unique_and_errors` |
| 5 | Grep berbasis ripgrep: `output_mode` (files_with_matches/content/count), `glob`, `head_limit`; Glob maks. 100 hasil urut mtime | `tools-reference` | [V] | `grep_files` (rg, cadangan Python), `glob_files` | `test_grep_and_glob_with_ripgrep`, `test_grep_python_fallback…` |
| 6 | Bash: timeout default 2 menit (maks. 10), output dipotong, proses background | `tools-reference` | [V] (background: tidak ditiru) | `run_command`: timeout 120 s (maks. 600), batas 30k karakter, kill pohon proses | `test_run_command_timeout`, `test_timeout_kills_process_tree` |
| 7 | Evaluasi izin berurutan **deny → ask → allow**; allow tidak bisa membatalkan deny; ditegakkan harness, bukan model | `permissions`: "Rules are evaluated in order: deny, then ask, then allow" | [V] | `PermissionPolicy.evaluate` + `PermissionHook` (hook `BeforeToolCallEvent`) | `test_deny_beats_allow_and_ask_beats_allow` |
| 8 | Mode: `default`, `acceptEdits`, `plan`, `bypassPermissions` (+ `auto`, `dontAsk`) | `permission-modes` | [V] | `default`, `acceptEdits`, `plan`, `bypass` (Shift+Tab / `/mode`) | `test_plan_mode*`, `test_default_mode` |
| 9 | Prompt izin: Yes / "don't ask again for…" / No (+ komentar); Esc = No | `permissions` | [V] | Dialog satu-tombol `y`/`a`/`n`/`e` + Esc; opsi `a` membuat aturan sesi spesifik | `test_single_key_choices`, `test_allow_for_session…` |
| 10 | Path terlindungi (`.git`, `.vscode`, `.idea`, `.husky`) selalu ditanya kecuali bypass | `permissions` | [V] | `Workspace.is_protected` | `test_outside_cwd_and_protected` |
| 11 | Plan mode: model riset lalu `ExitPlanMode` meminta persetujuan | `tools-reference` (ExitPlanMode butuh izin), `permission-modes` | [V] | `exit_plan_mode` + dialog rencana (3 opsi) | `test_plan_mode_blocks_writes_then_plan_approval…` |
| 12 | Memori berlapis CLAUDE.md (user → proyek → lokal), `@import` | `memory` | [V] | `maba_code/memory.py` → `MABA.md`, `MABA.local.md`, `~/.maba/MABA.md`, `@import` | `test_memory_file_is_injected_into_system_prompt` |
| 13 | Kompaksi: bersihkan output tool lama dulu, baru ringkas; `/compact [instruksi]` | `how-claude-code-works` | [V]; ambang pemicu **tidak** terverifikasi | `SummarizingConversationManager` (proaktif di 75% konteks) + `/compact [fokus]` — ambang 75% adalah pilihan saya **[I]** | `test_compact_reduces_messages` |
| 14 | Sesi tersimpan JSONL per proyek; `--continue`, `--resume` | `sessions`; struktur lokal `~/.claude/projects/<path>/<id>.jsonl` (kunci `uuid`, `parentUuid`, `cwd`, …) | [V] | `FileSessionManager` + meta judul; `--continue`, `--resume ID`, `/resume` | `test_session_persists_and_resumes`; **[V-saya]** resume dengan model nyata |
| 15 | Checkpoint file sebelum edit; `/rewind`; perubahan via Bash tidak dilacak | `checkpointing` | [V] | `Workspace.checkpoints` + `/undo` (hanya sesi berjalan; Bash tidak dilacak) | `test_write_existing_preserves_crlf_and_creates_checkpoint` |
| 16 | Slash command/skill berbasis markdown (`$ARGUMENTS`) | `skills` | [V] | `.maba/commands/*.md` (+ `~/.maba/commands`) | `test_custom_command_expands_arguments` |
| 17 | Sub-agen: konteks baru, hanya ringkasan kembali; Explore/Plan read-only | `sub-agents` | [V] | tool `explore` (agen read-only, izin ditolak otomatis) | `test_explore_subagent_returns_report` |
| 18 | UI: `Esc` interupsi, `Shift+Tab` ganti mode, `!` shell, `@` path, status line | `interactive-mode` | [V] | Esc (watcher), Shift+Tab, `!cmd`, `@file` + autocomplete, toolbar status | `tests/test_interactive.py` |
| 19 | Diff sebelum edit | Terverifikasi hanya untuk VS Code/desktop; terminal hanya terlihat di changelog | [I] | Dialog izin menampilkan diff berwarna; edit yang diterima otomatis tetap menampilkan diff ringkas | `test_dialog_shows_diff…` |
| 20 | Hooks, MCP, skills penuh, auto-memory, sub-agen dari file, background bash, /loop, WebFetch | `hooks`, `mcp`, `skills`, … | [V] | **Diimplementasikan di v0.2** — lihat bagian D | `tests/test_extensions.py` |
| 21 | Worktree, auto-mode classifier, WebSearch, hook tipe http/prompt/agent | `hooks`, `tools-reference` | [V] | Belum (WebSearch: Bedrock tidak punya backend pencarian) | — |

Rujukan lokal (milik pengguna, read-only): `C:\Users\agung\.claude\settings.json` baris 2-4 (`defaultMode`),
6-17 (hook PreToolUse bertipe `command`), 34 (`autoMemoryEnabled`).

## B. Temuan tentang Strands Agents 1.54.0 yang memengaruhi desain

Lokasi source: `…\Python312\Lib\site-packages\strands` (selanjutnya `S`).

| Temuan | Bukti | Status | Dampak desain |
|---|---|---|---|
| `Agent(…)` menerima `hooks`, `conversation_manager`, `session_manager`, `tool_executor`; `stream_async(prompt, limits=, cancel_signal=)`; `agent.cancel()` thread-safe | `S\agent\agent.py` (ctor, `cancel`, `stream_async`) | [V-saya] introspeksi `inspect.signature` | Pakai `stream_async` di loop asyncio milik aplikasi, bukan `agent()` sinkron |
| `BeforeToolCallEvent.cancel_tool = "teks"` membatalkan tool dan mengirim teks ke model sebagai error | `S\hooks\events.py:208-232` | [V-saya] dibaca; teruji lewat `PermissionHook` | Persetujuan dilakukan **inline** di hook async (bukan Interrupts): lebih sederhana, UI tetap dalam satu event loop |
| `limits={"turns": N}` → `stop_reason "limit_turns"`, history tetap valid | `S\types\agent.py:17-37`, `event_loop.py:67-93` | [V-saya] | Batas turn per perintah |
| `BedrockModel` tidak boleh menerima `boto_session` **dan** `region_name` bersamaan | `S\models\bedrock.py:249` (REA-2) | [V] | Region diatur di `boto3.Session`, bukan di model |
| `strands_tools.shell`/`python_repl` gagal di-import di Windows (`termios`, `pty`, `fcntl`); `file_write`/`editor` memakai `open(path,"w")` tanpa encoding | `T\shell.py:53-60`, `T\python_repl.py:42-51`, `T\file_write.py:260` (REA-2) | [V] | **Semua tool ditulis sendiri** (UTF-8, pathlib, PowerShell, kill pohon proses) |
| Item `list[PydanticModel]` pada `@tool` diteruskan sebagai `dict` mentah | — | [V-saya] ditemukan lewat uji (`'dict' object has no attribute 'content'`) | `todo_write` memvalidasi sendiri dengan `TodoItem.model_validate` |
| `gpt-6-luna` tidak ada di tabel context window Strands (`gpt-5.6-luna` = 1.050.000) | `S\models\_defaults.py:91-98` | [V-saya] | `contextWindow` dibuat configurable (default 256k, **asumsi**) |
| Cancel saat tool berjalan menyisakan history berakhir pada pesan `user` | REA-2 (diuji dengan model palsu) | [V] | `_compose_prompt` menyisipkan pesan asisten "[Dihentikan oleh pengguna]" | 

## C. Verifikasi Bedrock nyata (profile `agung`, region ap-southeast-1)

| Pemeriksaan | Hasil |
|---|---|
| `aws sts get-caller-identity --profile agung` | berhasil (identitas tipe `user`) |
| ID model `openai.gpt-6-luna` (on-demand) | **Ditolak**: "on-demand throughput isn't supported… use an inference profile" |
| ID `global.openai.gpt-6-luna` — Converse + tool use | `stopReason: tool_use`, argumen `{"a":17,"b":25}` benar |
| `converse_stream` + tool use | event `contentBlockDelta.toolUse` mengalir; `messageStop: tool_use` |
| `maba --doctor` | profile ✓ · kredensial ✓ (tidak dicetak) · STS ✓ · model merespons 1,5 s |
| Tugas agentik nyata (cari bug → edit → jalankan tes → lapor) | berhasil; diff tampil; tes lulus setelah perbaikan |
| Resume sesi nyata (`--continue`) | model mengingat perubahan sebelumnya dari riwayat tersimpan |
| Mode `default` non-interaktif | penulisan ditolak, model menjelaskan dan tidak membuat file |

Ditemukan dan diperbaiki selama pengujian: ID tanpa prefiks `global.` ditolak Bedrock; `git branch -D x`
sempat lolos sebagai perintah "aman" (regex terlalu longgar — kini hanya bentuk persis yang diizinkan);
hitungan "token masuk" mengabaikan token cache; Enter setelah Tab-completion tidak mengirim pesan.

Seluruh suite v0.1: **74 uji lulus** (`python -m unittest discover -s tests -t .`).

---

## D. Putaran 2 (v0.2): kemampuan agentik lanjutan, tetap di atas GPT-6 Luna

**Metode.** Sama, clean-room. Permintaan `/rea https://docs.anthropic.com/en/docs/claude-code` tidak dijalankan
sebagai `capture-browser-scenario`: perintah itu bersifat runtime (membuka browser) dan hanya membedah bundle JS situs
dokumentasi, bukan perilaku agen. Sebagai gantinya: (1) dokumentasi publik `code.claude.com/docs/en/{hooks, mcp,
sub-agents, skills, tools-reference, interactive-mode, memory, permissions, settings, scheduled-tasks}` lewat WebFetch
(sebagian halaman diringkas WebFetch → *lossy*); (2) source Strands 1.54.0 + `mcp` 1.29.1 yang terpasang.
**Kendala desain dari pengguna:** GPT-6 Luna tidak boleh diganti; semua fitur baru dikunci ke `CORE_MODEL`.

### D1. Temuan Claude Code → implementasi

| # | Temuan (dokumentasi publik) | Status | Implementasi MABA v0.2 | Uji |
|---|---|---|---|---|
| 22 | Hooks: `hooks.<Event>[{matcher, hooks:[{type:"command", command, timeout}]}]`; stdin JSON; exit 0/2/lain; JSON `permissionDecision`, `updatedInput`, `additionalContext`, `decision:block`, `continue`; hook paralel, keputusan paling ketat menang; timeout default 600 s; batas lanjut Stop 8× | [V] | `hooks.py` + PreToolUse/PostToolUse di `PermissionHook`, UserPromptSubmit/Stop di `run_prompt`, SubagentStop, SessionStart/End, PreCompact. Hook "allow" hanya ASK→ALLOW (tidak melewati deny/plan/pagar rahasia) — pilihan saya **[I]** | `HookIntegrationTests` (skrip Python nyata via PowerShell) |
| 23 | MCP: `.mcp.json` `mcpServers` (stdio / `type:"http"`/`sse`), `${VAR}`/`${VAR:-default}`, tool `mcp__<server>__<tool>`, server proyek perlu persetujuan, aturan `mcp__server` / `mcp__server__*` | [V] | `mcp_support.py` (parser sendiri + `MCPClient`/`MCPAgentTool` Strands), trust store bersidik jari, `/mcp` | `McpIntegrationTests` (server FastMCP lokal) |
| 24 | Sub-agen: `.claude/agents/*.md` (name, description, tools, disallowedTools, maxTurns, model); konteks terisolasi, hanya ringkasan kembali; bawaan Explore/Plan/general-purpose; **nesting kini diizinkan** (dibatasi kedalaman) | [V] | `agents.py` + tool `task(subagent_type, prompt)`; kedalaman maks. 2 via `ContextVar`; `model:` diabaikan (kendala Luna) | `SubagentTests` |
| 25 | Skills: `SKILL.md` + frontmatter (`description`, `allowed-tools`, `disable-model-invocation`, `user-invocable`, `context: fork`, `agent`); hanya deskripsi di konteks sampai dipanggil; `$ARGUMENTS`, `${CLAUDE_SKILL_DIR}` | [V] | `skills.py` + tool `skill` + `/nama`; `allowed-tools` hanya untuk skill pengguna, berlaku 1 giliran — pembatasan saya **[I]** | `SkillTests` |
| 26 | Background Bash: `run_in_background` → id; output ke file yang dibaca dengan Read; `TaskStop`; `BashOutput`/`KillShell` **sudah dihapus** di versi terbaru | [V] | `tasks.py`: log `~/.maba/tasks/…`, `task_stop`, `/tasks`, notifikasi selesai ke pesan berikutnya | `BackgroundTaskTests` |
| 27 | `/loop 5m <prompt>` interval tetap, hanya saat idle; tanpa interval = self-paced | [V] | `/loop` interval tetap (maks. 20×); self-paced belum | `LoopTests` |
| 28 | Auto memory: `~/.claude/projects/<p>/memory/`, indeks `MEMORY.md` (200 baris / 25 KB dimuat tiap sesi), agen menulis sendiri | [V] | `memory.auto_memory_dir`, prompt + izin `.md` tanpa dialog | `AutoMemoryTests` |
| 29 | WebFetch: URL + prompt, HTML→Markdown, dijawab panggilan model terpisah, cache 15 menit, redirect lintas host dikembalikan, tolak localhost/host tanpa titik, aturan `WebFetch(domain:x)` | [V] | `web.py` + sub-agen Luna tanpa tool; tambahan: tolak IP privat setelah DNS **[I]** | `WebFetchUnitTests`, `WebFetchFlowTests` |

### D2. Temuan Strands/mcp yang memengaruhi desain

| Temuan | Status | Dampak |
|---|---|---|
| `SummarizingConversationManager(summarization_agent=…)` memakai agen terpisah; **subclass** memecah `restore_from_session` (nama kelas dicek) | [V-saya] dibaca di source | Kompaksi dikunci ke Luna lewat `summarization_agent`, bukan subclass |
| `BeforeToolCallEvent.tool_use` dan `AfterToolCallEvent.result` dapat ditulis | [V-saya] teruji | `updatedInput` & konteks PostToolUse |
| Parameter tool bernama `agent` dicadangkan `@tool` (diisi instance Agent; hilang dari skema) | [V-saya] ditemukan saat implementasi | Parameter `task` dinamai `subagent_type` (sama dengan Claude Code) |
| `MCPClient.load_servers` meneruskan stderr server ke terminal & tidak mengenal `type` | [V] | Parser sendiri; `stdio_client(errlog=file)` |
| Nama tool Bedrock `^[a-zA-Z0-9_-]{1,64}$` (lebih ketat dari 128 Claude Code); registry menolak nama yang hanya beda `-`/`_` | [V] | `mcp_tool_name` (sanitasi + hash) & deduplikasi |
| `Agent.cancel()` saat agen diam membatalkan giliran **berikutnya** | [V-saya] dibaca di `agent.py` | Esc saat `/loop` menunggu hanya membangunkan loop |
| `AgentSkills` plugin menulis ulang `system_prompt` & tidak menegakkan `allowed-tools` | [V] | Skill dibuat sendiri |

### D3. Kunci GPT-6 Luna

`CORE_MODEL = "global.openai.gpt-6-luna"` di `config.py`; `ChatApp.core_model` dibuat sekali dan tidak disentuh
`/model`. Satu-satunya pembuat agen anak adalah `ChatApp._sub_agent()` (plus `self.summarizer`), keduanya memakai
`core_model`. `LunaPinTests` menjalankan explore, `web_fetch`, skill fork, general-purpose, dan `/compact` setelah
`/model sonnet`, lalu membuktikan kelimanya hanya memanggil skrip model Luna.

Seluruh suite v0.2: **118 uji lulus**. Belum diverifikasi terhadap Bedrock nyata pada putaran ini kecuali bila
dinyatakan di README/sesi.
