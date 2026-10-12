# 05 · Onay Kapıları

Kaynak: `@local_pipeline/approval.py`,
`creator_console/media_review.py`, `render_review.py`, `voice_review.py`,
`copyright_review.py`

## Temel ilke: onay üretimden SONRA ayrı bir adımdır

Üretim fonksiyonları (`script_generation.py`, `tts.py`, `render_utils.py`)
**onay argümanı kabul etmez**. Yalnızca `review_required` (veya `failed` /
`insufficient_data` / `development_placeholder`) üretebilirler.

Gerekçe modülün kendi docstring'inde açıkça yazılı: script'i üreten çağrıya
`script_approved=True` geçirmek, artefaktı **hiçbir insanın okumasına imkân
olmadan** aynı çağrı içinde "onaylı" damgalamak demektir — bu bir onay kapısı
değil, lastik damgadır.

`approved`'a yükseltme yalnızca `approval.py` içinde ve yalnızca çağıran
**incelediği içeriğin SHA-256'sını verdiğinde** olur.

## Onaylanabilir türler

`APPROVABLE_KINDS = ("processing_plan", "script", "voiceover", "transcript", "render")`

## İçeriğe bağlama

Her onay `approved_content_sha256` kaydeder — incelenen baytların özeti (script
metni / ses dosyası / video dosyası). `check_approval()` bu özeti artefaktın
**güncel** içeriğinden yeniden türetir.

Sonuç: script'i yeniden üretmek, TTS'i yeniden koşmak veya videoyu yeniden render
etmek önceki onayı **otomatik geçersizleştirir**; sessizce ileriye taşınmaz.

### İşleme planının özel durumu

`processing_plan` bir **karar** artefaktıdır, medya değil. Bu yüzden içerik
hash'i `plan["decision"]` bloğu üzerinden alınır
(`PROCESSING_PLAN_DECISION_KEY = "decision"`). Pipeline'ın ne yapacağını
değiştirebilecek her alan bu blokta olmak zorundadır; blok dışındakiler
betimleyicidir ve onayı geçersiz kılmaz.

## Render kapısı: tüm üst zincir onaylı olmalı

Bir render yalnızca **tüm** üst-akış zinciri onaylıysa onaylanabilir. Eksik bir
alan `False` sayılır.

| Mod | Zorunlu koşullar |
|---|---|
| `narrated_theory` | `technical_validation_passed`, `script_approved`, `voiceover_approved`, `voiceover_hash_matches_script` |
| `direct_clip_series` | `technical_validation_passed`, `moment_selection_approved` |

`evaluate_render_gate()` `{'unmet': [...], 'conditions': {...},
'failed_conditions': [...]}` döner. **Boş `unmet` listesi**, bir render'ın
onaylanabileceği tek durumdur.

Gerekçe: onaylanmamış bir script ve `development_placeholder` bir seslendirmeden
kurulmuş videoyu onaylamak, o onaysız girdileri "onaylı" bir artefaktın içine
**aklamak** olurdu.

## Asla onaylanamayan sağlayıcı

`NEVER_APPROVABLE_PROVIDERS = {"gtts"}` — çağıran ne geçirirse geçirsin gTTS
`approved` veya `production_ready` olamaz.

## İnsan inceleme listeleri (checklist)

`media_review.py` her medyaya ortak, **digest'e bağlı** bir inceleme listesi
altyapısı verir. Verdict sözlüğü: `pending` / `pass` / `needs_revision`.

Rapor edilebilir teknik alanlar (`REPORTABLE_FIELDS`): çözünürlük, süre, video
codec, ses akışı varlığı, altyazı kaynağı, altyazı teması, kelime sayısı, ses,
anlatım ön ayarı, durum.

### Render incelemesi (`render_review.py`)

Her madde bir **soru**, bir **düzeltme sekmesi** ve bir **düzeltme ipucu** taşır:

| Madde | Soru | Düzeltme yeri |
|---|---|---|
| `clip_boundaries` | Klip cümle ortasında başlamıyor/bitmiyor mu? | Moment Explorer |
| `crop` | 9:16 kadraj önemli yüz/aksiyonu kesmiyor mu? | Faz 0 planı (`render_mode`) |
| `subtitle_sync` | Altyazılar konuşmayla senkron mu? | Render Studio (daha büyük Whisper modeli) |
| `active_word_highlight` | Sarı vurgu doğru kelimenin üzerinde mi? | Render manifestinde altyazı kaynağı |
| `font_readability` | Yazı tipi/boyut/kontrast mobilde okunabilir mi? | Render ayarları |
| `audio_continuity` | Ses sürekliliği | |

Toplam **6 madde**.

Bir yuvanın 6 maddesi dolmadan o yuvanın onay düğmesi açılmaz — ve bir yuvada
atılan işaretler diğerini açmaz. **N Short = N ayrı onay.**

### Ses incelemesi (`voice_review.py`)

**6 madde**: `pronunciation` (özel adlar doğru mu — Targaryen, Rhaenyra…),
`naturalness`, `pacing`, `tone`, `script_match`, `audio_quality`. İki uyarlama
katmanı var:

- **Dile göre** soru metni (`_NATURALNESS_BY_LANGUAGE`: `en-US`, `tr-TR`).
- **Anlatım ön ayarına göre** soru metni (`_PRESET_QUESTIONS`, örn.
  `deep_storyteller` için "kontrollü, sinematik anlatıcı… film fragmanı
  abartısına kaçmadan").

Her maddenin düzeltme ipucu somut: örn. telaffuz için "Script Studio'da yazımı
okunuşa yakın biçimde değiştir, scripti yeniden onayla ve sesi yeniden üret".

### Telif incelemesi (`copyright_review.py`)

| Madde | Soru özü |
|---|---|
| `source_rights` | Bu kaynağı kullanmak için dayanağın var mı? |
| `transformative_use` | Short kaynağı yeniden yayınlamıyor; yorum/analiz/teori katıyor mu? |
| `extent` | Alınan bölüm gerektiği kadar mı; doruk noktasını tek başına taşımıyor mu? |
| `attribution` | Kaynak eser (yapım, sezon/bölüm) belirtilecek mi? |
| `platform_rules` | Platform kurallarına uygun mu? |

Toplam **5 madde**.

## Yayın kapıları (üç ayrı onay)

1. `publication_metadata` → metadata onayı
2. `publish_intent` → private upload onayı (`decision_sha256`)
3. görünürlük onayı → `unlisted`/`public` için ayrıca gerekli

Yükleme **daima** `private` başlar. İzleyiciye açan görünürlükler
(`AUDIENCE_EXPOSING_VISIBILITIES`) ikinci ve bağımsız bir insan kararı ister.

## Otomasyonun durduğu yerler

"Otomatik akışı başlat" düğmesi sıradaki **güvenli makine fazını** çalıştırır.
Plan türü, moment seçimi ve hash onayları insan kararıdır; otomasyon bu
noktalarda durur ve kullanıcı işleminden sonra aynı düğmeyle devam eder
(`creator_console/automation.py`).
