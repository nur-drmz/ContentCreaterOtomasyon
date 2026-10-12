# 13 · Kurulum ve Çalıştırma

## Üç çalıştırma biçimi

| Biçim | Komut | Port |
|---|---|---|
| **Creator Console** (üretim yolu) | `python -m creator_console` | 8765 |
| **FastAPI + React** (editör geliştirme) | `uvicorn services.api.main:app` + `npm run dev` | 8000 + 3000 |
| **Colab** | `experiments/colab/07_creator_console.ipynb` | iframe (loopback) |

---

## 1. Creator Console

### Kurulum

Ek bağımlılık **yoktur**. Konsol yalnızca Python standart kütüphanesini kullanır;
mevcut `.venv` (ffmpeg / faster-whisper / scenedetect / librosa) yeterlidir.
Node.js, npm veya build adımı gerekmez.

Windows'ta ilk kurulum:

```powershell
.\setup_local.ps1
python verify_local.py     # yerel pipeline smoke testi
```

### Başlatma

```bash
python -m creator_console                  # http://127.0.0.1:8765/
python -m creator_console --demo-fixtures  # sentetik kaynak videolu örnek proje
```

Windows'ta proje kökündeki **`Start Creator Console.exe`** çift tıklanabilir:
`.venv/Scripts/python.exe`'yi doğrular, konsol açık değilse ayrı terminalde
başlatır, hazır olmasını bekler ve tarayıcıyı açar. **`Stop Creator Console.exe`**
rastgele üretilmiş, yalnızca yerelde tutulan bir kontrol anahtarıyla **bu** örnekten
temiz kapanma ister; başka Python süreçlerini aramaz veya öldürmez.

> EXE'ler proje kökünde, `.venv` ve `creator_console` ile birlikte durmalıdır.

### Bayraklar

| Bayrak | İşlev |
|---|---|
| `--port 8765` | HTTP portu |
| `--host` | Varsayılan `127.0.0.1` |
| `--storage-root ./.console_data` | Tüm proje/artefakt/iş verisinin kökü |
| `--archive-root "G:\..."` | Tamamlanan projelerin taşınacağı harici disk kökü |
| `--source-library-root "G:\..."` | Tarayıcı upload'ı olmadan seçilebilen kaynak kütüphanesi |
| `--inline-jobs` | İşleri arka plan thread'i yerine senkron çalıştır |
| `--demo-fixtures` | Sentetik kaynak videolu örnek proje |
| `--user-id` / `--workspace-id` | Sahiplik kimlikleri |
| `--env-file DOSYA` | `.env.local` yerine başka yerel dosya |
| `--no-env-file` | Yerel sır dosyasını yükleme |

**Loopback dışına bağlanmak bilerek engellidir**: pre-alpha'da kimlik doğrulama
yoktur, ağa açmak konsolu herkese açmak demektir.

### API anahtarları

Proje kökündeki `.env.local` dosyasına yalnızca kullanacağınız anahtarları yazın:

```dotenv
GeminiKey=...
YoutubeKEY=...
GITHUB_TOKEN=...
```

Kaydedip konsolu **yeniden başlatın**. Dosya yalnızca sunucu sürecine yüklenir;
Ayarlar ekranı değer yerine `configured` / `missing` gösterir. İşletim sistemi
ortam değişkeni öncelikli; dosya mevcut değeri ezmez. Bilinmeyen değişken veya
bozuk satır sessizce kabul edilmez — konsol açık hatayla başlamayı reddeder.
`.env.local` Git tarafından yok sayılır.

Tüm değişken adları için `.env.example`'a bakın (yalnızca adlar, değer yok).

### Ayrı süreçli worker

```bash
python -m creator_console.worker --storage-root ./.console_data --project-id prj_xxx
```

---

## 2. FastAPI + React (Render Atölyesi geliştirme)

### Backend

```bash
CONSOLE_ROOT=./data/console python -m uvicorn services.api.main:app \
  --host 127.0.0.1 --port 8000
```

Doğrulama:

```bash
curl -s http://127.0.0.1:8000/health     # {"ok":true,"service":"api"}
```

Ortam değişkenleri: `CONSOLE_WORKSPACE` (`wsp_default`), `CONSOLE_USER`
(`usr_api`), `CONSOLE_ROOT` (`./data/console`).

### Frontend

```bash
cd apps/web
npm install        # ilk kez (173 paket)
npm run dev        # http://localhost:3000
npm test           # vitest
```

Vite `/api` isteklerini `127.0.0.1:8000`'e proxy'ler. Ekranlar API kökü olarak
`import.meta.env.VITE_API_ROOT ?? ''` kullanır — yani **göreli** yol, yani proxy.

> **Neden mutlak URL kullanılmıyor:** FastAPI katmanında CORS middleware yoktur.
> `http://127.0.0.1:8000` mutlak kökü tarayıcıdan çağrıldığında istek CORS'a
> takılır. Proxy bunu origin sorunu olmadan çözer.

### Örnek uçtan uca akış (curl)

