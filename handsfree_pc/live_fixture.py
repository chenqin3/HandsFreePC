from __future__ import annotations

import argparse
import ctypes
import os
from ctypes import wintypes

if os.name == "nt":
    _LRESULT = ctypes.c_ssize_t
    _WNDPROC = ctypes.WINFUNCTYPE(
        _LRESULT,
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    )

    class _WNDCLASSW(ctypes.Structure):
        _fields_ = [
            ("style", wintypes.UINT),
            ("lpfnWndProc", _WNDPROC),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", wintypes.HINSTANCE),
            ("hIcon", wintypes.HICON),
            ("hCursor", wintypes.HANDLE),
            ("hbrBackground", wintypes.HBRUSH),
            ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR),
        ]


def _run_windows_fixture(title: str) -> int:
    if os.name != "nt":
        raise OSError("the live UIA fixture is available only on Windows")
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    user32.RegisterClassW.argtypes = [ctypes.POINTER(_WNDCLASSW)]
    user32.RegisterClassW.restype = wintypes.ATOM
    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.HWND,
        wintypes.HMENU,
        wintypes.HINSTANCE,
        wintypes.LPVOID,
    ]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.DefWindowProcW.argtypes = [
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    user32.DefWindowProcW.restype = _LRESULT
    class_name = "HandsFreePCLiveFixtureWindow"
    wm_destroy = 0x0002
    ws_overlappedwindow = 0x00CF0000
    ws_visible = 0x10000000
    ws_child = 0x40000000
    ws_tabstop = 0x00010000
    ws_ex_clientedge = 0x00000200
    es_autohscroll = 0x0080
    sw_show = 5

    @_WNDPROC
    def window_proc(hwnd, message, wparam, lparam):
        if message == wm_destroy:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, message, wparam, lparam)

    instance = kernel32.GetModuleHandleW(None)
    window_class = _WNDCLASSW()
    window_class.lpfnWndProc = window_proc
    window_class.hInstance = instance
    window_class.hCursor = user32.LoadCursorW(None, ctypes.c_void_p(32512))
    window_class.hbrBackground = ctypes.c_void_p(6)
    window_class.lpszClassName = class_name
    atom = user32.RegisterClassW(ctypes.byref(window_class))
    if not atom and ctypes.get_last_error() != 1410:
        raise OSError(f"RegisterClassW failed: {ctypes.get_last_error()}")

    hwnd = user32.CreateWindowExW(
        0,
        class_name,
        title,
        ws_overlappedwindow | ws_visible,
        200,
        180,
        620,
        190,
        None,
        None,
        instance,
        None,
    )
    if not hwnd:
        raise OSError(f"CreateWindowExW failed: {ctypes.get_last_error()}")
    user32.CreateWindowExW(
        0,
        "STATIC",
        "HandsFreePC local UIA verification fixture",
        ws_child | ws_visible,
        24,
        22,
        550,
        28,
        hwnd,
        None,
        instance,
        None,
    )
    edit = user32.CreateWindowExW(
        ws_ex_clientedge,
        "EDIT",
        "",
        ws_child | ws_visible | ws_tabstop | es_autohscroll,
        24,
        66,
        550,
        32,
        hwnd,
        ctypes.c_void_p(1001),
        instance,
        None,
    )
    if not edit:
        user32.DestroyWindow(hwnd)
        raise OSError(f"CreateWindowExW EDIT failed: {ctypes.get_last_error()}")
    user32.ShowWindow(hwnd, sw_show)
    user32.UpdateWindow(hwnd)
    user32.SetFocus(edit)

    message = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(message))
        user32.DispatchMessageW(ctypes.byref(message))
    return int(message.wParam)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="HandsFreePC harmless UIA live-test fixture")
    parser.add_argument("--title", required=True)
    args = parser.parse_args(argv)
    if not args.title.startswith("HandsFreePC Live Fixture ") or len(args.title) > 100:
        raise ValueError("fixture title is outside the allow-listed format")
    return _run_windows_fixture(args.title)


if __name__ == "__main__":
    raise SystemExit(main())
