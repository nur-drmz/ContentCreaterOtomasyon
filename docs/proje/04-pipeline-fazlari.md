# 04 · Pipeline Fazları

## İki işleme modu

Faz 0 planı, projenin hangi hattan geçeceğini belirler
(`@local_pipeline/source_strategy.py`):

| Mod | Pipeline modu | Fikir |
|---|---|---|
| `direct_clip_series` | `original_audio_subtitles` | Kaynağın **kendi sesi** korunur; altyazı kaynağın kendi sözlerinin transkriptidir |
| `narrated_theory` | `narrated_recap` | Model yeni bir **anlatım scripti** yazar, TTS okur, altyazı bu scriptten üretilir |

Mod, hangi fazların çalışabileceğini `MODE_PHASE_POLICY` ile **gerekçeli olarak**
kısıtlar. Reddedilen faz sessizce atlanmaz; nedeni metin olarak taşınır:

| Faz | `direct_clip_series` | `narrated_theory` |
|---|---|---|
| `video_analysis` | ✅ | ✅ |
| `moment_selection` | ✅ | ✅ |
| `transcript` | ✅ (altyazı buradan gelir) | ❌ "altyazi onaylanan anlatim scriptinden uretilir" |
| `script_generation` | ❌ "model yeni metin YAZMAZ; hicbir script API cagrisi yapilmaz" | ✅ |
| `tts` | ❌ "orijinal ses korunur; hicbir TTS API cagrisi yapilmaz" | ✅ |
| `render` | ✅ | ✅ |

Bu, maliyet ve dürüstlük kararı aynı anda: `direct_clip_series` modunda **hiçbir**
script/TTS API çağrısı yapılmaz.

## Faz -1 · Trend alımı (opsiyonel)

`local_pipeline/trend_ingest.py` — 690 satır.

1. `POST /source-candidates` — arama terimleri ve/veya manuel URL'lerden aday
   listesi (`results/source_candidates.json`, `requires_rights_decision: true`).
2. `POST /source-candidates/probe` — adayın teknik uygunluğu.
3. `POST /rights` — **zorunlu hak kapısı**. Lisans türü `own` / `cc_by` /
   `licensed` / `fair_use_claim`. Üçüncü taraf türlerinde atıf (≥10 karakter) ve
   dönüştürücü katkı notu (≥20 karakter) zorunlu. Karar SHA-256'ya bağlanır
   (`rights_decision.decision_sha256`).
4. `POST /ingest` — indirme (yt-dlp), varsayılan tavanlar: ≤1080p, süre ve dosya
   boyutu sınırlı. URL'lerdeki sır benzeri sorgu parametreleri maskelenir.

İnsan hak kararı olmadan hiçbir kaynak seçilmez veya indirilmez.

## Faz 0 · Kaynak değerlendirme ve plan

`local_pipeline/source_strategy.py` — 984 satır.

- **Kaynak değerlendirme** (`source_assessment`): ffprobe ile süre/çözünürlük/ses
  varlığı, dosya parmak izi (`FINGERPRINT_EDGE_BYTES` — dosyanın uçlarından hash).
- **İki mod skoru** (`mode_scores`): hangi modun bu kaynak için daha uygun
  olduğuna dair gerekçeli skor.
- **İşleme planı** (`processing_plan`): mod, hedef süre
  (`DEFAULT_TARGET_DURATION_SEC = 60`), çıktı sayısı (`MAX_OUTPUT_COUNT = 10`),
  içerik dili (`en-US` varsayılan, `tr-TR` desteklenir), render ayarları.
- `narrated_theory` için teori açısı en az `MIN_THEORY_ANGLE_CHARS = 20` karakter.
- Plan **hash onayı** ile kilitlenir; `PLAN_BOUND_RENDER_KEYS`
  (`subtitle_language`, `subtitle_whisper_model_size`) plana bağlıdır, değişirse
  onay geçersizdir. `PRESENTATION_RENDER_KEYS` (`caption_style`, `caption_theme`,
  `preview_mode`, `render_mode`) plandan sonra da değiştirilebilir
  (`render_profile.py`).

Desteklenen kaynak formatları: **MP4, MOV, MKV, WebM, AVI, M4V**. MKV doğrudan
FFmpeg ile analiz edilir; tarayıcı uyumluluğu için önizleme ve final H.264/AAC
MP4 üretilir.

**Büyük yükleme:** HTTP gövdesi RAM'e alınmadan 8 MB parçalarla doğrudan proje
depolamasına yazılır; varsayılan yerel sınır 20 GB. Yarım kalan aktarım `.part`
dosyasını temizler; proje kaydına yalnızca eksiksiz dosya atomik olarak bağlanır.

## Faz 1 · Video analizi

`local_pipeline/video_analysis.py` — 1366 satır.

- Klip/ses çıkarma (FFmpeg), **transcript** (faster-whisper), **sahne tespiti**
  (PySceneDetect), **ses enerjisi** (librosa).
- Hot moment adayları puanlanır → `results/hot_moments.json`, daima
  `review_required`.
- Aday önizleme klipleri (`results/moment_previews/`).

**Uzun kaynak için tam tarama:** Normal analiz tek bitişik pencereye bakar
(varsayılan 240 sn) — aynı pencereden kesilen beş Short aynı sahnenin beş
varyasyonu olur. `FULL_SOURCE_ANALYSIS_STAGES` iki geçişli tam taramayı tanımlar:

| Aşama | Etiket |
|---|---|
| `full_scan` | Tum kaynak taraniyor (ucuz ses enerjisi profili) |
| `region_analysis` | Aday bolgeler inceleniyor (yalnızca öne çıkan bölgelerde Whisper + sahne) |
| `scoring` | Film geneli siralama |
| `previews` | Onizleme klipleri |

