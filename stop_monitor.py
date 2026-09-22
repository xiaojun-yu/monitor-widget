import ctypes
from ctypes import wintypes


STOP_EVENT_NAME = "Local\\MinimalSystemMonitor.Stop"


def stop_monitor():
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.OpenEventW.restype = ctypes.c_void_p
    event = kernel32.OpenEventW(0x0002, False, STOP_EVENT_NAME)
    if event:
        kernel32.SetEvent(event)
        kernel32.CloseHandle(event)


if __name__ == "__main__":
    stop_monitor()
