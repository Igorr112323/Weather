import importlib
import subprocess
import sys
import webbrowser
from pathlib import Path

REQUIREMENTS = [
    "numpy", "pandas", "scipy", "scikit-learn", "xarray", "zarr", "lightgbm",
    "netCDF4", "fastapi", "uvicorn", "apscheduler", "pyarrow", "requests", "pydantic",
]

BASE = Path(__file__).resolve().parent


def check_deps():
    missing = []
    for m in REQUIREMENTS:
        try:
            importlib.import_module(m)
        except Exception:
            missing.append(m)
    return missing


def install(missing):
    pkgs = []
    for m in missing:
        pkgs.append({"zarr": "zarr>=2.16,<3", "sklearn": "scikit-learn", "netCDF4": "netCDF4"}.get(m, m))
    print("Устанавливаю зависимости:", ", ".join(pkgs))
    subprocess.check_call([sys.executable, "-m", "pip", "install", *pkgs])


def main():
    missing = check_deps()
    if missing:
        try:
            install(missing)
        except Exception as exc:
            print("Не удалось установить зависимости автоматически:", exc)
            print("Установите вручную: pip install -r requirements.txt")
            input("Нажмите Enter для выхода")
            return
    BASE.joinpath("data").mkdir(exist_ok=True)
    port = 8501
    url = f"http://127.0.0.1:{port}"
    print()
    print("=" * 52)
    print("  AgroCast Россия · сезонные прогнозы для фермеров")
    print("  Откроется браузер:", url)
    print("  Для остановки: Ctrl+C в этом окне")
    print("=" * 52)
    print()
    import threading

    def _open():
        try:
            webbrowser.open(url)
        except Exception:
            pass

    threading.Timer(2.5, _open).start()
    import uvicorn

    print("Если браузер не открылся сам — скопируй в адресную строку:")
    print("   " + url)
    print()
    try:
        uvicorn.run("agrocast.serve.product:app", host="127.0.0.1", port=port, log_level="warning")
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        print()
        print("Ошибка запуска:", exc)
        print("Скопируйте этот текст и пришлите.")
    print()
    print("Сервер остановлен.")
    try:
        input("Нажмите Enter, чтобы закрыть окно...")
    except Exception:
        pass


if __name__ == "__main__":
    main()
