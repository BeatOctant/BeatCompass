def pre_safe_import_module(api):
    # As PyInstaller's own gi hooks: a module made from a typelib is not a file, so modulegraph is told it exists.
    api.add_runtime_module(api.module_name)
