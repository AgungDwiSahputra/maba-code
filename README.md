# MABA Code

Asisten coding agentik di terminal, dibangun di atas **[Strands Agents](https://strandsagents.com/)** dengan model
dari **Amazon Bedrock** atau **Claude API**. Alur kerjanya terinspirasi Claude Code (analisis clean-room dari
dokumentasi publik; bukti di `docs/REA-LAPORAN.md`, salinan lokal).

- **Mesin inti GPT-6 Luna** (`global.openai.gpt-6-luna`). Agen utama bisa diganti lewat `/model`, tetapi sub-agen,
  skill `context: fork`, `web_fetch`, dan kompaksi selalu memakai Luna.
- **Agentik:** tool file & shell, sub-agen, skills, hooks, klien MCP, task latar belakang, `/loop`, memori otomatis.
- **Aman secara default:** izin deny → ask → allow ditegakkan harness (bukan model), dialog izin menjelaskan dampak,
  perubahan file bisa dibatalkan dengan `/undo`.
- **Kredensial tidak pernah disentuh:** boto3 membaca profile AWS dan SDK `anthropic` membaca `ANTHROPIC_API_KEY`.
  MABA Code tidak membaca, mencetak, atau menyimpan kunci.

## Instalasi

**Prasyarat:** Python ≥ 3.10, Git, dan akses Amazon Bedrock lewat profile AWS (default bernama `agung`).
**Opsional:** ripgrep (pencarian lebih cepat) dan API key Anthropic (untuk model `api-*`).

### Windows (PowerShell)

```powershell
winget install Python.Python.3.12
winget install Git.Git
winget install Amazon.AWSCLI              # untuk menyiapkan profile AWS
winget install BurntSushi.ripgrep.MSVC    # opsional
# tutup lalu buka ulang terminal agar PATH baru terbaca

git clone https://github.com/AgungDwiSahputra/maba-code.git "D:\Workspace Agung\MABA"
cd "D:\Workspace Agung\MABA"
python -m pip install -e .
```

Perintah `maba` kini tersedia di semua folder. Bila tidak dikenali (folder `Scripts` Python tidak ada di PATH),
pakai launcher `& "D:\Workspace Agung\MABA\maba.cmd"` atau `python -m maba_code`.

### macOS (Terminal, zsh)

```bash
brew install python@3.12 git awscli ripgrep
git clone https://github.com/AgungDwiSahputra/maba-code.git ~/maba-code
cd ~/maba-code
python3.12 -m venv .venv                 # Python Homebrew menolak pip install global (PEP 668)
.venv/bin/pip install -e .
echo 'alias maba="$HOME/maba-code/.venv/bin/maba"' >> ~/.zshrc && source ~/.zshrc
```

> MABA Code dikembangkan dan diuji di Windows; jalur macOS belum diuji. Di macOS, shell agen memakai bash dan
> giliran dibatalkan dengan **Ctrl+C** (Esc hanya bekerja di konsol Windows).

### Kredensial

**AWS (wajib).** Siapkan profile, lalu pastikan model yang dipakai sudah aktif di Bedrock untuk akun tersebut:

```bash
aws configure --profile agung        # access key + region, mis. ap-southeast-1
aws configure sso --profile agung    # atau lewat SSO
```

#### Mengganti profile AWS (`--profile`)

Default-nya `agung`. Pilih profile lain lewat salah satu cara berikut; yang lebih bawah **menimpa** yang di atasnya:

| Prioritas | Cara | Cakupan |
|---|---|---|
| 1 (terendah) | `"profile": "kantor"` di `~/.maba/settings.json` | permanen |
| 2 | env `MABA_PROFILE` | sesi terminal (`$env:MABA_PROFILE="kantor"` · `export MABA_PROFILE=kantor`) atau permanen (`setx MABA_PROFILE kantor`, buka ulang terminal) |
| 3 (tertinggi) | `maba --profile kantor` | sekali jalan |

```bash
maba --profile kantor --doctor    # uji dulu: profile → kredensial → STS → model
maba --profile kantor -c          # pakai profile baru dan lanjutkan sesi terakhir
```

- **Dibaca saat start.** Tidak ada `/profile`: untuk berganti, keluar lalu jalankan `maba` lagi (riwayat aman lewat `-c`).
- **Berlaku untuk semua panggilan Bedrock**, termasuk sub-agen, `web_fetch`, dan kompaksi (selalu GPT-6 Luna). Karena itu
  profile tetap diperlukan walau agen utama memakai model `api-*`.
- **Region** mengikuti profile; timpa dengan `--region` atau env `MABA_REGION` bila perlu.
- **Settings proyek tidak berlaku:** `"profile"` di `.maba/settings*.json` diabaikan dengan peringatan.
- **Cek yang aktif:** banner awal, `/model` (tanpa argumen), atau `/config`.
- **Error umum:** *"Profile AWS 'x' tidak ditemukan"* → `aws configure list-profiles`; kredensial SSO kedaluwarsa →
  `aws sso login --profile x`.

**Anthropic (opsional, untuk model `api-*`).** Simpan di environment pengguna, jangan di file proyek atau settings:

| OS | Perintah |
|---|---|
| Windows | `setx ANTHROPIC_API_KEY "sk-ant-..."` lalu tutup **penuh** terminal/VS Code dan buka lagi |
| macOS | `echo 'export ANTHROPIC_API_KEY="sk-ant-..."' >> ~/.zshrc && source ~/.zshrc` |

### Verifikasi

```bash
maba --doctor                     # profile → kredensial → STS → GPT-6 Luna (biaya kecil, kunci tidak dicetak)
maba --model api-opus --doctor    # sekaligus menguji Claude API
```

## Pemakaian

```bash
cd folder-proyek
maba                                         # sesi interaktif
maba -p "jelaskan arsitektur proyek ini"     # sekali jalan (juga otomatis bila input lewat pipe)
maba -c                                      # lanjutkan sesi terakhir di folder ini
```

| Tombol | Fungsi |
|---|---|
| **Enter** · **Alt+Enter** (atau `\` + Enter) | kirim · baris baru |
| **Esc** / Ctrl+C | batalkan giliran; ketikan Anda saat agen bekerja tidak hilang |
| **Shift+Tab** | ganti mode izin: standar → terima-edit → rencana |
| `@` | lampirkan file/folder (maks. 5; file rahasia tidak pernah muncul) |
| `/` | menu perintah (bawaan, skill, kustom); **Tab** memilih, **Enter** menjalankan |
| `!perintah` | jalankan shell langsung; hasilnya masuk konteks pesan berikutnya |
| ↑/↓ · Ctrl+R | riwayat · cari riwayat |
| Ctrl+D · `/exit` | keluar |

Saat agen bekerja, baris status menampilkan tahap, langkah, dan waktu. Setiap giliran ditutup ringkasan: file yang
diubah, perintah, aksi ditolak, token, konteks, dan langkah berikutnya. Toolbar bawah menampilkan mode, model, meter
konteks, token, todo, dan task latar. Semua tampilan bisa dilihat tanpa memanggil model:
`python -m maba_code.ui.preview` (opsi `--width 60`, `--ascii`, `--svg hasil.svg`).

### Perintah slash

| Kelompok | Perintah |
|---|---|
| Percakapan | `/clear` `/resume [no\|id]` `/compact [fokus]` `/undo` `/todo` `/exit` |
| Model & izin | `/model [alias]` `/effort [level]` `/mode [nama]` `/plan [tugas]` `/permissions` `/tools` |
| Proyek & ekstensi | `/init` `/memory` `/agents` `/skills` `/mcp` `/tasks [stop id]` `/loop [interval] <prompt>` |
| Info | `/status` `/cost` `/config` `/doctor` `/help` |

Perintah kustom: `.maba/commands/nama.md` atau `~/.maba/commands/nama.md` (pakai `$ARGUMENTS`, frontmatter
`description:` opsional). Skill yang bisa dipanggil pengguna juga muncul sebagai `/nama`.

### Model

| Alias | Model ID | Jalur |
|---|---|---|
| `gpt-6-luna` (default, inti) | `global.openai.gpt-6-luna` | Bedrock |
| `api-opus` | `anthropic/claude-opus-5-5` | Claude API |
| `api-sonnet` | `anthropic/claude-sonnet-5-5` | Claude API |
| `api-haiku` | `anthropic/claude-haiku-5-5` | Claude API |
| `nova-2-lite` (juga `nova 2 lite`) | `global.amazon.nova-2-lite-v1:0` | Bedrock |

Model lain bisa dipakai dengan ID lengkap: ID Bedrock (`global.…`; model OpenAI di Bedrock **wajib** lewat inference
profile) atau `anthropic/<model-id>`. `/model` hanya mengganti **agen utama**; agen anak tetap GPT-6 Luna, jadi
profile AWS tetap diperlukan walau memakai Claude API.

**Catatan jalur Claude API:**

- **Effort** (`low` · `medium` · `high` · `xhigh` · `max`, default `medium`) mengatur kedalaman berpikir, biaya, dan
  kecepatan: `/effort high`, `--effort high`, atau `"effort"` di settings. Menggantinya memulai ulang cache prompt
  sekali; riwayat tidak berubah.
- Batas keluaran default 64.000 token karena token pikiran ikut dihitung (Bedrock: 8.192). Ubah lewat `maxTokens`.
- Otomatis aktif: prompt caching; `drop_block` agar blok thinking lama tidak memicu error 400 setelah kompaksi; dan
  fallback sisi server bila pengaman Anthropic menolak permintaan (Opus/Sonnet, bukan Haiku).
- Biaya ditagih ke akun Anthropic pemilik API key, terpisah dari tagihan AWS.

## Izin & keamanan

| Kelas | Tool | Perilaku |
|---|---|---|
| Baca | `read_file` `glob_files` `grep_files` `todo_write` `explore` `task` `skill` `task_stop` | otomatis (di luar folder kerja: ditanya) |
| Tulis | `write_file` `edit_file` | ditanya dengan diff; Enter = Ya |
| Berbahaya | `run_command` | ditanya (perintah baca-saja seperti `git status` otomatis); harus `y` |
| Internet | `web_fetch` | ditanya; "jangan tanya lagi" = `web_fetch(domain:host)`; harus `y` |
| MCP | `mcp__<server>__<tool>` | selalu ditanya; harus `y` |

Tool yang dipakai sub-agen melewati kebijakan yang sama (dialog berlabel `[sub-agen nama]`). Mode: **default** ·
**acceptEdits** (edit di folder kerja otomatis) · **plan** (hanya baca, lalu mengajukan rencana) · **bypass** (semua
otomatis; hanya untuk lingkungan tepercaya). Aturan dievaluasi **deny → ask → allow**; allow tidak bisa membatalkan
deny. Contoh `~/.maba/settings.json`:

```json
{
  "profile": "agung",
  "model": "gpt-6-luna",
  "effort": "medium",
  "contextWindow": 256000,
  "permissions": {
    "allow": ["run_command(npm test*)", "web_fetch(domain:*.python.org)", "mcp__github__*"],
    "ask":   ["run_command(git push*)"],
    "deny":  ["run_command(rm *)", "run_command(Remove-Item*)"]
  },
  "autoMemory": true
}
```

Pagar tambahan:

- **Rahasia diblokir** (juga di mode bypass): `.env*`, `*.pem`, `*.key`, `~/.aws`, `~/.ssh`, serta perintah yang
  menyentuhnya. Proses `run_command` tidak mewarisi env kredensial (AWS, `ANTHROPIC_API_KEY`, token Git, dsb.).
- **Settings proyek tidak dipercaya:** `.maba/settings*.json` di repo hanya boleh mengatur `model`, `effort`, batas
  (`contextWindow`, `maxTokens`, `maxTurns`) dan menambah aturan `deny`/`ask`. Kunci lain diabaikan dengan peringatan.
- Aturan allow berpola tidak berlaku untuk perintah majemuk (`;` `&&` `|` `>` …). Edit wajib didahului `read_file`
  dan ditolak bila file berubah di disk setelah dibaca.

> Pagar berbasis pola ini *best-effort*, bukan sandbox. Perlindungan sebenarnya adalah izin IAM minimal pada profile
> Anda dan mode default untuk proyek yang tidak dikenal.

## Ekstensi

**Memori proyek.** `MABA.md` dimuat otomatis dari `~/.maba/MABA.md`, lalu `MABA.md`/`.maba/MABA.md` dari akar sampai
folder kerja, lalu `MABA.local.md` (mendukung `@impor/file.md`); buat dengan `/init`. Agen juga menulis memori
otomatis di `~/.maba/projects/<proyek>/memory/` (lihat dengan `/memory`, matikan dengan `"autoMemory": false`).

**Sub-agen.** Tool `task` menjalankan sub-agen GPT-6 Luna di konteks baru; beberapa `task` berjalan paralel. Bawaan:
`explore` (read-only), `plan` (read-only, mengembalikan rencana), `general-purpose` (semua tool). Sub-agen sendiri di
`~/.maba/agents/*.md` atau `.maba/agents/*.md`:

```markdown
---
name: reviewer
description: Meninjau diff untuk bug & keamanan. Gunakan setelah perubahan kode.
tools: read_file, grep_files, glob_files     # opsional; nama Claude Code (Read, Grep, Bash…) juga diterima
maxTurns: 20                                 # opsional (default 25)
---
Kamu peninjau kode yang teliti…
```

**Skills.** `~/.maba/skills/<nama>/SKILL.md` atau `.maba/skills/<nama>/SKILL.md`. Hanya `nama: deskripsi` yang masuk
system prompt; isi lengkap dimuat saat model memanggil tool `skill` atau pengguna mengetik `/nama`. Frontmatter:
`description`, `allowed-tools` (skill pengguna saja), `disable-model-invocation`, `user-invocable`, `context: fork`,
`agent`. Variabel `${MABA_SKILL_DIR}` menunjuk folder skill.

**Hooks** (format Claude Code, **hanya** dari `~/.maba/settings.json`):

```json
"hooks": {
  "PreToolUse": [{"matcher": "run_command|Bash", "hooks": [{"type": "command", "command": "python C:/hooks/cek.py"}]}],
  "Stop":       [{"hooks": [{"type": "command", "command": "python C:/hooks/wajib-tes.py"}]}]
}
```

Event: `PreToolUse` (exit 2 atau `permissionDecision: deny` memblokir; `allow` melewati dialog *ask*; `updatedInput`
mengganti argumen) · `PostToolUse` · `UserPromptSubmit` · `Stop`/`SubagentStop` (exit 2 memaksa lanjut, maks. 8×) ·
`SessionStart` · `SessionEnd` · `PreCompact`. Exit 0 = sukses (stdout JSON diproses), 2 = blokir (stderr = alasan).
Matcher: kosong/`*` = semua, `A|B` = nama persis, selain itu regex. Timeout default 600 s.

**MCP.** `~/.maba/mcp.json` (dipercaya) atau `.mcp.json` di proyek (perlu persetujuan sekali per server):

```json
{"mcpServers": {
  "github": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"], "env": {"GITHUB_TOKEN": "${GITHUB_TOKEN}"}},
  "docs":   {"type": "http", "url": "https://example.com/mcp", "headers": {"Authorization": "Bearer ${DOCS_TOKEN}"}}
}}
```

Transport stdio, `http`, atau `sse`; tool muncul sebagai `mcp__<server>__<tool>` dan selalu ditanya. Konfigurasi
proyek yang berubah ditanya ulang; `${VAR}` bernama mirip rahasia tidak diekspansi untuk `.mcp.json` proyek. Status: `/mcp`.

**Task latar belakang & `/loop`.** `run_command(..., run_in_background=true)` mengembalikan id `bg-xxxxxx` (log di
`~/.maba/tasks/<proyek>/`; maks. 8 berjalan; dihentikan dengan `/tasks stop <id>`). `/loop 5m <prompt>` mengulang
tiap interval (default 10m, min. 10s, maks. 20 iterasi).

**`web_fetch`.** Halaman diubah ke Markdown lalu dijawab sub-agen Luna tanpa tool. URL http dinaikkan ke https; redirect ke
host lain tidak diikuti; host lokal/privat dan URL berkredensial ditolak; cache 15 menit; maks. 5 MB.

## Sesi & opsi CLI

Percakapan tersimpan di `~/.maba/sessions/<proyek>/` (lanjutkan dengan `-c`, `--resume ID`, atau `/resume`). Konteks
diringkas otomatis di ≈75% window atau manual dengan `/compact`. `/undo` mengembalikan file yang diubah agen pada
giliran terakhir (perubahan lewat `run_command` tidak dilacak).

Opsi: `-p` · `--profile` · `--model` · `--effort` · `--region` · `--mode` · `--max-turns` · `-c` · `--resume ID` ·
`--cwd` · `--doctor` · `--version`. Env: `MABA_PROFILE`, `MABA_MODEL`, `MABA_REGION`, `MABA_MODE`, `MABA_HOME`,
`MABA_ASCII=1`, `MABA_DEBUG=1` (log ke `~/.maba/debug.log`).

## Pengujian

```bash
python -m unittest discover -s tests -t .      # 211 uji, offline, ±2 menit
```

Folder `tests/` tidak ikut ter-commit (lihat `.gitignore`). Suite memakai model skrip yang mengalirkan event
sungguhan, jadi loop Strands, hook izin, tool, UI, dan prompt_toolkit benar-benar berjalan tanpa jaringan. Jalur
Claude API diuji dengan `httpx.MockTransport`. Uji nyata: `--doctor` dan `-p`.

## Riwayat versi

Versi dijaga di `maba_code/__init__.py` dan `pyproject.toml` (`VersionTests`). Setelah menaikkan versi, jalankan
ulang `pip install -e .`.

| Versi | Isi |
|---|---|
| **0.4.0** | **Claude API langsung:** model `anthropic/…` (alias `api-opus`/`api-sonnet`/`api-haiku`) memakai `ANTHROPIC_API_KEY`, dengan `/effort`/`--effort`, batas keluaran 64K, prompt caching, `drop_block`, dan fallback sisi server. Model baru `nova-2-lite`. **Perubahan perilaku:** `/model` hanya menawarkan 5 alias; alias lama (`luna`, `sonnet`, `opus`, dll.) dihapus, modelnya tetap bisa dipakai lewat ID lengkap. Dependensi kini `strands-agents[anthropic]`. |
| **0.3.0** | Antarmuka yang menjelaskan diri: baris status, dialog izin berlencana dengan dampak, ringkasan akhir giliran, toolbar adaptif, `/status`, pratinjau UI. **Perubahan perilaku:** Enter tidak lagi menyetujui dialog berbahaya/internet/MCP. |
| 0.2.0 | Hooks, sub-agen dari file, skills, klien MCP, task latar belakang, `/loop`, memori otomatis, `web_fetch`. |
| 0.1.0 | Agen terminal dasar: tool file/shell, izin deny → ask → allow, sesi, kompaksi, mode izin. |

## Batasan yang diketahui

- Tampilan TTY interaktif diuji lewat pipe input dan console rekam, belum diamati visual di terminal sungguhan.
  Pembatalan dengan Esc hanya ada di konsol Windows dan tidak punya uji otomatis.
- macOS belum diuji. Kompaksi otomatis di 75% baru diuji secara tidak langsung.
- Jalur Claude API baru diuji dengan server palsu, ditambah satu panggilan nyata lewat `--doctor`.
- `contextWindow` GPT-6 Luna adalah asumsi 256k. Biaya dalam dolar tidak dihitung (`/cost` menampilkan token).
- Esc menghentikan model di batas chunk dan `run_command`; tool sinkron pihak ketiga tidak bisa diinterupsi.
- Hooks hanya tipe `command`. `/loop` hanya interval tetap. Resource/prompt MCP belum diekspos.
- Task latar belakang tertinggal bila proses MABA dibunuh paksa. Memori otomatis bisa berisi catatan keliru
  (mis. dari prompt injection): periksa `/memory`.
- Belum ada: WebSearch, membaca gambar/PDF, antrean pesan, checkpoint persisten antar-proses.

## Struktur

```
maba_code/
  cli.py  repl.py  session.py  commands.py                    # titik masuk, REPL, orkestrasi agen, perintah slash
  config.py  models.py  anthropic_api.py                      # settings + alias model, Bedrock, Claude API
  permissions.py  tools.py  fsops.py  shell.py  workspace.py  # izin, tool, utilitas file, shell
  memory.py  prompts.py  mentions.py                          # MABA.md, system prompt, @file
  hooks.py  agents.py  skills.py  frontmatter.py              # hooks pengguna, sub-agen, skills
  tasks.py  web.py  mcp_support.py                            # task latar belakang, web_fetch, klien MCP
  ui/  terminal.py  prompt.py  markdown_stream.py  keys.py  theme.py  activity.py  preview.py
maba.cmd   MABA.md   pyproject.toml
```
