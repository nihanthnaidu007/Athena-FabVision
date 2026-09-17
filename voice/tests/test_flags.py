"""Voice feature flag: on only with a complete LiveKit config."""

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from voice import flags

from .fakes import LIVEKIT_CONFIG

User = get_user_model()


@override_settings(**LIVEKIT_CONFIG)
class LivekitFlagTests(TestCase):
    def test_flag_on_when_fully_configured(self):
        self.assertTrue(flags.livekit_configured())

    def test_flag_off_when_any_setting_missing(self):
        # All three settings are required together: a partial deployment
        # would mint tokens the worker cannot honor.
        for missing in ('LIVEKIT_URL', 'LIVEKIT_API_KEY', 'LIVEKIT_API_SECRET'):
            with self.subTest(missing=missing):
                partial = {**LIVEKIT_CONFIG, missing: ''}
                with override_settings(**partial):
                    self.assertFalse(flags.livekit_configured())

    def test_flag_off_without_any_settings(self):
        with override_settings(LIVEKIT_URL='', LIVEKIT_API_KEY='', LIVEKIT_API_SECRET=''):
            self.assertFalse(flags.livekit_configured())

    def test_room_and_identity_track_user_pk(self):
        user = User.objects.create_user(username='nina', password='pw12345!')
        self.assertEqual(flags.voice_room_name(user), f'voice-user-{user.pk}')
        self.assertEqual(flags.voice_identity(user), f'user-{user.pk}')
