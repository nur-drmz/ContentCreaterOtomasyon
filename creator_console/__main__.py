"""THE documented launch command:

    python -m creator_console

Everything else is optional. `--demo-fixtures` seeds a deterministic project so
the whole source -> strategy -> job -> review -> output flow can be walked
without Colab, a source video, or a single real credential.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import APP_NAME, __version__
from .app import DEFAULT_USER_ID, DEFAULT_WORKSPACE_ID, ConsoleApp, ConsoleConfig
from .env_file import EnvFileError, load_env_file
from .server import run

DEFAULT_STORAGE_DIR = ".console_data"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m creator_console",
        description=f"{APP_NAME} {__version__} -- yerel, tek kullanicili pre-alpha konsol.",
    )
    parser.add_argument("--host", default="127.0.0.1",
                        help="varsayilan 127.0.0.1 (loopback disi adres bilerek engellenir)")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--storage-root", default=None,
                        help=f"varsayilan: ./{DEFAULT_STORAGE_DIR}")
    parser.add_argument("--user-id", default=DEFAULT_USER_ID)
    parser.add_argument("--workspace-id", default=DEFAULT_WORKSPACE_ID)
    parser.add_argument("--inline-jobs", action="store_true",
                        help="isleri arka plan thread'i yerine senkron calistir")
    parser.add_argument("--demo-fixtures", action="store_true",
                        help="deterministik ornek proje olustur (gercek saglayici cagrisi YOK)")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--env-file", default=None,
                        help="varsayilan: proje kokundeki .env.local")
    parser.add_argument("--no-env-file", action="store_true",
                        help="yerel API dosyasini yukleme")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    project_root = Path(__file__).resolve().parent.parent
    storage_root = Path(args.storage_root or (project_root / DEFAULT_STORAGE_DIR))
    env_summary = {"exists": False, "loaded_count": 0, "preserved_count": 0}
    if not args.no_env_file:
        env_path = Path(args.env_file).expanduser() if args.env_file else project_root / ".env.local"
        try:
            env_summary = load_env_file(env_path)
        except EnvFileError as exc:
            print(f"[console] API ayar dosyasi HATALI: {exc}", file=sys.stderr)
            return 2
    if not args.quiet:
        print(
            "[console] yerel API ayarlari: "
            f"dosya={'bulundu' if env_summary['exists'] else 'yok'}, "
            f"yuklenen={env_summary['loaded_count']}, "
            f"ortamdan-korunan={env_summary['preserved_count']}"
        )

    config = ConsoleConfig(
        storage_root=storage_root,
        user_id=args.user_id,
        workspace_id=args.workspace_id,
        host=args.host,
        port=args.port,
        inline_jobs=args.inline_jobs,
        # Secrets are read from the process environment on the SERVER only, and
        # only ever surface as configured/missing.
        secret_getter=lambda name: os.environ.get(name),
        secret_setter=lambda name, value: os.environ.__setitem__(name, value),
        env_file_path=env_path if not args.no_env_file else None,
    )
    app = ConsoleApp(config)

    if args.demo_fixtures:
        from .fixtures import seed_demo_project
        summary = seed_demo_project(app)
        print(f"[console] demo proje hazir: {summary['project_id']} "
              f"({summary['title']}) -- sahte saglayicilarla, gercek cagri yapilmadi")

    return run(app, host=args.host, port=args.port, quiet=args.quiet)


if __name__ == "__main__":
    sys.exit(main())
