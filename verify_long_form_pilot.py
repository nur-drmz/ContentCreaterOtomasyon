#!/usr/bin/env python
"""OPT-IN canlı pilot: bir gerçek ~18 dakikalık uzun video üretimi.

    set YTO_LONG_FORM_PILOT=1
    python verify_long_form_pilot.py --project <proje-id>

BU SCRIPT GERÇEK PARA HARCAR
----------------------------
Diğer bütün `verify_*` paketleri sahte sağlayıcı ikizleriyle çalışır ve hiçbir
ücret doğurmaz. Bu script onlardan farklıdır: gerçek Gemini isteklerini yapar.
Bu yüzden

  * ortam değişkeni `YTO_LONG_FORM_PILOT=1` olmadan **çalışmaz**,
  * hiçbir otomatik bataryaya dahil **değildir**,
  * ve `--yes` verilmedikçe başlamadan önce tahmini maliyeti gösterip onay ister.

18 dakikalık bir anlatım yapılandırılmış limitlerle tek geçişte bitmez
(TTS modelleri dakikada 3 istek, günde 10 istek). Script bunu baştan söyler ve
kaldığı yerden devam edecek şekilde çalışır -- her geçişte yalnızca eksik
parçalar istenir.

RAPORLADIKLARI
--------------
sağlayıcı/model çağrıları, girdi/çıktı kullanımı, gecikme, yeniden denemeler,
parça başına süre, toplam anlatım süresi, çıktı hash'leri, meta veride varsa
tahmini/gerçek maliyet ve ses sürekliliği inceleme gereksinimi.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

ENV_FLAG = "YTO_LONG_FORM_PILOT"


def main() -> int:
    parser = argparse.ArgumentParser(description="Canli uzun video pilotu")
    parser.add_argument("--project", required=True, help="proje kimligi")
    parser.add_argument("--storage", default=".console_data",
                        help="depolama koku (varsayilan: .console_data)")
    parser.add_argument("--max-chunks", type=int, default=0,
                        help="bu gecişte en fazla kac TTS parcasi uretilsin")
    parser.add_argument("--stage-only", default="",
                        help="yalnizca bu asamayi calistir")
    parser.add_argument("--yes", action="store_true",
                        help="maliyet onayini atla")
    args = parser.parse_args()

    if os.environ.get(ENV_FLAG, "").strip() not in {"1", "true", "yes"}:
        print(f"DURDURULDU: bu script GERCEK API cagrisi yapar ve ucret dogurur.\n"
              f"Calistirmak icin {ENV_FLAG}=1 ayarla.")
        return 2

    from creator_console.app import ConsoleApp, ConsoleConfig
    from creator_console.env_file import load_env_file

    # Every other live script loads this explicitly; only `__main__` does it for
    # the server. Without it the credential resolver sees an empty environment
    # and the pilot would report "not configured" while the key sits on disk.
    env_path = PROJECT_ROOT / ".env.local"
    if env_path.is_file():
        loaded = load_env_file(env_path)
        # Names only. A count and a list of KEY NAMES is enough to debug a
        # missing credential; the values never belong in output.
        print(f"Ortam dosyasi yuklendi: {len(loaded)} anahtar "
              f"({', '.join(sorted(loaded))})")
    else:
        print("UYARI: .env.local bulunamadi; kimlik ortamdan gelmeli.")

    app = ConsoleApp(ConsoleConfig(storage_root=Path(args.storage)))
    try:
        ownership = app.ownership(args.project)
        service = app.service(ownership)
        studio = service.long_form_studio()

        plan = studio.plan()
        if not plan:
            print("DURDURULDU: bu projede uzun video plani yok.")
            return 2
        estimates = studio.estimates()
        readiness = studio.provider_readiness()
        calls = estimates["api_calls"]

        print("=" * 70)
        print(f"Proje    : {args.project}")
        print(f"Konu     : {plan.get('topic')}")
        print(f"Sure     : {plan.get('target_duration_min')} dk / "
              f"{plan.get('chapter_count')} bolum")
        print(f"Kelime   : {estimates['word_range']['minimum']}-"
              f"{estimates['word_range']['maximum']}")
        print(f"Tahmini API istegi: {calls['total']} "
              f"(arastirma {calls['research']}, outline {calls['outline']}, "
              f"bolum {calls['chapters']}, tutarlilik {calls['consistency']}, "
              f"TTS {calls['tts_chunks']})")
        capacity = readiness.get("tts_capacity") or {}
        print(f"TTS kapasitesi    : gunluk {capacity.get('daily_request_capacity')}, "
              f"dakikalik {capacity.get('per_minute_request_capacity')} "
              f"(sinirlayan: {capacity.get('binding_limit')})")
        for warning in readiness.get("warnings") or []:
            print(f"UYARI    : {warning}")
        for blocker in readiness.get("blockers") or []:
            print(f"ENGEL    : {blocker}")
        if readiness.get("blockers"):
            return 2
        print("=" * 70)
        print("BU CALISMA GERCEK PARA HARCAR.")
        if not args.yes:
            answer = input("Devam edilsin mi? (evet yaz): ").strip().lower()
            if answer not in {"evet", "e", "yes", "y"}:
                print("Iptal edildi; hicbir istek yapilmadi.")
                return 1

        before = studio.quota_summary()["request_total"]
        started = time.time()
        ran = []
        if args.stage_only:
            outcome = {"ran": args.stage_only}
            getattr(studio, f"run_{args.stage_only}")(actor="pilot")
            ran.append(args.stage_only)
        else:
            # One stage per iteration so a failure stops at a stage boundary and
            # the next run resumes from exactly there.
            while True:
                outcome = studio.run_next_stage(actor="pilot")
                if outcome.get("blocked") or not outcome.get("ran"):
                    print(f"DURDU: {outcome.get('reason')}")
                    break
                ran.append(outcome["ran"])
                print(f"  asama tamam: {outcome['ran']}")
        elapsed = time.time() - started

        manifest = studio.chunk_manifest()
        chunks = manifest.get("chunks") or []
        completed = [c for c in chunks if c.get("status") == "completed"]
        failed = [c for c in chunks if c.get("status") == "failed"]
        after = studio.quota_summary()

        print()
        print("=" * 70)
        print("PILOT RAPORU")
        print("=" * 70)
        print(f"Calisan asamalar   : {ran or '-'}")
        print(f"Gecen sure         : {round(elapsed, 1)} sn")
        print(f"Kota istegi (delta): {after['request_total'] - before}")
        for name, entry in (after.get("models") or {}).items():
            print(f"  {name}: bugun {entry.get('requests_today')} istek, "
                  f"basarili {entry.get('successful_calls')}, "
                  f"basarisiz {entry.get('failed_calls')}")
        print(f"TTS parcasi        : {len(completed)}/{len(chunks)} tamam, "
              f"{len(failed)} basarisiz")
        total_attempts = sum(int(c.get("attempts") or 0) for c in chunks)
        print(f"Toplam deneme      : {total_attempts} "
              f"(yeniden deneme: {max(0, total_attempts - len(chunks))})")
        for chunk in chunks:
            print(f"  {chunk['id']} {chunk.get('chapter_id')} "
                  f"{chunk.get('status'):9} "
                  f"{chunk.get('duration_sec') or '-'}s "
                  f"gecikme {chunk.get('provider_latency_sec') or '-'}s "
                  f"sha {(chunk.get('output_sha256') or '')[:12]} "
                  f"{chunk.get('error_kind') or ''}")
        combined = manifest.get("combined")
        if combined:
            check = combined.get("duration_check") or {}
            print(f"Birlesik anlatim   : {combined['duration_sec']}s "
                  f"({check.get('measured_min')} dk), "
                  f"hedefte: {check.get('within_target')}")
            print(f"Birlesik hash      : {combined.get('sha256')}")
        else:
            print("Birlesik anlatim   : henuz uretilmedi")
        chapters = (studio.chapters() or {}).get("chapters") or []
        if chapters:
            print(f"Bolum kelimeleri   : "
                  f"{[c.get('word_count') for c in chapters]} "
                  f"(toplam {sum(c.get('word_count') or 0 for c in chapters)})")
        print()
        print("MALIYET: bu depoda saglayici fiyat meta verisi tutulmuyor; "
              "yukaridaki istek sayilarini kendi tarifenle carp.")
        print("SES SUREKLILIGI: parcalar ayri isteklerle uretildigi icin ton ve "
              "hiz kaymasi MUMKUNDUR. Birlesik anlatim ONAYLANMADAN once "
              "bastan sona DINLENMELIDIR -- bu otomatik dogrulanamaz.")
        print("Gemini TTS PREVIEW modeldir; cikti her zaman review_required.")
        return 0 if not failed else 1
    finally:
        app.shutdown()


if __name__ == "__main__":
    sys.exit(main())