```bash
# 1) proje
P=$(curl -s -X POST http://127.0.0.1:8000/api/projects \
      -H 'Content-Type: application/json' \
      -d '{"title":"Atolye","operator_name":"nur"}' \
    | python3 -c "import sys,json;print(json.load(sys.stdin)['project']['project_id'])")

# 2) EDL
curl -s -X POST http://127.0.0.1:8000/api/projects/$P/edl \
  -H 'Content-Type: application/json' \
  -d '{"source_path":"source.mp4","source_duration":60,"title":"E2E"}'

# 3) crop + altyazı konumu/rengi
curl -s -X PATCH http://127.0.0.1:8000/api/projects/$P/edl/clips/0 \
  -H 'Content-Type: application/json' \
  -d '{"start":5,"end":30,"crop":{"x":0.12,"y":0.08,"w":0.7,"h":0.8}}'

# 4) render
curl -s -X POST http://127.0.0.1:8000/api/projects/$P/edl/render \
  -H 'Content-Type: application/json' -d '{"output_name":"preview.mp4"}'
```

---

## 3. Colab

```bash
python tools/build_colab_console_bundle.py
python tools/build_colab_console_bundle.py --verify
python verify_colab_console.py
```

Ardından Colab'da `07_creator_console.ipynb` açılır. Drive sözleşmesi:

```
MyDrive/YouTubeAutomation/
  app/creator_console_colab.zip(.sha256)
  app/07_creator_console.ipynb
  source_videos/     # büyük kaynakların kalıcı kütüphanesi
  projects/          # aktif Console projeleri
  archive/           # tamamlanan projeler
  results/  cache/   # eski/self-contained notebook çıktıları
```

Konsol Colab'da da `127.0.0.1` üzerinde kalır; **public ngrok/cloudflared tüneli
kurulmaz**. Arayüz yalnızca Colab'ın mevcut kullanıcı oturumuyla korunan
`serve_kernel_port_as_iframe` görünümünden açılır. `GeminiKey` Colab Secrets'tan
yalnızca sunucu sürecine aktarılır, notebook çıktısına yazılmaz.

Büyük videolar Colab proxy'sinden tarayıcı upload ile gönderilmemelidir; önce
`source_videos/` altına konmalıdır. Colab runtime diski geçicidir; kalıcı kaynak
`projects/` ve `archive/`'dir.

---

## Bağımlılıklar

`requirements-local.txt`:

```
anthropic==0.120.2          faster-whisper==1.2.1
google-api-python-client==2.198.0   google-genai==2.15.0
imageio-ffmpeg==0.6.0       ipykernel==7.3.0
jupyterlab==4.6.2           librosa==0.11.0
moviepy==2.2.1              numpy==2.4.6
openai==2.50.0              pandas==3.0.5
requests==2.34.2            scenedetect-headless==0.7.1
tqdm==4.70.0                yt-dlp==2026.7.4
playwright==1.62.0
```

Colab için ayrıca `requirements-colab.txt`.

FFmpeg **paketlenmiş** gelir (`imageio-ffmpeg`); sistem kurulumu gerekmez.

---

## Bilinen tuzaklar

### Sunucuyu `&` ile arka plana atmak

`uvicorn ... &` şeklinde başlatılan süreç stdin okumaya kalkarsa `SIGTTIN` alır ve
**`T` (stopped)** durumuna düşer. Sonuç: portu tutar ama isteklere yanıt vermez ve
yeni bir sunucu `[Errno 48] address already in use` alır. Teşhis ve çözüm:

```bash
lsof -nP -iTCP:8000 -sTCP:LISTEN     # PID'i bul
ps -o pid,stat,command -p <PID>      # STAT 'T' veya 'TN' ise durdurulmuş
kill -9 <PID>                        # sonra düzgün şekilde yeniden başlat
```

### Vite yalnızca `localhost`'a bağlanır

`curl http://127.0.0.1:3000` **connection refused** verebilirken
`curl http://localhost:3000` çalışır (IPv6/IPv4 farkı). Ağa açmak için
`--host` gerekir.

### Yeni bir audit olayı eklemek

`audit.record("yeni.olay")` çağırmadan önce olay adını
`@creator_console/audit.py` içindeki
`EVENT_TYPES` kümesine eklemek **zorunludur**. Aksi halde çağrı
`ValueError: bilinmeyen audit olayi` yükseltir ve API katmanı bunu
`validation_failed` (HTTP 400) olarak döner — artefakt yazılmış olsa bile istek
başarısız görünür.

### `local_pipeline/` düzenledikten sonra

Notebook'lara gömülü snapshot'lar bayatlar:

```bash
python tools/refresh_embedded_payloads.py
python verify_local.py
```

### `numpy` bulunamadı

Sistem `python3`'ü ile `creator_console.pipeline_service` import edilemez.
`.venv`'i etkinleştirin veya `requirements-local.txt`'i kurun.

---

## Hızlı sağlık kontrolü

```bash
python -m pytest tests/ -q                      # 128 test
python verify_faults.py | tail -3               # 70/70
cd apps/web && npm test                         # vitest
curl -s http://127.0.0.1:8000/health            # API
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:3000/   # web
```
