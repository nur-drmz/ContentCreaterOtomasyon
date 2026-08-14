# YouTube Shorts Platform Roadmap

Bu belge, proje başlangıcındaki **AI Content Intelligence & Production
Platform** ana planlama belgesindeki kilometre taşlarını yeniden görünür kılar
ve mevcut Colab prototipinin durumuyla eşler.

Günlük durum, kabul kriterleri, açık riskler ve kanıt takibi için
[MILESTONES.md](MILESTONES.md) kullanılır.

## Değişmeyen ürün sınırları

- Ana üretim hattı yalnızca kullanıcının verdiği kaynak medyayı işler.
- Ana hat: `kaynak video → analiz → moment seçimi → script → seslendirme → edit/render`.
- Trend, kanal ve viral-potential araştırmaları bağımsız karar-destek yan koludur.
- Hiçbir çıktı kendini onaylamaz veya otomatik yayımlanmaz.
- Sağlayıcılar değiştirilebilir; tek bir AI/TTS sağlayıcısına bağımlı mimari kurulmaz.
- Tahminler kanıt ve güven seviyesi taşır; kesin izlenme garantisi verilmez.

## Başlangıçta tanımlanan kullanıcı yolculuğu

1. Opportunity Radar
2. Project Creation
3. Source Video Upload
4. Source Processing
5. Hot Moment Extraction
6. Moment Selection
7. Prompt Studio
8. Rough Cut Generation
9. Script Studio
10. Voice Studio
11. Cloud Timeline Editor
12. Variant Arena
13. Viral Potential Report
14. View Forecast
15. YouTube private upload ve ayrı yayın onayı
16. Analytics and Learning

## Sürüm kilometre taşları ve mevcut durum

| Kilometre taşı | Başlangıç kapsamı | Mevcut durum |
|---|---|---|
| Phase 0 — Planning and validation | MVP PRD, kullanıcı akışları, yetki matrisi, upload/analiz/render/private-upload PoC, maliyet doğrulama | Kısmi. Ürün planı ve yerel analiz/render PoC mevcut; kesin MVP kabul kriterleri ve private-upload PoC tamamlanmadı. |
| Phase 0.1 — Technical prototype | Tek kullanıcı, tek kanal, tek kaynak video, transcript, scene detection, moment ranking, manuel seçim, rough cut | Büyük ölçüde hazır. Gerçek Colab videosuyla uçtan uca canlı koşu ve moment inceleme UX'i kapanış kriteridir. |
| Phase 0.2 — Moment Explorer | Preview kartları, filtreler, benzer momentler, Scene Genome v0, music-rise ve narrative score | Kısmi. Colab preview oynatımı ve hash-bağlı manuel seçim eklendi; gerçek video doğrulaması, benzer-moment ve gelişmiş filtreler bekliyor. |
| Phase 0.3 — Prompt and script | Prompt Studio, Logical Montage, Theory, üç script varyantı, evidence links, Theory Guard v0 | Kısmi. Gemini tabanlı seçili-script üretimi ve onay kapısı var; üçlü varyant arena ve tam evidence-link/guard tamamlanmadı. |
| Phase 0.4 — Voice, edit, and publication | TTS provider, temel timeline, final render, private upload, public approval gate | Kısmen tamamlandı. Trend ingest, metadata onay kapısı, YouTube private upload ve görünürlük onay kapısı kodlandı; API ve React skeletleri eklendi. Gerçek API erişimi ve E2E doğrulama bekliyor. |
| Phase 0.5 — Multi-channel | Çoklu kanal/concept, Channel Brain v0, kanal ayarları, Scene Reuse Guard | Başlanmadı. YouTube kanal araştırması yalnızca bağımsız veri yan koludur. |
| Phase 0.6 — Web and mobile beta | Responsive web, mobil onay/izleme, bildirimler, roller, audit log | Başlanmadı. |
| Phase 1.0 — SaaS foundation | Workspace, invitation, RBAC, usage, subscription, admin, tenant security | Başlanmadı. |
| Phase 1.1 — Intelligence advantage | Narrative Graph, Portfolio Optimizer, Variant Arena, retention learning, kanal-kalibre tahmin | Deneysel parçalar var; ürün fazı başlamadı. |
| Phase 2.0 — Scale | Agency, white label, public API, provider marketplace, localization, gelişmiş mobil edit | Başlanmadı. |

## Şu anki kapanış hedefi: Phase 0.1 + Phase 0.4 sesli Short PoC

Bir kaynak video için aşağıdaki zincirin gerçek Colab ortamında tamamlanması
gerekir:

1. Video tamamen ve bozulmadan okunur.
2. Transcript, sahne ve ses sinyalleri üretilir.
3. En az bir hot moment kullanıcı tarafından incelenip seçilir.
4. Gemini ile hedef süreye uygun script üretilir, okunur ve hash ile onaylanır.
5. Script, Gemini TTS ile seslendirilir; ses oynatılır ve hash ile onaylanır.
6. Seçilen kaynak aralığı 9:16 görüntü, altyazı, voice-over ve audio ducking ile render edilir.
7. Önizleme izlenir; teknik doğrulama ve tüm üst-akış onayları geçmeden final onayı verilmez.
8. `final_short.mp4` üretildikten sonra hiçbir yere otomatik yükleme yapılmaz.

## Voice Studio / seslendirme kabul kriterleri

- Seslendirme ana pipeline'ın zorunlu adımıdır; sessiz final render onaylanmaz.
- Varsayılan ücretsiz rota `GeminiKey` ile Gemini TTS'tir.
- Üretilen WAV gerçek, boş olmayan ve teknik olarak okunabilir olmalıdır.
- Ses, onaydan önce notebook içinde dinletilmelidir.
- Voice-over artefaktı script içeriğinin SHA-256 değerine bağlanmalıdır.
- Script değişirse mevcut ses onayı geçersiz olmalıdır.
- Render; voice-over seviyesi, orijinal ses seviyesi ve audio ducking uygulamalıdır.
- gTTS yalnızca geliştirme placeholder'ıdır ve final onayı alamaz.
- ElevenLabs, OpenAI, Piper ve gelecekteki sağlayıcılar aynı sözleşmeyle değiştirilebilir olmalıdır.

## Araştırma yan kolu

YouTube anahtar-kelime keşfi, trend skoru, kanal geçmişi ve viral tahmin ana
pipeline'dan bağımsız tutulur. Bu çıktıların sözleşmesi:

```json
{
  "pipeline_role": "research_sidecar",
  "feeds_main_pipeline": false
}
```

Araştırma sonuçları fikir verebilir; fakat kaynak videoyu, moment'i, script'i,
sesi veya render girdisini otomatik seçemez.
