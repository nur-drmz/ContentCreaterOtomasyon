import base64
import hashlib
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
YOUTUBE_DATA_MODULE_PATH = PROJECT_ROOT / 'local_pipeline' / 'youtube_data.py'
LOCAL_PIPELINE_DIR = PROJECT_ROOT / 'local_pipeline'


def build_local_pipeline_bootstrap_cell(module_names):
    """Returns the source for a bootstrap code cell that loads one or more
    local_pipeline modules (e.g. ['video_analysis', 'render_utils']) via, in
    order: (1) direct import (repo already on sys.path), (2) a copy left in
    the Drive-persisted project root or an ancestor of cwd, (3) `git clone`
    from PROJECT_REPO_URL if set, (4) a self-contained fallback: base64-
    embedded byte-identical snapshots of each local_pipeline/<name>.py, each
    with a recorded SHA-256 hash that is verified against the decoded bytes
    before use -- a corrupted/truncated embedded payload raises a clear error
    instead of silently importing broken code.

    The embedded snapshots + their hashes are regenerated every time this
    function runs (it reads the canonical files fresh), so there is only ONE
    source of truth; verify_local.py separately re-derives each module's
    SHA-256 from the CURRENT canonical file and compares it against the hash
    recorded in each notebook's bootstrap cell to catch a notebook that was
    built from a since-changed module and never regenerated.
    """
    payloads = {}
    for name in module_names:
        module_path = LOCAL_PIPELINE_DIR / f'{name}.py'
        source_bytes = module_path.read_text(encoding='utf-8').encode('utf-8')
        digest = hashlib.sha256(source_bytes).hexdigest()
        b64 = base64.b64encode(source_bytes).decode('ascii')
        payloads[name] = (b64, digest)

    lines = []
    lines.append("import sys, os, tempfile, subprocess, base64, hashlib")
    lines.append("from pathlib import Path")
    lines.append("")
    lines.append("PROJECT_REPO_URL = ''  # opsiyonel: gercek bir git repo URL'si verirsen modul(ler) oradan klonlanir")
    lines.append("")
    lines.append("_LOCAL_PIPELINE_MODULE_NAMES = %r" % (list(module_names),))
    lines.append("")
    lines.append("# Her modul icin: (base64 kaynak, SHA-256 hash). Hash, gomulu payload'in")
    lines.append("# bozulmadigini/kesilmedigini dogrulamak icin kullanilir -- eslesmezse acik")
    lines.append("# bir hata verilir, asla sessizce bozuk kod calistirilmaz.")
    lines.append("_LOCAL_PIPELINE_PAYLOADS = {")
    for name in module_names:
        b64, digest = payloads[name]
        wrapped = '\n'.join(b64[i:i + 100] for i in range(0, len(b64), 100))
        lines.append(f"    {name!r}: {{")
        lines.append(f"        'sha256': {digest!r},")
        lines.append("        'b64': (")
        for chunk in wrapped.splitlines():
            lines.append(f"            {chunk!r}")
        lines.append("        ),")
        lines.append("    },")
    lines.append("}")
    lines.append("")
    lines.append("def _load_local_pipeline_modules():")
    lines.append("    # 1) Zaten import edilebiliyorsa (yerel .venv, repo Colab'da klonlanmis) dogrudan kullan.")
    lines.append("    try:")
    lines.append("        import local_pipeline")
    lines.append("        modules = {}")
    lines.append("        for _name in _LOCAL_PIPELINE_MODULE_NAMES:")
    lines.append("            modules[_name] = __import__(f'local_pipeline.{_name}', fromlist=[_name])")
    lines.append("        print('local_pipeline modulleri dogrudan import edildi:', list(modules))")
    lines.append("        return modules")
    lines.append("    except Exception:")
    lines.append("        pass")
    lines.append("")
    lines.append("    # 2) Drive'da veya proje kokunun bir ustunde daha once birakilmis bir kopya var mi?")
    lines.append("    search_roots = []")
    lines.append("    try:")
    lines.append("        search_roots.append(Path(DRIVE_PROJECT_ROOT))")
    lines.append("    except NameError:")
    lines.append("        pass")
    lines.append("    search_roots += [Path.cwd().resolve(), *Path.cwd().resolve().parents]")
    lines.append("    for root in search_roots:")
    lines.append("        candidate_dir = Path(root) / 'local_pipeline'")
    lines.append("        if all((candidate_dir / f'{_n}.py').is_file() for _n in _LOCAL_PIPELINE_MODULE_NAMES):")
    lines.append("            sys.path.insert(0, str(candidate_dir.parent))")
    lines.append("            try:")
    lines.append("                modules = {}")
    lines.append("                for _name in _LOCAL_PIPELINE_MODULE_NAMES:")
    lines.append("                    modules[_name] = __import__(f'local_pipeline.{_name}', fromlist=[_name])")
    lines.append("                print('local_pipeline modulleri onceden birakilmis kopyadan yuklendi:', candidate_dir)")
    lines.append("                return modules")
    lines.append("            except Exception:")
    lines.append("                pass")
    lines.append("")
    lines.append("    # 3) PROJECT_REPO_URL verilmisse git clone dene.")
    lines.append("    if PROJECT_REPO_URL:")
    lines.append("        clone_dir = Path('/content/youtube_automation_repo') if os.path.isdir('/content') else Path.cwd() / '_youtube_automation_repo'")
    lines.append("        if not (clone_dir / 'local_pipeline' / (_LOCAL_PIPELINE_MODULE_NAMES[0] + '.py')).is_file():")
    lines.append("            subprocess.run(['git', 'clone', '--depth', '1', PROJECT_REPO_URL, str(clone_dir)], check=False)")
    lines.append("        if all((clone_dir / 'local_pipeline' / f'{_n}.py').is_file() for _n in _LOCAL_PIPELINE_MODULE_NAMES):")
    lines.append("            sys.path.insert(0, str(clone_dir))")
    lines.append("            try:")
    lines.append("                modules = {}")
    lines.append("                for _name in _LOCAL_PIPELINE_MODULE_NAMES:")
    lines.append("                    modules[_name] = __import__(f'local_pipeline.{_name}', fromlist=[_name])")
    lines.append("                print('local_pipeline modulleri PROJECT_REPO_URL klonundan yuklendi:', clone_dir)")
    lines.append("                return modules")
    lines.append("            except Exception:")
    lines.append("                pass")
    lines.append("")
    lines.append("    # 4) Self-contained fallback: bu hucreye gomulu, build sirasinda")
    lines.append("    # local_pipeline/<modul>.py'den birebir uretilmis base64 kopyalari, SHA-256")
    lines.append("    # hash'lerini dogrulayarak diske yazip import et.")
    lines.append("    fallback_dir = Path(tempfile.mkdtemp(prefix='youtube_automation_bootstrap_'))")
    lines.append("    pkg_dir = fallback_dir / 'local_pipeline'")
    lines.append("    pkg_dir.mkdir(parents=True, exist_ok=True)")
    lines.append("    (pkg_dir / '__init__.py').write_text('', encoding='utf-8')")
    lines.append("    for _name, _entry in _LOCAL_PIPELINE_PAYLOADS.items():")
    lines.append("        _source_bytes = base64.b64decode(_entry['b64'])")
    lines.append("        _actual_digest = hashlib.sha256(_source_bytes).hexdigest()")
    lines.append("        if _actual_digest != _entry['sha256']:")
    lines.append('            raise RuntimeError(')
    lines.append('                f"Gomulu local_pipeline.{_name} payloadi bozuk/tutarsiz: "')
    lines.append('                f"beklenen sha256={_entry[\'sha256\']!r}, hesaplanan={_actual_digest!r}. "')
    lines.append('                "Bu notebook yeniden uretilmeli (nb_builder ile)."')
    lines.append('            )')
    lines.append("        (pkg_dir / f'{_name}.py').write_text(_source_bytes.decode('utf-8'), encoding='utf-8')")
    lines.append("    sys.path.insert(0, str(fallback_dir))")
    lines.append("    modules = {}")
    lines.append("    for _name in _LOCAL_PIPELINE_MODULE_NAMES:")
    lines.append("        modules[_name] = __import__(f'local_pipeline.{_name}', fromlist=[_name])")
    lines.append("    print('local_pipeline modulleri: repo/Drive/PROJECT_REPO_URL bulunamadi; gomulu (self-contained, hash dogrulanmis) kopyalar kullanildi.')")
    lines.append("    return modules")
    lines.append("")
    lines.append("_local_pipeline_modules = _load_local_pipeline_modules()")
    for name in module_names:
        lines.append(f"{name} = _local_pipeline_modules[{name!r}]")
    lines.append("")
    return '\n'.join(lines) + '\n'


