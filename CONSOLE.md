# Creator Console — pre-alpha (M0.9)

Yerel, **tek kullanıcılı**, **kimlik doğrulamasız**, **özel** bir web konsolu.
Notebook hücrelerini son kullanıcıya göstermeden kaynak video alımı, Faz 0 strateji
seçimi, moment inceleme, script/ses onayı ve render takibini tek arayüzden yönetir.

## Windows'ta çift tıklayarak açma

Proje kökündeki **`Start Creator Console.exe`** dosyasına çift tıklayın. Başlatıcı:

1. Projenin `.venv/Scripts/python.exe` dosyasını doğrular.
2. Console zaten açık değilse onu ayrı bir terminalde başlatır.
3. Hazır olmasını bekler.
4. Varsayılan tarayıcıda `http://127.0.0.1:8765/` adresini açar.

Açılan sunucu terminalini kapatmak Console'u da kapatır. EXE başka klasöre tek
başına taşınmamalıdır; proje kökünde `.venv` ve `creator_console` ile birlikte
durmalıdır. Derlenebilir kaynak `tools/StartCreatorConsole.cs` dosyasındadır.

Console'u kapatmak için proje kökündeki **`Stop Creator Console.exe`** dosyasına
çift tıklayın. Kapatıcı rastgele üretilmiş, yalnızca yerelde tutulan kontrol
anahtarını kullanarak bu Console örneğinden temiz kapanma ister; çalışan diğer
Python süreçlerini aramaz veya sonlandırmaz. Kaynağı `tools/StopCreatorConsole.cs`
dosyasındadır.

## Yerel API anahtarları

Proje kökündeki **`.env.local`** dosyasını bir metin düzenleyicide açın ve
yalnızca kullanacağınız anahtarların `=` sonrasını doldurun:

```dotenv
GeminiKey=BURAYA_GEMINI_ANAHTARI
YoutubeKEY=BURAYA_YOUTUBE_DATA_ANAHTARI
```

Dosyayı kaydedip Console'u kapatın ve yeniden başlatın. Console bu dosyayı
başlangıçta yalnızca sunucu sürecine yükler. Ayarlar ekranı değer yerine sadece
`configured` veya `missing` gösterir.

- `.env.local` Git tarafından yok sayılır.
- Dosyayı paylaşmayın, commit etmeyin veya ekran görüntüsüne almayın.
- İşletim sistemi ortam değişkeni ile aynı anahtar verilmişse ortam değişkeni
  önceliklidir; dosya mevcut değerin üzerine yazmaz.
- Bilinmeyen değişkenler ve bozuk satırlar sessizce kabul edilmez; Console açık
  bir hata mesajıyla başlamayı reddeder.

## Terminalden tek komut

```bash
python -m creator_console
```

Tarayıcıda `http://127.0.0.1:8765/` açılır. Kredi bilgisi, Colab veya ağ gerekmez.

Deterministik bir örnek projeyle (gerçek ama küçük sentetik MP4 + sahte sağlayıcılar)
başlamak için:

```bash
python -m creator_console --demo-fixtures
```

Faydalı bayraklar:

| Bayrak | Ne yapar |
|---|---|
| `--port 8765` | HTTP portu |
| `--storage-root ./.console_data` | Tüm proje/artefakt/iş verisinin kökü |
| `--inline-jobs` | İşleri arka plan thread'i yerine senkron çalıştır (hata ayıklama) |
| `--demo-fixtures` | Sentetik kaynak videolu örnek proje oluştur |
| `--user-id / --workspace-id` | Sahiplik kimliklerini değiştir |
| `--env-file DOSYA` | Varsayılan `.env.local` yerine başka yerel dosya kullan |
| `--no-env-file` | Yerel API dosyasını yükleme |

`--host` varsayılan `127.0.0.1`'dir. Loopback dışı bir adrese bağlanmak
**bilerek engellenmiştir**: pre-alpha'da kimlik doğrulama yoktur, ağa açmak
konsolu herkese açmak demektir.

## Kurulum

Ek bağımlılık **yoktur**. Konsol yalnızca Python standart kütüphanesini kullanır;
mevcut `.venv` (ffmpeg/faster-whisper/scenedetect/librosa) yeterlidir. Node.js,
npm veya build adımı gerekmez — frontend build-step'siz ES modülleri ve CSS
custom property'leridir.

```bash
python verify_console.py   # konsol test suiti (15 bolum)
python verify_faults.py    # fault-injection + cok surecli worker (10 aile, 70 senaryo)
python verify_browser.py   # gercek Chromium (Playwright) UI testleri (42 kontrol)
python verify_local.py     # mevcut pipeline suiti
```

