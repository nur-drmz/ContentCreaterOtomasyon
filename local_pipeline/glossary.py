"""Project glossary: proper names and domain phrases the recogniser keeps missing.

WHY A GLOSSARY AND NOT A REPLACEMENT TABLE
------------------------------------------
The obvious fix for `Rainer -> Rhaena` is a hardcoded substitution. It is the
wrong fix twice over: it only helps the one project that inspired it, and it
fires blindly, so a legitimate "Rainer" elsewhere is silently destroyed.

A glossary instead does two separate jobs, and keeping them separate is the
whole design:

  1. BEFORE recognition, the terms are handed to Whisper as context, so the
     model is more likely to produce the right spelling in the first place.
     Fixing the input beats patching the output.
  2. AFTER recognition, terms are matched against what actually came back. An
     exact match modulo case/punctuation is applied; anything less certain
     becomes a SUGGESTION a human accepts or rejects. It never rewrites text on
     a guess -- that is how "Rhaenyra" would quietly become "Rhaena".

WHAT THE INSTALLED WHISPER ACTUALLY SUPPORTS
--------------------------------------------
Checked against faster-whisper 1.2.1 (the version pinned in this environment),
`WhisperModel.transcribe` accepts BOTH `initial_prompt` and `hotwords`. They are
not equivalent and this module uses each for what it is:

  * `initial_prompt` is prepended once and only conditions the FIRST window, so
    it suits the caption theme's style hint.
  * `hotwords` is injected into every window's prompt (`get_prompt` puts it in
    the `sot_prev` slot), which is what a name list needs -- a name can occur at
    the end of the clip as easily as the beginning.

Two constraints come straight from that source, not from guesswork:
`hotwords` is IGNORED when `prefix` is set (this pipeline never sets `prefix`),
and it is truncated at `max_length // 2 - 1` tokens, so the term list is capped
here rather than being silently cut by the decoder.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timezone
from difflib import SequenceMatcher

GLOSSARY_ARTIFACT = "results/glossary.json"

MAX_TERMS = 200
MAX_TERM_CHARS = 80

#: Whisper truncates hotwords at max_length // 2 - 1 tokens (223 for the 448
#: token context). Capping by characters well under that keeps the whole list
#: intact instead of losing whichever terms happen to fall off the end.
MAX_HOTWORD_CHARS = 700

#: A phrase longer than this is not a proper name, it is a sentence, and feeding
#: sentences as hotwords biases the transcript toward repeating them.
MAX_TERM_WORDS = 5

#: Suggestion threshold, tuned against the cases this exists for and MEASURED,
#: not guessed. With the blend below, on the real examples:
#:     Rainer         -> Rhaena                0.735   (Rhaenyra scores 0.700, loses)
#:     Steelers Rider -> Sheepstealer's rider  0.823
#:     Renira         -> Rhaenyra              0.850   (Rhaena scores 0.793, loses)
#: and on near misses that must NOT win:
#:     dragon -> Rhaena                        0.675
#:     Daemon -> Rhaena                        0.555
#:     steel  -> Sheepstealer                  0.548
#:     rider  -> Sheepstealer's rider          0.406
#: 0.70 sits in the gap. The margin is narrow (0.675 vs 0.735), which is exactly
#: why a match here is only ever a SUGGESTION a human accepts -- and why the
#: candidate filter below refuses to guess at ordinary lowercase words at all.
FUZZY_SUGGEST_THRESHOLD = 0.70

#: Above this the two strings differ only in trivia; treated as a confident
#: normalisation rather than a guess.
EXACT_AFTER_NORMALISATION = 1.0

_WORD_RE = re.compile(r"[^\W\d_]+(?:['’][^\W\d_]+)*", re.UNICODE)
_VOWELS = frozenset("aeiouy")


class GlossaryError(ValueError):
    """A rejected glossary edit, phrased for the person editing it."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalise(text) -> str:
    """Case-, accent- and punctuation-insensitive form used for comparison.

    Apostrophes go too, so `Sheepstealer's` and `Sheepstealers` are the same
    key: the recogniser's choice of apostrophe is not a different word.
    """
    folded = unicodedata.normalize("NFKD", str(text or "")).casefold()
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    folded = re.sub(r"[^\w\s]", "", folded)
    return re.sub(r"\s+", " ", folded).strip()