def youtube_bootstrap_cell(extra_search_note=''):
    """Returns the source for a bootstrap code cell that loads
    local_pipeline.youtube_data via, in order: (1) direct import (local venv /
    repo already on sys.path), (2) a copy left in the Drive-persisted project
    root or an ancestor of cwd, (3) `git clone` from PROJECT_REPO_URL if set,
    (4) a self-contained fallback: a base64-embedded byte-identical snapshot
    of local_pipeline/youtube_data.py, decoded and imported from a temp dir.

    The embedded snapshot is regenerated every time this function runs (it
    reads the canonical file fresh), so there is only ONE source of truth --
    this is not a hand-maintained second copy.
    """
    module_source = YOUTUBE_DATA_MODULE_PATH.read_text(encoding='utf-8')
    b64 = base64.b64encode(module_source.encode('utf-8')).decode('ascii')
    # Wrap the base64 blob across multiple short lines so no single notebook
    # JSON string element is absurdly long; harmless for base64 (no meaning
    # to whitespace once concatenated).
    wrapped = '\n'.join(b64[i:i + 100] for i in range(0, len(b64), 100))

    return (
        "import sys, os, tempfile, subprocess, base64\n"
        "from pathlib import Path\n\n"
        "PROJECT_REPO_URL = ''  # opsiyonel: gercek bir git repo URL'si verirsen modul oradan klonlanir\n\n"
        "_YOUTUBE_DATA_MODULE_B64 = (\n"
        + '\n'.join(f"    '{chunk}'" for chunk in wrapped.splitlines())
        + "\n)\n\n"
        "def _load_youtube_data_module():\n"
        "    # 1) Zaten import edilebiliyorsa (yerel .venv, repo Colab'da klonlanmis) dogrudan kullan.\n"
        "    try:\n"
        "        from local_pipeline import youtube_data as yd\n"
        "        print('local_pipeline.youtube_data: dogrudan import edildi ->', yd.__file__)\n"
        "        return yd\n"
        "    except Exception:\n"
        "        pass\n\n"
        "    # 2) Drive'da veya proje kokunun bir ustunde daha once birakilmis bir kopya var mi?\n"
        "    search_roots = []\n"
        "    try:\n"
        "        search_roots.append(Path(DRIVE_PROJECT_ROOT))\n"
        "    except NameError:\n"
        "        pass\n"
        "    search_roots += [Path.cwd().resolve(), *Path.cwd().resolve().parents]\n"
        "    for root in search_roots:\n"
        "        candidate = Path(root) / 'local_pipeline' / 'youtube_data.py'\n"
        "        if candidate.is_file():\n"
        "            sys.path.insert(0, str(candidate.parent.parent))\n"
        "            try:\n"
        "                from local_pipeline import youtube_data as yd\n"
        "                print('local_pipeline.youtube_data: onceden birakilmis kopyadan yuklendi ->', yd.__file__)\n"
        "                return yd\n"
        "            except Exception:\n"
        "                pass\n\n"
        "    # 3) PROJECT_REPO_URL verilmisse git clone dene.\n"
        "    if PROJECT_REPO_URL:\n"
        "        clone_dir = Path('/content/youtube_automation_repo') if os.path.isdir('/content') else Path.cwd() / '_youtube_automation_repo'\n"
        "        if not (clone_dir / 'local_pipeline' / 'youtube_data.py').is_file():\n"
        "            subprocess.run(['git', 'clone', '--depth', '1', PROJECT_REPO_URL, str(clone_dir)], check=False)\n"
        "        if (clone_dir / 'local_pipeline' / 'youtube_data.py').is_file():\n"
        "            sys.path.insert(0, str(clone_dir))\n"
        "            try:\n"
        "                from local_pipeline import youtube_data as yd\n"
        "                print('local_pipeline.youtube_data: PROJECT_REPO_URL klonundan yuklendi ->', yd.__file__)\n"
        "                return yd\n"
        "            except Exception:\n"
        "                pass\n\n"
        "    # 4) Self-contained fallback: bu hucreye gomulu, build sirasinda\n"
        "    # local_pipeline/youtube_data.py'den birebir uretilmis base64 kopyayi\n"
        "    # diske yazip import et. Bu, uc farkli elle-tutulan kopya YERINE tek\n"
        "    # kaynaktan otomatik uretilen tek bir gomulu yedektir.\n"
        "    module_source = base64.b64decode(_YOUTUBE_DATA_MODULE_B64).decode('utf-8')\n"
        "    fallback_dir = Path(tempfile.mkdtemp(prefix='youtube_automation_bootstrap_'))\n"
        "    pkg_dir = fallback_dir / 'local_pipeline'\n"
        "    pkg_dir.mkdir(parents=True, exist_ok=True)\n"
        "    (pkg_dir / '__init__.py').write_text('', encoding='utf-8')\n"
        "    (pkg_dir / 'youtube_data.py').write_text(module_source, encoding='utf-8')\n"
        "    sys.path.insert(0, str(fallback_dir))\n"
        "    from local_pipeline import youtube_data as yd\n"
        "    print('local_pipeline.youtube_data: repo/Drive/PROJECT_REPO_URL bulunamadi; gomulu (self-contained) kopya kullanildi.')\n"
        "    return yd\n\n"
        "yd = _load_youtube_data_module()\n"
    )


def build_notebook(cells, colab_name):
    """cells: list of ('markdown'|'code', source_str)"""
    nb_cells = []
    for cell_type, source in cells:
        lines = source.splitlines(keepends=True)
        nb_cells.append({
            'cell_type': cell_type,
            'metadata': {},
            'source': lines,
            **({'outputs': [], 'execution_count': None} if cell_type == 'code' else {}),
        })
    nb = {
        'nbformat': 4,
        'nbformat_minor': 0,
        'metadata': {
            'colab': {'name': colab_name, 'provenance': []},
            'kernelspec': {'name': 'python3', 'display_name': 'Python 3'},
        },
        'cells': nb_cells,
    }
    return nb


def write_notebook(path, cells, colab_name):
    nb = build_notebook(cells, colab_name)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(nb, f, ensure_ascii=False, indent=1)
        f.write('\n')
    print('wrote', path, 'cells:', len(cells))
