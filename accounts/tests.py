import os
from io import StringIO
from unittest.mock import Mock, patch

from django.core.management import call_command
from django.test import SimpleTestCase


class SeedTestUserCommandTests(SimpleTestCase):
    @patch("accounts.management.commands.seed_test_user.get_user_model")
    def test_creates_configured_user_without_printing_password(self, get_user_model):
        password = "test-password-not-for-output"
        manager = Mock()
        manager.filter.return_value.first.return_value = None
        get_user_model.return_value.objects = manager
        output = StringIO()

        with patch.dict(
            os.environ,
            {
                "SEED_TEST_USER_USERNAME": "test_01",
                "SEED_TEST_USER_EMAIL": "test_01@gmail.com",
                "SEED_TEST_USER_PASSWORD": password,
            },
        ):
            call_command("seed_test_user", stdout=output)

        manager.create_user.assert_called_once_with(
            username="test_01",
            email="test_01@gmail.com",
            password=password,
        )
        self.assertNotIn(password, output.getvalue())
