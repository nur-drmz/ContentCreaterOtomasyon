"""Reusable FFmpeg render-pipeline helpers shared by 06_render_pipeline.ipynb (which
inlines an equivalent, Colab-independent copy of this logic) and verify_local.py
(which imports this module directly for the local render smoke test).

Nothing here uploads or publishes anything. It only reads local files and writes
local files under a caller-supplied output path.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path

STATUS_VALUES = {
    "experimental", "development_placeholder", "review_required",
    "approved", "insufficient_data", "failed", "production_ready",
}

SUBTITLE_WHISPER_MODELS = {'tiny', 'small', 'medium', 'large-v3-turbo'}

CAPTION_THEME_PROFILES = {
    'default': {
        'profile_version': 1,
        'font_name': 'DejaVu Sans',
        'font_size': 72,
        'active_color_ass': '&H0000FFFF',
        'base_color_ass': '&H00FFFFFF',
        'max_words': 4,
        'max_chars': 30,
        'margin_v': 220,
        'uppercase': False,
        'letter_spacing': 0,
    },
    'game_of_thrones': {
        'profile_version': 2,
        'font_name': 'Marcellus SC',
        'font_size': 70,
        'active_color_ass': '&H0000FFFF',
        'base_color_ass': '&H00FFFFFF',
        'max_words': 4,
        'max_chars': 28,
        'margin_v': 220,
        'uppercase': True,
        'letter_spacing': 0.8,
        'transcription_prompt': (
            'Daemon Targaryen, Rhaenyra Targaryen, Viserys Targaryen, Mysaria, '
            'House Targaryen, Westeros, Dragonstone, King\'s Landing, '
            'Iron Throne, Valyrian, Velaryon'
        ),
        'font_filename': 'MarcellusSC-Regular.ttf',
        'font_sha256': '03044ab65228b0554c9a5c1e63df29d4e58a71857e212801aaf768dfd106bdf0',
        'font_url': 'https://raw.githubusercontent.com/google/fonts/main/ofl/marcellussc/MarcellusSC-Regular.ttf',
        'license_filename': 'OFL.txt',
        'license_url': 'https://raw.githubusercontent.com/google/fonts/main/ofl/marcellussc/OFL.txt',
        'bundled_relative_path': 'assets/fonts/marcellus-sc/MarcellusSC-Regular.ttf',
        'bundled_license_relative_path': 'assets/fonts/marcellus-sc/OFL.txt',
    },
    # Kept as an explicit comparison/rollback option. Automatic GOT routing now
    # uses the more subtitle-readable Marcellus SC profile above.
    'game_of_thrones_cinzel': {
        'profile_version': 1,
        'font_name': 'Cinzel',
        'font_size': 68,
        'active_color_ass': '&H0000FFFF',
        'base_color_ass': '&H00FFFFFF',
        'max_words': 4,
        'max_chars': 27,
        'margin_v': 220,
        'uppercase': True,
        'letter_spacing': 1.2,
        'font_filename': 'Cinzel-Variable.ttf',
        'font_sha256': 'f4d83d34d1f6c741193e4acf4b3dff9531e5a67b6aa65228d00a7db72a4e0f34',
        'font_url': 'https://raw.githubusercontent.com/google/fonts/main/ofl/cinzel/Cinzel%5Bwght%5D.ttf',
        'license_filename': 'OFL.txt',
        'license_url': 'https://raw.githubusercontent.com/google/fonts/main/ofl/cinzel/OFL.txt',
        'bundled_relative_path': 'assets/fonts/cinzel/Cinzel-Variable.ttf',
        'bundled_license_relative_path': 'assets/fonts/cinzel/OFL.txt',
    },
}

GAME_OF_THRONES_THEME_KEYWORDS = (
    'game of thrones', 'house of the dragon', 'targaryen', 'westeros',
    'iron throne', 'dragonstone', 'daemon', 'rhaenyra', 'winterfell',
)


def resolve_caption_theme(requested_theme: str = 'auto', context_text: str = '') -> dict:
    """Resolve a deterministic caption profile from explicit theme or content."""
    requested = str(requested_theme or 'auto').strip().lower()
    if requested != 'auto':
        if requested not in CAPTION_THEME_PROFILES:
            raise ValueError(
                f"Bilinmeyen caption theme: {requested!r}; "
                f"mevcut profiller: {sorted(CAPTION_THEME_PROFILES)}"
            )
        resolved, reason = requested, 'explicit_theme'
    else:
        normalized_context = str(context_text or '').lower()
        matched = next(
            (keyword for keyword in GAME_OF_THRONES_THEME_KEYWORDS if keyword in normalized_context),
            None,
        )
        if matched:
            resolved, reason = 'game_of_thrones', f'keyword:{matched}'
        else:
            resolved, reason = 'default', 'no_theme_keyword_match'
    return {
        'requested_theme': requested,
        'resolved_theme': resolved,
        'resolution_reason': reason,
        'profile': dict(CAPTION_THEME_PROFILES[resolved]),
    }


def _sha256_file_full(path) -> str:
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(65536), b''):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_caption_theme_font(theme_result: dict, cache_dir) -> dict:
    """Make a profile font available locally and verify its pinned SHA-256.

    Bundled assets win. In single-file Colab mode the exact official Google
    Fonts file is downloaded once into the Drive-backed render cache. A themed
    render fails loudly rather than silently switching to a different font.
    """
    profile = theme_result['profile']
    filename = profile.get('font_filename')
    if not filename:
        return {'font_path': None, 'fonts_dir': None, 'source': 'system_font'}

    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    destination = cache_dir / filename
    expected_sha256 = profile['font_sha256'].lower()
    if destination.is_file() and _sha256_file_full(destination).lower() == expected_sha256:
        return {'font_path': str(destination), 'fonts_dir': str(cache_dir), 'source': 'verified_cache'}

    project_root = Path(__file__).resolve().parent.parent
    bundled_path = project_root / profile['bundled_relative_path']
    if bundled_path.is_file() and _sha256_file_full(bundled_path).lower() == expected_sha256:
        shutil.copy2(bundled_path, destination)
        bundled_license = project_root / profile.get('bundled_license_relative_path', '')
        if bundled_license.is_file():
            shutil.copy2(bundled_license, cache_dir / profile.get('license_filename', 'OFL.txt'))
        source = 'bundled_verified_asset'
    else:
        import urllib.request

        temporary = cache_dir / f'.{filename}.download'
        try:
            with urllib.request.urlopen(profile['font_url'], timeout=45) as response:
                temporary.write_bytes(response.read())
            if _sha256_file_full(temporary).lower() != expected_sha256:
                raise RuntimeError('indirilen font SHA-256 dogrulamasi basarisiz')
            temporary.replace(destination)
            try:
                with urllib.request.urlopen(profile['license_url'], timeout=20) as response:
                    (cache_dir / profile.get('license_filename', 'OFL.txt')).write_bytes(response.read())
            except Exception as license_exc:
                print(f'UYARI: font lisans kopyasi indirilemedi: {license_exc}')
            source = 'official_download_verified'
        except Exception as exc:
            if temporary.exists():
                temporary.unlink()
            raise RuntimeError(
                f"Tema fontu hazirlanamadi ({profile['font_name']}): {exc}. "
                "Farkli fonta sessiz gecis yapilmadi."
            ) from exc

    if _sha256_file_full(destination).lower() != expected_sha256:
        raise RuntimeError(f'Tema fontu son SHA-256 kontrolunden gecemedi: {destination}')
    return {'font_path': str(destination), 'fonts_dir': str(cache_dir), 'source': source}


def resolve_ffmpeg_binaries():
    """Returns (ffmpeg_bin, ffprobe_bin). ffprobe_bin may be None if not found —
    imageio_ffmpeg only ships an ffmpeg binary, not ffprobe, so on a machine with
    no system ffmpeg install ffprobe_bin is commonly None. Callers must use
    probe_media() (which falls back to parsing `ffmpeg -i`) rather than assuming
    ffprobe_bin is usable."""
    import imageio_ffmpeg

    ffmpeg_bin = shutil.which('ffmpeg') or imageio_ffmpeg.get_ffmpeg_exe()
    ffprobe_bin = shutil.which('ffprobe')
    return ffmpeg_bin, ffprobe_bin


def escape_path_for_filter(path) -> str:
    """Escape a filesystem path for embedding inside an ffmpeg filtergraph string
    (e.g. subtitles=<path>). Converts backslashes to forward slashes and escapes
    a Windows drive-letter colon (C: -> C\\:)."""
    text = str(path).replace('\\', '/')
    if len(text) > 1 and text[1] == ':':
        text = text[0] + '\\:' + text[2:]
    return text


def build_vertical_filtergraph(mode: str, width: int, height: int, blur_sigma: int = 20) -> str:
    """Returns an ffmpeg filtergraph fragment (no leading/trailing brackets on the
    overall chain) that reframes [0:v] into a vertical WxH output labeled [framed].
    mode: 'center_crop' or 'blurred_background'.
    """
    if mode == 'center_crop':
        return (
            f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height}[framed]"
        )
    if mode == 'blurred_background':
        return (
            f"[0:v]split=2[bg_src][fg_src];"
            f"[bg_src]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},gblur=sigma={blur_sigma}[bg];"
            f"[fg_src]scale={width}:-2:force_original_aspect_ratio=decrease[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2[framed]"
        )
    raise ValueError(f"bilinmeyen render mode: {mode!r} (beklenen: center_crop, blurred_background)")


def build_audio_filtergraph(has_original_audio: bool, ducking_enabled: bool,
                             duck_ratio: float, base_volume: float, narration_volume: float) -> str:
    """Returns an ffmpeg filtergraph fragment mixing [0:a] (original clip audio,
    only referenced if has_original_audio) with [1:a] (narration) into [aout]."""
    if not has_original_audio:
        return f"[1:a]volume={narration_volume}[aout]"
    if ducking_enabled:
        return (
            f"[0:a]volume={base_volume}[bgvol];"
            f"[1:a]volume={narration_volume}[narvol];"
            f"[narvol]asplit=2[narside][narmix];"
            f"[bgvol][narside]sidechaincompress=threshold=0.05:ratio={duck_ratio}:attack=5:release=300[ducked];"
            f"[ducked][narmix]amix=inputs=2:duration=longest:dropout_transition=200[aout]"
        )
    return (
        f"[0:a]volume={base_volume}[bgvol];"
        f"[1:a]volume={narration_volume}[narvol];"
        f"[bgvol][narvol]amix=inputs=2:duration=longest:dropout_transition=200[aout]"
    )


def run_ffmpeg(ffmpeg_bin: str, args: list[str], *, progress=None,
               total_duration_sec: float | None = None) -> subprocess.CompletedProcess:
    """Runs ffmpeg with -y -hide_banner -loglevel error + args. Raises RuntimeError
    with the captured stderr (truncated) on non-zero exit.

    When BOTH `progress` (an observer with .count()) and `total_duration_sec` are
    given, ffmpeg's own `-progress` stream is read and encoded-seconds-so-far is
    reported against the known output duration. That is a genuine measurement,
    not an estimate: ffmpeg states how much output it has written, and the total
    output length is known before the call.

    Without both, the original single-shot path is used unchanged, so every
    notebook call site behaves exactly as before.
    """
    if progress is None or not total_duration_sec or total_duration_sec <= 0:
        cmd = [ffmpeg_bin, '-y', '-hide_banner', '-loglevel', 'error', *args]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f'ffmpeg basarisiz (exit {proc.returncode}): {proc.stderr[-3000:]}')
        return proc

    cmd = [ffmpeg_bin, '-y', '-hide_banner', '-loglevel', 'error',
           '-progress', 'pipe:1', '-nostats', *args]
    total = float(total_duration_sec)
    popen = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, bufsize=1)
    # stderr is drained on its own thread: with -loglevel error it is normally
    # empty, but a failing run can emit enough to fill the pipe and deadlock the
    # stdout reader below.
    captured_err: list[str] = []

    def _drain():
        try:
            captured_err.append(popen.stderr.read() or '')
        except Exception:  # noqa: BLE001
            pass

    drainer = threading.Thread(target=_drain, daemon=True)
    drainer.start()
    try:
        for line in popen.stdout:
            key, _, value = line.strip().partition('=')
            if key not in ('out_time_us', 'out_time_ms'):
                continue
            try:
                micros = float(value)
            except ValueError:
                continue          # ffmpeg emits 'N/A' before the first frame
            # `out_time_ms` is microseconds in ffmpeg despite the name; both
            # keys carry the same unit, so one conversion covers them.
            done = min(total, micros / 1_000_000.0)
            progress.count(round(done, 1), round(total, 1), unit='sn',
                           message=f'FFmpeg {done:.0f}/{total:.0f} sn kodladi')
    finally:
        popen.stdout.close()
        returncode = popen.wait()
        drainer.join(timeout=5.0)
    stderr = captured_err[0] if captured_err else ''
    if returncode != 0:
        raise RuntimeError(f'ffmpeg basarisiz (exit {returncode}): {stderr[-3000:]}')
    progress.count(round(total, 1), round(total, 1), unit='sn',
                   message=f'FFmpeg {total:.0f}/{total:.0f} sn kodladi')
    return subprocess.CompletedProcess(cmd, returncode, '', stderr)


def ffprobe_streams(ffprobe_bin: str, path) -> dict:
    """Returns parsed `ffprobe -show_streams -show_format` JSON for path.
    Raises RuntimeError if ffprobe_bin is falsy or the call fails — callers that
    want automatic fallback to ffmpeg-based parsing should use probe_media()
    instead of calling this directly."""
    if not ffprobe_bin:
        raise RuntimeError('ffprobe bulunamadi.')
    proc = subprocess.run(
        [ffprobe_bin, '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(path)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f'ffprobe basarisiz: {proc.stderr[-2000:]}')
    return json.loads(proc.stdout)


def probe_via_ffmpeg_fallback(ffmpeg_bin: str, path) -> dict:
    """Best-effort media probe by parsing `ffmpeg -i <path>` stderr, for machines
    that have ffmpeg (e.g. via imageio_ffmpeg) but no separate ffprobe binary.

    `ffmpeg -i <path>` with no output always exits non-zero — that is expected
    here and is NOT treated as failure. Only a genuinely unreadable file (no
    'Duration:' line and no stream lines at all) raises.

    Returns a dict shaped like a trimmed ffprobe JSON response:
    {'streams': [{'codec_type': 'video', 'width': int|None, 'height': int|None}, ...],
     'format': {'duration': str|None}}
    This is less precise than real ffprobe (e.g. width/height parsed via regex
    from human-readable text) but is sufficient for the has-video/has-audio/
    resolution/duration checks used by validate_render().
    """
    if not ffmpeg_bin:
        raise RuntimeError('ffmpeg bulunamadi; fallback probe da yapilamiyor.')
    proc = subprocess.run(
        [ffmpeg_bin, '-hide_banner', '-i', str(path)],
        capture_output=True, text=True,
    )
    stderr = proc.stderr or ''

    duration_match = re.search(r'Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)', stderr)
    video_match = re.search(r'Stream #\d+:\d+[^\n]*?Video:[^\n]*', stderr)
    audio_match = re.search(r'Stream #\d+:\d+[^\n]*?Audio:[^\n]*', stderr)

    if duration_match is None and video_match is None and audio_match is None:
        raise RuntimeError(f'ffmpeg fallback probe dosyayi okuyamadi: {stderr[-1500:]}')

    streams = []
    if video_match:
        res_match = re.search(r'(?<!\d)(\d{2,5})x(\d{2,5})(?!\d)', video_match.group(0))
        width = int(res_match.group(1)) if res_match else None
        height = int(res_match.group(2)) if res_match else None
        streams.append({'codec_type': 'video', 'width': width, 'height': height})
    if audio_match:
        streams.append({'codec_type': 'audio'})

    duration_str = None
    if duration_match:
        hours, minutes, seconds = duration_match.groups()
        duration_str = str(int(hours) * 3600 + int(minutes) * 60 + float(seconds))

    return {'streams': streams, 'format': {'duration': duration_str}}


def probe_media(ffmpeg_bin: str, ffprobe_bin: str, path) -> tuple[dict, str]:
    """Probes `path` using ffprobe when available, otherwise falls back to
    parsing `ffmpeg -i`. Returns (info_dict, backend) where backend is
    'ffprobe' or 'ffmpeg_fallback'. Raises RuntimeError only if BOTH the
    ffprobe attempt (when available) and the fallback fail — a missing
    ffprobe binary alone must never raise here, it must trigger the fallback."""
    if ffprobe_bin:
        try:
            return ffprobe_streams(ffprobe_bin, path), 'ffprobe'
        except Exception:
            pass  # fall through to the ffmpeg-based fallback below
    return probe_via_ffmpeg_fallback(ffmpeg_bin, path), 'ffmpeg_fallback'


def validate_render(ffmpeg_bin: str, ffprobe_bin: str, path, expected_width: int, expected_height: int,
                     min_duration_sec: float = 0.5, max_duration_sec: float = 3600.0) -> dict:
    """Runs a set of sanity checks against a rendered file. Returns a dict with a
    'passed' bool, a 'checks' dict of individual check -> bool, and 'details'
    (including which probe backend was used: 'ffprobe' or 'ffmpeg_fallback').

    This never silently reports success when media info could not be determined
    — if both ffprobe and the ffmpeg fallback fail to read the file, every
    stream/resolution/duration check is explicitly False and 'passed' is False.
    """
    checks: dict = {}
    details: dict = {'probe_backend': None}
    path = Path(path)

    checks['file_exists'] = path.is_file()
    checks['file_nonempty'] = path.is_file() and path.stat().st_size > 0

    if not checks['file_exists']:
        checks.update({
            'has_video_stream': False, 'has_audio_stream': False,
            'resolution_correct': False, 'duration_sensible': False, 'probe_readable': False,
        })
        return {'passed': False, 'checks': checks, 'details': details}

    try:
        info, backend = probe_media(ffmpeg_bin, ffprobe_bin, path)
        checks['probe_readable'] = True
        details['probe_backend'] = backend
    except Exception as exc:
        checks['probe_readable'] = False
        checks['has_video_stream'] = False
        checks['has_audio_stream'] = False
        checks['resolution_correct'] = False
        checks['duration_sensible'] = False
        details['probe_error'] = str(exc)
        return {'passed': False, 'checks': checks, 'details': details}

    streams = info.get('streams', [])
    video_streams = [s for s in streams if s.get('codec_type') == 'video']
    audio_streams = [s for s in streams if s.get('codec_type') == 'audio']
    checks['has_video_stream'] = len(video_streams) > 0
    checks['has_audio_stream'] = len(audio_streams) > 0

    if video_streams:
        w, h = video_streams[0].get('width'), video_streams[0].get('height')
        details['resolution'] = f'{w}x{h}'
        checks['resolution_correct'] = (w == expected_width and h == expected_height)
    else:
        checks['resolution_correct'] = False

    duration = None
    fmt = info.get('format', {})
    if fmt.get('duration'):
        try:
            duration = float(fmt['duration'])
        except (TypeError, ValueError):
            duration = None
    details['duration_sec'] = duration
    checks['duration_sensible'] = duration is not None and min_duration_sec <= duration <= max_duration_sec

    passed = all(bool(v) for v in checks.values())
    return {'passed': passed, 'checks': checks, 'details': details}


# ---------------------------------------------------------------------------
# Subtitle generation (shared by 06_render_pipeline.ipynb and the master
# notebook's RUN_RENDER phase).
# ---------------------------------------------------------------------------

def format_srt_timestamp(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    millis = int(round((secs - int(secs)) * 1000))
    return f'{int(hours):02d}:{int(minutes):02d}:{int(secs):02d},{millis:03d}'


def build_cues_from_segments(segments: list[dict], max_chars: int = 42) -> list[dict]:
    """segments: [{'start','end','text'}, ...] (faster-whisper style). Splits
    any segment longer than max_chars into multiple time-proportional cues."""
    cues = []
    for seg in segments:
        text = seg['text'].strip()
        if not text:
            continue
        if len(text) <= max_chars:
            cues.append({'start': seg['start'], 'end': seg['end'], 'text': text})
            continue
        words = text.split()
        seg_duration = max(seg['end'] - seg['start'], 0.5)
        chunk, chunk_len = [], 0
        chunk_start = seg['start']
        total_chars = max(len(text), 1)
        consumed_chars = 0
        for word in words:
            chunk.append(word)
            chunk_len += len(word) + 1
            consumed_chars += len(word) + 1
            if chunk_len >= max_chars:
                frac = consumed_chars / total_chars
                chunk_end = seg['start'] + seg_duration * frac
                cues.append({'start': chunk_start, 'end': chunk_end, 'text': ' '.join(chunk)})
                chunk_start = chunk_end
                chunk, chunk_len = [], 0
        if chunk:
            cues.append({'start': chunk_start, 'end': seg['end'], 'text': ' '.join(chunk)})
    return cues


def write_srt(cues: list[dict], path) -> None:
    lines = []
    for idx, cue in enumerate(cues, start=1):
        lines.append(str(idx))
        lines.append(f"{format_srt_timestamp(cue['start'])} --> {format_srt_timestamp(cue['end'])}")
        lines.append(cue['text'])
        lines.append('')
    Path(path).write_text('\n'.join(lines), encoding='utf-8')


def transcribe_for_subtitles(narration_audio_path, fallback_text: str = '', max_chars_per_cue: int = 42,
                              whisper_model_size: str = 'small', language: str | None = None,
                              initial_prompt: str = ''):
    """Transcribes narration_audio_path with faster-whisper (word-timestamp
    aligned) to build subtitle cues. On any failure (missing dependency,
    corrupt audio, etc.) falls back to splitting fallback_text into
    proportional-duration chunks across the narration's probed duration --
    never raises, always returns (cues, subtitle_source)."""
    try:
        from faster_whisper import WhisperModel

        model = WhisperModel(whisper_model_size, device='cpu', compute_type='int8')
        segments_iter, _info = model.transcribe(
            str(narration_audio_path), word_timestamps=True, vad_filter=True,
            condition_on_previous_text=False, language=language, beam_size=5,
            initial_prompt=(initial_prompt or None),
        )
        segments = [{'start': s.start, 'end': s.end, 'text': s.text} for s in segments_iter]
        if not segments:
            raise RuntimeError('faster-whisper bos transkript dondurdu')
        return build_cues_from_segments(segments, max_chars_per_cue), 'faster-whisper-alignment'
    except Exception as exc:
        print(f'UYARI: faster-whisper hizalamasi basarisiz ({exc}); proportional-fallback kullanilacak.')
        ffmpeg_bin, ffprobe_bin = resolve_ffmpeg_binaries()
        try:
            narration_info, _backend = probe_media(ffmpeg_bin, ffprobe_bin, narration_audio_path)
            narration_duration = float(narration_info.get('format', {}).get('duration') or 10.0)
        except Exception:
            narration_duration = 10.0
        words = (fallback_text or '').split() or ['(script metni yok)']
        chunk_size = 6
        word_chunks = [words[i:i + chunk_size] for i in range(0, len(words), chunk_size)]
        per_chunk = narration_duration / max(len(word_chunks), 1)
        cues = [
            {'start': i * per_chunk, 'end': (i + 1) * per_chunk, 'text': ' '.join(chunk)}
            for i, chunk in enumerate(word_chunks)
        ]
        return cues, 'proportional-fallback'


def transcribe_for_dynamic_captions(audio_path, fallback_text: str = '',
                                    max_chars_per_cue: int = 42,
                                    whisper_model_size: str = 'small',
                                    language: str | None = None,
                                    initial_prompt: str = ''):
    """Return word timestamps plus ordinary cues for dynamic caption rendering.

    The word list drives active-word highlighting. Ordinary cues are returned
    as a deterministic fallback when word alignment is unavailable.
    """
    try:
        from faster_whisper import WhisperModel

        model = WhisperModel(whisper_model_size, device='cpu', compute_type='int8')
        segments_iter, _info = model.transcribe(
            str(audio_path), word_timestamps=True, vad_filter=True,
            condition_on_previous_text=False, language=language, beam_size=5,
            initial_prompt=(initial_prompt or None),
        )
        segments = []
        words = []
        for segment in segments_iter:
            segments.append({'start': float(segment.start), 'end': float(segment.end),
                             'text': segment.text})
            for word in (segment.words or []):
                text = str(word.word or '').strip()
                if not text or word.start is None or word.end is None:
                    continue
                start, end = float(word.start), float(word.end)
                if end <= start:
                    end = start + 0.08
                words.append({
                    'start': round(start, 3), 'end': round(end, 3),
                    'text': text, 'probability': round(float(word.probability or 0.0), 4),
                })
        if not segments:
            raise RuntimeError('faster-whisper bos transkript dondurdu')
        cues = build_cues_from_segments(segments, max_chars_per_cue)
        if not words:
            return [], cues, 'faster-whisper-segment-fallback'
        return words, cues, 'faster-whisper-word-alignment'
    except Exception as exc:
        print(f'UYARI: dinamik kelime hizalamasi basarisiz ({exc}); duz SRT fallback kullanilacak.')
        cues, source = transcribe_for_subtitles(
            audio_path, fallback_text, max_chars_per_cue,
            whisper_model_size=whisper_model_size,
            language=language, initial_prompt=initial_prompt,
        )
        return [], cues, source


def _ass_timestamp(seconds: float) -> str:
    total_centiseconds = max(0, int(round(float(seconds) * 100)))
    hours, remainder = divmod(total_centiseconds, 360000)
    minutes, remainder = divmod(remainder, 6000)
    secs, centiseconds = divmod(remainder, 100)
    return f'{hours}:{minutes:02d}:{secs:02d}.{centiseconds:02d}'


def _ass_escape(text: str) -> str:
    return str(text).replace('\\', '／').replace('{', '(').replace('}', ')').replace('\n', ' ')


def group_caption_words(words: list[dict], max_words: int = 4,
                        max_chars: int = 30, max_duration_sec: float = 2.8,
                        break_gap_sec: float = 0.65) -> list[list[dict]]:
    """Group aligned words into short, stable, phone-readable caption cards."""
    groups, current = [], []
    for word in words:
        if current:
            candidate_text = ' '.join([item['text'] for item in current] + [word['text']])
            duration = float(word['end']) - float(current[0]['start'])
            gap = float(word['start']) - float(current[-1]['end'])
            previous_ends_sentence = current[-1]['text'].rstrip().endswith(('.', '!', '?', ':', ';'))
            should_break = (
                len(current) >= max(1, int(max_words))
                or len(candidate_text) > max(8, int(max_chars))
                or duration > max(0.5, float(max_duration_sec))
                or gap >= max(0.1, float(break_gap_sec))
                or previous_ends_sentence
            )
            if should_break:
                groups.append(current)
                current = []
        current.append(word)
    if current:
        groups.append(current)
    return groups


def write_active_word_ass(words: list[dict], path, *, play_res_x: int = 1080,
                          play_res_y: int = 1920, font_name: str = 'DejaVu Sans',
                          font_size: int = 72, margin_v: int = 220,
                          max_words: int = 4, max_chars: int = 30,
                          active_color_ass: str = '&H0000FFFF',
                          base_color_ass: str = '&H00FFFFFF',
                          uppercase: bool = False,
                          letter_spacing: float = 0.0) -> dict:
    """Write ASS events where only the currently spoken word is yellow.

    Each event repeats one short caption card and changes the inline colour of
    exactly one word. This avoids the "previous words stay highlighted" effect
    of ordinary karaoke tags and gives a CapCut-like moving active word.
    """
    groups = group_caption_words(words, max_words=max_words, max_chars=max_chars)
    if not groups:
        raise ValueError('Dinamik ASS icin kelime zaman damgasi yok')

    header = [
        '[Script Info]',
        'ScriptType: v4.00+',
        f'PlayResX: {int(play_res_x)}',
        f'PlayResY: {int(play_res_y)}',
        'ScaledBorderAndShadow: yes',
        'WrapStyle: 2',
        '',
        '[V4+ Styles]',
        'Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, '
        'BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, '
        'BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding',
        f'Style: ActiveWord,{font_name},{int(font_size)},{base_color_ass},{active_color_ass},'
        f'&H00000000,&H78000000,-1,0,0,0,100,100,{float(letter_spacing):g},0,1,5,1,2,'
        f'80,80,{int(margin_v)},1',
        '',
        '[Events]',
        'Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text',
    ]
    events = []
    for group_index, group in enumerate(groups):
        for index, active_word in enumerate(group):
            start = float(active_word['start'])
            if index + 1 < len(group):
                next_event_start = float(group[index + 1]['start'])
            elif group_index + 1 < len(groups):
                next_event_start = float(groups[group_index + 1][0]['start'])
            else:
                next_event_start = None
            if next_event_start is not None:
                # Never let two caption cards coexist. Whisper word intervals
                # may overlap slightly; switching just before the next event
                # removes the faint previous-line accumulation seen in Colab.
                end = min(float(active_word['end']) + 0.08, next_event_start - 0.01)
                if end <= start:
                    continue
            else:
                end = float(active_word['end']) + 0.08
            rendered_words = []
            for word_index, word in enumerate(group):
                word_text = str(word['text']).upper() if uppercase else str(word['text'])
                safe_word = _ass_escape(word_text)
                if word_index == index:
                    rendered_words.append(
                        f'{{\\c{active_color_ass}&\\b1}}{safe_word}'
                        f'{{\\c{base_color_ass}&\\b1}}'
                    )
                else:
                    rendered_words.append(safe_word)
            event_text = ' '.join(rendered_words)
            events.append(
                f"Dialogue: 0,{_ass_timestamp(start)},{_ass_timestamp(end)},"
                f"ActiveWord,,0,0,0,,{event_text}"
            )
    Path(path).write_text('\n'.join(header + events) + '\n', encoding='utf-8')
    return {
        'group_count': len(groups), 'event_count': len(events),
        'uppercase': bool(uppercase), 'letter_spacing': float(letter_spacing),
    }


def sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(65536), b''):
            h.update(chunk)
    return h.hexdigest()[:16]


def resolve_render_source(hot_moments_data: dict, selected_moment: dict, source_video_path_override: str = ''):
    """Picks which file to seek into for the base clip: an explicit override,
    else raw_video_path, else analysis_clip_path, else the moment's own
    preview_path. Returns (chosen_path, resolution_mode, seek_start, seek_duration,
    description). Raises FileNotFoundError if none of the candidates exist on
    disk -- this must be a clear, actionable error, never a silent fallback to
    fabricated timestamps."""
    source_meta = hot_moments_data.get('source') or {}
    raw_video_path = source_meta.get('raw_video_path')
    analysis_clip_path = source_meta.get('analysis_clip_path')

    start_sec = selected_moment.get('source_start_sec')
    end_sec = selected_moment.get('source_end_sec')
    aligned_start_sec = selected_moment.get('aligned_start_sec')
    aligned_end_sec = selected_moment.get('aligned_end_sec')
    for label, value in [
        ('source_start_sec', start_sec), ('source_end_sec', end_sec),
        ('aligned_start_sec', aligned_start_sec), ('aligned_end_sec', aligned_end_sec),
    ]:
        if not isinstance(value, (int, float)):
            raise ValueError(f"Secili anda '{label}' sayisal degil: {value!r}. hot_moments.json bozuk olabilir.")
    if not (end_sec > start_sec):
        raise ValueError(f"source_end_sec ({end_sec}) source_start_sec'ten ({start_sec}) buyuk degil.")
    if not (aligned_end_sec > aligned_start_sec):
        raise ValueError(f"aligned_end_sec ({aligned_end_sec}) aligned_start_sec'ten ({aligned_start_sec}) buyuk degil.")

    if source_video_path_override and Path(source_video_path_override).is_file():
        chosen_path, resolution_mode = source_video_path_override, 'raw'
    elif raw_video_path and Path(raw_video_path).is_file():
        chosen_path, resolution_mode = raw_video_path, 'raw'
    elif analysis_clip_path and Path(analysis_clip_path).is_file():
        chosen_path, resolution_mode = analysis_clip_path, 'analysis_clip'
    elif selected_moment.get('preview_path') and Path(selected_moment['preview_path']).is_file():
        chosen_path, resolution_mode = selected_moment['preview_path'], 'preview'
    else:
        raise FileNotFoundError(
            'Ne raw_video_path, ne analysis_clip_path, ne de an onizleme klibi diskte bulunabildi. '
            'source_video_path_override degiskenini elle ayarla.'
        )

    if resolution_mode == 'raw':
        seek_start, seek_duration = start_sec, end_sec - start_sec
        description = f'orijinal kaynak video (raw_video_path): {chosen_path} [source {start_sec}-{end_sec}s]'
    elif resolution_mode == 'analysis_clip':
        seek_start, seek_duration = aligned_start_sec, aligned_end_sec - aligned_start_sec
        description = (
            f"analiz klibi (analysis_clip_path, zaten {source_meta.get('clip_start_sec')}s'den kirpilmis): "
            f'{chosen_path} [aligned {aligned_start_sec}-{aligned_end_sec}s]'
        )
    else:
        seek_start, seek_duration = 0.0, aligned_end_sec - aligned_start_sec
        description = f'an onizleme klibi (preview_path, zaten tam ana kirpilmis, ek seek YOK): {chosen_path}'

    return chosen_path, resolution_mode, seek_start, seek_duration, description


def run_original_audio_subtitle_pipeline(
        hot_moments_data: dict, moment_selection: dict, hot_moments_path,
        moment_selection_path, render_dir, render_mode: str = 'blurred_background',
        target_width: int = 1080, target_height: int = 1920,
        preview_mode: bool = True, subtitle_fontsize: int = 20,
        subtitle_margin_v: int = 90, subtitle_max_chars_per_cue: int = 42,
        subtitle_whisper_model_size: str = 'small',
        subtitle_language: str = 'auto',
        caption_style: str = 'active_word_yellow',
        caption_theme: str = 'auto', caption_font_name: str | None = None,
        caption_font_size: int | None = None,
        caption_active_color_ass: str | None = None,
        caption_base_color_ass: str | None = None,
        caption_max_words: int | None = None, caption_max_chars: int | None = None,
        caption_margin_v: int | None = None,
        source_video_path_override: str = '', progress=None) -> dict:
    """Render the selected source moment with its ORIGINAL audio and subtitles.

    This is the canonical main-pipeline path for ordinary dialogue clips. It
    does not call an LLM or TTS provider. The selected source clip is cut first,
    then its own audio is transcribed so subtitle timestamps are relative to the
    rendered clip. The original audio stream remains the final audio stream.
    """
    report = progress if progress is not None else _NullProgress()

    from . import video_analysis

    selection_errors = video_analysis.validate_moment_selection(
        moment_selection, hot_moments_data, require_preview=True,
    )
    if selection_errors:
        raise ValueError(f"Moment secimi gecersiz: {selection_errors}")

    selected_rank = moment_selection.get('selected_moment_rank')
    moments_by_rank = {moment['rank']: moment for moment in hot_moments_data['hot_moments']}
    if selected_rank not in moments_by_rank:
        raise ValueError(
            f"Secilen rank={selected_rank} hot_moments.json icinde yok. "
            f"Mevcut ranklar: {sorted(moments_by_rank)}"
        )
    selected_moment = moments_by_rank[selected_rank]

    render_dir = Path(render_dir)
    render_dir.mkdir(parents=True, exist_ok=True)
    report.stage('preparation', 'Girdiler dogrulaniyor')
    ffmpeg_bin, ffprobe_bin = resolve_ffmpeg_binaries()
    chosen_path, resolution_mode, seek_start, seek_duration, source_description = resolve_render_source(
        hot_moments_data, selected_moment, source_video_path_override,
    )
    theme_context = ' '.join([
        str(chosen_path), str(source_description),
        str(selected_moment.get('transcript') or ''),
        str(selected_moment.get('reason') or ''),
    ])
    caption_theme_result = resolve_caption_theme(caption_theme, theme_context)
    caption_profile = caption_theme_result['profile']
    if subtitle_whisper_model_size not in SUBTITLE_WHISPER_MODELS:
        raise ValueError(
            f'desteklenmeyen subtitle Whisper modeli: {subtitle_whisper_model_size!r}; '
            f'beklenen: {sorted(SUBTITLE_WHISPER_MODELS)}'
        )
    requested_subtitle_language = str(subtitle_language or 'auto').strip().lower()
    if requested_subtitle_language == 'auto':
        detected_language = str(hot_moments_data.get('transcript_language') or '').strip().lower()
        resolved_subtitle_language = detected_language or None
        subtitle_language_source = (
            'analysis_transcript_language' if detected_language else 'whisper_auto_detection')
    elif re.fullmatch(r'[a-z]{2}', requested_subtitle_language):
        resolved_subtitle_language = requested_subtitle_language
        subtitle_language_source = 'explicit'
    else:
        raise ValueError(
            "subtitle_language 'auto' veya iki harfli bir Whisper dil kodu olmali "
            f"(ornek: 'en', 'tr'); alinan={subtitle_language!r}"
        )
    transcription_prompt = str(caption_profile.get('transcription_prompt') or '').strip()
    resolved_font_name = caption_font_name or caption_profile['font_name']
    resolved_font_size = int(caption_font_size or caption_profile['font_size'])
    resolved_active_color = caption_active_color_ass or caption_profile['active_color_ass']
    resolved_base_color = caption_base_color_ass or caption_profile['base_color_ass']
    resolved_max_words = int(caption_max_words or caption_profile['max_words'])
    resolved_max_chars = int(caption_max_chars or caption_profile['max_chars'])
    resolved_margin_v = int(caption_margin_v or caption_profile['margin_v'])
    resolved_uppercase = bool(caption_profile.get('uppercase', False))
    resolved_letter_spacing = float(caption_profile.get('letter_spacing', 0.0))
    caption_font_asset = ensure_caption_theme_font(caption_theme_result, render_dir / 'fonts')

    if preview_mode:
        encode_preset, encode_crf, encode_scale = 'ultrafast', 30, 0.5
    else:
        encode_preset, encode_crf, encode_scale = 'slow', 18, 1.0
    render_width = max(2, int(target_width * encode_scale) // 2 * 2)
    render_height = max(2, int(target_height * encode_scale) // 2 * 2)

    base_clip_path = render_dir / 'base_clip.mp4'
    report.note('Secilen moment kesiliyor')
    run_ffmpeg(ffmpeg_bin, [
        '-ss', str(seek_start), '-i', str(chosen_path), '-t', str(seek_duration),
        '-map', '0:v:0', '-map', '0:a:0?',
        '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20', '-pix_fmt', 'yuv420p',
        '-c:a', 'aac', str(base_clip_path),
    ])
    base_info, _base_backend = probe_media(ffmpeg_bin, ffprobe_bin, base_clip_path)
    has_original_audio = any(stream.get('codec_type') == 'audio' for stream in base_info.get('streams', []))
    if not has_original_audio:
        raise RuntimeError('Secilen kaynak klipte ses akisi yok; orijinal-ses altyazi modu calisamaz.')

    fallback_text = (selected_moment.get('transcript') or '').strip()
    report.stage('subtitles', 'Orijinal sesten dinamik altyazi')
    words, cues, subtitle_source = transcribe_for_dynamic_captions(
        base_clip_path, fallback_text, subtitle_max_chars_per_cue,
        whisper_model_size=subtitle_whisper_model_size,
        language=resolved_subtitle_language,
        initial_prompt=transcription_prompt,
    )
    if not cues:
        raise RuntimeError('Orijinal sesten altyazi cue uretilemedi.')
    subtitles_srt_path = render_dir / 'subtitles.srt'
    write_srt(cues, subtitles_srt_path)
    report.count(len(cues), len(cues), unit='altyazi',
                 message=f'{len(cues)} altyazi satiri ({subtitle_source})')

    vertical_graph = build_vertical_filtergraph(render_mode, render_width, render_height)
    subtitles_ass_path = render_dir / 'subtitles_dynamic.ass'
    dynamic_caption_meta = None
    if caption_style == 'active_word_yellow' and words:
        dynamic_caption_meta = write_active_word_ass(
            words, subtitles_ass_path,
            play_res_x=target_width, play_res_y=target_height,
            font_name=resolved_font_name, font_size=resolved_font_size,
            margin_v=resolved_margin_v, max_words=resolved_max_words,
            max_chars=resolved_max_chars,
            active_color_ass=resolved_active_color,
            base_color_ass=resolved_base_color,
            uppercase=resolved_uppercase,
            letter_spacing=resolved_letter_spacing,
        )
        subs_path_escaped = escape_path_for_filter(subtitles_ass_path)
        fonts_dir = caption_font_asset.get('fonts_dir')
        fonts_dir_option = (
            f":fontsdir='{escape_path_for_filter(fonts_dir)}'" if fonts_dir else ''
        )
        subtitle_graph = f"[framed]ass='{subs_path_escaped}'{fonts_dir_option}:shaping=auto[vout]"
        subtitle_format = 'ass'
        dynamic_word_highlight = True
    else:
        subs_path_escaped = escape_path_for_filter(subtitles_srt_path)
        subs_style = (
            f'FontSize={subtitle_fontsize},PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,'
            f'BorderStyle=3,Outline=2,MarginV={subtitle_margin_v}'
        )
        subtitle_graph = f"[framed]subtitles='{subs_path_escaped}':force_style='{subs_style}'[vout]"
        subtitle_format = 'srt'
        dynamic_word_highlight = False
    filter_complex = ';'.join([vertical_graph, subtitle_graph])

    final_output_path = render_dir / 'final_short.mp4'
    report.stage('ffmpeg', f'Dikey kadraj + altyazi tek geciste ({encode_preset})')
    import time as _time
    started_at = _time.time()
    run_ffmpeg(ffmpeg_bin, [
        '-i', str(base_clip_path), '-filter_complex', filter_complex,
        '-map', '[vout]', '-map', '0:a:0',
        '-c:v', 'libx264', '-preset', encode_preset, '-crf', str(encode_crf), '-pix_fmt', 'yuv420p',
        '-c:a', 'aac', '-movflags', '+faststart', str(final_output_path),
    ], progress=report, total_duration_sec=seek_duration)
    render_latency_sec = _time.time() - started_at

    report.stage('validation', 'Cikti teknik olarak dogrulaniyor')
    render_validation = validate_render(
        ffmpeg_bin, ffprobe_bin, final_output_path, render_width, render_height,
        min_duration_sec=1.0, max_duration_sec=max(seek_duration * 2, 15.0),
    )
    render_validation['validated_at'] = datetime.now(timezone.utc).isoformat()
    render_validation['status'] = 'review_required' if render_validation['passed'] else 'failed'
    render_validation['status_reason'] = (
        'teknik kontroller gecti; video ve altyazi insan incelemesi bekliyor'
        if render_validation['passed'] else
        'bir veya daha fazla teknik video/ses kontrolu basarisiz oldu'
    )

    report.stage('manifest', 'Render manifesti hazirlaniyor')
    approval_conditions = {
        'moment_selection_approved': moment_selection.get('status') == 'approved',
        'technical_validation_passed': render_validation['passed'],
    }
    failed_conditions = [name for name, ok in approval_conditions.items() if not ok]
    final_status = 'review_required' if render_validation['passed'] else 'failed'
    word_probabilities = [
        float(word.get('probability')) for word in words
        if isinstance(word.get('probability'), (int, float))
    ]
    average_word_probability = (
        round(sum(word_probabilities) / len(word_probabilities), 4)
        if word_probabilities else None
    )
    low_confidence_word_ratio = (
        round(sum(probability < 0.55 for probability in word_probabilities)
              / len(word_probabilities), 4)
        if word_probabilities else None
    )

    return {
        'status': final_status,
        'status_reason': (
            'orijinal ses korundu ve altyazilar videoya gomuldu; insan incelemesi bekleniyor'
            if render_validation['passed'] else
            'orijinal-ses altyazi renderi teknik dogrulamadan gecemedi'
        ),
        'not_uploaded': True,
        'inputs': {
            'hot_moments_path': str(hot_moments_path),
            'moment_selection_path': str(moment_selection_path),
            'selected_moment_rank': selected_rank,
            'source_resolution_mode': resolution_mode,
            'source_description': source_description,
        },
        'approval': {
            'conditions': approval_conditions,
            'failed_conditions': failed_conditions,
        },
        'config': {
            'pipeline_mode': 'original_audio_subtitles',
            'render_mode': render_mode,
            'preview_mode': preview_mode,
            'target_width': target_width,
            'target_height': target_height,
            'render_width': render_width,
            'render_height': render_height,
            'subtitle_whisper_model_size': subtitle_whisper_model_size,
            'subtitle_language_requested': requested_subtitle_language,
            'subtitle_language_resolved': resolved_subtitle_language,
            'subtitle_language_source': subtitle_language_source,
            'subtitle_initial_prompt_used': bool(transcription_prompt),
            'caption_style': caption_style,
            'caption_theme_requested': caption_theme_result['requested_theme'],
            'caption_theme_resolved': caption_theme_result['resolved_theme'],
            'caption_theme_resolution_reason': caption_theme_result['resolution_reason'],
            'caption_theme_profile_version': caption_profile['profile_version'],
            'caption_font_name': resolved_font_name,
            'caption_font_size': resolved_font_size,
            'caption_font_path': caption_font_asset.get('font_path'),
            'caption_font_source': caption_font_asset.get('source'),
            'caption_active_color_ass': resolved_active_color,
            'caption_base_color_ass': resolved_base_color,
            'caption_max_words': resolved_max_words,
            'caption_max_chars': resolved_max_chars,
            'caption_margin_v': resolved_margin_v,
            'caption_uppercase': resolved_uppercase,
            'caption_letter_spacing': resolved_letter_spacing,
            'original_audio_preserved': True,
            'tts_used': False,
            'encode_preset': encode_preset,
            'encode_crf': encode_crf,
        },
        'subtitles': {
            'source': subtitle_source,
            'cue_count': len(cues),
            'word_count': len(words),
            'srt_path': str(subtitles_srt_path),
            'ass_path': str(subtitles_ass_path) if subtitles_ass_path.is_file() else None,
            'format_used_for_render': subtitle_format,
            'dynamic_word_highlight': dynamic_word_highlight,
            'active_word_color': 'yellow' if dynamic_word_highlight else None,
            'dynamic_caption_meta': dynamic_caption_meta,
            'average_word_probability': average_word_probability,
            'low_confidence_word_ratio': low_confidence_word_ratio,
            'fallback_transcript': fallback_text,
        },
        'output': {
            'final_video_path': str(final_output_path),
            'final_video_sha256_16': sha256_file(final_output_path) if final_output_path.is_file() else None,
            'validation': render_validation,
            'render_latency_sec': round(render_latency_sec, 2),
        },
        'human_approval': {
            'approved': False, 'approved_by': None, 'approved_at': None,
            'notes': None, 'approved_content_sha256': None,
        },
        'created_at': datetime.now(timezone.utc).isoformat(),
    }


def run_render_pipeline(hot_moments_data: dict, selected_script: dict, selected_voiceover: dict,
                         hot_moments_path, selected_script_path, selected_voiceover_path,
                         render_dir, render_mode: str = 'blurred_background', target_width: int = 1080,
                         target_height: int = 1920, preview_mode: bool = True, ducking_enabled: bool = True,
                         original_audio_base_volume: float = 0.35, narration_volume: float = 1.0,
                         duck_ratio: float = 8, subtitle_fontsize: int = 20, subtitle_margin_v: int = 90,
                         subtitle_max_chars_per_cue: int = 42, source_video_path_override: str = '',
                         progress=None) -> dict:
    """High-level, single-run render entry point shared by
    06_render_pipeline.ipynb and 00_youtube_automation_master.ipynb: extracts
    the selected moment's source clip, generates subtitles from the real
    narration audio, reframes to vertical + burns subtitles + mixes audio in
    one ffmpeg pass, technically validates the output, and returns the
    render_manifest.json contract dict.

    Never uploads or publishes anything (not_uploaded=True always).

    Approval contract: this function CANNOT produce status='approved' -- it takes
    no approval argument. A freshly rendered video is always 'review_required'
    (or 'failed' when technical validation fails), because nobody has watched it
    yet. Promotion to 'approved' happens only afterwards via
    `local_pipeline.approval.approve_artifact()`, which requires the SHA-256 of
    the video file the human actually played AND a passing technical validation.

    The upstream approval preconditions (script approved, voiceover approved,
    voiceover-hash matches the script) are still evaluated and recorded under
    `approval.conditions` / `approval.failed_conditions`, so the separate
    approval step -- and the human -- can see exactly what is outstanding.
    """
    # Optional observer with .stage()/.count(). Notebooks pass nothing and get
    # exactly the previous behaviour; the console passes a tracker so the
    # render screen can name the stage instead of only showing a spinner.
    report = progress if progress is not None else _NullProgress()

    render_dir = Path(render_dir)
    render_dir.mkdir(parents=True, exist_ok=True)
    report.stage('preparation', 'Girdiler dogrulaniyor')
    ffmpeg_bin, ffprobe_bin = resolve_ffmpeg_binaries()

    input_approval_warnings = []
    if selected_script.get('status') != 'approved':
        input_approval_warnings.append(f"selected_script.json status='{selected_script.get('status')}' (approved degil)")
    if selected_voiceover.get('status') != 'approved':
        input_approval_warnings.append(f"selected_voiceover.json status='{selected_voiceover.get('status')}' (approved degil)")

    narration_audio_path = selected_voiceover.get('audio_path')
    if not narration_audio_path or not Path(narration_audio_path).is_file():
        raise FileNotFoundError(f"Seslendirme dosyasi bulunamadi: {narration_audio_path!r}.")

    selected_moment_info = selected_script.get('selected_moment') or {}
    selected_moment_rank = selected_moment_info.get('rank')
    if selected_moment_rank is None:
        raise ValueError("selected_script.json icinde 'selected_moment.rank' yok.")
    moments_by_rank = {m['rank']: m for m in hot_moments_data['hot_moments']}
    if selected_moment_rank not in moments_by_rank:
        raise ValueError(
            f"selected_script.json rank={selected_moment_rank} hot_moments.json icinde yok. "
            f"Mevcut ranklar: {sorted(moments_by_rank)}."
        )
    selected_moment = moments_by_rank[selected_moment_rank]

    def _mismatch(field, tolerance=0.01):
        a, b = selected_moment_info.get(field), selected_moment.get(field)
        if a is None or b is None:
            return False
        return abs(float(a) - float(b)) > tolerance

    moment_mismatches = [f for f in ('source_start_sec', 'source_end_sec') if _mismatch(f)]
    if moment_mismatches:
        raise ValueError(f"selected_script.json'daki secili an zaman damgalari hot_moments.json ile uyusmuyor: {moment_mismatches}.")

    chosen_path, resolution_mode, seek_start, seek_duration, source_description = resolve_render_source(
        hot_moments_data, selected_moment, source_video_path_override,
    )

    if preview_mode:
        encode_preset, encode_crf, encode_scale = 'ultrafast', 30, 0.5
    else:
        encode_preset, encode_crf, encode_scale = 'slow', 18, 1.0
    render_width = max(2, int(target_width * encode_scale) // 2 * 2)
    render_height = max(2, int(target_height * encode_scale) // 2 * 2)

    base_clip_path = render_dir / 'base_clip.mp4'
    report.note('Kaynak klip cikariliyor')
    run_ffmpeg(ffmpeg_bin, [
        '-ss', str(seek_start), '-i', str(chosen_path), '-t', str(seek_duration),
        '-map', '0:v:0', '-map', '0:a:0?',
        '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20', '-pix_fmt', 'yuv420p',
        '-c:a', 'aac', str(base_clip_path),
    ])
    base_info, _base_backend = probe_media(ffmpeg_bin, ffprobe_bin, base_clip_path)
    has_original_audio = any(s.get('codec_type') == 'audio' for s in base_info.get('streams', []))

    script_text_for_subs = (selected_script.get('script_text') or selected_moment.get('transcript') or '').strip()
    report.stage('subtitles', 'Anlatimdan altyazi uretiliyor')
    cues, subtitle_source = transcribe_for_subtitles(narration_audio_path, script_text_for_subs, subtitle_max_chars_per_cue)
    subtitles_srt_path = render_dir / 'subtitles.srt'
    write_srt(cues, subtitles_srt_path)
    report.count(len(cues), len(cues), unit='altyazi',
                 message=f'{len(cues)} altyazi satiri ({subtitle_source})')

    vertical_graph = build_vertical_filtergraph(render_mode, render_width, render_height)
    subs_path_escaped = escape_path_for_filter(subtitles_srt_path)
    subs_style = (
        f'FontSize={subtitle_fontsize},PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,'
        f'BorderStyle=3,Outline=2,MarginV={subtitle_margin_v}'
    )
    subtitle_graph = f"[framed]subtitles='{subs_path_escaped}':force_style='{subs_style}'[vout]"
    audio_graph = build_audio_filtergraph(has_original_audio, ducking_enabled, duck_ratio, original_audio_base_volume, narration_volume)
    filter_complex = ';'.join([vertical_graph, subtitle_graph, audio_graph])

    final_output_path = render_dir / 'final_short.mp4'
    report.stage('ffmpeg', f'Dikey kadraj + altyazi + ses tek geciste ({encode_preset})')
    import time as _time
    t0 = _time.time()
    run_ffmpeg(ffmpeg_bin, [
        '-i', str(base_clip_path), '-i', str(narration_audio_path),
        '-filter_complex', filter_complex,
        '-map', '[vout]', '-map', '[aout]',
        '-c:v', 'libx264', '-preset', encode_preset, '-crf', str(encode_crf), '-pix_fmt', 'yuv420p',
        '-c:a', 'aac', '-movflags', '+faststart',
        str(final_output_path),
    ], progress=report, total_duration_sec=seek_duration)
    render_latency_sec = _time.time() - t0

    report.stage('validation', 'Cikti teknik olarak dogrulaniyor')
    render_validation = validate_render(
        ffmpeg_bin, ffprobe_bin, final_output_path, render_width, render_height,
        min_duration_sec=1.0, max_duration_sec=max(seek_duration * 2, 15.0),
    )
    render_validation['validated_at'] = datetime.now(timezone.utc).isoformat()
    render_validation['status'] = 'review_required' if render_validation['passed'] else 'failed'
    render_validation['status_reason'] = (
        'teknik ffprobe/ffmpeg-fallback kontrolleri gecti (passed=true); bu INSAN ONAYI DEGILDIR, '
        'dosya hala izlenip onaylanmalidir'
        if render_validation['passed'] else
        'bir veya daha fazla teknik ffprobe/ffmpeg-fallback kontrolu basarisiz oldu'
    )

    report.stage('manifest', 'Render manifesti hazirlaniyor')
    selected_script_hash = hashlib.sha256((selected_script.get('script_text') or '').encode('utf-8')).hexdigest()[:16]
    # Upstream preconditions for a LATER approval. 'render_approved_flag' is
    # deliberately absent: a render can no longer approve itself during the call
    # that produced it. These are recorded so the separate approval step (and the
    # human reading the manifest) can see what is still outstanding.
    approval_conditions = {
        'script_approved': selected_script.get('status') == 'approved',
        'voiceover_approved': selected_voiceover.get('status') == 'approved',
        'voiceover_hash_matches_script': (
            selected_script.get('script_text') is not None
            and selected_voiceover.get('source_script_hash') == selected_script_hash
        ),
        'technical_validation_passed': render_validation['passed'],
    }
    failed_conditions = [name for name, ok in approval_conditions.items() if not ok]

    if not render_validation['passed']:
        final_status = 'failed'
        final_status_reason = 'teknik dogrulama basarisiz; onay kapisi degerlendirilmedi'
    else:
        final_status = 'review_required'
        final_status_reason = (
            'render tamamlandi ve teknik dogrulama gecti; INSAN INCELEMESI BEKLIYOR '
            '(videoyu izledikten sonra onay ayri bir adimda, approval.approve_artifact ile verilir)'
        )
        if failed_conditions:
            final_status_reason += f'. Onay oncesi saglanmayan kosullar: {failed_conditions}'

    return {
        'status': final_status,
        'status_reason': final_status_reason,
        'not_uploaded': True,
        'inputs': {
            'hot_moments_path': str(hot_moments_path),
            'selected_script_path': str(selected_script_path),
            'selected_voiceover_path': str(selected_voiceover_path),
            'selected_moment_rank': selected_moment['rank'],
            'source_resolution_mode': resolution_mode,
            'source_description': source_description,
            'input_approval_warnings': input_approval_warnings,
        },
        'approval': {
            'conditions': approval_conditions,
            'failed_conditions': failed_conditions,
        },
        'config': {
            'render_mode': render_mode, 'preview_mode': preview_mode,
            'target_width': target_width, 'target_height': target_height,
            'render_width': render_width, 'render_height': render_height,
            'ducking_enabled': ducking_enabled,
            'original_audio_base_volume': original_audio_base_volume,
            'narration_volume': narration_volume,
            'encode_preset': encode_preset, 'encode_crf': encode_crf,
        },
        'subtitles': {'source': subtitle_source, 'cue_count': len(cues), 'srt_path': str(subtitles_srt_path)},
        'output': {
            'final_video_path': str(final_output_path),
            'final_video_sha256_16': sha256_file(final_output_path) if final_output_path.is_file() else None,
            'render_latency_sec': round(render_latency_sec, 2),
            'validation': render_validation,
        },
        'human_approval': {
            'approved': False, 'approved_by': None, 'approved_at': None,
            'notes': None, 'approved_content_sha256': None,
        },
        'created_at': datetime.now(timezone.utc).isoformat(),
    }


class _NullProgress:
    """No-op observer, so the notebook call paths stay untouched."""

    def stage(self, key, message='') -> None: ...
    def count(self, completed, total, *, unit='', message='') -> None: ...
    def note(self, message: str) -> None: ...
