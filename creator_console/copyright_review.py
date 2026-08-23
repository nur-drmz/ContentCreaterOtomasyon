"""Copyright and transformative-use half of the human review checklist.

The RULES (digest binding, staleness, revision records) live in
`media_review.py`, shared with render and voice review. Only the questions, the
artifact path and the fix tabs are copyright-specific, and only those are here.

WHY THIS EXISTS
---------------
Everything else in this pipeline has a rights story except the one input the
whole product is built on. Music carries a license status, stock clips carry a
provider and an attribution -- but the SOURCE video, the copyrighted film or
episode the Shorts are cut from, had no check at all. The operator could take a
whole series, cut it, burn captions on it and reach the upload gate without ever
being asked whether they had any basis for it.

    results/copyright_review.json

This is not legal advice and it does not decide anything. It asks the operator
the questions a human has to answer for themselves, records the answers as an
artifact, and refuses to let Phase 0 close until they have been answered.

WHERE THE GATE SITS, AND WHY THERE
----------------------------------
On the PROCESSING PLAN approval -- the earliest gate in the pipeline, before a
single frame is analysed or rendered. Asking later would mean spending minutes
of local compute, and possibly provider quota, on material the operator was
always going to decide they could not use. The cheapest moment to stop is
before the work starts.

BOUND TO THE SOURCE, LIKE EVERY OTHER APPROVAL
----------------------------------------------
Each entry records the source video's fingerprint. Swapping the source resets
the checklist, because an answer about one film says nothing about another. That
is the same rule `approval.check_approval` and the render checklist apply.

WHAT THIS MODULE MUST NOT DO
----------------------------
It cannot approve anything, it never inspects the video, and it never claims a
work is or is not fair use. It records what a human asserted.
"""
from __future__ import annotations

from . import media_review
from .media_review import (  # re-exported: callers use these unqualified
    VERDICT_NEEDS_REVISION,
    VERDICT_PASS,
    VERDICT_PENDING,
)

COPYRIGHT_REVIEW_ARTIFACT = "results/copyright_review.json"

#: Deliberately short. A checklist nobody reads protects nobody, and each of
#: these is a question the operator can actually answer about their own project.
CHECKLIST_ITEMS = (
    {"key": "source_rights", "label": "Kaynak uzerindeki hak",
     "question": "Bu kaynak videoyu kullanmak icin bir dayanagin var mi? "
                 "(kendi icerigin, lisansli, kamu malı veya alintilama/adil "
                 "kullanim degerlendirmesi yaptin)",
     "fix_tab": "phase0",
     "fix_hint": "Dayanagin yoksa baska bir kaynak yukle; bu proje icin devam etme."},
    {"key": "transformative_use", "label": "Donusturucu kullanim",
     "question": "Uretilecek Short kaynagi YENIDEN YAYINLAMIYOR; yorum, analiz, "
                 "teori veya eleştiri katiyor mu?",
     "fix_tab": "phase0",
     "fix_hint": "Faz 0'da narrated_theory modunu veya daha guclu bir anlatim "
                 "acisi secerek donusturucu katkiyi artir."},
    {"key": "extent", "label": "Kullanilan miktar",
     "question": "Alinan bolum, anlatmak istedigin sey icin gereken kadar mi? "
                 "Kaynagin tamamini veya doruk noktasini tek basina tasimiyor mu?",
     "fix_tab": "moments",
     "fix_hint": "Moment Explorer'da daha kisa veya farkli bir moment sec."},
    {"key": "attribution", "label": "Kaynak gosterimi",
     "question": "Kaynak eser (yapim adi, sezon/bolum) izleyiciye veya "
                 "aciklamaya yazilacak sekilde belirlendi mi?",
     "fix_tab": "phase0",
     "fix_hint": "Proje notlarina kaynak eserin adini yaz; metadata asamasinda "
                 "aciklamaya tasinir."},
    {"key": "platform_rules", "label": "Platform kurallari",
     "question": "Yayinlanacak platformun yeniden kullanim/telif kurallarini bu "
                 "icerik icin gozden gecirdin mi?",
     "fix_tab": "phase0",
     "fix_hint": "Yayin oncesi platformun telif ve yeniden kullanim politikasini oku."},
)

CHECKLIST_KEYS = tuple(item["key"] for item in CHECKLIST_ITEMS)

SPEC = media_review.ReviewSpec(
    kind="copyright",
    artifact_path=COPYRIGHT_REVIEW_ARTIFACT,
    artifact_type="copyright_review",
    items=CHECKLIST_ITEMS,
    fix_tabs=("phase0", "moments"),
    missing_media_blocker="Once kaynak video yukle; telif kontrolu kaynaga baglanir.",
    revision_blocker=("Bu kaynak icin acik bir 'revizyon gerekli' kaydi var: "
                      "{notes}. Once onu cozun."),
    stale_blocker=("{count} madde BASKA bir kaynak videoya isaretlenmis. Kaynak "
                   "degisti; maddeleri bu kaynak icin tekrar cevapla."),
)


def new_review(ownership, source_sha256: str | None = None) -> dict:
    return media_review.new_review(SPEC, ownership, source_sha256)


def evaluate(review, source_sha256, ownership) -> dict:
    return media_review.evaluate(SPEC, review, source_sha256, ownership)


def set_item(review, key, *, checked, reviewed_by, source_sha256, note: str = ""):
    return media_review.set_item(SPEC, review, key, checked=checked,
                                 media_sha256=source_sha256,
                                 reviewed_by=reviewed_by, note=note)


def request_revision(review, *, source_sha256, notes, requested_by, target_tab):
    return media_review.request_revision(
        SPEC, review, media_sha256=source_sha256, notes=notes,
        requested_by=requested_by, target_tab=target_tab)


def clear_revision(review, *, source_sha256, resolved_by):
    return media_review.clear_revision(SPEC, review, media_sha256=source_sha256,
                                       resolved_by=resolved_by)