def skeleton(text) -> str:
    """Consonant skeleton: the part of a name the recogniser tends to keep.

    Whisper's mistakes on proper nouns are overwhelmingly vowel-and-h spelling
    drift (`Rhaena` -> `Rainer`, `Rhaenyra` -> `Renira`) while the consonant run
    survives. Comparing skeletons separates a misspelling of a glossary term
    from an unrelated word that merely shares letters -- plain string similarity
    scores `Rainer->Rhaena` and `Daemon->Rhaena` identically at 0.50.
    """
    out = []
    for ch in normalise(text).replace(" ", ""):
        if ch in _VOWELS or ch == "h":
            continue
        if out and out[-1] == ch:
            continue
        out.append(ch)
    return "".join(out)


def similarity(candidate, term) -> float:
    """Blended score in 0..1. Higher means "more likely the same name".

    Three signals, because no one of them separates the cases this has to get
    right. Plain similarity alone scores `Rainer->Rhaena` and `Daemon->Rhaena`
    identically at 0.50. The consonant skeleton fixes that but then ranks
    `Rainer->Rhaenyra` (skeleton rnr == rnr, a perfect 1.00) above the correct
    `Rainer->Rhaena`. Length agreement breaks that tie, because a recogniser
    mishearing a name rarely invents two extra syllables.
    """
    left, right = normalise(candidate), normalise(term)
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    plain = SequenceMatcher(None, left, right).ratio()
    bone_a, bone_b = skeleton(candidate), skeleton(term)
    bone = SequenceMatcher(None, bone_a, bone_b).ratio() if bone_a and bone_b else 0.0
    length = min(len(left), len(right)) / max(len(left), len(right))
    return round(0.35 * plain + 0.45 * bone + 0.20 * length, 4)


# ------------------------------------------------------------------ artifact
def new_glossary(ownership=None) -> dict:
    record = {
        "schema_version": 1,
        "artifact_type": "project_glossary",
        "status": "review_required",
        "not_uploaded": True,
        "terms": [],
        "suggestions": [],
        "updated_at": _now(),
        "updated_by": None,
    }
    if ownership is not None:
        record.update(ownership.to_dict())
    return record


def _clean_term(term) -> str:
    text = re.sub(r"\s+", " ", str(term or "")).strip()
    if not text:
        raise GlossaryError("terim bos olamaz")
    if len(text) > MAX_TERM_CHARS:
        raise GlossaryError(f"terim en fazla {MAX_TERM_CHARS} karakter olabilir: {text[:30]}...")
    if len(text.split()) > MAX_TERM_WORDS:
        raise GlossaryError(
            f"terim en fazla {MAX_TERM_WORDS} kelime olabilir; bu bir cumle gibi "
            f"gorunuyor: {text[:40]}")
    if not normalise(text):
        raise GlossaryError(f"terimde harf yok: {text!r}")
    return text


