# 07 · Sağlayıcılar, Model Router ve Sırlar

Kaynak: `@local_pipeline/google_ai.py` (2717
satır), `provider_adapters.py` (839), `creator_console/secrets_ref.py`,
`env_file.py`, `redaction.py`

## Tek kanonik router

Bu repodaki **her** Google çağrısı tek bir modülden geçer. Hiçbir notebook hücresi
`genai.Client(...)` kurmaz veya `model='gemini-…'` sabitlemez; geri gelirse bir
regresyon testi (`[ga:no-hardcoded-gemini-in-notebooks]`) başarısız olur.

## Görev yönlendirme tablosu

| Görev | Rota (sırayla denenir, ilk başarıda durur) | thinking_level |
|---|---|---|
| `hotmoment_review`, `script_generation`, `consistency_review`, `quality_audit` | `gemini-3.6-flash` → `gemini-3.5-flash` → `gemini-3.5-flash-lite` → `gemma-4-31b-it` | medium / low / none |
| `bulk_classification`, `viral_interpretation`, `metadata_generation`, `transcript_cleanup` | `gemini-3.5-flash-lite` → `gemini-3.1-flash-lite` → `gemma-4-26b-a4b-it` | minimal |
| `grounded_research` | `gemini-2.5-flash` + Google Search grounding | low |
| `tts` | `gemini-3.1-flash-tts-preview` → `gemini-2.5-flash-preview-tts` | — |
| `embeddings` | `gemini-embedding-2` (`embed_content`, 768 boyut) | — |
| `antigravity_research` | `antigravity-preview-05-2026` (deneysel, varsayılan **kapalı**) | — |

Başarılı ilk çağrı rotayı **bitirir**. Üretim yolu bir karşılaştırma kataloğunu
baştan sona dolaşmaz; kasıtlı çoklu-model kıyaslaması yalnızca bağımsız
notebook'larda (02/03) ve açıkça vardır.

**Asla otomatik yönlendirilmeyenler:** bu proje için sıfır ücretsiz kotayla
yapılandırılmış modeller (`gemini-2.5-pro`, `gemini-3.1-pro-preview`, görsel
modelleri). `MODEL_REGISTRY`'de yalnızca dışlamanın **denetlenebilir** olması için
kalırlar.

## Yetenek keşfi

`client.models.list()` sonucu açık bir allowlist ile **kesiştirilir**, ardından
sıfır kotalı modeller düşürülür — çünkü `models.list()` tek başına bir modelin
kullanılabilir ücretsiz kotası olduğunu kanıtlamaz. Sanitize edilmiş sonuç
`results/google_capability_manifest.json` içinde yapılandırılabilir bir TTL ile
önbelleklenir; yeniden koşu kataloğu tekrar yoklamaz.

Tek seferlik smoke test (`RUN_GOOGLE_CONNECTION_TEST = True`) yalnızca seçili
görevlerin **birincil** modeline ping atar, tüm kataloğa değil ve anahtarı asla
yazdırmaz.

## Hata politikası

| Koşul | Davranış |
|---|---|
| RPM 429 | `Retry-After`'a uy, yoksa jitter'lı exponential backoff (sınırlı); sonra sıradaki model |
| RPD / günlük 429 | **Uzun uyku yok** — model Pasifik gece yarısına dek kullanılamaz işaretlenir, sıradakine geçilir |
| 401 / 403 | Kimlik bilgisi tüm koşu için devre dışı; eyleme dönük, sanitize mesaj; durur |
| 404 / model yok | Yalnızca o model devre dışı; allowlist'te devam |
| 5xx / timeout | Sınırlı retry, sonra fallback; 3 ardışık geçici hatada **devre kesici** açılır (15 dk soğuma → yarı-açık prob → başarıda kapanır) |
| Geçersiz yapılandırılmış çıktı | Aynı modelde **tam bir** düzeltici retry, sonra fallback |
| Güvenlik reddi | **Durur** — güvenliği aşmak için model/sağlayıcı değiştirilmez |
| Her şey tükendi | `insufficient_data` / `failed`, devam ettirilebilir durum korunur, çıktı **asla** uydurulmaz |

Yapılandırılmış çıktı iki kez doğrulanır: şema/sözdizimi, sonra **anlam** —
`{"script": ""}` geçerli JSON'dur ve yine reddedilir.

## Kota defteri