## Faz 2 · Moment seçimi (insan)

Moment Explorer: aday kartları, önizleme oynatma, çoklu seçim. Sonuç
`moment_selection.json` — **önizleme/hash'e bağlı**. Düz bir boolean bu kapıyı
geçemez. Aynı moment iki çıktı yuvasında seçilemez.

`clip_bounds` uç noktaları klip başlangıç/bitişinin insan tarafından ince
ayarlanmasını sağlar (`clip_bounds.saved` audit olayı).

## Faz 3a · Altyazı transkripti (`direct_clip_series`)

`local_pipeline/transcript_edit.py` (444 satır) + `glossary.py` (399 satır).

- ASR segmentleri insan tarafından düzeltilebilir: segment düzenleme, tam metin
  değiştirme, dışlamalar, ASR'a geri dönüş.
- **Sözlük** (`glossary.json`): tanıyıcının sürekli kaçırdığı özel adlar ve alan
  ifadeleri; düzeltmeler tekrar kullanılabilir.
- Revizyon geçmişi tutulur; elle yapılan düzenlemeler arşivlenir
  (`transcript.manual_edits_archived`).
- `lyrics_review.py` / `lyrics_training.py`: şarkı/söz içeren bölümler için
  muhafazakâr öneriler ve insan etiketli örnekler.

## Faz 3b · Script üretimi (`narrated_theory`)

`local_pipeline/script_generation.py` — 620 satır.

- Prompt kurulumu, LLM yanıt ayrıştırma, sağlayıcı çağrıları →
  `selected_script.json`.
- Yalnızca **geçerli ve güncel** bir `moment_selection.json` varsa çalışır.
- Yapılandırılmış çıktı **iki kez** doğrulanır: şema/sözdizimi, ardından
  **anlam** — `{"script": ""}` geçerli JSON'dur ve yine reddedilir.
- Script gösterilen SHA-256 ile onaylanır.

## Faz 4 · Seslendirme (TTS)

`local_pipeline/tts.py` (351) + `tts_chunks.py` (487) + `narration_presets.py`
(161) + `creator_console/tts_runner.py` (258).

- Varsayılan rota Gemini TTS; Piper / ElevenLabs / OpenAI seçilebilir sağlayıcı
  alternatifleri.
- Gemini başlıksız 24 kHz/16-bit/mono PCM döner → geçerli WAV'a sarılır, sonra
  doğrulanır (konteyner, codec, örnekleme hızı, boyut, süre, boş olmama).
- Uzun anlatım **parça parça ve devam ettirilebilir** üretilir (`tts_chunks`).
- Anlatım ön ayarları: registry'den bir ses + teslim yönergesi.
- **gTTS asla onaylanamaz** (`development_placeholder`).
- Ses onayı **script'in SHA-256'sına bağlıdır**: script değişirse ses onayı
  otomatik geçersiz olur.

## Faz 5 · Render

`local_pipeline/render_utils.py` (1835) + `timeline.py` (653) +
`subtitle_edit.py` (138). Ayrıntı: [`08-render-ve-scene-editor.md`](08-render-ve-scene-editor.md).

- 9:16 yeniden çerçeveleme (`center_crop` veya `blurred_background`), altyazı
  yazımı, voice-over mix, **audio ducking**.
- Kalite ön ayarları (`render_profile.QUALITY_PRESETS`):
  `draft` (scale 0.35, crf 32, ultrafast), `preview` (0.5 / 30 / ultrafast),
  `final` (1.0 / 18 / slow). Teslim daima `final`.
- Çözünürlük: `vertical_1080x1920` veya `vertical_720x1280`.
- Çıktı `results/render/final_short.mp4` (+ slot 2..N için `output_NN/`),
  manifest `results/render/render_manifest.json`.
- Teknik doğrulama geçmeden ve tüm üst-akış onayları tamamlanmadan final onayı
  verilemez.

## Faz 6 · Yayın

`local_pipeline/metadata_studio.py` (501) + `publish.py` (563) +
`publish_youtube.py` (800).

Üç ayrı insan kapısı:

1. **Metadata onayı** — başlık/açıklama/etiket varyantları üretilir veya elle
   yazılır, biri seçilir, onaylanır (`publication_metadata`).
2. **Private upload onayı** — `publish_intent`, kararı SHA-256'ya bağlar.
   Yükleme **daima `private`** başlar (`INITIAL_PRIVACY = 'private'`).
   OAuth 2.0 + resumable upload.
3. **Görünürlük onayı** — `unlisted`/`public` gibi izleyiciye açan görünürlükler
   (`AUDIENCE_EXPOSING_VISIBILITIES`) ayrı, açık bir onay gerektirir.

Planlanan platformlar (`PLANNED_PLATFORMS`): `tiktok`, `instagram`, `facebook`,
`x` — sözleşme sağlayıcı-bağımsızdır ama yalnızca YouTube kodludur.

## Araştırma yan kolu (ana hattı beslemez)

- `youtube_data.py` (1028) — salt-okunur YouTube Data API v3 keşif, kanal
  geçmişi, SHA-256 anahtarlı TTL cache, kota tahmini.
- `viral_prediction.py` (187) — istatistiksel taban çizgisi; `MIN_HISTORY_ROWS`
  altında `insufficient_data` döner, güvenli tahmin üretmez.
- `stock_media.py` (574) — sağlayıcı-nötr stok video arama ve denetlenebilir
  B-roll planı (yalnızca açık düğme ile arar).
- `music_library.py` (204) — arka plan müziği ve **onunla seyahat etmesi gereken
  lisans bilgisi**. Eksik sidecar'lı parça listelenir ama "lisans doğrulanmadı"
  olarak işaretlenir; bu proje hiçbir parçaya "royalty-free" demez.