def set_terms(record: dict, terms, *, actor: str) -> dict:
    """Replace the whole term list. Duplicates collapse on the normalised key."""
    record = dict(record or new_glossary())
    seen, cleaned = {}, []
    for entry in (terms or []):
        text = entry.get("term") if isinstance(entry, dict) else entry
        term = _clean_term(text)
        key = normalise(term)
        if key in seen:
            continue
        variants = []
        if isinstance(entry, dict):
            for variant in (entry.get("variants") or []):
                variant_text = re.sub(r"\s+", " ", str(variant or "")).strip()
                if variant_text and normalise(variant_text) != key:
                    variants.append(variant_text[:MAX_TERM_CHARS])
        seen[key] = True
        cleaned.append({
            "term": term,
            "key": key,
            "skeleton": skeleton(term),
            "variants": variants[:10],
            "source": (entry.get("source") if isinstance(entry, dict) else None) or "manual",
            "added_by": (entry.get("added_by") if isinstance(entry, dict) else None) or actor,
            "added_at": (entry.get("added_at") if isinstance(entry, dict) else None) or _now(),
        })
    if len(cleaned) > MAX_TERMS:
        raise GlossaryError(f"en fazla {MAX_TERMS} terim tutulabilir ({len(cleaned)} verildi)")
    record["terms"] = cleaned
    record["updated_at"] = _now()
    record["updated_by"] = actor
    return record


def terms_of(record) -> list:
    return list((record or {}).get("terms") or [])


# --------------------------------------------------------------- ASR context
def asr_context(record, *, style_prompt: str = "") -> dict:
    """What to hand `WhisperModel.transcribe`, and an honest account of it.

    Returns the two parameter values plus which terms actually fit, because a
    term that was dropped for length did NOT influence the transcript and the
    manifest should not imply otherwise.
    """
    terms = [entry.get("term") for entry in terms_of(record) if entry.get("term")]
    included, dropped, budget = [], [], MAX_HOTWORD_CHARS
    for term in terms:
        cost = len(term) + 2
        if cost > budget:
            dropped.append(term)
            continue
        budget -= cost
        included.append(term)
    return {
        # Comma-separated: the decoder sees this as prompt text, and commas keep
        # multi-word names from bleeding into each other.
        "hotwords": ", ".join(included) if included else None,
        "initial_prompt": (style_prompt or "").strip() or None,
        "terms_used": included,
        "terms_dropped": dropped,
        "term_count": len(included),
        "supported_params": ["initial_prompt", "hotwords"],
    }


# --------------------------------------------------------------- suggestions
def _windows(tokens, max_len):
    for size in range(1, max_len + 1):
        for index in range(0, len(tokens) - size + 1):
            yield index, size, tokens[index:index + size]


def suggest_for_text(text, record, *, low_confidence_terms=(), limit: int = 25) -> list:
    """Glossary matches for a transcript, split by how sure we are.

    `certain` entries differ from the glossary term only in case or punctuation
    and can be applied without asking. `suggested` entries are close but not
    certain and are offered to the reviewer -- never applied automatically.

    Only proper-noun-shaped candidates are considered: a window must contain a
    capitalised word, or a word the recogniser itself flagged as low confidence.
    Without that filter an ordinary lowercase `dragon` scores 0.675 against
    `Rhaena`, close enough to the real `Rainer` match at 0.735 that the list
    would fill with noise.
    """
    entries = terms_of(record)
    if not entries or not str(text or "").strip():
        return []
    by_key = {entry["key"]: entry for entry in entries if entry.get("key")}
    for entry in entries:
        for variant in entry.get("variants") or []:
            by_key.setdefault(normalise(variant), entry)
    unsure = {normalise(item) for item in (low_confidence_terms or []) if normalise(item)}
    max_words = max((len(entry["key"].split()) for entry in entries), default=1)

    tokens = _WORD_RE.findall(str(text))
    if not tokens:
        return []
    # A token that already IS a glossary term is correct as written. Windows
    # containing one are skipped entirely, so "Rhaenyra waited" is never offered
    # as a sloppy match for "Rhaenyra" and one real name is never turned into
    # another.
    exact_at = {index for index, token in enumerate(tokens)
                if normalise(token) in by_key}

    scored = []
    for index, size, window in _windows(tokens, min(max_words, MAX_TERM_WORDS)):
        span = range(index, index + size)
        phrase = " ".join(window)
        key = normalise(phrase)
        if not key:
            continue
        exact = by_key.get(key)
        if exact is not None:
            if phrase != exact["term"]:
                scored.append((1.0, index, size, phrase, exact, "certain"))
            continue
        if any(position in exact_at for position in span):
            continue
        looks_named = any(token[:1].isupper() for token in window) or any(
            normalise(token) in unsure for token in window)
        if not looks_named:
            continue
        best, best_entry = 0.0, None
        for entry in entries:
            value = similarity(phrase, entry["term"])
            if value > best:
                best, best_entry = value, entry
        if best_entry is not None and best >= FUZZY_SUGGEST_THRESHOLD:
            scored.append((best, index, size, phrase, best_entry, "suggested"))

    # Longer windows win over the shorter ones inside them: "Steelers Rider"
    # should be offered as one phrase, not as two competing single words.
    scored.sort(key=lambda row: (-row[2], -row[0]))
    results, claimed = [], set()
    for value, index, size, phrase, entry, kind in scored:
        span = set(range(index, index + size))
        if span & claimed:
            continue
        claimed |= span
        results.append({
            "from": phrase,
            "to": entry["term"],
            "score": round(float(value), 4),
            "kind": kind,
            "token_index": index,
            "token_span": size,
        })
        if len(results) >= limit:
            break
    results.sort(key=lambda row: row["token_index"])
    return results


