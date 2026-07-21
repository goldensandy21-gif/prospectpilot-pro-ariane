from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("prospects", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="PublicEmail",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("email", models.EmailField(db_index=True, max_length=254)),
                ("email_type", models.CharField(choices=[("generic", "Générique"), ("personal", "Nominatif"), ("unknown", "Inconnu")], db_index=True, default="unknown", max_length=20)),
                ("source_url", models.URLField(blank=True, max_length=1000)),
                ("source_type", models.CharField(choices=[("website", "Site web"), ("contact_page", "Page contact"), ("legal_notice", "Mentions légales"), ("manual", "Ajout manuel"), ("other", "Autre")], default="website", max_length=30)),
                ("is_primary", models.BooleanField(default=False)),
                ("is_active", models.BooleanField(default=True)),
                ("discovery_method", models.CharField(default="public_page", max_length=80)),
                ("notes", models.TextField(blank=True)),
                ("found_at", models.DateTimeField(auto_now_add=True)),
                ("prospect", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="public_emails", to="prospects.prospect")),
            ],
            options={
                "ordering": ["-is_primary", "email"],
            },
        ),
        migrations.CreateModel(
            name="PublicPhone",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("phone", models.CharField(db_index=True, max_length=40)),
                ("source_url", models.URLField(blank=True, max_length=1000)),
                ("source_type", models.CharField(choices=[("website", "Site web"), ("contact_page", "Page contact"), ("legal_notice", "Mentions légales"), ("manual", "Ajout manuel"), ("other", "Autre")], default="website", max_length=30)),
                ("is_primary", models.BooleanField(default=False)),
                ("is_active", models.BooleanField(default=True)),
                ("discovery_method", models.CharField(default="public_page", max_length=80)),
                ("notes", models.TextField(blank=True)),
                ("found_at", models.DateTimeField(auto_now_add=True)),
                ("prospect", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="public_phones", to="prospects.prospect")),
            ],
            options={
                "ordering": ["-is_primary", "phone"],
            },
        ),
        migrations.CreateModel(
            name="PublicContactForm",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("page_url", models.URLField(max_length=1000)),
                ("form_action", models.CharField(blank=True, max_length=1000)),
                ("form_method", models.CharField(blank=True, max_length=12)),
                ("has_email_field", models.BooleanField(default=False)),
                ("has_phone_field", models.BooleanField(default=False)),
                ("is_primary", models.BooleanField(default=False)),
                ("is_active", models.BooleanField(default=True)),
                ("discovery_method", models.CharField(default="public_page", max_length=80)),
                ("found_at", models.DateTimeField(auto_now_add=True)),
                ("prospect", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="contact_forms", to="prospects.prospect")),
            ],
            options={
                "ordering": ["-is_primary", "page_url"],
            },
        ),
        migrations.CreateModel(
            name="PublicSocialLink",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("platform", models.CharField(choices=[("linkedin", "LinkedIn"), ("facebook", "Facebook"), ("instagram", "Instagram"), ("x", "X / Twitter"), ("other", "Autre")], default="other", max_length=30)),
                ("url", models.URLField(max_length=1000)),
                ("source_url", models.URLField(blank=True, max_length=1000)),
                ("is_active", models.BooleanField(default=True)),
                ("discovery_method", models.CharField(default="public_page", max_length=80)),
                ("found_at", models.DateTimeField(auto_now_add=True)),
                ("prospect", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="social_links", to="prospects.prospect")),
            ],
            options={
                "ordering": ["platform", "url"],
            },
        ),
        migrations.AddConstraint(
            model_name="publicemail",
            constraint=models.UniqueConstraint(fields=("prospect", "email"), name="unique_public_email_per_prospect"),
        ),
        migrations.AddConstraint(
            model_name="publicphone",
            constraint=models.UniqueConstraint(fields=("prospect", "phone"), name="unique_public_phone_per_prospect"),
        ),
        migrations.AddConstraint(
            model_name="publiccontactform",
            constraint=models.UniqueConstraint(fields=("prospect", "page_url", "form_action"), name="unique_contact_form_per_prospect"),
        ),
        migrations.AddConstraint(
            model_name="publicsociallink",
            constraint=models.UniqueConstraint(fields=("prospect", "url"), name="unique_social_link_per_prospect"),
        ),
    ]
