"""Outline-first long-form script generation.

WHY NOT ONE BIG CALL
--------------------
Asking a model for 2500 words in one request fails in three ways at once: the
tail drifts off the brief, a single refusal or timeout costs the whole script,
and nothing can be regenerated without regenerating everything. So the script is
built in layers -- research, outline, then ONE call per chapter, then a
consistency pass over the assembled whole.

THE STORY BIBLE
---------------
Per-chapter calls only stay coherent if every call sees the same small set of
decisions. That is the story bible: topic, audience, tone, thesis, approved
facts, glossary, the PREVIOUS chapter's summary and the NEXT chapter's goal. It
is deliberately compact -- pasting the whole script so far into every call would
re-introduce the cost and drift the layering exists to avoid.

SOURCED FACT vs INTERPRETATION
------------------------------
The research pack keeps these in separate lists and every fact keeps whatever
source metadata the grounded route returned. A commentary video is allowed to
speculate; it is not allowed to present speculation with a citation's authority,
and the split is what lets a reviewer see which is which.

NO FABRICATION ON FAILURE
-------------------------
Every builder here returns prompts and parses responses. When a provider fails,
the caller records a failure -- there is no placeholder text, no "draft" filled
in locally, and no silent downgrade to a shorter script.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from . import long_form

RESEARCH_ARTIFACT = "results/long_form/research_pack.json"
OUTLINE_ARTIFACT = "results/long_form/outline.json"
CHAPTERS_ARTIFACT = "results/long_form/chapters.json"
CONSISTENCY_ARTIFACT = "results/long_form/consistency_review.json"

#: Kept small on purpose: this text is repeated in EVERY chapter call, so each
#: extra paragraph is paid for once per chapter.
MAX_BIBLE_FACTS = 12
MAX_BIBLE_TERMS = 20
MAX_SUMMARY_CHARS = 600


class LongFormScriptError(ValueError):
    """A rejected script-stage input or an unusable provider response."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean(text, limit: int) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()[:limit]


