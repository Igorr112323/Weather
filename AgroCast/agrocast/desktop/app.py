import json
import os
import socket
import sys
import threading
import time
import urllib.request
from pathlib import Path

# PyInstaller with console=False (windowed / GUI mode) sets sys.stdout and
# sys.stderr to None. uvicorn's logging setup and the default log formatters
# call sys.stdout.isatty() / sys.stderr.isatty() at import/config time and
# crash with AttributeError ("Unable to configure formatter 'default'").
# Replace a missing stdio stream with the null device so the app boots even
# when there is no console attached.
def _ensure_stdio():
    for _stream_name in ("stdout", "stderr"):
        if getattr(sys, _stream_name) is None:
            setattr(sys, _stream_name, open(os.devnull, "w", encoding="utf-8"))


_ensure_stdio()


def bundled_root():
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))


def prepare_environment(home=None):
    root = bundled_root()
    state = Path(home or os.environ.get("AGROCAST_DESKTOP_HOME") or (Path.home() / ".agrocast")).expanduser().resolve()
    os.environ["AGROCAST_STATIC_DIR"] = str(root / "static")
    os.environ["AGROCAST_MIGRATIONS_DIR"] = str(root / "migrations")
    os.environ.setdefault("AGROCAST_WORLD_DIR", str(root / "world"))
    os.environ.setdefault("AGROCAST_BUNDLES_DIR", str(state.parent / "bundles"))
    os.environ["AGROCAST_STATE_DIR"] = str(state)
    os.environ.setdefault("AGROCAST_PUBLIC_ORIGIN", "https://127.0.0.1")
    os.environ["AGROCAST_DESKTOP"] = "1"
    os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--no-sandbox --disable-gpu")
    os.environ.setdefault("QT_LOGGING_RULES", "*.warning=false")
    _verify_bundle_integrity(root / "world")
    return state, root


def _bundle_content_bytes(path):
    # The scientific data bundle must produce the same sha256 on every OS. Git
    # may normalise text files to CRLF on Windows during checkout, so we fold
    # line endings back to LF for text (non-binary) files before hashing.
    data = path.read_bytes()
    if b"\x00" in data[:8192]:
        return data
    return data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def _verify_bundle_integrity(world_dir):
    import hashlib

    marker = world_dir / "integrity.json"
    if not marker.exists():
        return
    expected = json.loads(marker.read_text(encoding="utf-8"))
    digest = hashlib.sha256()
    for entry in sorted(world_dir.rglob("*")):
        if entry.is_file() and entry.name != "integrity.json":
            digest.update(entry.relative_to(world_dir).as_posix().encode())
            digest.update(_bundle_content_bytes(entry))
    actual = digest.hexdigest()[:16]
    if actual != expected.get("sha256_prefix"):
        raise RuntimeError(
            "Набор данных повреждён или изменён (ожидался %s, получен %s). Переустановите приложение."
            % (expected.get("sha256_prefix"), actual)
        )


def _wait_until_ready(url, errors, timeout=120.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if errors:
            return False
        try:
            with urllib.request.urlopen(url + "health/live", timeout=1.5) as response:
                if response.status == 200:
                    return True
        except OSError:
            time.sleep(0.2)
    return False


def _serve(server, errors):
    try:
        server.run()
    except BaseException as error:
        errors.append(error)


def start_server():
    from agrocast.core.settings import RuntimeSettings
    from agrocast.serve.product import create_app

    import uvicorn

    settings = RuntimeSettings.from_environment()
    from agrocast.bundle.releases import active_release

    active = active_release(settings.bundles_dir)
    if active is not None and active["problems"]:
        raise RuntimeError(
            "Локальный набор данных повреждён (release %s). Выполните откат: python -m scripts.bundle_publish rollback --releases-root %s"
            % (active["release_id"], settings.bundles_dir)
        )
    application = create_app(settings=settings)
    last_error = None
    for _attempt in range(5):
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        url = "http://127.0.0.1:%d/" % port
        config = uvicorn.Config(application, host="127.0.0.1", port=port, log_level=settings.log_level.lower(), lifespan="on")
        server = uvicorn.Server(config)
        errors = []
        thread = threading.Thread(target=_serve, args=(server, errors), daemon=True, name="agrocast-local-server")
        thread.start()
        if _wait_until_ready(url, errors):
            return server, thread, url
        server.should_exit = True
        thread.join(timeout=10)
        last_error = errors[0] if errors else RuntimeError("startup timed out on port %d" % port)
    raise RuntimeError("Local AgroCast server did not start: %r; check %s logs" % (last_error, settings.state_dir))


def _report_fatal(error):
    message = str(error)
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox

        app = QApplication.instance() or QApplication([])
        QMessageBox.critical(None, "AgroCast не запустился", message)
    except Exception:
        # Windowed mode has no console; fall back to the (possibly null) stderr.
        try:
            print(message, file=sys.stderr)
        except Exception:
            pass
    return 78


def main(argv=None):
    state, _root = prepare_environment()
    try:
        server, thread, url = start_server()
    except Exception as error:
        return _report_fatal(error)
    try:
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QIcon
        from PySide6.QtWidgets import QApplication
        from PySide6.QtWebEngineWidgets import QWebEngineView
    except ImportError:
        print("AgroCast работает локально: %s (для нативного окна установите PySide6)" % url, file=sys.stderr)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
        code = 0
    else:
        app = QApplication([])
        app.setApplicationName("AgroCast")
        icon = bundled_root() / "static" / "agrocast.png"
        if icon.exists():
            app.setWindowIcon(QIcon(str(icon)))
        view = QWebEngineView()
        view.setWindowTitle("AgroCast — локальные прогнозы")
        view.setUrl(QUrl(url))
        view.resize(1320, 900)
        view.show()
        code = app.exec()
    server.should_exit = True
    thread.join(timeout=15)
    return code


if __name__ == "__main__":
    sys.exit(main())
