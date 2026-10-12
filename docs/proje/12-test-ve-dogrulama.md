# 12 · Test ve Doğrulama

Projede **iki** test kültürü var: klasik `pytest` suiti ve kök dizindeki
`verify_*.py` doğrulama betikleri (35 dosya, ~21.000 satır).

## pytest suiti

```bash
python -m pytest tests/ -q          # 128 test
python -m pytest tests/test_scene_editor.py -q
```

| Dosya | Kapsam |
|---|---|
| `conftest.py` | Paylaşılan fixture'lar ve **sağlayıcı dublörleri** |
| `test_scene_editor.py` | EDL modeli, filtergraph derleyicisi, komut kurulumu |
| `test_publish.py` | Yayın sözleşmesi ve kapıları |
| `test_metadata_studio.py` | Metadata üretimi/seçimi |
| `test_trend_ingest.py` | Aday keşfi, hak kapısı, alım |
| `test_subtitle_edit.py` | Altyazı izi düzenleme |
| `test_http_media.py` | Medya sunumu |

`conftest.py` politikası nettir: **buradaki her sağlayıcı bir dublördür.** Hiçbir
test ağ çağrısı yapmaz, gerçek kimlik bilgisi okumaz, üçüncü taraf medya
indirmez, hiçbir şey yüklemez. `yt_dlp` yerine `FakeYtDlpModule` enjekte edilir.

### Scene editor regresyon testleri

Gerçek koşuda bulunan hatalar için eklenenler:

| Test | Neyi korur |
|---|---|
| `test_preview_filtergraph_labels_are_unique` | Klip başına tekil pad etiketleri; `[framed]` çakışması geri gelmesin |
| `test_preview_filtergraph_normalises_streams_before_concat` | `setsar=1` + `fps=30` normalizasyonu (SAR uyuşmazlığı) |
| `test_preview_filtergraph_draws_caption_at_requested_position` | drawtext konumu, rengi, boyutu, çerçeve, gölge |
| `test_preview_filtergraph_places_broll_at_requested_position` | B-roll overlay konum ifadesi |

## `verify_*.py` doğrulama betikleri

Her biri bağımsız çalışan, kendi izole temp fixture'larını kuran ve sonunda özet
basan betiklerdir. En büyükleri:

| Betik | Satır | Kapsam |
|---|---:|---|
| `verify_local.py` | 4374 | Tüm pipeline suiti; notebook JSON + `ast.parse`, gömülü payload senkronu, sır taraması |
| `verify_turbo_studio.py` | 2075 | Turbo'nun 14 aşaması |
| `verify_browser.py` | 1705 | **Gerçek Chromium** (Playwright) UI testleri |
| `verify_console.py` | 1444 | Konsol test suiti (15 bölüm) |
| `verify_faults.py` | 1071 | Fault-injection + çok süreçli worker (10 aile, 70 senaryo) |
| `verify_render_profile.py` | 914 | Sunum ayarları |
| `verify_transcript_editor.py` | 823 | Transcript düzenleme |
| `verify_acceptance.py` | 783 | Kabul kriterleri |
| `verify_long_form_studio.py` | 741 | Uzun form 12 aşaması |
| `verify_diagnostics.py` | 591 | İşlevsel tanılama |

Diğerleri: `verify_batch_outputs`, `verify_cancel_long_video`,
`verify_clip_bounds`, `verify_colab_console`, `verify_content_language`,
`verify_copyright_gate`, `verify_forms_progress`, `verify_large_upload`,
`verify_lifecycle_browser`, `verify_live_script`, `verify_live_tts`,
`verify_long_form_pilot`, `verify_long_form_planner`, `verify_lyrics_review`,
`verify_lyrics_training`, `verify_mkv_support`, `verify_project_archive`,
`verify_project_lifecycle`, `verify_real_preview`, `verify_revision_flow`,
`verify_secret_entry`, `verify_short_poc`, `verify_stock_media`,
`verify_turbo_browser`, `verify_voice_review`.

### Sık kullanılan komutlar

