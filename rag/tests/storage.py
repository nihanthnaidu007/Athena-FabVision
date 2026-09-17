"""Shared test infrastructure: offline embedders and throwaway media storage."""

import shutil
import tempfile

STORAGE_BACKEND = 'django.core.files.storage.FileSystemStorage'
STATICFILES_BACKEND = 'django.contrib.staticfiles.storage.StaticFilesStorage'


class TempMediaMixin:
    """Redirect FileField storage to a throwaway directory per test.

    FileField's default storage resolves MEDIA_ROOT when the storage
    handler is reset, so tests override STORAGES (not just MEDIA_ROOT)
    and clean the directory up afterwards. Nothing is written to the repo.
    """

    def setUp(self):
        super().setUp()
        self._media_dir = tempfile.mkdtemp()
        self._storage_override = self.settings(
            MEDIA_ROOT=self._media_dir,
            STORAGES={
                'default': {'BACKEND': STORAGE_BACKEND},
                'staticfiles': {'BACKEND': STATICFILES_BACKEND},
            },
        )
        self._storage_override.enable()
        self.addCleanup(self._storage_override.disable)
        self.addCleanup(shutil.rmtree, self._media_dir, ignore_errors=True)