`results/google_quota_usage.json` — açıkça `estimated: true` etiketli **yerel
tahmin**; yetkili sunucu kotası hakkında bir iddia değildir (onu yalnızca canlı
429'lar ve AI Studio söyler).

Model başına tutar: mevcut dakikadaki istek/token, mevcut proje günündeki istek,
başarı/başarısızlık, son çağrı, son 429, soğuma, yapılandırılmış RPM/TPM/RPD,
tahmini kalan RPD ve varsa sunucunun bildirdiği `usage_metadata`.

- Atomik yazma + kilit dosyası ile kalıcıdır → hücreyi yeniden koşmak kullanımı
  unutturmaz.
- Günlük sayaçlar **Pasifik gece yarısı**nda döner.
- Yerel tahmin bir limitin bittiğini kanıtlıyorsa **çağrı hiç denenmez**.
- Elle sıfırlama mümkün ama **açık bir gerekçe zorunludur** ve kaydedilir; yalnızca
  yerel tahmini düzeltir, sunucu kotasını asla sıfırlayamaz.

## Kota atlatma yasağı

Oran limitleri **anahtar başına değil proje başına** uygulanır; aynı projede
ikinci bir anahtar hiçbir limiti yükseltmez. RPD/RPM/TPM'den kaçmak için ek
hesap/proje açmak kota atlatmadır ve bu repo bunu yasaklar:

- `ALLOW_ACCOUNT_QUOTA_ROTATION = False`
- `google_ai.assert_no_account_quota_rotation()` ile zorlanır
- anahtar/hesap rotasyonu eklenirse başarısız olan bir regresyon testi
  (`[ga:no-account-rotation]`)

Router **tek proje içinde model** değiştirir, ileride **sağlayıcı** değiştirebilir
— asla hesap değiştirmez.

## Kimlik profilleri

`GOOGLE_CREDENTIAL_PROFILE` / `YOUTUBE_CREDENTIAL_PROFILE` ile **manuel** seçilen
`secondary` profiller desteklenir (`GeminiKeySecondary`, `YoutubeKEYSecondary`).
429 sonrası başka profile **otomatik geçilmez**. Profil etiketleri manifestlere
yazılır, sır değerleri özel kalır. Yetenek cache'i ve kota defteri
profil-kapsamlı dosya adları kullanır, böylece meşru proje/kanal ortamları
birbirinin yerel tahminini kirletmez.

## Ücretsiz katman veri politikası

Ücretsiz katmanda gönderilen içerik Google tarafından ürün geliştirmede
kullanılabilir ve insan gözden geçiricilerce okunabilir. Bu yüzden
`ALLOW_RAW_MEDIA_TO_FREE_TIER = False`: ham (telifli) kaynak video/ses
**yüklenmez**. Yerel FFmpeg / faster-whisper / PySceneDetect / librosa ilk analiz
katmanı olarak kalır; yalnızca türetilmiş transcript pencereleri, zaman damgaları,
sahne/ses skorları ve aday metadata'sı gönderilir. Ham medya yüklemesi açık bir
opt-in **artı** gerekli hakların bulunduğuna dair onay ister. Her artefakt
`raw_media_submitted` ve veri kullanım bildirimini kaydeder.

## Çapraz sağlayıcı yedekleme (opsiyonel, kimlik-korumalı)

| Yetenek | Sıra |
|---|---|
| Yüksek kaliteli metin | Google Rota A → Groq `openai/gpt-oss-120b` → Cloudflare `@cf/openai/gpt-oss-120b` |
| Yüksek hacimli metin | Google Rota B → Groq `qwen/qwen3.6-27b` → Cloudflare çok dilli Qwen |
| Embedding | `gemini-embedding-2` → Cloudflare `@cf/baai/bge-m3` → yerel çok dilli sentence-transformers |
| Transkripsiyon | **yerel faster-whisper (birincil, değişmez)** → Groq `whisper-large-v3-turbo` → Cloudflare `@cf/openai/whisper-large-v3-turbo` |
| TTS | Gemini TTS rotası → Azure Neural `tr-TR-AhmetNeural` / `tr-TR-EmelNeural` |

- Eksik kimlik bilgisi **hata değildir**: adaptör `skipped` bildirir, router devam
  eder.
- Hiçbir adaptör başarılı bir sonuç **uydurmaz**.
- Metin fallback'leri artefakta dönüşmeden önce **aynı script JSON sözleşmesini**
  doğrulamak zorundadır.
- Güvenlik reddi terminaldir ve asla sağlayıcı değişimine yol açmaz.
- Ham ses otomatik olarak bir bulut sağlayıcısına gönderilmez.
- Cloudflare model kimlikleri sık değişir — güvenmeden önce yeniden doğrulanmalıdır.

## Sır yönetimi

### İsim eşlemeleri (`SECRET_ALIASES`)

| Amaç | Aranan adlar (sırayla) |
|---|---|
| Google/Gemini | `GeminiKey`, `GOOGLE_API_KEY` |
| YouTube Data | `YoutubeKEY`, `YOUTUBE_DATA_API_KEY` |
| Groq | `GROQ_API_KEY` |
| NVIDIA | `NVIDIA_API_KEY` |
| Cloudflare | `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_AUTH_TOKEN` |
| Azure Speech | `AZURE_SPEECH_KEY`, `AZURE_SPEECH_REGION` |

Gemini ve YouTube **iki farklı anahtardır**; biri diğerinin yerine çalışmaz.
Opsiyonel (yoksa temiz atlanır): `TMDB_API_KEY`, `OPENAI_API_KEY`,
`ANTHROPIC_API_KEY`, `ELEVENLABS_API_KEY`, `PEXELS_API_KEY`, `PIXABAY_API_KEY`.

### `.env.local` yükleyicisi (`env_file.py`)

- Bağımlılıksız, minimal okuyucu; dosya **yalnızca sunucu sürecine** yüklenir.
- İşletim sistemi ortam değişkeni öncelikli; dosya mevcut değeri **ezmez**.
- Bilinmeyen değişkenler ve bozuk satırlar sessizce kabul **edilmez**; Console açık
  bir hata mesajıyla başlamayı reddeder.
- `.env.local` Git tarafından yok sayılır.

### Değerler nereye gitmez

`secrets_ref.py` sunucu tarafı **referans** tutar; değerler süreci terk etmez.
Tarayıcıya yalnızca `configured` / `missing` gider. GitHub token'ı Ayarlar'dan
girilebilir; değer yalnızca `.env.local`'e atomik yazılır ve API yanıtında, DOM'da,
logda veya proje artefaktlarında geri gösterilmez.

`redaction.py` süreç-yerel maskeleme kaydıdır: aynı token bir istisnada, sunucu
günlüğünde ve iş kaydında maskeli görünür — `verify_faults.py` bunu senaryo
senaryo doğrular.
