# 11 · Modül Referansı

Satır sayıları ölçülmüştür (docstring'lerin ilk satırı modülün kendi tanımıdır).

## `local_pipeline/` — kanonik faz mantığı

Notebook'lar ve Creator Console **aynı** fonksiyonları çağırır; ayrışan kopya yok.

| Modül | Satır | Sorumluluk |
|---|---:|---|
| `google_ai.py` | 2717 | **Kanonik** görev-farkında Google (Gemini/Gemma) router'ı: sır alias'ları, istemci kurulumu, model registry + kotalar, yetenek keşfi, görev rotaları, kota defteri, retry/backoff/devre kesici, yapılandırılmış çıktı doğrulama, sanitize hatalar |
| `render_utils.py` | 1835 | FFmpeg render yardımcıları: dikey filtergraph, ffprobe/ffmpeg fallback probing, altyazı üretimi, tam render + onay kapısı |
| `video_analysis.py` | 1366 | Klip/ses çıkarma, transkripsiyon, sahne tespiti, ses tepeleri, hot-moment skorlama, `hot_moments.json` şeması |
| `youtube_data.py` | 1028 | Salt-okunur YouTube Data API v3 toplama, TTL cache, kota tahmini, kimlik sanitizasyonu |
| `source_strategy.py` | 984 | **Faz 0**: kaynak değerlendirme, işleme stratejisi planlama, mod politikası, içerik dili |
| `provider_adapters.py` | 839 | Opsiyonel, kimlik-korumalı çapraz sağlayıcı adaptörleri (Groq/Cloudflare/Azure) |
| `publish_youtube.py` | 800 | YouTube yayın sağlayıcısı: OAuth 2.0 + resumable upload + görünürlük değişimi |
| `trend_ingest.py` | 690 | **Faz -1**: trend odaklı kaynak alımı + zorunlu hak kapısı |
| `timeline.py` | 653 | Klip listesi + anlatım → tek video; paylaşılan motor |
| `script_generation.py` | 620 | Prompt kurulumu, LLM yanıt ayrıştırma, `selected_script.json` şeması |
| `scene_editor.py` | 618 | **EDL** modeli ve preview derleyicisi (bkz. [08](08-render-ve-scene-editor.md)) |
| `stock_media.py` | 574 | Sağlayıcı-nötr stok video arama + denetlenebilir B-roll planı |
| `publish.py` | 563 | Sağlayıcı-bağımsız yayın sözleşmesi ve insan kapıları |
| `metadata_studio.py` | 501 | Başlık/açıklama/etiket: üretilmiş veya elle yazılmış |
| `tts_chunks.py` | 487 | Uzun anlatım için devam ettirilebilir, parça bazlı TTS |
| `long_form_script.py` | 446 | Outline-first uzun form script üretimi |
| `transcript_edit.py` | 444 | İncelenebilir transcript: segmentler, insan düzenlemeleri, revizyon geçmişi |
| `turbo.py` | 459 | Turbo Studio'nun şekli, kuralları ve tahminleri (yan etki yok) |
| `approval.py` | 414 | **Kanonik** üretim-sonrası insan onay kapısı |
| `glossary.py` | 399 | Proje sözlüğü: tanıyıcının kaçırdığı özel adlar ve alan ifadeleri |
| `lyrics_review.py` | 371 | Şarkı/söz için muhafazakâr altyazı önerileri |
| `tts.py` | 351 | TTS model registry'si, sağlayıcı çağrıları, `selected_voiceover.json` şeması |
| `stage_machine.py` | 259 | Kalıcı, devam ettirilebilir aşama makinesi (tüm uzun workflow'lar paylaşır) |
| `long_form.py` | 253 | Uzun form (15–20 dk) workflow: aşamalar, durum, tahminler |
| `lyrics_training.py` | 243 | Gelecek bir söz sınıflandırıcısı için insan etiketli örnekler |
| `music_library.py` | 204 | Arka plan müziği + onunla seyahat etmesi gereken lisans bilgisi |
| `turbo_script.py` | 204 | Turbo'nun script'i: tek yönlendirilmiş çağrı, anlatım **ve** sahne beat'leri |
| `viral_prediction.py` | 187 | İstatistiksel viral tahmin taban çizgisi + holdout değerlendirme |
| `narration_presets.py` | 161 | Adlandırılmış anlatım stilleri: registry'den ses + teslim yönergesi |
| `subtitle_edit.py` | 138 | Render edilmiş anlatım altyazılarından insan-düzenlenebilir izler |

## `creator_console/` — konsol uygulaması

| Modül | Satır | Sorumluluk |
|---|---:|---|
| `pipeline_service.py` | 4310 | **`local_pipeline`'ın çağrıldığı TEK yer**; artefakt IO, şema doğrulama, tüm faz giriş noktaları |
| `turbo_studio.py` | 1530 | Turbo'nun 14 aşamasını gerçekten çalıştıran servis |
| `app.py` | 1339 | Uygulama çekirdeği: yapılandırma, iş yürütme, view model'ler |
| `api.py` | 1236 | HTTP yüzeyi. **Yalnızca taşıma** — pipeline mantığı yok, sır değeri yok |
| `long_form_studio.py` | 1010 | Uzun formun 12 aşamasını çalıştıran servis |
| `diagnostics.py` | 879 | İşlevsel tanılama: her kullanıcıya dönük yetenek gerçekten çalışıyor mu? |
| `jobs.py` | 588 | Kalıcı iş modeli, store ve takılabilir runner (idempotency, tek claim, lease) |
| `automation.py` | 493 | Sunucuda kalıcı otomatik akış |
| `progress.py` | 434 | Uzun fazlar için sunucuda kalıcı ilerleme |
| `status.py` | 428 | Yerel ön kontroller + pipeline iş akışı haritası |
| `server.py` | 322 | `ThreadingHTTPServer` adaptörü |
| `render_profile.py` | 286 | Faz 0 sonrası değiştirilebilen slot bazlı sunum ayarları |
| `media_review.py` | 285 | Digest'e bağlı insan inceleme listeleri altyapısı |
| `storage.py` | 268 | Sert namespace izolasyonlu depolama adaptörü |
| `tts_runner.py` | 258 | Anlatan her workflow'un paylaştığı tek devam ettirilebilir TTS geçişi |
| `voice_review.py` | 247 | İnceleme listesinin ses tarafı (dil ve preset'e uyarlanan sorular) |
| `errors.py` | 221 | Yapısal, kapalı katalog API hataları |
| `faults.py` | 219 | Deterministik depolama hata enjeksiyonu (yalnızca test) |
| `fixtures.py` | 208 | Deterministik fixture'lar + mock sağlayıcı istemcileri |
| `render_review.py` | 175 | İnceleme listesinin render tarafı (6 madde, düzeltme sekmesi/ipucu) |
| `env_file.py` | 166 | Yerel sır dosyası için minimal, bağımlılıksız yükleyici |
| `audit.py` | 139 | Append-only audit olayları, kapalı olay sözlüğü |
| `secrets_ref.py` | 137 | Sunucu tarafı sır **referansları**; değerler süreci terk etmez |
| `ids.py` | 138 | Sahiplik kimlikleri ve her yol parçasının uyması gereken güvenli-ad kuralları |
| `worker.py` | 149 | Ayrı süreçli iş worker'ı |
| `quota_signals.py` | 128 | Sağlayıcı reddini zamanlama kararına çevirme (tek yerde, herkes için) |
| `redaction.py` | 123 | Süreç-yerel sır maskeleme kaydı |
| `copyright_review.py` | 123 | İnceleme listesinin telif/dönüştürücü kullanım tarafı |
| `__main__.py` | 112 | Belgelenmiş başlatma komutu |
| `__init__.py` | 38 | Paket tanımı (pre-alpha M0.9) |

### Statik frontend

| Dosya | Boyut |
|---|---|
| `static/app.js` | ~270 KB — build-step'siz ES modülleri |
| `static/app.css` | ~38 KB — CSS custom property'leri |
| `static/index.html` | ~1.5 KB |

## `apps/web/` — React iskeleti

| Dosya | İçerik |
|---|---|
| `src/App.jsx` | 4 sekme: Trend Radar · Sahne Editörü · Yayın · Render Atolyesi |
| `src/screens/RenderAtelierScreen.jsx` | Canva benzeri sürükle-bırak EDL editörü |
| `src/screens/SceneEditorScreen.jsx` | Basit EDL test ekranı (düğme bazlı) |
| `src/screens/TrendRadarScreen.jsx` | Aday keşfi |
| `src/screens/PublishScreen.jsx` | Yayın durumu + metadata üretimi |
| `src/App.test.jsx` | Vitest + Testing Library sekme testleri |
| `vite.config.js` | Port 3000, `/api` → `127.0.0.1:8000` proxy, jsdom test ortamı |

Bağımlılıklar: React 19, react-router 7; dev: Vite 6, Vitest 2, jsdom 25,
`@testing-library/react` 16.

## `services/api/` — FastAPI adaptörü

`main.py` (121 satır) — rota mantığı **yok**; `ConsoleApi.handle`'a devreder,
durum/yükü FastAPI yanıtına çevirir.

## `tools/`

| Dosya | İş |
|---|---|
| `refresh_embedded_payloads.py` | Notebook'lara gömülü modül snapshot'larını `local_pipeline/` ile senkron tutar |
| `build_colab_console_bundle.py` | Colab için küçük kaynak paketi üretir/doğrular |
| `nb_builder.py` | Notebook üretim yardımcıları |
| `StartCreatorConsole.cs` / `StopCreatorConsole.cs` | Windows çift tıklama başlat/durdur EXE kaynakları |

## Gömülü payload mekanizması

`local_pipeline` modülüne ihtiyaç duyan her notebook, o modülün **zlib + Base85 +
SHA-256** snapshot'ını taşır. Yükleme sırası: doğrudan import → Drive/ata dizin
kopyası → `PROJECT_REPO_URL` klonu → gömülü snapshot. Hash uyuşmazlığı bozuk kod
import etmek yerine **hata yükseltir**.

`local_pipeline/` altında bir şey değiştiğinde snapshot'lar bayatlar;
`verify_local.py` bunu yüksek sesle bildirir. Düzeltme:

```bash
python tools/refresh_embedded_payloads.py          # bayat payload'ları yerinde yaz
python tools/refresh_embedded_payloads.py --check  # yalnızca doğrula; bayatsa exit 1
```
