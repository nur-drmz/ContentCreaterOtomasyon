# 09 · Stüdyolar: Turbo ve Long-form

İki ek workflow, ana Short hattından ayrı ama aynı altyapıyı (stage machine, job
kuyruğu, onay kapıları, kota sinyalleri) kullanır.

## Ortak temel: stage machine

`@local_pipeline/stage_machine.py` (259
satır) — **kalıcı, devam ettirilebilir** aşama makinesi. Her uzun süreli workflow
bunu paylaşır.

Aşama durumları:

| Durum | Anlamı |
|---|---|
| `not_started` | Henüz sıraya girmedi |
| `queued` | Sırada |
| `running` | Çalışıyor |
| `waiting_rate_limit` | Dakika limiti; bekle |
| `waiting_daily_quota` | Günlük kota bitti; yarın |
| `blocked_billing` | Plan/faturalandırma engeli; insan müdahalesi gerekli |
| `review_required` | İnsan incelemesi bekliyor |
| `completed` | Bitti |
| `failed` | Başarısız |
| `cancelled` | İptal |

`WAITING_STATES`, `BLOCKED_STATES` ve `TERMINAL_OK` demetleri hangi durumun ne
anlama geldiğini tek yerde tanımlar; sınıflandırma kararı
[`quota_signals.py`](06-is-kuyrugu-ve-otomasyon.md#kota-sinyalleri-sağlayıcı-hayır-dedi-ne-demek)
tarafından verilir.

---

## Turbo Studio — 14 aşama

`local_pipeline/turbo.py` (459 satır, yan etkisiz kurallar/tahminler) +
`creator_console/turbo_studio.py` (1530 satır, aşamaları çalıştıran servis) +
`turbo_script.py` (204).

Fikir: **kaynak video olmadan**, brief'ten stok materyalle Short/dikey video
üretmek.

| # | Aşama | Otomatik? | Girdi |
|---|---|---|---|
| 1 | `script` — Script ve sahne planı | ✅ | `brief` |
| 2 | `script_review` — Script incelemesi | 👤 | `script` |
| 3 | `tts` — Anlatım parçaları | ✅ | `script` |
| 4 | `audio_concat` — Anlatım birleştirme | ✅ | `tts` |
| 5 | `audio_review` — Ses incelemesi | 👤 | `audio_concat` |
| 6 | `media_plan` — Klip slotları | ✅ | `script` |
| 7 | `media` — Stok materyal (ara / seç / indir) | 👤 | `media_plan` |
| 8 | `music` — Müzik seçimi ve lisans | 👤 | — |
| 9 | `subtitles` — Altyazı zamanlaması | ✅ | `audio_concat` |
| 10 | `timeline` — Zaman çizelgesi manifesti | ✅ | brief, audio, subtitles, media_plan, media, music |
| 11 | `timeline_review` — Zaman çizelgesi incelemesi | 👤 | `timeline` |
| 12 | `preview_render` — Önizleme render | ✅ | `timeline` |
| 13 | `final_render` — Final render | 👤 | `timeline` |
| 14 | `archive` — Arşiv / dışa aktarım | 👤 | `final_render` |

`STAGE_INPUTS` her aşamanın neye bağlı olduğunu açıkça tanımlar; bir aşama girdisi
hazır olmadan çalışmaz.

### Sınırlar ve varsayılanlar

```
ASPECT_RATIOS = {'9:16': 1080x1920, '16:9': 1920x1080, '1:1': 1080x1080}
ORDERINGS     = ('sequential', 'seeded_random')
FIT_MODES     = ('crop', 'fit')
TRANSITIONS   = ('none', 'fade')
CONTENT_TYPES = ('explainer', 'listicle', 'story', 'news_recap', 'tutorial')
MIN_DURATION_SEC = 15      MAX_DURATION_SEC = 1800
MIN_CLIP_SEC = 2.0         MAX_CLIP_SEC = 15.0        MAX_CLIPS = 240
WORDS_PER_MINUTE = 150     TTS_WORDS_PER_CHUNK = 180
```

### Revizyon türleri (`REVISION_KINDS`)

Her revizyon türü **hangi aşamaları geçersizleştirdiğini** ve yalnızca manifest
değişikliği mi olduğunu bildirir. Bu, gereksiz yeniden üretimi (ve API
harcamasını) engeller:

| Tür | Etkilenen aşama | Yalnızca manifest? |
|---|---|---|
| `frame` — Kadraj / zoom | — | ✅ |
| `retime` — Klip süresi | — | ✅ |
| `music_volume` / `music_remove` | — | ✅ |
| `subtitle_style` — Altyazı teması | `subtitles` | ❌ |
| `recue` — Altyazıyı yeniden oluştur | `subtitles` | ❌ |
| `replace_clip` — Klibi değiştir | `media` | ❌ |
| `narration_chunk` — Tek anlatım parçasını yenile | `tts` | ❌ |
| `script_edit` | `script` | ❌ |
| `brief_edit` | `brief` | ❌ |

Artefaktlar: `results/turbo/` altında `brief.json`, `workflow.json`,
`script.json`, `media_plan.json`, `timeline.json`, `tts_chunks.json`,
`narration.wav`, `subtitles.srt`, `subtitles.ass`.

API: `GET /turbo`, `POST /turbo/brief|approve|stage|media|music|music/upload|render|revision`,
`GET /turbo/revision-preview`.

---

## Long-form Studio — 12 aşama

`local_pipeline/long_form.py` (253) + `long_form_script.py` (446, outline-first
script üretimi) + `creator_console/long_form_studio.py` (1010).

Hedef: **15–20 dakikalık, 16:9** anlatım videosu.

| # | Aşama | Otomatik? |
|---|---|---|
| 1 | `research` — Araştırma paketi | ✅ |
| 2 | `outline` — Outline | ✅ |
| 3 | `chapters` — Bölüm scriptleri | ✅ |
| 4 | `consistency` — Tam script tutarlılık incelemesi | ✅ |
| 5 | `script_review` — İnsan script incelemesi | 👤 |
| 6 | `tts` — TTS parçaları | ✅ |
| 7 | `audio_review` — Ses incelemesi | 👤 |
| 8 | `broll` — Kaynak / B-roll eşlemesi | 👤 |
| 9 | `timeline` — 16:9 zaman çizelgesi | ✅ |
| 10 | `preview_render` — Önizleme render | ✅ |
| 11 | `final_render` — Final render | ✅ |
| 12 | `archive` — Arşiv / dışa aktarım | 👤 |

### Parametreler

```
DURATION_MIN_MIN = 15   DURATION_MAX_MIN = 20   DEFAULT_DURATION_MIN = 18
CHAPTER_MIN = 5         CHAPTER_MAX = 7
WPM_MIN = 130           WPM_MAX = 150
TTS_CHUNK_TARGET_SEC = 90  (min 60, max 120)
CONTENT_TYPES = ('theory', 'commentary', 'explainer', 'general_knowledge')
DEFAULT_ASPECT_RATIO = '16:9'
```

**Outline-first** yaklaşım: önce outline üretilir, sonra bölüm bölüm script
yazılır, ardından tüm script tutarlılık için ayrıca incelenir (`consistency`).
Uzun anlatım TTS'i 60–120 sn'lik parçalara bölünür ve devam ettirilebilir
(`tts_chunks.py`).

B-roll aşaması `stock_media.py` üzerinden **denetlenebilir** bir plan üretir:
sağlayıcı-nötr arama yalnızca açık düğme sonrası koşar, anahtarlar UI'ya gitmez.

Artefaktlar: `results/long_form/workflow.json`, `results/long_form_plan.json`.

API: `GET /long-form`, `POST /long-form/control|stage|chapter|tts|broll`,
`PATCH /long-form-plan`.

---

## Şu anki durum notu

Uzun form üretimi, kullanıcı talebi üzerine **şimdilik askıda** tutulmaktadır;
kod ve aşamalar yerinde ama aktif geliştirme Short/Render Atölyesi tarafındadır.
`verify_long_form_pilot.py`, `verify_long_form_planner.py` ve
`verify_long_form_studio.py` doğrulama betikleri mevcuttur.
