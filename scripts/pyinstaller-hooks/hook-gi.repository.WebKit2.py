"""The WebKit2 typelib for the window on Linux (PyInstaller has no hook for it); its library is the system's."""

from PyInstaller.utils.hooks.gi import GiModuleInfo

module_info = GiModuleInfo("WebKit2", "4.1")
if module_info.available:
    binaries, datas, hiddenimports = module_info.collect_typelib_data()
