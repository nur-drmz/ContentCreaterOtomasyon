# 01 · Genel Bakış

## Ürün ne yapıyor

Kullanıcının sahip olduğu (veya kullanım dayanağı bulunduğu) bir **kaynak
videodan**, dikey 9:16 YouTube Shorts üretir. Ana hat:

```
kaynak video / trend alımı
      ↓
Faz 0: kaynak değerlendirme + işleme planı (insan onayı)
      ↓
video analizi (transcript · sahne · ses enerjisi) → hot moment adayları
      ↓
moment seçimi (insan)
      ↓
altyazı transkripti onayı   VEYA   script üretimi + onayı
      ↓
seslendirme (TTS) + onayı
      ↓
render (9:16 · altyazı · voice-over · ducking)
      ↓
final inceleme + onay
      ↓
metadata onayı → YouTube private upload onayı → görünürlük onayı
```

Uzun form (15–20 dk, 16:9) ve "Turbo" (stok materyalden Short) ayrı akışlardır;
bkz. [`09-studyolar.md`](09-studyolar.md).

## Değişmez ürün sınırları

`ROADMAP.md` içinde ürün sözleşmesi olarak sabitlenmiş, kodda da zorlanan
kurallar:

- **Ana hat yalnızca kullanıcının verdiği kaynak medyayı işler.**
- **Trend / kanal / viral araştırması bağımsız karar-destek yan koludur.**
  Çıktıları `pipeline_role: research_sidecar`, `feeds_main_pipeline: false`
  taşır ve ana hattın hiçbir girdisini otomatik seçemez.
- **Hiçbir çıktı kendini onaylamaz veya otomatik yayımlanmaz.**
- **Sağlayıcı değiştirilebilir**; tek bir AI/TTS sağlayıcısına bağımlı mimari
  kurulmaz.
- **Tahminler kanıt ve güven seviyesi taşır**; kesin izlenme garantisi verilmez.

## Kalite / durum sözlüğü

Her artefakt JSON'u sabit bir kelime dağarcığından `status` taşır
(`@local_pipeline/source_strategy.py` ve
`publish.py` içinde `STATUS_VALUES`):

| status | Anlamı |
|---|---|
| `experimental` | Deney amaçlı, üretim için değil |
| `development_placeholder` | Yer tutucu (örn. gTTS) — asla onaylanamaz |
| `review_required` | Üretildi, insan incelemesi bekliyor |
| `approved` | İnsan tarafından hash'e bağlı olarak onaylandı |
| `insufficient_data` | Karar verecek kadar veri yok; uydurma yapılmadı |
| `failed` | Başarısız; kısmi/yanlış sonuç üretilmedi |
| `production_ready` | Yalnızca izin verilen artefakt türlerinde, açık onayla |

Yerel sezgisel yöntemler ve ücretsiz sağlayıcılar aday üretebilir ama kendini
`production_ready`'ye yükseltemez.

## Politika kısıtları (kodda zorlanır)

| Politika | Nerede |
|---|---|
| Hesap/anahtar rotasyonu ile kota aşımı **yasak** | `ALLOW_ACCOUNT_QUOTA_ROTATION = False`, `google_ai.assert_no_account_quota_rotation()` |
| Ham (telifli) medya ücretsiz katmana **gönderilmez** | `ALLOW_RAW_MEDIA_TO_FREE_TIER = False`; her artefakt `raw_media_submitted` kaydeder |
| gTTS **asla** `approved` olamaz | `approval.NEVER_APPROVABLE_PROVIDERS = {'gtts'}` |
| Yayın her zaman `private` başlar | `publish.INITIAL_PRIVACY = 'private'` |
| Güvenlik reddi (safety refusal) **terminaldir** | Sağlayıcı/model değiştirilerek aşılmaya çalışılmaz |
| Sır değerleri tarayıcıya/loga/artefakta yazılmaz | `secrets_ref.py`, `redaction.py` |
| Konsol yalnızca loopback'e bağlanır | `--host` varsayılanı `127.0.0.1`, dışa açma bilerek engelli |

## Telif ve içerik riski

Üçüncü taraf içeriği indirme/yeniden yayımlama akışı (`trend_ingest.py`)
**zorunlu bir hak kapısı** ardındadır. Lisans türleri: `own`, `cc_by`,
`licensed`, `fair_use_claim`. Üçüncü taraf türlerinde atıf (≥10 karakter) ve
dönüştürücü katkı notu (≥20 karakter) zorunludur; modül ayrıca açık bir
Content-ID risk uyarısı (`CONTENT_ID_RISK_NOTICE`) taşır ve yazılımın hukuki
değerlendirme yapmadığını belirtir.

## Neyin olmadığı (sahte gösterilmez)

Kimlik doğrulama/RBAC · gerçek çok kiracılılık · faturalandırma · public
deployment · otomatik YouTube yayını · nesne depolama. Bunlar Ayarlar ekranında
**"planlandı"** rozetiyle listelenir, çalışıyormuş gibi gösterilmez.
