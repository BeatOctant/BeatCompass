"""Contract-1 adapter. Only this module may import OctantVector."""

import warnings
from pathlib import Path

from . import (DBError, EngineIncompatibleError, EngineUnavailableError, RequestError, SeedNotEmbeddedError,
               SeedNotInDBError, SignatureWarning, UnknownPresetError, VersionError, recorded)


# The contract-1 runtime API this OctantCompass uses: the same major version, and at least this minor
# version (OctantSpecs contracts/engine-runtime.md, 版の付け方). Presets, expand_preset, SeedNotEmbeddedError and
# UnknownPresetError joined 1.0 without a new version, and so did import_archive and info()'s notice and credits;
# the OctantVector this OctantCompass installs (0.9) has them all, read_info too, and the 0.8.0 guards against a tar bomb
# and runaway params.
SUPPORTED_API = (1, 0)


def is_supported(api_version):
    try:
        major, minor = (int(part) for part in api_version.split("."))
    except (AttributeError, TypeError, ValueError):
        return False
    return major == SUPPORTED_API[0] and minor >= SUPPORTED_API[1]


class OctantVectorEngine:
    def __init__(self):
        self._runtime = None

    def _load_runtime(self):
        if self._runtime is not None:
            return self._runtime
        try:
            import octantvector.runtime as runtime
        except ImportError as exc:
            raise EngineUnavailableError("OctantVector runtime is unavailable") from exc
        if not is_supported(getattr(runtime, "API_VERSION", None)):
            raise EngineIncompatibleError("OctantVector runtime API is outside the supported range")
        self._runtime = runtime
        return runtime

    def _call(self, function, *args):
        runtime = self._load_runtime()
        try:
            return function(*args)
        except runtime.VersionError as exc:
            raise VersionError(str(exc)) from exc
        except runtime.DBError as exc:
            raise DBError(str(exc)) from exc
        except runtime.SeedNotEmbeddedError as exc:
            raise SeedNotEmbeddedError(str(exc)) from exc
        except runtime.SeedNotInDBError as exc:
            raise SeedNotInDBError(str(exc)) from exc
        except runtime.UnknownPresetError as exc:
            raise UnknownPresetError(str(exc)) from exc
        except runtime.RequestError as exc:
            raise RequestError(str(exc)) from exc

    def open_db(self, path):
        runtime = self._load_runtime()
        if not Path(path).exists():
            raise FileNotFoundError(path)
        return self._call(runtime.open_db, path)

    def db_info(self, db):
        return self._call(db.info)

    def read_info(self, path):
        runtime = self._load_runtime()
        if not Path(path).exists():
            raise FileNotFoundError(path)
        return self._call(runtime.read_info, path)

    def recommend(self, db, history, request, params):
        runtime = self._load_runtime()
        return self._call(runtime.recommend, db, history, request, params)

    def presets(self, db):
        runtime = self._load_runtime()
        return self._call(runtime.presets, db)

    def expand_preset(self, db, request):
        runtime = self._load_runtime()
        return self._call(runtime.expand_preset, db, request)

    def describe(self, db, history, params):
        runtime = self._load_runtime()
        return self._call(runtime.describe, db, history, params)

    def plan_update(self, index, local_version):
        runtime = self._load_runtime()
        return self._call(runtime.plan_update, index, local_version)

    def apply_update(self, files, base, out, trust=None):
        return self._signed(self._load_runtime().apply_update, files, base, out, trust)

    def import_archive(self, path, out, trust=None):
        return self._signed(self._load_runtime().import_archive, path, out, trust)

    def _signed(self, function, *args):
        runtime = self._load_runtime()
        # OctantVector's SignatureWarning goes on as OctantCompass's own, as the errors do.
        with recorded(runtime.SignatureWarning) as signatures:
            result = self._call(function, *args)
        for text in signatures:
            warnings.warn(SignatureWarning(text), stacklevel=3)
        return result
