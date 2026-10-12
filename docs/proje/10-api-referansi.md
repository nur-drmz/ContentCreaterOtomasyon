# 10 · API Referansı

Kaynak: `@creator_console/api.py` (1236
satır). `ConsoleApi.handle(method, path, body, headers)` → `(status, payload)`.

## Yol şeması

```
/                                  → statik konsol (index.html)
/static/...                        → statik varlıklar
/api/<global>                      → workspace düzeyi
/api/projects                      → proje oluşturma
/api/projects/<project_id>         → proje kökü
/api/projects/<project_id>/<rest…> → tüm proje uç noktaları
```

`parts[2]` proje kimliğidir ve `assert_owned()` ile doğrulanır; `rest = parts[3:]`
alt yolu verir.

## Ortak sözleşmeler

| Konu | Kural |
|---|---|
| Aktör | `X-Actor` başlığı → yoksa gövdedeki `actor` → yoksa `"console"` / proje aktörü |
| Zorunlu alan | `_required(payload, "alan")`; eksikse `missing_required_field` + `fields[]` |
| Slot | `?slot=N` veya gövdedeki `slot`; çoklu çıktı yuvası |
| Yanıt başlıkları | `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `Cache-Control: no-store` |
| Medya | `__raw__` / `__file__` özel yükleri; dosya yanıtlarında `Accept-Ranges: bytes` |
| Hata | Kapalı katalog + `debug_id`; iç ayrıntı gövdeye konmaz |

## Global uç noktalar

| Metot | Yol | Açıklama |
|---|---|---|
| GET | `/api/health` | Canlılık |
| GET | `/api/preflight` | Yerel ön kontroller (modüller, disk, ffmpeg) |
| GET | `/api/diagnostics` | Tanılama durumu |
| POST | `/api/diagnostics` | Tanılamayı çalıştır |
| GET | `/api/workspace` | Workspace görünümü, proje listesi |
| POST | `/api/settings/secrets` | Sır kaydet (`kind`, `secret_value` ≥20 karakter) — değer geri dönmez |
| POST | `/api/projects` | Proje oluştur (`title`, `operator_name` zorunlu) |

## Proje yaşam döngüsü

| Metot | Yol | Açıklama |
|---|---|---|
| GET | `/` (proje kökü) | Proje view model |
| PATCH | `/` | Ad/not güncelle |
| DELETE | `/` | Çöp kutusuna taşı (geri alınabilir) |
| POST | `/archive` | Harici diske arşivle |
| POST | `/lifecycle` | Yaşam döngüsü eylemi (`action`) |
| GET | `/revision` | Bir revizyonun **neyi arşivleyeceğini** anlatır (kör "emin misiniz?" yerine dosyaları adlandırır) |
| POST | `/revision` | Rehberli revizyon uygula |
| POST | `/revision/purge` | Arşivi temizle (`revision_id` + `confirmation`) |
| GET | `/audit` | Audit geçmişi |

## Kaynak ve Faz 0

| Metot | Yol | Açıklama |
|---|---|---|
| POST | `/source` | Kaynak video yükle (akış, 8 MB parça; `X-Filename`) |
| POST | `/source-library` | Sunucu tarafı kütüphaneden içe aktar (`filename`) |
| POST | `/plan` | İşleme planı oluştur (`mode` zorunlu) |
| PATCH | `/plan` | Planı güncelle |
| GET | `/plan/review` | Plan inceleme açıklaması |

## Analiz, moment, transcript

| Metot | Yol | Açıklama |
|---|---|---|
| POST | `/moment-selection` | Moment seçimini kaydet (slot bazlı) |
| GET | `/clip-bounds` · POST | Klip başlangıç/bitiş ince ayarı |
| GET | `/transcript` | Transcript görünümü |
| POST | `/transcript/segments` | Segment bazlı düzenleme |
| POST | `/transcript/full-text` | Tam metin değiştirme (`full_text`) |
| POST | `/transcript/corrections` | Düzeltmeler |
| POST | `/transcript/exclusions` | Segment dışlama (`segment_ids`) |
| POST | `/transcript/restore-asr` | ASR çıktısına geri dön |
| GET | `/glossary` · POST | Proje sözlüğü (özel adlar) |

> Segment bazlı ve tam-metin düzenlemeleri **tek** sunucu işlemidir, iki giriş
> şekliyle: textarea, satır editörünün zorladığı bir doğrulamayı atlayamaz.

## Script, ses, render

| Metot | Yol | Açıklama |
|---|---|---|
| PATCH | `/script` | Script metnini düzenle (+ onay durumu döner) |
| PATCH | `/subtitles` | Altyazı cue'larını düzenle (boş liste reddedilir) |
| GET | `/render-slot` | Yuva bilgisi |
| GET | `/render-profile` · POST | Sunum ayarları (render_mode, caption_style…) |
| GET | `/render-review` · POST | Render inceleme listesi |
| GET | `/voice-review` · POST | Ses inceleme listesi |
| GET | `/copyright-review` · POST | Telif inceleme listesi |
| POST | `/approvals/<kind>` | Onay ver (SHA-256 bağlı) |

## İşler, ilerleme, otomasyon

| Metot | Yol | Açıklama |
|---|---|---|
| GET | `/jobs` | İş listesi |
| POST | `/jobs` | İş gönder (idempotency_key) |
| GET | `/jobs/<job_id>` | Tek iş |
| POST | `/jobs/<job_id>/retry` | Yeniden dene |
| POST | `/jobs/<job_id>/cancel` | İptal isteği |
| GET | `/progress` | Sunucuda kalıcı ilerleme |
| GET | `/preflight` | Proje bazlı ön kontrol |
| GET | `/automation` · POST | Otomatik akış durumu/kontrolü |
| POST | `/auto-run` | Sıradaki güvenli makine fazını çalıştır |

## Çıktılar ve batch

| Metot | Yol | Açıklama |
|---|---|---|
| GET | `/outputs` | Üretilen Short'lar, manifestler, hata ayrıntısı |
| GET | `/batch` · POST | Çoklu çıktı panosu (yuva bazlı durum ve eylem) |
| GET | `/media/...` | Medya sunumu (range destekli) |

## Scene Editor / EDL

| Metot | Yol | Gövde |
|---|---|---|
| POST | `/edl` | `source_path`, `source_duration`, `title` |
| GET | `/edl` | Mevcut EDL |
| POST | `/edl/clips` | `source_path`, `source_start_sec`, `source_end_sec` |
| PATCH | `/edl/clips/<index>` | `start`, `end`, `crop`, `zoom`, `caption_style`, `b_roll`, `add_b_roll` |
| POST | `/edl/music` | `music_path` |
| POST | `/edl/preview` | `output_name` → ffmpeg komut listesi |
| POST | `/edl/render` | `output_name` → gerçek render (`returncode`, `stdout`, `stderr`) |
| POST | `/edl/revision` | `notes` → EDL; `revision_notes`, `revision_requested_by` dolanır, `version` artar |

`PATCH /edl/clips/<index>` alan alan uygulanır: `start`+`end` birlikte verilirse
`set_clip_source_bounds`, `b_roll` verilirse tüm liste değiştirilir, `add_b_roll`
verilirse tek B-roll eklenir.

## Trend alımı (Faz -1)

| Metot | Yol | Açıklama |
|---|---|---|
| POST | `/source-candidates` | `terms`, `manual_urls`, `max_search_calls`, `lookback_days`, `shorts_only` |
| POST | `/source-candidates/probe` | Adayın teknik uygunluğu |
| POST | `/rights` | **Zorunlu hak kararı** (lisans türü, atıf, dönüşüm notu) |
| POST | `/ingest` | İndirme (yt-dlp) |
| GET | `/ingest/state` | Alım durumu |

## Yayın

| Metot | Yol | Açıklama |
|---|---|---|
| GET | `/metadata` | Metadata görünümü |
| POST | `/metadata/generate` | Varyant üret |
| POST | `/metadata/manual` | Elle yaz |
| POST | `/metadata/select` | Varyant seç |
| POST | `/metadata/approve` | **Kapı 1** |
| GET | `/publish/state` | Yayın durumu |
| POST | `/publish/intent` | Yükleme niyeti |
| POST | `/publish/intent/approve` | **Kapı 2** (private upload) |
| POST | `/publish/upload` | Resumable upload (daima `private`) |
| POST | `/publish/visibility/approve` | **Kapı 3** (görünürlük) |
| POST | `/publish/visibility/apply` | Görünürlüğü uygula |

## Long-form Studio

| Metot | Yol |
|---|---|
| GET | `/long-form` (yalnızca **tanımlar**; ekranı açmak sağlayıcı isteği harcamaz) |
| POST | `/long-form/control` · `/long-form/stage` · `/long-form/chapter` · `/long-form/tts` · `/long-form/broll` |
| PATCH | `/long-form-plan` (`topic` zorunlu) |

## Turbo Studio

| Metot | Yol |
|---|---|
| GET | `/turbo` · `/turbo/revision-preview` |
| POST | `/turbo/brief` · `approve` · `stage` · `media` · `music` · `music/upload` · `render` · `revision` |

## FastAPI adaptörü

`@services/api/main.py` — tüm trafiği
`ConsoleApi.handle`'a devreder. Ortam değişkenleri:

| Değişken | Varsayılan |
|---|---|
| `CONSOLE_WORKSPACE` | `wsp_default` |
| `CONSOLE_USER` | `usr_api` |
| `CONSOLE_ROOT` | `./data/console` |
| max upload | 4 GiB |

Süreç başına bir `ConsoleApp` (lifespan), `skip_startup_recovery: True`.
`GET /health` → `{"ok": true, "service": "api"}`.

**CORS middleware yoktur** — tarayıcı istemcileri aynı origin veya bir proxy
kullanmak zorundadır.
