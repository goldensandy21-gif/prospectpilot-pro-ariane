EMAIL_TEMPLATE_DEFINITIONS = [
    {
        "name": "Signal clair",
        "subject": "{{ company_name }} - découvrir ProspectPilot Pro",
        "preheader": "Une prise de contact courte, claire et professionnelle.",
        "html_body": "VISUAL_TEMPLATE:signal",
        "text_body": """Bonjour,

J'ai découvert {{ company_name }} et votre activité professionnelle.

ProspectPilot Pro aide à identifier des prospects B2B exploitables, suivre les contacts et organiser les actions commerciales dans un seul espace.

Je peux vous transmettre une courte présentation adaptée à votre activité, sans engagement.

Bien cordialement,
Ariane""",
    },
    {
        "name": "Plan d'action",
        "subject": "{{ company_name }} - transformer vos signaux en décisions",
        "preheader": "Un message structuré pour présenter ProspectPilot Pro.",
        "html_body": "VISUAL_TEMPLATE:blueprint",
        "text_body": """Bonjour,

Je me permets de vous contacter au sujet de {{ company_name }}.

ProspectPilot Pro permet de collecter, qualifier et suivre les prospects réellement contactables avec plus de méthode.

Si le sujet est pertinent pour vous, je peux vous envoyer une présentation courte et concrète.

Bien cordialement,
Ariane""",
    },
    {
        "name": "Croissance & priorités",
        "subject": "{{ company_name }} - mieux détecter vos priorités de croissance",
        "preheader": "Une approche orientée priorités, signaux et opportunités.",
        "html_body": "VISUAL_TEMPLATE:growth",
        "text_body": """Bonjour,

J'ai identifié {{ company_name }} dans le cadre d'une recherche professionnelle liée à votre secteur.

ProspectPilot Pro aide à transformer les informations publiques disponibles en prospects qualifiés et suivis dans le CRM.

Je serais ravie de vous transmettre une courte présentation si cela peut vous intéresser.

Bien cordialement,
Ariane""",
    },
    {
        "name": "Approche chaleureuse",
        "subject": "{{ company_name }} - échange rapide autour de ProspectPilot Pro",
        "preheader": "Un ton plus direct pour ouvrir la discussion.",
        "html_body": "VISUAL_TEMPLATE:warm",
        "text_body": """Bonjour,

Je vous contacte simplement après avoir découvert {{ company_name }}.

Avec ProspectPilot Pro, l'objectif est de faciliter la prospection B2B en gardant uniquement les contacts utiles et exploitables.

Je peux vous envoyer une présentation courte, uniquement si le sujet vous paraît utile.

Bien cordialement,
Ariane""",
    },
]


def upsert_email_templates():
    from prospects.models import EmailTemplate

    for item in EMAIL_TEMPLATE_DEFINITIONS:
        EmailTemplate.objects.update_or_create(
            name=item["name"],
            defaults={
                "subject": item["subject"],
                "preheader": item["preheader"],
                "html_body": item["html_body"],
                "text_body": item["text_body"],
                "active": True,
            },
        )
