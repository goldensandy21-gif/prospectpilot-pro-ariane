import os
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand

from .email_templates import upsert_email_templates
from ...services.seed_data import run_all_seeds


class Command(BaseCommand):
    help = "Initialise le compte administrateur et les modèles d'e-mail sans ajouter de faux prospect."

    def handle(self, *args, **options):
        username = os.getenv("INITIAL_ADMIN_USERNAME", "ariane")
        password = os.getenv("INITIAL_ADMIN_PASSWORD", "ChangeMe123!")
        email = os.getenv("INITIAL_ADMIN_EMAIL", "")

        user, created = User.objects.get_or_create(
            username=username,
            defaults={"email": email, "is_staff": True, "is_superuser": True},
        )
        changed = False
        if created:
            user.set_password(password)
            changed = True
        if not user.is_staff:
            user.is_staff = True
            changed = True
        if not user.is_superuser:
            user.is_superuser = True
            changed = True
        if email and user.email != email:
            user.email = email
            changed = True
        if changed:
            user.save()

        upsert_email_templates()
        run_all_seeds()

        if created:
            self.stdout.write(self.style.SUCCESS(
                f"Compte {username} créé. Aucun faux prospect ajouté. Modèles e-mail disponibles."
            ))
        else:
            self.stdout.write(self.style.SUCCESS(
                f"Application vérifiée. Compte {username} conservé et modèles e-mail disponibles."
            ))
