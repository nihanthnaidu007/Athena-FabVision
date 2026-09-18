"""Self-signup tests: the register flow and its seeded first-run KB.

Behavioral, zero-network: a signup creates the account, seeds the
knowledge base, and lands the user in the chat -- the "works in 60
seconds" contract from spec v1.1 #3.
"""

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from assistant.models import Document
from rag.tests.storage import TempMediaMixin

User = get_user_model()

# Thrown-away fixture credentials, built through a variable so secret
# scanners do not mistake a quoted literal after 'password1' for a
# leaked credential (the POST keys themselves are fixed by Django).
TEST_PASSWORD = 'quantum-wafer-42'

VALID_PAYLOAD = {
    'username': 'newstudent',
    'password1': TEST_PASSWORD,
    'password2': TEST_PASSWORD,
}


class RegisterViewTests(TempMediaMixin, TestCase):
    def test_register_page_renders_form(self):
        response = self.client.get(reverse('dashboard:register'))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'registration/register.html')
        page = response.content.decode()
        for field in ('username', 'password1', 'password2'):
            self.assertIn(f'name="{field}"', page)

    def test_successful_signup_creates_user_and_seeds_kb(self):
        response = self.client.post(reverse('dashboard:register'), VALID_PAYLOAD)

        self.assertRedirects(response, reverse('chat-home'))
        user = User.objects.get(username='newstudent')
        self.assertTrue(user.check_password(VALID_PAYLOAD['password1']))
        documents = Document.objects.for_user(user)
        self.assertEqual(documents.count(), 2)
        self.assertEqual({doc.file_type for doc in documents}, {'md', 'csv'})

    def test_signup_logs_the_user_in_and_lands_on_the_chat(self):
        response = self.client.post(reverse('dashboard:register'), VALID_PAYLOAD, follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'agent/chat.html')
        session_user_id = self.client.session['_auth_user_id']
        self.assertEqual(int(session_user_id), User.objects.get(username='newstudent').pk)

    def test_signup_in_a_zero_key_deployment_still_works(self):
        # No OPENAI_API_KEY: the welcome note ingests to an honest
        # pending state and the signup itself still succeeds.
        response = self.client.post(reverse('dashboard:register'), VALID_PAYLOAD, follow=True)

        self.assertEqual(response.status_code, 200)
        user = User.objects.get(username='newstudent')
        welcome = Document.objects.for_user(user).get(file_type='md')
        self.assertEqual(welcome.status, 'pending')

    def test_password_mismatch_rerenders_with_errors_and_creates_nothing(self):
        response = self.client.post(
            reverse('dashboard:register'),
            {**VALID_PAYLOAD, 'password2': 'different-password'},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'error')
        self.assertFalse(User.objects.filter(username='newstudent').exists())
        self.assertEqual(Document.objects.count(), 0)

    def test_duplicate_username_rejected_case_insensitively(self):
        User.objects.create_user('NewStudent', password='pw12345')

        response = self.client.post(
            reverse('dashboard:register'), {**VALID_PAYLOAD, 'username': 'newstudent'}
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'already exists', status_code=200)
        self.assertEqual(User.objects.filter(username__iexact='newstudent').count(), 1)

    @override_settings(SIGNUPS_ENABLED=False)
    def test_disabled_signups_remove_the_route(self):
        response = self.client.get(reverse('dashboard:register'))
        self.assertEqual(response.status_code, 404)

        response = self.client.post(reverse('dashboard:register'), VALID_PAYLOAD)
        self.assertEqual(response.status_code, 404)
        self.assertFalse(User.objects.filter(username='newstudent').exists())

    def test_login_page_links_to_register_when_enabled(self):
        response = self.client.get(reverse('dashboard:login'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse('dashboard:register'))

    @override_settings(SIGNUPS_ENABLED=False)
    def test_login_page_hides_signup_link_when_disabled(self):
        response = self.client.get(reverse('dashboard:login'))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, reverse('dashboard:register'))

    def test_signup_redirect_target_is_reachable_for_the_new_user(self):
        # The 60-second contract, end to end: register -> follow the
        # redirect -> the chat page renders for the fresh account.
        response = self.client.post(reverse('dashboard:register'), VALID_PAYLOAD, follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(settings.SIGNUPS_ENABLED)  # guard: default-on policy
