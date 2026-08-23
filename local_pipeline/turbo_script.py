"""Turbo's script: one routed call that returns narration AND its scene beats.

WHY ONE CALL AND NOT ONE PER BEAT
---------------------------------
`long_form_script.py` deliberately splits an 18-minute script across an outline
call plus one call per chapter, because a single call cannot hold that much
coherent structure and a failure halfway would waste everything before it.

Turbo's videos are 15 seconds to 30 minutes but typically under three, and the
narration and the visual beats have to AGREE with each other -- beat 4's search
query must describe what sentence 4 is actually saying. Generating them
separately would mean a second call whose only job is to re-derive alignment
that the first call already knew. So one call returns both, and the parser
refuses anything where they do not line up.

The output feeds three consumers, which is why the shape is fixed here rather
than at each of them: `tts_chunks` speaks `narration`, `stock_media` searches
with `search_query`, and the timeline packer uses the beat order.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone

from .long_form_script import parse_json_object

SCHEMA_VERSION = 1

MIN_BEATS = 2
MAX_BEATS = 60
MAX_QUERY_WORDS = 5


class TurboScriptError(ValueError):
    """A model response that cannot be used as a script, and why."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean(value, limit: int) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def build_script_prompt(brief: dict, *, instructions: str = "") -> str:
    """One prompt, one response, both halves of the script.

    The word budget is stated as a target rather than a hard rule because a
    model that is forced to hit an exact count pads or truncates sentences to
    get there, and the padding is always the worst writing in the script. The
    real duration is measured from the TTS output afterwards.
    """
    duration = int(brief.get("duration_sec") or 60)
    words = int(brief.get("estimates", {}).get("narration_words")
                or round(duration / 60.0 * 150))
    beats = int(brief.get("estimates", {}).get("clip_count")
                or max(MIN_BEATS, round(duration / 5.0)))
    language = str(brief.get("language") or "tr")
    lines = [
        "Bir kisa video icin seslendirme metni ve gorsel sahne plani yaz.",
        "",
        f"Konu: {brief.get('topic')}",
        f"Icerik turu: {brief.get('content_type')}",
        f"Dil: {language}",
        f"Hedef sure: yaklasik {duration} saniye (~{words} kelime)",
        f"Sahne sayisi: {beats}",
        "",
        "Kurallar:",
        "- Anlatim tek parca, akici ve seslendirilmeye uygun olsun.",
        "- Her sahnenin metni, anlatimin o sahneye dusen bolumunun AYNISI olsun;",
        "  sahne metinlerini bastan sona birlestirdiginde anlatimin tamami cikmali.",
        "- Her sahne icin stok video aramasinda kullanilacak bir Ingilizce sorgu ver;",
        f"  sorgu en fazla {MAX_QUERY_WORDS} kelime, somut ve gorsel olsun",
        "  (ornek: 'ancient roman ruins sunset'). Soyut kelime kullanma.",
        "- Marka adi, kisi adi veya izlenmesi mumkun olmayan sahne isteme.",
        "- Uydurma istatistik, tarih veya alinti verme.",
        "",
        "Yalnizca su JSON nesnesini dondur, baska hicbir metin ekleme:",
        "{",
        '  "hook": "ilk cumle, izleyiciyi tutan",',
        '  "narration": "seslendirilecek metnin tamami",',
        '  "scene_beats": [',
        '    {"index": 1, "text": "bu sahnede okunan bolum",',
        '     "search_query": "concrete english query",',
        '     "keywords": ["kelime", "kelime"]}',
        "  ]",
        "}",
    ]
    if instructions:
        lines += ["", "Ek yonerge:", _clean(instructions, 600)]
    return "\n".join(lines)


