"""Ask Windows which speaker is currently the default. Standard library only.

PortAudio caches its device table at init, so it cannot answer this -- its idea
of "default" is frozen at whatever was true when the process started. Core Audio
can, via IMMDeviceEnumerator, and reading it costs microseconds.

That lets the daemon hold its output stream open for low latency and rebuild it
only when the default actually changes, instead of reopening on a timer and
paying up to a second of Bluetooth stream-open cost every time.
"""

import ctypes
from ctypes import POINTER, byref, c_void_p, c_wchar_p

ole32 = ctypes.OleDLL("ole32")

CLSID_MMDeviceEnumerator = "{BCDE0395-E52F-467C-8E3D-C4579291692E}"
IID_IMMDeviceEnumerator = "{A95664D2-9614-4F35-A746-DE8DB63617E6}"

CLSCTX_ALL = 0x17
COINIT_APARTMENTTHREADED = 0x2
E_RENDER = 0          # playback (as opposed to capture)
ROLE_CONSOLE = 0

# vtable slots: IUnknown takes 0-2, then the interface's own methods follow.
VT_RELEASE = 2
VT_GET_DEFAULT_ENDPOINT = 4      # IMMDeviceEnumerator
VT_GET_ID = 5                    # IMMDevice


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    def __init__(self, text):
        super().__init__()
        ole32.CLSIDFromString(text, byref(self))


def _method(ptr, index, restype, argtypes):
    vtable = ctypes.cast(ptr, POINTER(POINTER(c_void_p))).contents
    proto = ctypes.WINFUNCTYPE(restype, c_void_p, *argtypes)
    return proto(vtable[index])


def _release(ptr):
    if ptr:
        try:
            _method(ptr, VT_RELEASE, ctypes.c_ulong, ())(ptr)
        except Exception:
            pass


def com_init():
    """Call once per thread before default_render_id()."""
    try:
        ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
    except OSError:
        pass          # already initialised on this thread, which is fine


def default_render_id():
    """Stable device ID string of the current default speaker, or None."""
    enumerator = c_void_p()
    try:
        ole32.CoCreateInstance(
            byref(GUID(CLSID_MMDeviceEnumerator)), None, CLSCTX_ALL,
            byref(GUID(IID_IMMDeviceEnumerator)), byref(enumerator),
        )
    except OSError:
        return None
    if not enumerator:
        return None

    device = c_void_p()
    try:
        get_endpoint = _method(
            enumerator, VT_GET_DEFAULT_ENDPOINT, ctypes.HRESULT,
            (ctypes.c_int, ctypes.c_int, POINTER(c_void_p)),
        )
        get_endpoint(enumerator, E_RENDER, ROLE_CONSOLE, byref(device))
        if not device:
            return None

        buf = c_wchar_p()
        get_id = _method(device, VT_GET_ID, ctypes.HRESULT, (POINTER(c_wchar_p),))
        get_id(device, byref(buf))
        value = buf.value
        if buf:
            ole32.CoTaskMemFree(buf)
        return value
    except OSError:
        return None
    finally:
        _release(device)
        _release(enumerator)


if __name__ == "__main__":
    com_init()
    print(default_render_id())