```bash
python verify_console.py           # konsol suiti (15 bölüm)
python verify_faults.py            # fault-injection + worker (70 senaryo)
python verify_browser.py           # gerçek Chromium UI (42 kontrol)
python verify_local.py             # tam pipeline suiti
python verify_project_archive.py   # harici disk arşiv akışı
```

Tarayıcı testleri için bir kez:

```bash
pip install playwright
python -m playwright install chromium
```

## `verify_faults.py` ne kanıtlar

10 aile, 70 senaryo. Örnek kontrol başlıkları (gerçek çıktıdan):

```
OK      hata govdelerinde ic ayrinti
OK      katalog mesajlari statik mi
OK      SecretResolver'i ATLAYAN bir token istisnada
OK      ayni token sunucu gunlugunde
OK      onay hash'i (public, kullaniciya gosterilir)
OK      ayni token is kaydinda
toplam 70 senaryo, 70 gecti, 0 basarisiz

FAULT_INJECTION_SUITE_OK (izole temp fixture'lar, sahte saglayicilar; hicbir
gercek API cagrilmadi, hicbir kullanici dosyasi degistirilmedi, hicbir sey
yuklenmedi/yayinlanmadi)
```

Yani: sırların istisna gövdesine/loga/iş kaydına sızmadığı, hata kataloğunun
statik kaldığı ve depolama hatalarının (`faults.py` ile deterministik enjekte
edilerek) doğru sınıflandırıldığı senaryo senaryo doğrulanır.

## Frontend testleri

```bash
cd apps/web && npm test        # vitest run
```

`src/App.test.jsx` sekme geçişlerini ve Render Atölyesi ekranının açılışını
doğrular. **Dikkat:** Vitest `globals: true` ile yapılandırılmadığı için
Testing Library'nin otomatik `cleanup`'ı çalışmaz; test dosyası bu yüzden
`afterEach(cleanup)` çağırır. Ayrıca ekran başlığı ile nav butonu aynı metni
taşıdığından sorgular `getByRole('button', { name: … })` ile daraltılır.

## Test suitinin gerçekte kanıtladığı şey

`verify_local.py` üç farklı kontrol türünü karıştırır — etiketleri okumak
gerekir:

| Tür | Kapsam |
|---|---|
| **Gerçekten yerel araçlarla çalıştırılan** | ffmpeg/ffprobe medya probing, faster-whisper transkripsiyon, PySceneDetect, librosa, tam render smoke testi, WAV ayrıştırma, dosya hash'leme, JSON/artefakt IO |
| **Mock (ağ yok, kimlik yok)** | Her Google, Groq, Cloudflare, Azure, YouTube, OpenAI, Anthropic, ElevenLabs çağrısı. İstemciler ve HTTP taşıyıcıları enjekte edilmiş dublördür; kimlik bilgileri gerçek olmayan sahte string'lerdir; fixture'lar sonradan silinen temp dizinlerde yaşar |
| **Statik / yalnızca sözdizimi** | Notebook JSON + `ast.parse` doğrulaması, gömülü payload SHA-256 senkronu, izlenen dosyalarda sır taraması, hiçbir notebook hücresinin Gemini modelini sabitlemediğinin veya `genai.Client` kurmadığının kontrolü |

> Her sağlayıcı çağrısı mock olduğu için **yeşil bir koşu gerçek kimlik
> bilgilerinizin çalıştığını kanıtlamaz.** Bunun için master notebook'u gerçek
> sırlarla Colab'da koşup ortaya çıkan artefakt zincirini elle incelemek gerekir.

## Ortam notu

Depo kökündeki sistem `python3` altında `numpy` **kurulu değildir**; bu yüzden
`import creator_console.pipeline_service` doğrudan çalışmaz. `pytest` suiti
geçtiği için testler bu bağımlılığı ya kullanmaz ya da dublörle karşılar. Tam
pipeline için `requirements-local.txt` ile bir `.venv` gerekir
(bkz. [13](13-kurulum-ve-calistirma.md)).
