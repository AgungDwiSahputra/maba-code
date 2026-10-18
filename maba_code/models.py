"""Pembuatan model Bedrock lewat profile AWS lokal + penerjemah error yang ramah.

Kredensial TIDAK pernah dibaca/ditulis oleh kode ini: boto3 memuatnya sendiri dari profile
(``~/.aws/credentials``), sehingga tidak pernah muncul di kode, log, atau layar.
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
from strands.models import BedrockModel

from maba_code.config import Settings


def make_session(settings: Settings) -> boto3.Session:
    kwargs = {"profile_name": settings.profile}
    if settings.region:
        kwargs["region_name"] = settings.region
    return boto3.Session(**kwargs)


def build_model(settings: Settings, session: boto3.Session | None = None) -> BedrockModel:
    session = session or make_session(settings)
    # Catatan: boto_session dan region_name tidak boleh diberikan bersamaan ke BedrockModel;
    # region sudah ditangani di make_session().
    return BedrockModel(
        boto_session=session,
        model_id=settings.model,
        max_tokens=settings.max_tokens,
        streaming=True,
        context_window_limit=settings.context_window,
    )


def friendly_error(exc: BaseException, settings: Settings) -> tuple[str, str]:
    """Kembalikan (judul, saran) berbahasa Indonesia untuk error model/AWS yang umum."""
    name = type(exc).__name__
    text = str(exc)
    cause = exc.__cause__ or exc.__context__
    probe = " ".join(filter(None, [text, str(cause) if cause else ""]))

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
        return ("Bedrock sedang membatasi laju (throttling)", "Tunggu sebentar lalu ulangi; SDK sudah mencoba ulang otomatis.")
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