def suggest_terms_from_correction(source, target, record) -> list:
    """New glossary entries implied by a manual correction.

    A human who fixes `Steelers Rider` to `Sheepstealer's rider` has just taught
    the system a proper name. Offering to remember it is how the next
    transcription gets it right at recognition time instead of needing the same
    correction again.
    """
    target_text = re.sub(r"\s+", " ", str(target or "")).strip()
    if not target_text or not normalise(target_text):
        return []
    if len(target_text.split()) > MAX_TERM_WORDS or len(target_text) > MAX_TERM_CHARS:
        return []
    known = {entry.get("key") for entry in terms_of(record)}
    key = normalise(target_text)
    if key in known:
        return []
    # Only propose things that look like names: a correction from "teh" to "the"
    # is a typo fix, not vocabulary.
    if target_text.islower() and len(target_text.split()) == 1:
        return []
    return [{
        "term": target_text,
        "key": key,
        "reason": f"'{str(source or '').strip()}' manuel olarak '{target_text}' yapildi",
        "source": "correction",
        "suggested_at": _now(),
    }]


def record_suggestions(record: dict, suggestions, *, actor: str) -> dict:
    """Park proposed terms on the artifact without adding them to the list.

    Kept separate from `terms` on purpose: a suggestion has not been accepted,
    and anything in `terms` is fed to the recogniser.
    """
    record = dict(record or new_glossary())
    existing = {item.get("key") for item in (record.get("suggestions") or [])}
    known = {entry.get("key") for entry in terms_of(record)}
    merged = list(record.get("suggestions") or [])
    for item in (suggestions or []):
        key = item.get("key")
        if not key or key in existing or key in known:
            continue
        existing.add(key)
        merged.append(dict(item, suggested_by=actor))
    record["suggestions"] = merged[-50:]
    record["updated_at"] = _now()
    return record


def summarise(record) -> dict:
    """View model for the glossary panel."""
    entries = terms_of(record)
    context = asr_context(record)
    return {
        "terms": [
            {"term": entry.get("term"), "variants": entry.get("variants") or [],
             "source": entry.get("source"), "added_by": entry.get("added_by")}
            for entry in entries
        ],
        "term_count": len(entries),
        "suggestions": list((record or {}).get("suggestions") or [])[-20:],
        "max_terms": MAX_TERMS,
        "terms_dropped": context["terms_dropped"],
        "asr_hint": ("Terimler transkripsiyon ONCESINDE modele baglam olarak "
                     "verilir (faster-whisper `hotwords`), sonrasinda kor bir "
                     "degistirme yapilmaz."),
        "not_uploaded": True,
    }
