"""Voice page states (configured vs setup guide).

The chat sidebar's conditional voice entry lives with the chat UI
surface (the chat template ships separately from this branch).
"""

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from .fakes import LIVEKIT_CONFIG

User = get_user_model()


class VoicePageAccessTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='nina', password='pw12345!')

    def test_anonymous_is_redirected_to_login(self):
        response = self.client.get('/voice/')
        self.assertEqual(response.status_code, 302)
        self.assertIn('/accounts/login/', response['Location'])

    def test_flag_off_renders_the_setup_guide(self):
        self.client.force_login(self.user)
        response = self.client.get('/voice/')
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn('Voice is not configured', body)
        self.assertIn('LIVEKIT_URL', body)
        # No token flow advertised while the feature is off.
        self.assertNotIn('voice/token/', body)

    @override_settings(**LIVEKIT_CONFIG)
    def test_flag_on_renders_the_connect_state(self):
        self.client.force_login(self.user)
        response = self.client.get('/voice/')
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn(f'voice-user-{self.user.pk}', body)
        self.assertIn('voice/token/', body)
        self.assertIn('csrf-token', body)
