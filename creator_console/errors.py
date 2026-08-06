"""Structured, closed-catalog API errors.

WHY THIS EXISTS
---------------
The console used to build error responses from exception text
(`f"{type(exc).__name__}: {exc}"`) and scrub it on the way out. Scrubbing is a
good last line of defence, but it is the WRONG primary control: it can only
remove what it recognises. A secret that never went through `SecretResolver` --
an operator's env var read by a third-party SDK, a token embedded in a provider's
own error body -- is not in the redaction registry and does not have to look like
`AIza…`, so it could ride an exception message straight to the browser.

So the contract is inverted:

    the client receives a CODE from a closed catalog and a STATIC message.
    raw exception text never leaves the process.

Each response carries a `debug_id` that correlates to a server-side log line, so
an operator can still diagnose the failure without the browser ever seeing the
detail. `verify_faults.py` proves that an exception carrying an unregistered
secret cannot reach an HTTP body.
"""
from __future__ import annotations

import secrets as _secrets
from dataclasses import dataclass


@dataclass(frozen=True)
class ErrorSpec:
    code: str
    http_status: int
    message: str
    hint: str


def _spec(code, status, message, hint):
    return ErrorSpec(code=code, http_status=status, message=message, hint=hint)


#: The ONLY messages the API may return. Static strings: nothing here is built
#: from an exception, a path, a provider payload or user input.
CATALOG = {
    "invalid_identifier": _spec(
        "invalid_identifier", 400,
        "Gecersiz kimlik.",
        "Kimlikler yalnizca harf, rakam, '-' ve '_' icerebilir; yol ayraci ve '..' kabul edilmez."),
    "invalid_json": _spec(
        "invalid_json", 400,
        "Istek govdesi gecerli bir JSON nesnesi degil.",
        "Content-Type: application/json ve gecerli bir JSON nesnesi gonder."),
    "missing_required_field": _spec(
        "missing_required_field", 400,
        "Zorunlu alan(lar) bos.",
        "Yildiz (*) ile isaretli alanlari doldurup tekrar dene."),
    "mode_required": _spec(
        "mode_required", 400,
        "Isleme modu secilmedi.",
        "Faz 0 onerisi bir secim degildir; 'direct_clip_series' veya 'narrated_theory' sec."),
    "empty_upload": _spec(
        "empty_upload", 400,
        "Yuklenecek dosya govdesi bos.",
        "Bir video dosyasi sec ve tekrar dene."),
    "validation_failed": _spec(
        "validation_failed", 400,
        "Istek bu asamada uygulanamaz.",
        "Onceki adimlarin tamamlandigindan ve girdilerin gecerli oldugundan emin ol."),
    "approval_rejected": _spec(
        "approval_rejected", 400,
        "Onay reddedildi: verilen SHA-256 artefaktin su anki icerigiyle eslesmiyor.",
        "Inceleme panelini yenile, gosterilen guncel hash i kopyala ve tekrar dene."),
    "phase_blocked": _spec(
        "phase_blocked", 400,
        "Bu faz onaylanmis isleme plani tarafindan calistirilmiyor.",
        "Faz 0 planini gozden gecir; secilen mod bu fazi kapatiyor olabilir."),
    "storage_violation": _spec(
        "storage_violation", 403,
        "Istenen yol bu projenin depolama alani disinda.",
        "Yalnizca proje icindeki goreli yollar kullanilabilir."),
    "media_type_blocked": _spec(
        "media_type_blocked", 403,
        "Bu dosya turu sunulmuyor.",
        "Yalnizca uretilen medya ve manifest dosyalari indirilebilir."),
    "static_path_blocked": _spec(
        "static_path_blocked", 403,
        "Statik dosya yolu reddedildi.", ""),
    "not_found": _spec(
        "not_found", 404,
        "Kayit bulunamadi.",
        "Kimligi kontrol et; bu calisma alaninda boyle bir kayit yok."),
    "job_not_found": _spec(
        "job_not_found", 404,
        "Is bulunamadi.", ""),
    "media_not_found": _spec(
        "media_not_found", 404,
        "Medya dosyasi bulunamadi.",
        "Manifest bir dosyaya isaret ediyor olabilir ama dosya diskte yok; ilgili fazi yeniden calistir."),
    "static_not_found": _spec(
        "static_not_found", 404,
        "Statik dosya yok.", ""),
    "unknown_route": _spec(
        "unknown_route", 404,
        "Bilinmeyen uc nokta.", ""),
    "job_conflict": _spec(
        "job_conflict", 409,
        "Isin mevcut durumu bu islemi kabul etmiyor.",
        "Is listesini yenile; is calisiyor veya zaten tamamlanmis olabilir."),
    "upload_too_large": _spec(
        "upload_too_large", 413,
        "Dosya boyutu sinirini asiyor.", ""),
    "unsupported_video_type": _spec(
        "unsupported_video_type", 415,
        "Bu kaynak video uzantisi desteklenmiyor.",
        "MP4, MOV, MKV, WebM, AVI veya M4V biciminde bir video sec."),
    "artifact_problem": _spec(
        "artifact_problem", 422,
        "Artefakt bozuk, eksik veya bu surumle uyumsuz.",
        "Ilgili fazi yeniden calistir; bozuk artefakt asla kabul edilmez."),
    "media_empty": _spec(
        "media_empty", 422,
        "Medya dosyasi bos (yarim veya basarisiz uretim).",
        "Ilgili fazi yeniden calistir; bu dosya onaylanamaz."),
    "storage_unavailable": _spec(
        "storage_unavailable", 507,
        "Depolama su anda yazilamiyor.",
        "Disk alanini ve dosya izinlerini kontrol et; is guvenle yeniden calistirilabilir."),
    "internal_error": _spec(
        "internal_error", 500,
        "Beklenmeyen bir sunucu hatasi olustu.",
        "Istek guvenle tekrarlanabilir. Ayrinti sunucu gunlugunde debug_id ile kayitli."),
}