Tarayici testleri icin bir kez:

```bash
pip install playwright
python -m playwright install chromium
```

## Ayri surecli worker

```bash
python -m creator_console.worker --storage-root ./.console_data --project-id prj_xxx
```

Worker bagimsiz bir OS surecidir: isleri ayni compare-and-set + lease ile claim
eder, calisirken lease'i heartbeat ile uzatir ve baslangic kurtarmasi
**yapmaz**. Sunucu yeniden baslatildiginda yalnizca sahibi kanitlanabilir
sekilde olmus isler kurtarilir; canli bir worker'in isi elinden alinmaz.

## Ekranlar

1. **Projeler** — proje oluşturma, kaynak video yükleme ve tıklanabilir proje adları
2. **Kayıtlar** — önceki projeleri açma, ad/not düzenleme ve kaldığı yerden devam etme
3. **Faz 0 · Strateji** — kaynak değerlendirmesi, iki mod skoru, ayrıntılı plan oluşturma/düzenleme ve hash onayı
4. **İş akışı** — queued / running / review_required / failed / completed + yeniden deneme
5. **Moment Explorer** — aday kartları, preview oynatma, çoklu seçim
6. **Script Studio** — Gemini rotasıyla üretim + hash onayı (yalnızca `narrated_theory`)
7. **Voice Studio** — Gemini TTS + hash onayı
8. **Render Studio** — render çalıştırma + teknik sonuç + final onay
9. **Çıktılar** — üretilen Short'lar, manifestler, hata ayrıntısı, audit geçmişi
10. **Ayarlar** — kimlik bilgisi durumu (**değer asla gösterilmez**), sahiplik, "planlandı" listesi

Bir proje açıkken sayfanın üstündeki **Otomatik akışı başlat** düğmesi sıradaki güvenli
makine fazını çalıştırır. Plan türü, moment ve hash onayları insan kararı olarak kalır;
otomasyon bu noktalarda durur ve kullanıcı işleminden sonra aynı düğmeyle devam eder.
Kaynak değerlendirmesi, video analizi, script, TTS ve render ayrıca kendi sekmelerinden
manuel olarak yeniden çalıştırılabilir.

## Mimari sınır

```
tarayıcı  →  api.py (HTTP)  →  app.py (view model + iş yürütme)
                                   ↓
                          pipeline_service.py  ←  TEK giriş noktası
                                   ↓
                            local_pipeline/*   ←  kanonik mantık
```

- Notebook mantığı frontend'e **kopyalanmaz**. Tarayıcı sunucunun ürettiği view
  model'i render eder; `local_pipeline` adı bile bundle'da geçmez (test eder).
- Uzun işler `jobs.py` üzerinden yürür; durum diskte tutulur, sayfa yenilemesi
  veya backend restart'ı işi kaybettirmez.
- Onaylar mevcut `local_pipeline.approval` SHA-256 kapılarından geçer; UI'da
  onayı atlatacak bir yol yoktur.

## Çok kullanıcılıya hazırlık (şimdiden var)

| Alan | Durum |
|---|---|
| `user_id` / `workspace_id` / `project_id` / `job_id` | Gerçek, doğrulanmış, her artefaktta |
| Depolama namespace'i | `<root>/workspaces/<ws>/projects/<prj>/` — kaçış testli |
| Storage adapter | `StorageAdapter` arayüzü; nesne depolama için yeniden yazım gerekmez |
| Audit olayları | Append-only JSONL, kapalı olay sözlüğü, sanitize edilmiş |
| Secret referansları | Sunucu tarafında; tarayıcıya yalnızca `configured`/`missing` |
| Job runner | `JobRunner` arayüzü; thread → harici worker geçişi API'yi bozmaz |
| Idempotency | `idempotency_key` + tek-claim lease |

## Bu sürümde OLMAYAN (sahte gösterilmez)

Kimlik doğrulama/RBAC · gerçek çok kiracılılık · faturalandırma · public deployment ·
otomatik YouTube yayını · nesne depolama. Bunlar Ayarlar ekranında **"planlandı"**
rozetiyle listelenir; çalışıyormuş gibi gösterilmez.

## Politika

Hiçbir şey yüklenmez, hiçbir şey yayınlanmaz, hiçbir artefakt otomatik
`production_ready` olmaz, API anahtarı değeri tarayıcıya/loga/artefakta yazılmaz.
