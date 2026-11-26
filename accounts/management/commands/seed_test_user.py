from decouple import config
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Create the configured test user if it does not already exist."

    def handle(self, *args, **options):
        username = config("SEED_TEST_USER_USERNAME", default="").strip()
        email = config("SEED_TEST_USER_EMAIL", default="").strip()
        password = config("SEED_TEST_USER_PASSWORD", default="")

        if not username or not email or not password:
            raise CommandError(
                "Set SEED_TEST_USER_USERNAME, SEED_TEST_USER_EMAIL, and "
                "SEED_TEST_USER_PASSWORD in the environment."
            )
        if len(password) < 8:
            raise CommandError("SEED_TEST_USER_PASSWORD must be at least 8 characters.")

        user_model = get_user_model()
        existing = user_model.objects.filter(username=username).first()
        if existing:
            if existing.email != email:
                raise CommandError(
                    f"User {username!r} already exists with a different email."
                )
            self.stdout.write(f"Test user {username!r} already exists; left unchanged.")
            return

        user_model.objects.create_user(username=username, email=email, password=password)
        self.stdout.write(self.style.SUCCESS(f"Created test user {username!r}."))