#: Codes whose HTTP status implies the caller may safely retry the same request.
RETRYABLE_CODES = frozenset({"internal_error", "storage_unavailable", "job_conflict"})


#: Field names the API may name in a response, with the label the form uses.
#: A CLOSED set, like the message catalog: the response can point at a field, but
#: it can never echo a value or an arbitrary key back to the browser.
FIELD_LABELS = {
    "title": "Proje adi",
    "operator_name": "Proje ureticisi",
    "selected_by": "Secimi yapan kisi",
    "theory_angle": "Teori / yorum acisi",
    "output_count": "Cikti sayisi",
    "target_duration_sec": "Hedef sure (sn)",
    "reviewed_by": "Inceleyen kisi",
    "approved_by": "Onaylayan",
    "reviewed_sha256": "Dogrulama hash i",
    "mode": "Isleme modu",
    "note": "Revizyon notu",
    "updated_by": "Duzenleyen kisi",
    "voice": "Ses",
    "language_code": "Dil kodu",
    "confirmation": "Proje adi dogrulamasi",
    "script_text": "Script metni",
    "kind": "Secret turu",
    "secret_value": "GitHub token",
}


class ApiError(Exception):
    """An error with a catalog code. `detail` NEVER reaches the client."""

    def __init__(self, code: str, *, detail: str = "", context: dict | None = None,
                 fields=None):
        spec = CATALOG.get(code) or CATALOG["internal_error"]
        super().__init__(spec.message)
        self.spec = spec
        self.code = spec.code
        self.status = spec.http_status
        # Server-side only. Kept for the log line, never serialized to a response.
        self.detail = str(detail or "")
        self.context = dict(context or {})
        # Filtered against the closed set, so an unexpected key cannot ride out.
        self.fields = [name for name in (fields or []) if name in FIELD_LABELS]
        self.debug_id = f"dbg_{_secrets.token_hex(6)}"

    def to_payload(self) -> dict:
        """The response body. Built ONLY from the static catalog entry."""
        payload = {
            "code": self.code,
            "message": self.spec.message,
            "hint": self.spec.hint,
            "retryable": self.code in RETRYABLE_CODES,
            "debug_id": self.debug_id,
        }
        if self.fields:
            payload["fields"] = [
                {"name": name, "label": FIELD_LABELS[name]} for name in self.fields]
        return {"error": payload}

    def log_line(self) -> str:
        """Scrubbed server-side detail, correlated by debug_id."""
        from .redaction import scrub_text

        parts = [f"[{self.debug_id}] {self.code}"]
        if self.detail:
            parts.append(scrub_text(self.detail, max_len=1200))
        if self.context:
            parts.append(scrub_text(str(self.context), max_len=400))
        return " | ".join(parts)


def payload_for(code: str, *, detail: str = "") -> tuple:
    error = ApiError(code, detail=detail)
    return error.status, error.to_payload()
