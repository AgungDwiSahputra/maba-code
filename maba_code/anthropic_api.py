"""Jalur Claude API langsung (Anthropic) untuk agen utama.

API key dibaca oleh SDK ``anthropic`` sendiri dari env ``ANTHROPIC_API_KEY`` (atau ``ANTHROPIC_AUTH_TOKEN`` /
profil ``ant auth login``). MABA Code tidak pernah membaca nilainya, mencetaknya, atau menyimpannya ke file;
``shell.py`` juga membuangnya dari env perintah yang dijalankan agen.

Modul ini mengimpor ``anthropic`` di tingkat modul. ``models.build_model`` memuatnya secara malas, sehingga
pengguna yang hanya memakai Bedrock tidak memerlukan paket tersebut.
"""

from __future__ import annotations

import logging
import os
import warnings
from collections.abc import AsyncGenerator
from typing import Any

import anthropic
from strands.models.anthropic import AnthropicModel

from maba_code.config import Settings, anthropic_model_id

log = logging.getLogger("maba")

CREDENTIAL_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")

# MABA Code mengubah riwayat percakapan: kompaksi meringkas giliran lama tetapi menyisakan giliran terbaru, dan
# system prompt dibangun ulang saat mode/model berganti. Perubahan itu membuat blok thinking lama tidak sah lagi.
# Akun Anthropic yang dibuat sejak 31 Agustus 2026 mendapat error 400 untuk blok seperti itu; dengan "drop_block"
# API cukup membuang blok yang tidak sah (model kehilangan penalaran lama itu saja) dan permintaan tetap jalan.
THINKING_BINDING_BETA = "thinking-binding-controls-2026-08-01"
# Bila pengaman Anthropic menolak permintaan (stop_reason "refusal"), API mengulanginya di model cadangan pilihan
# Anthropic untuk kategori penolakan itu, di dalam panggilan yang sama.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
FALLBACK_MODELS = frozenset({"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"})

# Jenis delta yang dipahami parser Strands; jenis lain (mis. dari blok fallback) dilewati alih-alih menggagalkan giliran.
_KNOWN_DELTAS = frozenset({"text_delta", "input_json_delta", "thinking_delta", "signature_delta"})
# Strands memanggil model_dump() pada event SDK; untuk jenis blok yang lebih baru dari SDK, Pydantic mencetak
# peringatan ke stderr yang merusak tampilan terminal. Isinya tidak berguna bagi pengguna, jadi diredam.
warnings.filterwarnings("ignore", message="Pydantic serializer warnings", category=UserWarning, module="pydantic.main")


def has_credentials() -> bool:
    """Apakah env kredensial Anthropic terisi. Nilainya tidak pernah dibaca lebih jauh atau ditampilkan."""
    return any(os.environ.get(name) for name in CREDENTIAL_ENV)


def request_params(model_id: str, effort: str) -> dict[str, Any]:
    """Parameter tambahan untuk setiap permintaan Messages API dari agen utama."""
    betas = [THINKING_BINDING_BETA]
    body: dict[str, Any] = {
        "thinking": {"type": "adaptive", "block_binding": {"prefix_mismatch_behavior": "drop_block"}},
    }
    if model_id in FALLBACK_MODELS:
        betas.append(FALLBACK_BETA)
        body["fallbacks"] = "default"
    return {
        "output_config": {"effort": effort},
        # Cache otomatis: system prompt, daftar tool, dan riwayat dikirim ulang tiap putaran agen; bagian yang
        # sama dibaca dari cache dengan tarif jauh lebih murah.
        "cache_control": {"type": "ephemeral"},
        "extra_headers": {"anthropic-beta": ",".join(betas)},
        "extra_body": body,
    }


def _signed_reasoning(block: dict[str, Any]) -> bool:
    reasoning = block.get("reasoningContent")
    if reasoning is None:
        return True
    return bool((reasoning.get("reasoningText") or {}).get("signature"))


def _filter_blocks(messages: list[dict[str, Any]], keep: Any) -> list[dict[str, Any]]:
    result = []
    for msg in messages:
        content = msg.get("content") or []
        kept = [b for b in content if keep(b)]
        result.append({**msg, "content": kept} if kept and len(kept) != len(content) else msg)
    return result


def _has_reasoning(messages: list[dict[str, Any]]) -> bool:
    return any("reasoningContent" in b for m in messages for b in m.get("content") or [])


class MabaAnthropicModel(AnthropicModel):
    """``AnthropicModel`` Strands dengan tiga pengaman untuk riwayat MABA Code.

    1. Blok penalaran tanpa signature (mis. dari GPT-6 Luna sebelum ``/model`` diganti) tidak dikirim ke Claude API.
    2. Bila API menolak signature thinking (mis. blok dari Claude di Bedrock), permintaan diulang sekali tanpa
       blok thinking. Penolakan seperti itu terjadi sebelum ada keluaran, jadi tidak ada teks ganda.
    3. Jenis delta stream yang belum dikenal Strands dilewati, tidak menghentikan giliran.
    """

    def format_request(self, messages: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return super().format_request(_filter_blocks(messages, _signed_reasoning), *args, **kwargs)

    def format_chunk(self, event: dict[str, Any]) -> Any:
        if event.get("type") == "content_block_delta" and (event.get("delta") or {}).get("type") not in _KNOWN_DELTAS:
            return {}
        return super().format_chunk(event)

    async def stream(self, messages: Any, *args: Any, **kwargs: Any) -> AsyncGenerator[Any, None]:
        produced = False
        try:
            async for chunk in super().stream(messages, *args, **kwargs):
                produced = True
                yield chunk
        except anthropic.BadRequestError as exc:
            if produced or "signature" not in str(exc) or not _has_reasoning(messages):
                raise
            log.debug("signature thinking ditolak Claude API; diulang sekali tanpa blok thinking", exc_info=True)
            stripped = _filter_blocks(messages, lambda b: "reasoningContent" not in b)
            async for chunk in super().stream(stripped, *args, **kwargs):
                yield chunk


def build_anthropic_model(settings: Settings) -> MabaAnthropicModel:
    model_id = anthropic_model_id(settings.model)
    return MabaAnthropicModel(
        model_id=model_id,
        max_tokens=settings.output_tokens(),
        params=request_params(model_id, settings.effort),
        context_window_limit=settings.context_window,
    )


async def ping(model: str) -> str | None:
    """Panggilan kecil untuk ``/doctor`` (biaya kecil). Mengembalikan stop_reason; error SDK diteruskan."""
    async with anthropic.AsyncAnthropic() as client:
        message = await client.messages.create(
            model=anthropic_model_id(model),
            max_tokens=1024,
            output_config={"effort": "low"},
            messages=[{"role": "user", "content": "Balas dengan satu kata: OK"}],
        )
    return message.stop_reason
