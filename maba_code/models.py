"""Pembuatan model (Bedrock lewat profile AWS, atau Claude API langsung) + penerjemah error yang ramah.

Kredensial TIDAK pernah dibaca/ditulis oleh kode ini: boto3 memuatnya sendiri dari profile
(``~/.aws/credentials``) dan SDK ``anthropic`` dari env ``ANTHROPIC_API_KEY``, sehingga tidak pernah muncul
di kode, log, atau layar.
"""

from __future__ import annotations

import boto3
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    NoCredentialsError,
    ProfileNotFound,
    TokenRetrievalError,
)
from strands.models import BedrockModel, Model

from maba_code.config import Settings, anthropic_model_id, is_anthropic_api, provider_label

ANTHROPIC_INSTALL_HINT = 'Pasang dengan `pip install "strands-agents[anthropic]"` (atau `pip install -e .` di folder MABA Code).'
ANTHROPIC_KEY_HINT = (
    "Setel env ANTHROPIC_API_KEY dengan API key dari console.anthropic.com, lalu buka ulang terminal "
    "(PowerShell: `setx ANTHROPIC_API_KEY \"sk-ant-...\"`). Jangan simpan kunci di file proyek."
)


def make_session(settings: Settings) -> boto3.Session:
    kwargs = {"profile_name": settings.profile}
    if settings.region:
        kwargs["region_name"] = settings.region
    return boto3.Session(**kwargs)


def build_model(settings: Settings, session: boto3.Session | None = None) -> Model:
    if is_anthropic_api(settings.model):
        # Impor malas: paket `anthropic` hanya diperlukan bila jalur Claude API dipakai.
        from maba_code.anthropic_api import build_anthropic_model

        return build_anthropic_model(settings)
    session = session or make_session(settings)
    # Catatan: boto_session dan region_name tidak boleh diberikan bersamaan ke BedrockModel;
    # region sudah ditangani di make_session().
    return BedrockModel(
        boto_session=session,
        model_id=settings.model,
        max_tokens=settings.output_tokens(),
        streaming=True,
        context_window_limit=settings.context_window,
    )


def _anthropic_error(exc: BaseException, settings: Settings, probe: str) -> tuple[str, str] | None:
    """Error khas jalur Claude API. Dicocokkan lewat nama kelas agar tidak perlu mengimpor `anthropic`."""
    if isinstance(exc, ModuleNotFoundError) and exc.name == "anthropic":
        return ("Paket `anthropic` belum terpasang", ANTHROPIC_INSTALL_HINT)
    if "Could not resolve authentication method" in probe:
        return ("API key Anthropic belum diset", ANTHROPIC_KEY_HINT)
    if not type(exc).__module__.startswith("anthropic"):
        return None
    name = type(exc).__name__
    model_id = anthropic_model_id(settings.model)
    status = getattr(exc, "status_code", None)
    if name == "AuthenticationError":
        return ("API key Anthropic ditolak (tidak valid atau sudah dicabut)", ANTHROPIC_KEY_HINT)
    if name == "PermissionDeniedError":
        return ("API key Anthropic tidak punya akses ke model ini", f"Periksa izin workspace untuk '{model_id}' di console.anthropic.com.")
    if name == "RateLimitError":
        return ("Claude API membatasi laju (rate limit)", "Tunggu sebentar lalu ulangi, atau cek batas tier di console.anthropic.com.")
    if name == "NotFoundError":
        return ("Model tidak ditemukan di Claude API", f"Periksa ID '{model_id}' (lihat `/model` untuk alias `api-…`).")
    if "data retention" in probe:
        return ("Model ini mensyaratkan retensi data 30 hari", "Aktifkan retensi data di organisasi/workspace Anthropic Anda, atau pilih model lain.")
    if name == "BadRequestError":
        return ("Permintaan ditolak Claude API (400)", str(exc)[:300])
    if name in ("APIConnectionError", "APITimeoutError"):
        return ("Tidak bisa terhubung ke Claude API", "Periksa koneksi internet/proxy, lalu ulangi.")
    if status and status >= 500:
        return ("Claude API sedang bermasalah atau kelebihan beban", "Tunggu sebentar lalu ulangi; SDK sudah mencoba ulang otomatis.")
    return (f"Error Claude API ({name})", str(exc)[:300])


def friendly_error(exc: BaseException, settings: Settings) -> tuple[str, str]:
    """Kembalikan (judul, saran) berbahasa Indonesia untuk error model/AWS/Anthropic yang umum."""
    name = type(exc).__name__
    text = str(exc)
    cause = exc.__cause__ or exc.__context__
    probe = " ".join(filter(None, [text, str(cause) if cause else ""]))

    found = _anthropic_error(exc, settings, probe)
    # Exception Strands (konteks penuh, throttling, token habis) membungkus error SDK: pesan Strands-nya yang dipakai.
    if found is None and cause is not None and not type(exc).__module__.startswith("strands"):
        found = _anthropic_error(cause, settings, probe)
    if found:
        return found

    if isinstance(exc, ProfileNotFound) or "could not be found" in probe and "profile" in probe.lower():
        return (
            f"Profile AWS '{settings.profile}' tidak ditemukan",
            "Cek `aws configure list-profiles`, atau pilih profile lain dengan `--profile NAMA`.",
        )
    if isinstance(exc, (NoCredentialsError, TokenRetrievalError)) or "ExpiredToken" in probe or "expired" in probe.lower():
        return (
            "Kredensial AWS kosong atau kedaluwarsa",
            f"Jika memakai SSO: `aws sso login --profile {settings.profile}`; jika tidak, perbarui kredensial profile tersebut.",
        )
    if "AccessDenied" in probe or "not authorized" in probe:
        return (
            "Akses ke model ditolak oleh IAM/Bedrock",
            f"Pastikan profile '{settings.profile}' punya izin bedrock:InvokeModel* untuk '{settings.model}' dan model sudah diaktifkan.",
        )
    if "on-demand throughput" in probe or "inference profile" in probe:
        return (
            "Model ini wajib memakai inference profile",
            "Gunakan ID berawalan `global.` atau `apac.`, mis. `global.openai.gpt-6-luna`.",
        )
    if "ValidationException" in probe or "ResourceNotFoundException" in probe:
        return (
            "Model tidak valid atau tidak tersedia di region ini",
            f"Periksa ID model '{settings.model}' (lihat `/model` untuk alias) dan region profile Anda.",
        )
    if "Throttling" in probe or name == "ModelThrottledException":
        return (
            f"{provider_label(settings.model)} sedang membatasi laju (throttling)",
            "Tunggu sebentar lalu ulangi; SDK sudah mencoba ulang otomatis.",
        )
    if name == "ContextWindowOverflowException":
        return ("Konteks percakapan terlalu besar", "Jalankan `/compact` atau mulai sesi baru dengan `/clear`.")
    if name == "MaxTokensReachedException":
        return (
            "Jawaban model terpotong (batas token keluaran tercapai)",
            "Minta model memecah pekerjaan menjadi langkah lebih kecil, atau naikkan `maxTokens` di settings.",
        )
    if isinstance(exc, (ClientError, BotoCoreError)):
        return (f"Error AWS ({name})", text[:300])
    return (f"Terjadi kesalahan ({name})", text[:300])
