# MABA Code — catatan untuk agen

Asisten coding agentik di terminal: Strands Agents + Amazon Bedrock, mesin inti GPT-6 Luna. Python ≥ 3.10,
dikembangkan di Windows.

## Perintah
- Tes (offline, tanpa jaringan): `python -m unittest discover -s tests -t .`
- Jalankan: `python -m maba_code` atau `maba.cmd` · diagnosis: `python -m maba_code --doctor`
- Tes nyata ke Bedrock hanya lewat `--doctor` atau `-p "..."` (memakai profile `agung`, biaya kecil).

## Versi & catatan rilis
- Versi sekarang **0.3.0**. Naikkan di `maba_code/__init__.py` **dan** `pyproject.toml` (dijaga `VersionTests`), lalu catat
  di tabel "Riwayat versi" pada README. Semua tampilan versi (banner, `--version`, `--doctor`, User-Agent) membaca
  `__version__`; `maba_code.egg-info/` hasil `pip install -e .` bisa usang, abaikan atau pasang ulang.
- Pra-1.0: fitur/perubahan perilaku = naik minor, perbaikan = naik patch. `docs/REA-LAPORAN.md` adalah bukti historis v0.2:
  jangan diubah untuk rilis baru.

## Arsitektur (maba_code/)
- `session.py` ChatApp: merakit Agent Strands, giliran (`run_prompt` → `_stream_once` + hook Stop), sesi, kompaksi,
  sub-agen (`run_subagent`), skill, `web_fetch`, MCP (`start`/`shutdown`), `/loop` (`run_loop`).
- `permissions.py` kebijakan deny→ask→allow + `PermissionHook` (PreToolUse/PostToolUse hook ada di sini juga).
- `tools.py` semua tool (async, UTF-8) + `tool_class()`; `fsops.py` utilitas file murni; `shell.py` PowerShell/bash.
- `hooks.py` hook shell pengguna; `agents.py` sub-agen; `skills.py` SKILL.md; `frontmatter.py` parser YAML.
- `tasks.py` task latar belakang; `web.py` fetch + pagar SSRF; `mcp_support.py` klien MCP + trust store.
- `ui/` rich + prompt_toolkit (`activity.py` status kerja agen & ringkasan giliran, `theme.py` simbol/label/level,
  `preview.py` pratinjau tanpa model); `commands.py` slash command; `config.py` settings berlapis + `CORE_MODEL`.

## Aturan penting
- Jangan pernah mencetak/menyimpan kredensial AWS; profile dibaca boto3 saja.
- **Mesin inti GPT-6 Luna (`CORE_MODEL = global.openai.gpt-6-luna`) tidak boleh diganti.** Setiap fitur ber-model
  selain agen utama WAJIB dibuat lewat `ChatApp._sub_agent()` (memakai `core_model`) atau `self.summarizer`; jangan
  pernah membuat `Agent(model=...)` sendiri atau memakai `self.model` untuk agen anak. `LunaPinTests` menjaganya.
- ID model tanpa `global.` ditolak Bedrock.
- Nama class style prompt_toolkit harus huruf kecil. Strands meneruskan `list[BaseModel]` sebagai dict.
- Parameter tool bernama `agent` dicadangkan Strands (diisi instance Agent) — jangan dipakai.
- Konfigurasi yang menjalankan perintah (hooks, mcpServers) hanya dari `~/.maba/`; file proyek tidak dipercaya.
- Jangan subclass `SummarizingConversationManager`: nama kelasnya disimpan di sesi dan dicek saat resume.
- Tool baru: tambahkan ke kelas izin di `tools.py` (`tool_class`) dan tulis uji di `tests/`
  (fitur v0.2 di `tests/test_extensions.py`; `Harness(core_turns=...)` memisahkan skrip model Luna).
- UI: teks, simbol, label, dan ambang tampilan diletakkan di `ui/theme.py`/`config.py` (mis. `AUTO_COMPACT_PCT`), bukan
  ditulis ulang di pemanggil. Pesan lewat `ui.notice(teks, gaya)`: gaya warna otomatis jadi ikon level (`ui/theme.NOTICE_LEVELS`).
  Perintah bawaan baru wajib didaftarkan di `commands.HELP_GROUPS` (dijaga tes). Jangan mencetak dari handler tombol
  prompt_toolkit (merusak prompt): pakai keterangan sesaat di toolbar (`ChatApp._flash`). Setelah mengubah tampilan,
  jalankan `python -m maba_code.ui.preview` (juga `--width 60 --ascii`) dan `tests/test_ui_ux.py`.
- Dialog izin: kelas ketat (berbahaya/internet/MCP/tak dikenal, atau `danger=True`) tidak boleh menyetujui lewat Enter.

## Keputusan desain v0.3 (non-obvious, jangan dibalik tanpa alasan)
- Enter tidak menyetujui dialog kelas ketat; edit file tetap Enter = Ya. Dijaga tes lewat prompt_toolkit sungguhan.
- Tahap "menunggu izin" bukan fase spinner (dialog menggantikan spinner); waktu tunggunya dicatat di `Activity.waited`
  dan dipisah dari waktu kerja di ringkasan giliran.
- Kutipan di dialog ("Agen menjelaskan:") adalah kalimat terakhir teks agen sebelum memanggil tool, best-effort, bukan
  klaim tentang tujuan agen. Disembunyikan bila kosong.
- Baris status memakai detik utuh (desimal berdenyut terasa gelisah); ringkasan akhir memakai desimal.
- Notice "Dihentikan"/"Batas giliran" tidak dicetak terpisah: menjadi judul ringkasan akhir (`turn_footer(outcome=...)`).
- Jangan menambah panggilan model untuk keperluan UI: `test_extensions` mengunci jumlah panggilan GPT-6 Luna.

## Belum terverifikasi (cek dulu di terminal nyata sebelum mengklaim beres)
- Tampilan TTY interaktif (placeholder, toolbar adaptif, keterangan sesaat Shift+Tab, spinner bersama `EscWatcher`/type-ahead
  di konsol Windows) hanya diuji lewat pipe input, console rekam, dan `ui.preview`.
- Kompaksi otomatis proaktif di 75% (hanya `reduce_context` yang diuji langsung) dan mode `-p` terhadap Bedrock.
