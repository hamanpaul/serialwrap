import importlib


def test_daemon_main_importable_from_package():
    mod = importlib.import_module("sw_core.daemon")
    assert callable(mod.main)
    assert {"file.push", "file.pull"} <= set(mod.BLOCKING_RPC_METHODS)
