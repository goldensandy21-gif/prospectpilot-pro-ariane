def build_message(prospect, summary=None):
    issues = list(summary.issues[:3]) if summary else []
    if issues:
        observation = ", ".join(x.lower() for x in issues)
    else:
        observation = "plusieurs signaux qui pourraient être mieux suivis ou priorisés"
    return (
        f"Bonjour,\n\n"
        f"J’ai découvert {prospect.name} et votre activité professionnelle. "
        f"J’ai notamment relevé {observation}.\n\n"
        "ProspectPilot Pro aide à identifier des prospects B2B exploitables, suivre les contacts "
        "et organiser les actions commerciales dans un seul espace.\n\n"
        "Je peux vous transmettre une courte présentation adaptée à votre activité, sans engagement.\n\n"
        "Si vous ne souhaitez pas recevoir de message de ma part, dites-le-moi simplement et je mettrai immédiatement à jour ma liste d’opposition.\n\n"
        "Bien cordialement,\nAriane"
    )
