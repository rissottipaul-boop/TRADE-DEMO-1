"""Загрузчик панели «Контур» для ops/panel.ps1 (PANEL-LAUNCHER-SEC).

    python -m src.control_panel_boot kontur-panel <nonce> <port> <log> <token.dpapi>

- Токен читается из файла DPAPI текущего пользователя (формат ConvertFrom-SecureString: hex от
  CryptProtectData над UTF-16LE). В командной строке, логе и stdout его нет.
- Дескриптор 1 уходит в os.devnull (src.control_panel печатает туда ссылку с токеном), 2 — в лог.
  Переназначение на уровне ОС: и print, и прямой os.write, и faulthandler не блокируют сервер.
- Метка `kontur-panel <nonce>` в командной строке — по ней ops/panel.ps1 подтверждает свой процесс.
Сам сервер и его аргументы — src.control_panel.main; здесь только безопасная обвязка запуска.
"""
from __future__ import annotations

import ctypes
import os
import sys
import time

MARKER = "kontur-panel"


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]


def read_dpapi_token(path: str) -> str:
    """Расшифровать токен, сохранённый ConvertFrom-SecureString (DPAPI, CurrentUser)."""
    if os.name != "nt":
        raise OSError("DPAPI доступен только в Windows")
    with open(path, encoding="utf-8-sig") as stream:
        data = bytes.fromhex(stream.read().strip())
    if not data:
        raise ValueError("пустой файл токена")
    buffer = ctypes.create_string_buffer(data, len(data))
    blob_in = _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    blob_out = _Blob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    if not crypt32.CryptUnprotectData(ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
        raise OSError(ctypes.get_last_error(), "CryptUnprotectData: файл токена не расшифровать")
    try:
        raw = ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(blob_out.pbData, ctypes.c_void_p))
    token = raw.decode("utf-16-le")
    if not token or not all(ch.isalnum() or ch in "-_" for ch in token):
        raise ValueError("токен в файле повреждён")
    return token


def _redirect(log_path: str):
    log = open(log_path, "a", encoding="utf-8", buffering=1)
    null = open(os.devnull, "w", encoding="utf-8")
    os.dup2(null.fileno(), 1)
    os.dup2(log.fileno(), 2)
    sys.stdout = null
    sys.stderr = log
    return log


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 5 or args[0] != MARKER or not args[2].isdigit():
        sys.stderr.write("использование: python -m src.control_panel_boot kontur-panel <nonce> <port> <log> <token>\n")
        return 2
    _, _nonce, port, log_path, token_path = args
    log = _redirect(log_path)
    log.write("%s kontur-panel start pid=%d port=%s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), os.getpid(), port))
    token = read_dpapi_token(token_path)
    from src import control_panel
    return control_panel.main(["--port", port, "--token", token])


if __name__ == "__main__":
    raise SystemExit(main())
