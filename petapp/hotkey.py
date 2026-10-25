"""One global toggle shortcut. Windows releases the registration on process exit."""
import ctypes
import sys
from ctypes import wintypes
from PySide6.QtCore import QAbstractNativeEventFilter
from PySide6.QtWidgets import QApplication


class MiniHotkey(QAbstractNativeEventFilter):
    def __init__(self, callback):
        super().__init__()
        self.callback = callback
        self.registered = False
        self.label = '点桌宠打开'
        self._id = 0x4D50
        if sys.platform == 'win32':
            self.api = ctypes.WinDLL('user32', use_last_error=True)
            self.api.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
            self.api.RegisterHotKey.restype = wintypes.BOOL
            self.api.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
            # Try a documented fallback if another app owns Ctrl+Alt+Space.
            for modifiers, key, name in ((0x4003,0x20,'Ctrl+Alt+Space'),(0x4003,0x4D,'Ctrl+Alt+M'),
                                         (0x4006,0x4D,'Ctrl+Shift+M')):
                if self.api.RegisterHotKey(None,self._id,modifiers,key):
                    self.registered = True
                    self.label = name
                    QApplication.instance().installNativeEventFilter(self)
                    break

    def nativeEventFilter(self, event_type, message):
        if self.registered and event_type in (b'windows_generic_MSG', b'windows_dispatcher_MSG'):
            msg = wintypes.MSG.from_address(int(message))
            if msg.message == 0x0312 and int(msg.wParam) == self._id:
                self.callback()
                return True, 0
        return False, 0

    def close(self):
        if self.registered:
            self.api.UnregisterHotKey(None,self._id)
            QApplication.instance().removeNativeEventFilter(self)
            self.registered = False
