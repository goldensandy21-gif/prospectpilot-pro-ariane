from django.contrib.auth.models import User
from django.core.management.base import BaseCommand

from .email_templates import upsert_email_templates


class Command(BaseCommand):
    def handle(self, *args, **kwargs):
        user, _ = User.objects.get_or_create(username="ariane")
        user.set_password("ChangeMe123!")
        user.is_staff = True
        user.is_superuser = True
        user.save()
        upsert_email_templates()
        self.stdout.write(self.style.SUCCESS("Compte et modèles e-mail créés. Aucun faux prospect ajouté."))
