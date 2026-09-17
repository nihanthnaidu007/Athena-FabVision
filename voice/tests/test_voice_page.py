"""Voice page states and the chat sidebar's conditional voice entry."""

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


class ChatSidebarVoiceEntryTests(TestCase):
    """The chat sidebar shows a link or a visible-disabled entry, never nothing."""

    def setUp(self):
        self.user = User.objects.create_user(username='nina', password='pw12345!')
        self.client.force_login(self.user)

    def test_flag_off_shows_disabled_voice_entry(self):
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn('aria-disabled="true"', body)
        self.assertIn('Voice is not configured', body)
        self.assertNotIn('href="/voice/"', body)

    @override_settings(**LIVEKIT_CONFIG)
    def test_flag_on_links_the_voice_page(self):
        response = self.client.get('/')
        body = response.content.decode()
        self.assertIn('href="/voice/"', body)
        self.assertNotIn('aria-disabled="true"', body)