def parse_json_object(raw, *, what: str) -> dict:
    """Parse a model response that must be a JSON object.

    Fenced blocks are unwrapped because models add them unprompted. Anything
    still unparseable raises -- returning `{}` here would turn a provider
    failure into an empty-but-successful stage, which is the exact lie this
    workflow must never tell.
    """
    text = str(raw or "").strip()
    if not text:
        raise LongFormScriptError(f"{what}: saglayici bos yanit dondurdu")
    fenced = re.search(r"```(?:json)?\s*(.+?)```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise LongFormScriptError(f"{what}: yanit JSON nesnesi degil")
    try:
        parsed = json.loads(text[start:end + 1])
    except json.JSONDecodeError as exc:
        raise LongFormScriptError(f"{what}: JSON ayristirilamadi ({exc.msg})") from None
    if not isinstance(parsed, dict):
        raise LongFormScriptError(f"{what}: yanit JSON nesnesi degil")
    return parsed


# ------------------------------------------------------------------ research
def build_research_prompt(plan: dict, *, instructions: str = "") -> str:
    return "\n".join([
        "Bir uzun-form YouTube videosu icin ARASTIRMA PAKETI hazirla.",
        f"Konu: {plan.get('topic')}",
        f"Icerik turu: {plan.get('content_type')}",
        f"Hedef kitle: {plan.get('audience')}",
        f"Dil: {plan.get('language')}",
        f"Anlatim acisi: {plan.get('angle') or '(belirtilmedi)'}",
        f"Ek arastirma talimati: {instructions or '(yok)'}",
        "",
        "KURALLAR:",
        "- Dogrulanabilir olgular ile yorum/teoriyi AYRI listelerde ver.",
        "- Her olgu icin varsa kaynak basligi ve URL'sini koru.",
        "- Emin olmadigin bir seyi olgu olarak yazma; yorum listesine koy.",
        "- Terimler/ozel isimler icin kisa bir sozluk ver.",
        "",
        "SADECE su JSON nesnesini dondur:",
        '{"thesis": "...", "facts": [{"claim": "...", "source_title": "...",'
        ' "source_url": "...", "confidence": "high|medium|low"}],'
        ' "interpretations": [{"claim": "...", "basis": "..."}],'
        ' "glossary": [{"term": "...", "definition": "..."}],'
        ' "open_questions": ["..."]}',
    ])


def parse_research(raw, *, grounding_sources=None) -> dict:
    parsed = parse_json_object(raw, what="arastirma paketi")
    facts = []
    for item in (parsed.get("facts") or [])[:60]:
        if not isinstance(item, dict) or not str(item.get("claim") or "").strip():
            continue
        facts.append({
            "claim": _clean(item.get("claim"), 600),
            "source_title": _clean(item.get("source_title"), 200) or None,
            "source_url": _clean(item.get("source_url"), 500) or None,
            "confidence": (str(item.get("confidence") or "medium").lower()
                           if str(item.get("confidence") or "").lower()
                           in {"high", "medium", "low"} else "medium"),
            "kind": "sourced_fact",
        })
    interpretations = []
    for item in (parsed.get("interpretations") or [])[:60]:
        if not isinstance(item, dict) or not str(item.get("claim") or "").strip():
            continue
        interpretations.append({"claim": _clean(item.get("claim"), 600),
                                "basis": _clean(item.get("basis"), 400) or None,
                                "kind": "interpretation"})
    glossary = []
    for item in (parsed.get("glossary") or [])[:80]:
        if not isinstance(item, dict) or not str(item.get("term") or "").strip():
            continue
        glossary.append({"term": _clean(item.get("term"), 80),
                         "definition": _clean(item.get("definition"), 300)})
    return {
        "schema_version": 1,
        "artifact_type": "long_form_research_pack",
        "status": "review_required",
        "status_reason": "arastirma paketi uretildi; insan incelemesi bekleniyor",
        "not_uploaded": True,
        "thesis": _clean(parsed.get("thesis"), 800),
        "facts": facts,
        "interpretations": interpretations,
        "glossary": glossary,
        "open_questions": [_clean(q, 300) for q in (parsed.get("open_questions") or [])[:20]
                           if str(q or "").strip()],
        # Whatever the grounded route actually returned, kept beside the model's
        # own citations so the two can be compared instead of merged.
        "grounding_sources": list(grounding_sources or []),
        "fact_count": len(facts),
        "interpretation_count": len(interpretations),
        "separation_note": ("Olgular ve yorumlar AYRI tutulur: yorum, kaynakli "
                            "bir olgunun otoritesiyle sunulmamalidir."),
        "created_at": _now(),
    }


# ------------------------------------------------------------------- outline
def build_outline_prompt(plan: dict, research: dict) -> str:
    targets = long_form.chapter_targets(plan.get("target_duration_min"),
                                        plan.get("chapter_count"))
    facts = "\n".join(f"- {fact['claim']}" for fact in (research.get("facts") or [])[:MAX_BIBLE_FACTS])
    return "\n".join([
        "Bir uzun-form video icin BOLUM OUTLINE'i hazirla.",
        f"Konu: {plan.get('topic')}",
        f"Tez: {research.get('thesis')}",
        f"Ton: {plan.get('tone')} | Kitle: {plan.get('audience')} | Dil: {plan.get('language')}",
        f"Toplam hedef: {long_form.word_target(plan.get('target_duration_min'))['target']} kelime",
        f"Bolum sayisi: {plan.get('chapter_count')} (kelime hedefleri: "
        + ", ".join(str(item['target_words']) for item in targets) + ")",
        "",
        "Dogrulanmis olgulardan bazilari:",
        facts or "- (yok)",
        "",
        "KURALLAR:",
        "- Her bolumun kendi amaci ve bir sonrakine baglantisi olsun.",
        "- Bolum sinirlarini ve kelime hedeflerini KORU.",
        "- Ayni noktayi iki bolumde tekrarlama.",
        "",
        "SADECE su JSON nesnesini dondur:",
        '{"chapters": [{"index": 1, "title": "...", "goal": "...",'
        ' "beats": ["..."], "target_words": 420}]}',
    ])


def parse_outline(raw, plan: dict) -> dict:
    parsed = parse_json_object(raw, what="outline")
    expected = int(plan.get("chapter_count") or 0)
    targets = {item["index"]: item for item in
               long_form.chapter_targets(plan.get("target_duration_min"), expected)}
    chapters = []
    for item in (parsed.get("chapters") or []):
        if not isinstance(item, dict):
            continue
        try:
            index = int(item.get("index"))
        except (TypeError, ValueError):
            continue
        if index not in targets:
            continue
        chapters.append({
            "id": f"ch_{index:02d}",
            "index": index,
            "title": _clean(item.get("title"), 200) or f"Bolum {index}",
            "goal": _clean(item.get("goal"), 600),
            "beats": [_clean(beat, 300) for beat in (item.get("beats") or [])[:12]
                      if str(beat or "").strip()],
            "target_words": targets[index]["target_words"],
            "minimum_words": targets[index]["minimum_words"],
            "maximum_words": targets[index]["maximum_words"],
        })
    chapters.sort(key=lambda item: item["index"])
    if len(chapters) != expected:
        # Refusing here is the point: a five-chapter outline for a six-chapter
        # plan would quietly produce a shorter video than the operator approved.
        raise LongFormScriptError(
            f"outline {expected} bolum icermeli, {len(chapters)} bolum dondu")
    return {
        "schema_version": 1,
        "artifact_type": "long_form_outline",
        "status": "review_required",
        "status_reason": "outline uretildi; insan incelemesi bekleniyor",
        "not_uploaded": True,
        "chapters": chapters,
        "chapter_count": len(chapters),
        "total_target_words": sum(item["target_words"] for item in chapters),
        "created_at": _now(),
    }


# ------------------------------------------------------------- story bible
def build_story_bible(plan: dict, research: dict, outline: dict, *, index: int,
                      previous_summary: str = "") -> dict:
    """The compact shared context every chapter call receives.

    Compact is a requirement, not a style choice: this is repeated once per
    chapter, so anything included here is paid for `chapter_count` times.
    """
    chapters = {item["index"]: item for item in (outline.get("chapters") or [])}
    current = chapters.get(index) or {}
    following = chapters.get(index + 1)
    return {
        "topic": plan.get("topic"),
        "audience": plan.get("audience"),
        "tone": plan.get("tone"),
        "language": plan.get("language"),
        "thesis": research.get("thesis"),
        "approved_facts": [fact["claim"] for fact in (research.get("facts") or [])
                           [:MAX_BIBLE_FACTS]],
        "glossary": [f"{item['term']}: {item['definition']}"
                     for item in (research.get("glossary") or [])[:MAX_BIBLE_TERMS]],
        "previous_chapter_summary": _clean(previous_summary, MAX_SUMMARY_CHARS),
        "current_chapter": {"index": index, "title": current.get("title"),
                            "goal": current.get("goal"),
                            "beats": current.get("beats") or [],
                            "target_words": current.get("target_words")},
        "next_chapter_goal": (following or {}).get("goal") or "(son bolum)",
    }


def build_chapter_prompt(bible: dict) -> str:
    current = bible.get("current_chapter") or {}
    return "\n".join([
        "Bu uzun-form videonun SADECE su bolumunu yaz.",
        f"Bolum {current.get('index')}: {current.get('title')}",
        f"Bolum amaci: {current.get('goal')}",
        f"Hedef kelime: {current.get('target_words')} (+/- %15)",
        "",
        f"Konu: {bible.get('topic')}",
        f"Tez: {bible.get('thesis')}",
        f"Ton: {bible.get('tone')} | Kitle: {bible.get('audience')} | "
        f"Dil: {bible.get('language')}",
        "",
        "Onaylanmis olgular (bunlarin disina cikma, uydurma):",
        "\n".join(f"- {fact}" for fact in (bible.get("approved_facts") or [])) or "- (yok)",
        "",
        "Sozluk:",
        "\n".join(f"- {item}" for item in (bible.get("glossary") or [])) or "- (yok)",
        "",
        f"Onceki bolum ozeti: {bible.get('previous_chapter_summary') or '(ilk bolum)'}",
        f"Sonraki bolumun amaci: {bible.get('next_chapter_goal')}",
        "",
        "KURALLAR:",
        "- Sadece bu bolumu yaz; sonraki bolumun icerigine girme.",
        "- Onceki bolumde soylenenleri tekrar etme.",
        "- Seslendirilecek duz anlatim metni yaz (sahne yonergesi, baslik yok).",
        "- Yorum yapiyorsan bunu dilde belli et; olgu gibi sunma.",
        "",
        "SADECE su JSON nesnesini dondur:",
        '{"text": "...", "summary": "...", "uses_facts": ["..."],'
        ' "interpretation_notes": ["..."]}',
    ])


def parse_chapter(raw, bible: dict) -> dict:
    parsed = parse_json_object(raw, what="bolum scripti")
    text = str(parsed.get("text") or "").strip()
    if not text:
        raise LongFormScriptError("bolum scripti bos metin dondurdu")
    current = bible.get("current_chapter") or {}
    words = long_form.count_words(text)
    target = int(current.get("target_words") or 0)
    return {
        "id": f"ch_{int(current.get('index') or 0):02d}",
        "index": current.get("index"),
        "title": current.get("title"),
        "text": text,
        "summary": _clean(parsed.get("summary"), MAX_SUMMARY_CHARS),
        "uses_facts": [_clean(item, 300) for item in (parsed.get("uses_facts") or [])[:20]],
        "interpretation_notes": [_clean(item, 300) for item in
                                 (parsed.get("interpretation_notes") or [])[:20]],
        "word_count": words,
        "target_words": target,
        # Reported, not enforced: a chapter 8% under target is fine, and
        # regenerating it would cost a request to fix nothing.
        "within_target": (bool(target) and
                          int(target * 0.85) <= words <= int(target * 1.15)),
        "duration_estimate": long_form.duration_estimate_sec(words),
        "text_sha256": long_form.sha256_text(text),
        "edited_by_human": False,
        "created_at": _now(),
    }


def chapters_artifact(chapters: list, plan: dict) -> dict:
    total = sum(int(item.get("word_count") or 0) for item in chapters)
    return {
        "schema_version": 1,
        "artifact_type": "long_form_chapters",
        "status": "review_required",
        "status_reason": "bolum scriptleri uretildi; insan incelemesi bekleniyor",
        "not_uploaded": True,
        "chapters": sorted(chapters, key=lambda item: int(item.get("index") or 0)),
        "chapter_count": len(chapters),
        "total_word_count": total,
        "word_target": long_form.word_target(plan.get("target_duration_min")),
        "duration_estimate": long_form.duration_estimate_sec(total),
        "created_at": _now(),
        "updated_at": _now(),
    }


def full_script_text(chapters_record: dict) -> str:
    return "\n\n".join(
        str(chapter.get("text") or "").strip()
        for chapter in sorted((chapters_record or {}).get("chapters") or [],
                              key=lambda item: int(item.get("index") or 0))
        if str(chapter.get("text") or "").strip())


# -------------------------------------------------------------- consistency
def build_consistency_prompt(plan: dict, chapters_record: dict) -> str:
    body = "\n\n".join(
        f"[Bolum {chapter.get('index')}: {chapter.get('title')}]\n{chapter.get('text')}"
        for chapter in (chapters_record.get("chapters") or []))
    return "\n".join([
        "Asagidaki uzun-form video scriptini TUTARLILIK ve TEKRAR acisindan incele.",
        f"Konu: {plan.get('topic')} | Ton: {plan.get('tone')}",
        "",
        "Sunlari ara:",
        "- Bolumler arasi celisen ifadeler",
        "- Ayni noktanin gereksiz tekrari",
        "- Terim/isim tutarsizligi",
        "- Yorumun olgu gibi sunuldugu yerler",
        "",
        "Metni YENIDEN YAZMA; sadece bulgu ver.",
        "",
        "SADECE su JSON nesnesini dondur:",
        '{"issues": [{"kind": "contradiction|repetition|terminology|unmarked_opinion",'
        ' "chapters": [1,2], "detail": "...", "severity": "high|medium|low",'
        ' "suggestion": "..."}], "overall": "..."}',
        "",
        body,
    ])


def parse_consistency(raw, chapters_record: dict) -> dict:
    parsed = parse_json_object(raw, what="tutarlilik incelemesi")
    kinds = {"contradiction", "repetition", "terminology", "unmarked_opinion"}
    issues = []
    for item in (parsed.get("issues") or [])[:80]:
        if not isinstance(item, dict) or not str(item.get("detail") or "").strip():
            continue
        chapters = []
        for value in (item.get("chapters") or [])[:10]:
            try:
                chapters.append(int(value))
            except (TypeError, ValueError):
                continue
        issues.append({
            "kind": (item.get("kind") if item.get("kind") in kinds else "repetition"),
            "chapters": chapters,
            "detail": _clean(item.get("detail"), 600),
            "severity": (str(item.get("severity") or "medium").lower()
                         if str(item.get("severity") or "").lower()
                         in {"high", "medium", "low"} else "medium"),
            "suggestion": _clean(item.get("suggestion"), 400) or None,
        })
    high = [issue for issue in issues if issue["severity"] == "high"]
    return {
        "schema_version": 1,
        "artifact_type": "long_form_consistency_review",
        "status": "review_required",
        "status_reason": ("tutarlilik incelemesi tamamlandi; bulgular insan "
                          "incelemesi bekliyor"),
        "not_uploaded": True,
        "issues": issues,
        "issue_count": len(issues),
        "high_severity_count": len(high),
        "overall": _clean(parsed.get("overall"), 1000),
        "reviewed_chapter_count": len(chapters_record.get("chapters") or []),
        "reviewed_script_sha256": long_form.sha256_text(full_script_text(chapters_record)),
        "created_at": _now(),
    }
