#!/usr/bin/env python
"""Yönlendirilmiş revizyon akışı doğrulaması.

    python verify_revision_flow.py

NE KANITLIYOR
-------------
Revizyon istemek yalnızca bir NOTTU. Kod bunu açıkça söylüyordu:
"A revision request is a NOTE: it changes no artifact, no status, and never
touches the video." Yani reddedilen klip, transkript ve video tam olarak bir
sonraki koşunun onları yeniden kullanacağı yerde duruyordu.

Bu paket üç şeyi denetler:

  1. Revizyon başlatmak gecersizlesen aşamayı gerçekten **taşıyor** -- ve
     silmiyor: eski çıktı `revisions/<id>/` altında içeriğiyle duruyor.
  2. Onay, taşınacak dosyaları **isimleriyle** söylüyor. Neyi taşıdığını
     söyleyemeyen bir "emin misin?" onay değildir; bu yüzden plan saf bir
     okuma ve hiçbir şeye dokunmuyor.
  3. Kalıcı silme AYRI bir işlem ve kimliği birebir yazmayı istiyor.

Ayrıca kapatılan hata: `moment_transcript.json` hiçbir geri dönüş aşamasında
yoktu, dolayısıyla analize veya momente dönmek önceki momentin transkriptini
yerinde bırakıyordu.

GÜVENLİK
--------
İzole geçici depolama, sahte sağlayıcılar, sıfır gerçek API çağrısı.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from creator_console import fixtures  # noqa: E402
from creator_console.api import ConsoleApi  # noqa: E402
from creator_console.app import ConsoleApp, ConsoleConfig  # noqa: E402
from creator_console.pipeline_service import REWIND_PATHS  # noqa: E402
from local_pipeline import render_utils  # noqa: E402

FAILURES: list = []
CHECKS = 0
TMP: list = []


def check(condition, label: str, message: str = "") -> bool:
    global CHECKS
    CHECKS += 1
    if condition:
        print(f"  [{label}] OK" + (f" - {message}" if message else ""))
        return True
    FAILURES.append(f"{label}: {message}")
    print(f"  [{label}] BASARISIZ - {message}")
    return False


def section(title: str) -> None:
    print(f"\n--- {title} ---")


def call(api, method, path, body=None):
    raw = b"" if body is None else json.dumps(body).encode("utf-8")
    return api.handle(method, path, body=raw, headers={})


def seeded_project(root, title):
    app = ConsoleApp(ConsoleConfig(
        storage_root=root, inline_jobs=True,
        secret_getter=fixtures.fake_secret_getter(),
        **fixtures.fixture_client_factories()))
    api = ConsoleApi(app)
    pid = fixtures.seed_demo_project(app, title=title)["project_id"]
    service = app.service(app.ownership(pid))
    service.write_artifact("moment_transcript",
                           render_utils.build_moment_transcript_artifact(
                               [{"text": "Rainer", "start": 0.0, "end": 0.5,
                                 "probability": 0.4}],
                               [{"start": 0.0, "end": 0.5, "text": "Rainer"}],
                               source="s", language="en", moment_rank=1,
                               moment_selection_sha256="x",
                               whisper_model_size="tiny", initial_prompt_used=False),
                           actor="verify")
    return app, api, pid, service


def main() -> int:
    section("1. Geri donus haritasi transkripti kapsiyor")
    for stage in ("assessment", "plan", "analysis", "moment", "script"):
        check("results/moment_transcript.json" in REWIND_PATHS[stage],
              f"transkript-{stage}-asamasinda-arsivleniyor",
              "eski momentin transkripti yerinde kalirsa sonraki kosu onu kullanir")
    for stage in ("voice", "render"):
        check("results/moment_transcript.json" not in REWIND_PATHS[stage],
              f"transkript-{stage}-asamasinda-KORUNUYOR",
              "encode'u yenilemek icin incelenmis altyaziyi atmak insan emegini yok eder")
    check(not any("results/glossary.json" in paths for paths in REWIND_PATHS.values()),
          "sozluk-hicbir-asamada-arsivlenmiyor",
          "ozel isimler kaynak hakkinda bilgi; analizi yenilemek onlari unutturmamali")

    section("2. Plan saf okuma, dosyalari isimleriyle sayiyor")
    root = Path(tempfile.mkdtemp(prefix="revflow_"))
    TMP.append(root)
    app, api, pid, service = seeded_project(root, "Revizyon akisi")
    transcript_path = Path(service.path("moment_transcript"))

    status, payload = call(api, "GET", f"/api/projects/{pid}/revision?target=moment")
    view = payload["revision"]
    check(status == 200, "plan-http", f"HTTP {status}")
    paths = [move["path"] for move in view["plan"]["moves"]]
    check("results/moment_transcript.json" in paths, "plan-transkripti-listeliyor",
          str(paths))
    check(view["plan"]["file_count"] >= 1 and view["plan"]["size_bytes"] > 0,
          "plan-dosya-sayisi-ve-boyut", f"{view['plan']['file_count']} dosya")
    check(view["plan"]["reversible"] is True, "plan-geri-alinabilir-oldugunu-soyluyor")
    check(transcript_path.is_file(), "plan-HICBIR-SEYI-TASIMIYOR",
          "GET bir onizleme; dosyaya dokunursa onay anlamsizlasir")
    check(bool(view["suggested"]["stage"]), "asama-onerisi-var",
          view["suggested"]["stage"])
    check(bool(view["suggested"].get("note")) or view["suggested"]["reasons"],
          "oneri-gerekcesini-soyluyor")
    check(len(view["stages"]) == 7, "tum-asamalar-secilebilir",
          str([s["key"] for s in view["stages"]]))

    status, payload = call(api, "GET", f"/api/projects/{pid}/revision?target=bogus")
    check(status >= 400, "gecersiz-asama-reddediliyor", f"HTTP {status}")

    section("3. Baslatma tasiyor, silmiyor")
    status, payload = call(api, "POST", f"/api/projects/{pid}/revision",
                           {"target": "moment", "notes": "Klip cumle ortasinda basliyor."})
    check(status == 200, "revizyon-baslatildi", f"HTTP {status}")
    revision_id = payload["revision"]["revision"]["revision_id"]
    moved = payload["revision"]["revision"]["moved_paths"]
    check("results/moment_transcript.json" in moved, "transkript-tasindi", str(moved))
    check(not transcript_path.is_file(), "eski-yerinde-DEGIL",
          "yerinde kalirsa sonraki kosu onu yeniden kullanir")
    archived = Path(service.storage.resolve(service.ownership, ".revisions",
                                             revision_id, "results",
                                             "moment_transcript.json"))
    check(archived.is_file(), "arsivde-duruyor", str(archived.name))
    # Guarded: when the archive is missing the suite must REPORT that, not die
    # with a traceback that hides every check after it.
    archived_text = (json.loads(archived.read_text("utf-8")).get("text")
                     if archived.is_file() else None)
    check(archived_text == "Rainer", "arsivlenen-icerik-bozulmamis",
          str(archived_text))
    check(payload["archive"]["count"] == 1, "arsiv-listesinde-gorunuyor",
          str(payload["archive"]["count"]))

    status, payload = call(api, "POST", f"/api/projects/{pid}/revision",
                           {"target": "moment"})
    check(status >= 400, "bos-asamada-revizyon-reddediliyor",
          f"HTTP {status} - tasinacak sey yokken 'basarili' demek yalan olur")

    section("4. Kalici silme ayri ve acik")
    status, _ = call(api, "POST", f"/api/projects/{pid}/revision/purge",
                     {"revision_id": revision_id, "confirmation": "evet"})
    check(status >= 400, "yanlis-onay-reddediliyor", f"HTTP {status}")
    check(archived.is_file(), "yanlis-onaydan-sonra-arsiv-duruyor")
    status, _ = call(api, "POST", f"/api/projects/{pid}/revision/purge",
                     {"revision_id": "../../escape", "confirmation": "../../escape"})
    check(status >= 400, "yol-gezinme-reddediliyor", f"HTTP {status}")
    status, payload = call(api, "POST", f"/api/projects/{pid}/revision/purge",
                           {"revision_id": revision_id, "confirmation": revision_id})
    check(status == 200 and payload["archive"]["count"] == 0,
          "birebir-onayla-siliniyor", f"HTTP {status}")
    check(not archived.exists(), "silinen-arsiv-diskten-gitti")

    check(bool(fixtures.PROVIDER_CALLS) is False, "sifir-saglayici-cagrisi",
          str(len(fixtures.PROVIDER_CALLS)))

    print("\n" + "=" * 74)
    print(f"toplam {CHECKS} kontrol, {CHECKS - len(FAILURES)} gecti, {len(FAILURES)} basarisiz")
    if FAILURES:
        for line in FAILURES:
            print("  BASARISIZ:", line)
        return 1
    print("REVISION_FLOW_OK (izole depolama, sahte saglayicilar; hicbir gercek API "
          "cagrilmadi, hicbir sey yuklenmedi/yayinlanmadi)")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        for path in TMP:
            shutil.rmtree(path, ignore_errors=True)
    sys.exit(code)