def parse_script(raw, brief: dict) -> dict:
    """Turn a model response into a usable script, or refuse with a reason.

    Refusing is the point. A half-parsed script would be discovered three
    stages later, after the TTS quota had already been spent on it.
    """
    data = parse_json_object(raw, what="turbo script")

    narration = _clean(data.get("narration"), 60000)
    if len(narration) < 40:
        raise TurboScriptError(
            "model kullanilabilir bir anlatim metni dondurmedi "
            f"({len(narration)} karakter)")

    raw_beats = data.get("scene_beats")
    if not isinstance(raw_beats, list) or len(raw_beats) < MIN_BEATS:
        raise TurboScriptError(
            f"en az {MIN_BEATS} sahne bekleniyordu, "
            f"{len(raw_beats) if isinstance(raw_beats, list) else 0} geldi")
    if len(raw_beats) > MAX_BEATS:
        raise TurboScriptError(
            f"{len(raw_beats)} sahne cok fazla (ust sinir {MAX_BEATS})")

    beats = []
    for position, item in enumerate(raw_beats, start=1):
        if not isinstance(item, dict):
            raise TurboScriptError(f"{position}. sahne bir nesne degil")
        text = _clean(item.get("text"), 2000)
        query = _clean(item.get("search_query"), 120)
        if not text:
            raise TurboScriptError(f"{position}. sahnenin metni bos")
        if not query:
            raise TurboScriptError(f"{position}. sahnenin arama sorgusu bos")
        words = query.split()
        if len(words) > MAX_QUERY_WORDS:
            # Truncated rather than rejected: an over-long query still names the
            # right subject in its first words, and losing the whole script over
            # a style slip would cost far more than trimming it.
            query = " ".join(words[:MAX_QUERY_WORDS])
        keywords = [_clean(word, 40) for word in (item.get("keywords") or [])
                    if _clean(word, 40)][:8]
        beats.append({
            "index": position,
            "slot_id": "beat-{0:03d}".format(position),
            "text": text,
            "search_query": query,
            "keywords": keywords,
        })

    joined = " ".join(beat["text"] for beat in beats)
    coverage = _coverage(narration, joined)
    return {
        "schema_version": SCHEMA_VERSION,
        "artifact_type": "turbo_script",
        "status": "review_required",
        "hook": _clean(data.get("hook"), 400),
        "narration": narration,
        "scene_beats": beats,
        "beat_count": len(beats),
        "word_count": len(narration.split()),
        "brief_sha256": brief.get("brief_sha256", ""),
        "script_sha256": script_sha256(narration, beats),
        #: How much of the narration the beats actually account for. Recorded
        #: rather than enforced: a model that paraphrases slightly still gives
        #: usable search queries, and the operator can see the number and judge.
        "beat_coverage": coverage,
        "coverage_note": (
            "sahne metinleri anlatimi tam karsiliyor" if coverage >= 0.9 else
            "sahne metinleri anlatimdan farkli; arama sorgulari yine de "
            "kullanilabilir ama gorsel-anlatim uyumunu kontrol edin"),
        "not_uploaded": True,
        "created_at": _now(),
    }


def _coverage(narration: str, joined: str) -> float:
    """Fraction of the narration's words that the beats also contain."""
    source = [word.lower() for word in re.findall(r"\w+", narration)]
    if not source:
        return 0.0
    covered = {word.lower() for word in re.findall(r"\w+", joined)}
    return round(sum(1 for word in source if word in covered) / len(source), 3)


def script_sha256(narration: str, beats) -> str:
    """Identity of the script for staleness. Includes the beats, because
    changing a search query changes which footage the plan should fetch even
    when every spoken word stays the same."""
    material = json.dumps(
        {"narration": narration,
         "beats": [{"index": b["index"], "text": b["text"],
                    "search_query": b["search_query"]} for b in beats]},
        sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def as_chapters(script: dict) -> list:
    """The beats in the shape `stock_media.new_plan` already accepts.

    `stock_media` was written for long-form and takes chapters. Rather than
    changing its signature -- which `verify_stock_media.py` and
    `long_form_studio.py` both pin -- Turbo presents its beats in that shape.
    The freshness hash and slot ordering then come for free.
    """
    return [{"index": beat["index"],
             "title": beat["search_query"],
             "text": beat["text"]}
            for beat in (script or {}).get("scene_beats") or []]
