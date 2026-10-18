# MABA Code

Asisten coding agentik di terminal — dibangun di atas **[Strands Agents](https://strandsagents.com/)** dan model
**Amazon Bedrock**. Alur kerjanya terinspirasi Claude Code (dipelajari lewat analisis clean-room dari
dokumentasi publik; lihat [`docs/REA-LAPORAN.md`](docs/REA-LAPORAN.md) untuk buktinya).

- **Mesin inti: GPT-6 Luna** (`global.openai.gpt-6-luna`, konstanta `CORE_MODEL`). Agen utama memakainya secara
  default (bisa diganti dengan `--model`/`/model`), tetapi **sub-agen, skill `context: fork`, `web_fetch`, dan
  kompaksi selalu dikunci ke GPT-6 Luna**, apa pun pilihan `/model`.
- **Kemampuan agentik (v0.2):** hooks, sub-agen dari file, skills, klien MCP, task shell latar belakang, `/loop`,
  memori otomatis, dan `web_fetch`. Semuanya ditegakkan oleh harness yang sama (izin deny → ask → allow).
- **Antarmuka (v0.3):** status kerja agen real-time, dialog izin yang menjelaskan dampak, ringkasan akhir giliran,
  toolbar adaptif, `/status`, dan pratinjau UI tanpa model (lihat [Pengalaman pemakaian](#pengalaman-pemakaian)).
- **Kredensial tersembunyi:** memakai profile AWS lokal `agung` (`--profile agung` adalah default). Kunci tidak
  pernah dibaca, ditulis, atau ditampilkan oleh MABA Code; boto3 memuatnya sendiri dari profile.

## Mulai cepat

```powershell
cd "D:\Workspace Agung\MABA"
python -m maba_code --doctor        # uji profile → kredensial → STS → model (aman, tidak mencetak kunci)

cd D:\proyek\saya                    # masuk ke proyek yang ingin dikerjakan
& "D:\Workspace Agung\MABA\maba.cmd"                                 # sesi interaktif
& "D:\Workspace Agung\MABA\maba.cmd" -p "jelaskan arsitektur proyek ini"   # sekali jalan
```

Opsional: `pip install -e .` di folder MABA agar perintah `maba` tersedia global.
Dependensi: `strands-agents`, `boto3`, `rich`, `prompt_toolkit` (semua sudah terpasang di mesin ini).

## Pengalaman pemakaian

Tujuan antarmukanya: pengguna selalu tahu **apa yang sedang dikerjakan agen**, **apa yang akan terjadi bila ia
menyetujui**, dan **apa yang bisa dilakukan berikutnya**.

```
❯ perbaiki bug di calc.py lalu jalankan tes
● Cari file  **/calc.py
  ⎿ ✓ 1 file · 0.2s
● Baca  calc.py
  ⎿ ✓ 8 baris (dari 8) · 0.2s
╭─ ⚠ Izin diperlukan · TULIS ─────────────────────────────────╮
│ Edit file  calc.py  (menulis file)                           │
│ Agen menjelaskan: "Saya akan memperbaiki pembagi di mean."   │
│ -    return sum(nums) / (len(nums) + 1)                      │
│ +    return sum(nums) / len(nums)                            │
│ Dampak: Mengubah isi file yang sudah ada. Bisa dibatalkan    │
│ dengan /undo.                                                │
│ Mode aktif: standar · tanya dulu                             │
│ [y] Ya, terapkan perubahan                                   │
│ [n] Tidak — agen diberi tahu lalu mencari cara lain          │
│ [e] Tidak, beri arahan — Anda mengetik alasan/arahan         │
│ Enter = Ya.                                                  │
╰──────────────────────────────────────────────────────────────╯
⠹ Menjalankan python -m unittest  langkah 3 · 12s · langkah ini 9s  (Esc untuk batal)
  ✓ Selesai dalam 14s · 3 langkah
    Diubah: calc.py (+1 −1)
    Dijalankan: 1 perintah
    1.2k masuk · 340 keluar · konteks 32% (diringkas otomatis di 75%)
    Berikutnya: /undo membatalkan perubahan file
```

Lihat semua tampilan tanpa memanggil model (tanpa biaya): `python -m maba_code.ui.preview` (opsi `--width 60`,
`--ascii`, `--svg hasil.svg`).

| Fitur | Cara pakai |
|---|---|
| Kirim pesan | ketik lalu **Enter**; **Alt+Enter** atau `\` + Enter untuk baris baru. Input kosong menampilkan petunjuk (placeholder) |
| Batalkan giliran | **Esc** (atau Ctrl+C); ketikan Anda saat agen bekerja tidak hilang, muncul lagi di prompt |
| Baris status | saat agen bekerja: tahap (Berpikir / Menulis jawaban / Membaca … / Menjalankan …), nomor langkah, waktu berjalan, dan lama langkah yang lambat. Sub-agen tampil sebagai `explore › Membaca a.py (3 langkah)` |
| Ringkasan giliran | selesai/dihentikan/batas tercapai, jumlah langkah, file yang diubah (+/−), perintah dijalankan, aksi ditolak, token & konteks, dan **langkah berikutnya** (mis. `/undo`). Waktu menunggu jawaban Anda dipisahkan dari waktu kerja |
| Ganti mode izin | **Shift+Tab** (standar → terima-edit → rencana) atau `/mode`; arti mode baru tampil sesaat di toolbar |
| Sertakan file/folder | ketik `@` → menu file & folder langsung muncul (bagian yang cocok di-highlight); isi file dilampirkan ke pesan (maks. 5 file); file rahasia tidak pernah muncul |
| Shell langsung | `!git status` (hasilnya masuk konteks pesan berikutnya) |
| Perintah | ketik `/` → menu semua perintah (bawaan, skill, kustom) + deskripsi langsung muncul |
| Menu `/` & `@` | ↑/↓ pilih · **Tab** pilih (folder: lanjut ke isinya) · **Enter** pada `/…` menjalankan perintah yang di-highlight · **Enter** pada `@…` memilih file (Enter lagi untuk mengirim) · **Esc** tutup menu. Toolbar menampilkan petunjuk tombol yang sesuai |
| Riwayat | ↑/↓ di baris pertama/terakhir, atau **Ctrl+R** untuk mencari |
| Toolbar bawah | mode + keterangan (mis. "tanya dulu") · model · meter konteks `▰▰▱▱▱ 32%` (kuning ≥60%, merah ≥85%) · token · todo · task latar; bagian yang kurang penting dijatuhkan di terminal sempit |
| Keadaan sesi | `/status`: mode, model, konteks, todo, file yang bisa di-undo, task latar, server MCP |
| Pesan sistem | berikon per level: `ℹ` info · `✓` sukses · `⚠` peringatan · `✗` error (pengganti ASCII: `i v ! x`) |
| Keluar | `/exit`, Ctrl+D, atau Ctrl+C dua kali |

### Perintah slash

`/help` `/clear` `/model [alias]` `/mode [nama]` `/plan [tugas]` `/compact [fokus]` `/resume [no|id]` `/todo` `/undo`
`/status` `/cost` `/init` `/memory` `/tools` `/permissions` `/config` `/doctor` `/exit`
`/agents` `/skills` `/tasks [stop id]` `/loop [interval] <prompt>` `/mcp`

Perintah kustom: simpan prompt di `.maba/commands/nama.md` (atau `~/.maba/commands/`) — gunakan `$ARGUMENTS`.
Frontmatter `description:` opsional. Skill yang bisa dipanggil pengguna juga muncul sebagai `/nama` (dan menang atas
perintah kustom bernama sama).

### Alias model

`luna` (default, inti) · `sol` · `astra` · `sonnet` · `opus` · `haiku` · `fable` · `kimi` · `glm` · `grok`,
atau ID Bedrock lengkap. Model OpenAI di Bedrock **wajib** lewat inference profile (`global.…`).
`/model` hanya mengganti **agen utama**; semua agen anak tetap GPT-6 Luna. `--doctor` selalu ikut menguji Luna.

## Izin & keamanan

Tiga kelas tool, ditegakkan oleh harness (hook Strands), bukan oleh model:

| Kelas | Tool | Perilaku |
|---|---|---|
| Baca | `read_file` `glob_files` `grep_files` `todo_write` `explore` `task` `skill` `task_stop` | otomatis (di luar folder kerja: ditanya, kecuali folder skill, log task & memori) |
| Tulis | `write_file` `edit_file` | ditanya, dengan diff (`.md` di folder memori otomatis: otomatis) |
| Berbahaya | `run_command` | ditanya (perintah baca-saja seperti `git status` otomatis; di latar belakang selalu ditanya) |
| Internet | `web_fetch` | ditanya; "jangan tanya lagi" = `web_fetch(domain:host)` |
| MCP | `mcp__<server>__<tool>` | selalu ditanya; aturan `mcp__server`, `mcp__server__*`, `mcp__server__tool` |

Sub-agen (`task`) sendiri tidak ditanya, tetapi **setiap tool yang dipakai sub-agen** melewati kebijakan yang sama;
dialognya diberi label `[sub-agen nama]`.

**Dialog izin** selalu menjelaskan tiga hal: *apa* yang diminta (judul, target, diff/perintah, dan kutipan kalimat
terakhir agen sebelum memanggil tool bila ada), *dampaknya* bila disetujui, dan *apa yang terjadi* setelah dijawab.
Lencana kelas izin di judul panel: `TULIS` (kuning) · `BERBAHAYA` · `INTERNET` · `MCP` (merah). Untuk tiga kelas
terakhir **Enter tidak menyetujui**; harus menekan `y` (edit file tetap Enter = Ya).

Mode: **default** · **acceptEdits** (edit di folder kerja otomatis) · **plan** (hanya baca, lalu `exit_plan_mode`
untuk persetujuan) · **bypass** (semua otomatis; hanya untuk lingkungan yang Anda percaya).

Aturan evaluasi: **deny → ask → allow**; allow tidak bisa membatalkan deny. Contoh `~/.maba/settings.json`:

```json
{
  "profile": "agung",
  "model": "luna",
  "contextWindow": 256000,
  "permissions": {
    "allow": ["run_command(dotnet build*)", "run_command(npm test*)", "web_fetch(domain:*.python.org)", "mcp__github__*"],
    "ask":   ["run_command(git push*)"],
    "deny":  ["run_command(rm *)", "run_command(Remove-Item*)"]
  },
  "autoMemory": true,
  "hooks": { "...": "lihat bagian Hooks" },
  "mcpServers": { "...": "lihat bagian MCP" }
}
```

Pagar tambahan:
- **Rahasia diblokir** (juga di mode bypass): `.env*`, `*.pem`, `*.key`, `~/.aws`, `~/.ssh`, serta perintah yang
  menyentuhnya atau mencetak `AWS_SECRET_ACCESS_KEY`. Autocomplete & grep ikut menyaring file ini.
- Proses `run_command` **tidak mewarisi** variabel lingkungan kredensial (`AWS_SECRET_ACCESS_KEY`, token, dsb.).
- **Settings proyek tidak dipercaya**: `.maba/settings*.json` di repo hanya boleh menambah aturan `deny`/`ask` dan
  mengatur `model`/batas; `profile`, `region`, `mode`, `permissions.allow`, `hooks`, `mcpServers`, dan `autoMemory`
  diabaikan (dilaporkan sebagai peringatan). Server MCP dari `.mcp.json` proyek perlu persetujuan; skill proyek tidak
  boleh memberi izin lewat `allowed-tools`.
- Aturan allow berpola tidak berlaku untuk perintah majemuk (`;` `&&` `|` `>` …); `python …` dsb. hanya cocok persis.
- Edit wajib didahului `read_file` dan ditolak bila file berubah di disk setelah dibaca.

> Pagar berbasis pola ini *best-effort*, bukan sandbox. Perlindungan sebenarnya adalah izin IAM minimal pada profile
> Anda dan membiarkan mode default tetap aktif untuk proyek yang tidak dikenal.

## Memori proyek

`MABA.md` (setara `CLAUDE.md`) dimuat otomatis: `~/.maba/MABA.md` → `MABA.md`/`.maba/MABA.md` dari akar sampai
folder kerja → `MABA.local.md`. Mendukung `@impor/file.md`. Buat otomatis dengan `/init`.

### Memori otomatis

Agen menulis catatannya sendiri di `~/.maba/projects/<proyek>/memory/` (satu fakta per file `.md`, indeks
`MEMORY.md`; 200 baris / 25 KB pertama indeks dimuat tiap sesi). Menulis `.md` di folder itu tidak memerlukan dialog
(plan mode tetap memblokir; pagar rahasia tetap berlaku) dan setiap penulisan diumumkan di layar. Lihat dengan
`/memory`; matikan dengan `"autoMemory": false` di `~/.maba/settings.json`.

## Sub-agen

Tool `task(subagent_type, prompt)` menjalankan sub-agen di **konteks baru** (selalu GPT-6 Luna); hanya laporan
akhirnya yang kembali. Beberapa `task` dalam satu giliran berjalan paralel. Sub-agen boleh bersarang satu tingkat
(sub-agen tingkat 2 tidak lagi mendapat `task`). Bawaan:

| Nama | Tool | Izin |
|---|---|---|
| `explore` | baca/cari | read-only; permintaan izin otomatis ditolak (juga tersedia sebagai tool `explore`) |
| `plan` | baca/cari | read-only; mengembalikan rencana implementasi |
| `general-purpose` | semua tool (kecuali `todo_write`/`exit_plan_mode`) | sama dengan agen utama; dialog berlabel `[sub-agen …]` |

Agen sendiri: `~/.maba/agents/*.md` atau `.maba/agents/*.md` (file pengguna menang):

```markdown
---
name: reviewer
description: Meninjau diff untuk bug & keamanan. Gunakan setelah perubahan kode.
tools: read_file, grep_files, glob_files     # opsional; nama Claude Code (Read, Grep, Bash…) juga diterima
disallowedTools: run_command                 # opsional
maxTurns: 20                                 # opsional (default 25)
---
Kamu peninjau kode yang teliti…
```

Field `model:` diabaikan dengan peringatan: sub-agen MABA selalu GPT-6 Luna. Lihat daftar dengan `/agents`.

## Skills

`~/.maba/skills/<nama>/SKILL.md` atau `.maba/skills/<nama>/SKILL.md` (+ file pendukung di folder yang sama). Hanya
`nama: deskripsi` yang masuk system prompt; isi lengkap dimuat saat model memanggil tool `skill` atau pengguna
mengetik `/nama argumen`.

```markdown
---
description: Siapkan catatan rilis dari git log. Gunakan saat diminta membuat release notes.
allowed-tools: Bash(git log *) run_command(git tag *)   # izin tanpa dialog selama giliran itu (skill pengguna saja)
disable-model-invocation: false                          # true = hanya lewat /nama
user-invocable: true                                     # false = hanya model
context: fork                                            # opsional: jalankan di sub-agen (GPT-6 Luna)
agent: general-purpose                                   # sub-agen untuk context: fork
---
Buat catatan rilis untuk $ARGUMENTS memakai template ${MABA_SKILL_DIR}/template.md …
```

## Hooks

Perintah shell yang dijalankan harness di titik-titik siklus agen (format Claude Code). **Hanya** dari
`~/.maba/settings.json`; kunci `hooks` di settings proyek diabaikan.

```json
"hooks": {
  "PreToolUse":  [{"matcher": "run_command|Bash", "hooks": [{"type": "command", "command": "python C:/hooks/cek.py", "timeout": 30}]}],
  "PostToolUse": [{"matcher": "edit_file|write_file", "hooks": [{"type": "command", "command": "npx prettier --check ."}]}],
  "Stop":        [{"hooks": [{"type": "command", "command": "python C:/hooks/wajib-tes.py"}]}]
}
```

| Event | Kapan | Efek khusus |
|---|---|---|
| `PreToolUse` | sebelum tool, sebelum kebijakan izin dievaluasi | exit 2 / `permissionDecision: deny` memblokir (alasan ke model); `allow` melewati dialog *ask* (tidak bisa melewati deny/plan/pagar rahasia); `ask` memaksa dialog; `updatedInput` mengganti argumen (lalu dievaluasi ulang) |
| `PostToolUse` | setelah tool berhasil berjalan | `additionalContext` / exit 2 ditempel ke hasil tool |
| `UserPromptSubmit` | sebelum prompt dikirim | exit 2 memblokir prompt; stdout menjadi konteks |
| `Stop` / `SubagentStop` | agen selesai menjawab | exit 2 / `decision: block` memaksa lanjut dengan alasan (maks. 8×) |
| `SessionStart` | startup, resume, clear, compact | stdout menjadi konteks pesan berikutnya |
| `SessionEnd` | keluar | — (timeout 5 s) |
| `PreCompact` | sebelum kompaksi manual/otomatis | — |

Stdin JSON: `session_id`, `transcript_path`, `cwd`, `permission_mode`, `hook_event_name`, `tool_name`, `tool_input`,
`tool_use_id`, `tool_response`, `prompt`, `stop_hook_active`, `agent_type`, `source`, `reason`, `trigger`.
Exit 0 = sukses (stdout JSON diproses), exit 2 = blokir (stderr = alasan), lainnya = peringatan non-blokir.
Matcher: kosong/`*` = semua, `A|B` = daftar nama persis (alias Claude Code `Bash`, `Edit`, `Write`, `Read`, … diterima),
selain itu regex. Hook yang cocok berjalan paralel; keputusan paling ketat menang. Timeout default 600 s; Esc
membatalkan. Env tambahan: `MABA_PROJECT_DIR` (juga `CLAUDE_PROJECT_DIR`).

## MCP

```json
// ~/.maba/mcp.json (dipercaya)  atau  .mcp.json di proyek (perlu persetujuan sekali per server)
{"mcpServers": {
  "github": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"], "env": {"GITHUB_TOKEN": "${GITHUB_TOKEN}"}},
  "docs":   {"type": "http", "url": "https://example.com/mcp", "headers": {"Authorization": "Bearer ${DOCS_TOKEN}"}}
}}
```

Server dijalankan saat sesi mulai (stdio, `http`, atau `sse`); tool-nya muncul sebagai `mcp__<server>__<tool>` (nama
disanitasi agar valid untuk Bedrock, maks. 64 karakter) dan selalu ditanya. Persetujuan server proyek disimpan di
`~/.maba/mcp-trust.json` bersama sidik jari konfigurasinya (konfigurasi berubah → ditanya lagi); di mode `-p` server
proyek yang belum disetujui dilewati. `${VAR}` bernama mirip rahasia (TOKEN, KEY, SECRET, …) **tidak** diekspansi untuk
`.mcp.json` proyek. Output tool dipotong 100k karakter; stderr server ditulis ke `~/.maba/logs/mcp-<proyek>.log`.
Status: `/mcp`.

## Task latar belakang & `/loop`

- `run_command(..., run_in_background=true)` langsung mengembalikan id `bg-xxxxxx`; output ditulis ke
  `~/.maba/tasks/<proyek>/<id>.log` (model membacanya dengan `read_file`). Saat selesai, ringkasan (exit code + 20 baris
  terakhir) disisipkan ke pesan berikutnya. `task_stop` / `/tasks stop <id>` menghentikan; maks. 8 berjalan; semua
  dimatikan saat MABA Code keluar. Toolbar menampilkan jumlah task yang berjalan.
- `/loop 5m <prompt atau /perintah>` mengulang tiap interval tetap (default 10m, minimum 10s, maks. 20 iterasi);
  berhenti dengan Esc/Ctrl+C, saat error, atau saat prompt diblokir hook.

## web_fetch

`web_fetch(url, prompt)`: halaman diubah ke Markdown lalu dijawab oleh sub-agen GPT-6 Luna tanpa tool (isi halaman
diperlakukan sebagai data tak tepercaya). http → https, redirect ke host lain dikembalikan (tidak diikuti), cache
15 menit, maks. 5 MB / 100k karakter. Host lokal/privat (termasuk hasil DNS ke IP privat) dan URL berkredensial ditolak.
Tidak ada WebSearch: Bedrock tidak menyediakan backend pencarian.

## Sesi

Setiap percakapan disimpan di `~/.maba/sessions/<proyek>/`. Lanjutkan dengan `--continue`, `--resume ID`, atau `/resume`.
Konteks panjang diringkas otomatis (≈75% window) atau manual dengan `/compact`. `/undo` mengembalikan file yang
diubah agen pada giliran terakhir (perubahan lewat `run_command` tidak dilacak).

## Opsi CLI

`-p/--print` · `--profile` · `--model` · `--region` · `--mode` · `--max-turns` · `-c/--continue` · `--resume ID` ·
`--cwd` · `--doctor` · `--version`. Tanpa terminal (mis. `echo "..." | maba`) otomatis memakai mode `-p`.
Env: `MABA_PROFILE`, `MABA_MODEL`, `MABA_REGION`, `MABA_MODE`, `MABA_HOME`, `MABA_ASCII=1`, `MABA_DEBUG=1` (log ke `~/.maba/debug.log`).

## Pengujian

```powershell
python -m unittest discover -s tests -t .      # 188 uji, offline, ±2 menit
```

Suite memakai model skrip yang mengalirkan event Converse sungguhan, sehingga loop Strands, hook izin, tool, UI, dan
prompt_toolkit (lewat pipe input) benar-benar dijalankan tanpa jaringan. Fitur v0.2 diuji di `tests/test_extensions.py`:
hook memakai skrip Python sungguhan lewat PowerShell, MCP memakai server FastMCP lokal (`tests/fixtures/`),
`web_fetch` memakai `httpx.MockTransport`, dan `LunaPinTests` membuktikan semua agen anak memakai GPT-6 Luna setelah
`/model sonnet`. Antarmuka (status, dialog izin, ringkasan giliran, toolbar, banner, `/help`, `/status`) diuji di
`tests/test_ui_ux.py`, termasuk lewat prompt_toolkit sungguhan. Pengujian nyata ke Bedrock: `--doctor` dan `-p`.

## Riwayat versi

Versi ada di dua tempat yang dijaga tes (`VersionTests`): `maba_code/__init__.py` dan `pyproject.toml`.
Tampil di banner, `--version`, `--doctor`, dan User-Agent `web_fetch`. Setelah menaikkan versi, jalankan ulang
`pip install -e .` agar metadata paket ikut diperbarui.

| Versi | Isi |
|---|---|
| **0.3.0** | **Antarmuka yang menjelaskan diri.** Baris status berisi tahap, langkah, dan waktu; sub-agen terlihat; dialog izin berlencana (TULIS/BERBAHAYA/INTERNET/MCP) dengan dampak dan kutipan agen; ringkasan akhir giliran (file diubah, perintah, aksi ditolak, token, konteks, langkah berikutnya); pesan berikon per level; toolbar adaptif dengan meter konteks dan petunjuk sesuai menu; placeholder input; banner adaptif dengan catatan kesiapan; `/help` berkelompok; `/status`; pengumuman kompaksi otomatis; peringatan mendekati batas giliran; `python -m maba_code.ui.preview`. **Perubahan perilaku:** Enter tidak lagi menyetujui dialog berbahaya/internet/MCP (harus `y`). |
| 0.2.0 | Kemampuan agentik: hooks, sub-agen dari file, skills, klien MCP, task latar belakang, `/loop`, memori otomatis, `web_fetch`. |
| 0.1.0 | Agen terminal dasar: tool file/shell, izin deny → ask → allow, sesi, kompaksi, mode izin. |

## Batasan yang diketahui

- Tampilan TTY interaktif diuji lewat pipe input & console rekam, **bukan** diamati visual di terminal sungguhan.
  Pengamat Esc dan *type-ahead* (memakai `msvcrt`, hanya aktif di konsol Windows nyata) tidak punya uji otomatis;
  Ctrl+C sebagai pembatal giliran juga belum diuji di TTY nyata. Untuk menilai tampilan tanpa memanggil model,
  pakai `python -m maba_code.ui.preview`. Animasi baris status (spinner) hanya terlihat di terminal sungguhan.
- Pengumuman kompaksi otomatis diuji dengan memanggil `reduce_context` langsung; pemicu proaktif Strands di 75% baru
  terbukti pada sesi panjang nyata. Mode `-p` belum diamati terhadap Bedrock setelah pembaruan UI v0.3.
- `contextWindow` untuk `gpt-6-luna` adalah **asumsi** 256k (tidak ada di tabel Strands); sesuaikan bila perlu.
- Biaya dolar tidak dihitung (tarif model akun Anda tidak diketahui); `/cost` menampilkan token.
- Pembatalan (Esc) menghentikan model di batas chunk dan `run_command`; tool sinkron pihak ketiga tidak bisa diinterupsi.
- Hooks hanya tipe `command` (belum `http`/`prompt`/`agent`/`mcp_tool`); event lain Claude Code (PermissionRequest,
  Notification, …) belum ada. `/loop` hanya interval tetap (belum self-paced/cron). Sub-agen belum bisa dijalankan
  di latar belakang atau dilanjutkan. Resource/prompt MCP belum diekspos (hanya tool).
- Task latar belakang dimatikan saat keluar normal/Ctrl+C; bila proses MABA dibunuh paksa, task bisa tertinggal
  (sengaja tidak ada "pembersihan PID" otomatis karena PID bisa dipakai ulang proses lain).
- Memori otomatis bisa terisi catatan yang keliru (mis. dari prompt injection di file yang dibaca): periksa `/memory`.
- Belum ada: WebSearch, membaca gambar/PDF, antrean pesan, checkpoint persisten antar-proses.

## Struktur

```
maba_code/
  cli.py  repl.py  session.py  commands.py   # titik masuk, REPL, orkestrasi agen, perintah slash
  permissions.py  tools.py  fsops.py  shell.py  workspace.py   # izin, tool, utilitas file, shell
  config.py  models.py  memory.py  prompts.py  mentions.py     # settings + CORE_MODEL, Bedrock, MABA.md, prompt, @file
  hooks.py  agents.py  skills.py  frontmatter.py               # hooks pengguna, sub-agen, skills
  tasks.py  web.py  mcp_support.py                             # task latar belakang, web_fetch, klien MCP
  ui/  terminal.py  prompt.py  markdown_stream.py  keys.py  theme.py   # tampilan, input, simbol & label
       activity.py  preview.py                                          # status kerja agen, pratinjau UI
tests/  (fixtures/mcp_echo_server.py)   docs/REA-LAPORAN.md   MABA.md   maba.cmd
```
