"""Section J (automatisation email) — détection des réponses entrantes.

Connexion IMAP en lecture seule à la boîte contact-predict@predictneed-ia.com.
Identifiants exclusivement lus depuis des variables d'environnement
(IMAP_HOST/IMAP_PORT/IMAP_USER/IMAP_PASSWORD/IMAP_MAILBOX) — jamais en dur
dans le code, jamais committés. Ce module NE RÉPOND JAMAIS automatiquement à
un prospect : uniquement réception, rattachement, journalisation et arrêt de
séquence (via ContactLog outcome="replied", déjà utilisé par
campaign_sequencing.py::_stop_reason — aucune deuxième logique d'arrêt).

`process_inbound_message()` est la logique pure (aucun accès réseau) —
testable avec un email.message.Message construit à la main. `poll_inbound_replies()`
est le seul point qui ouvre une vraie connexion IMAP.
"""
import email
import hashlib
import imaplib
import os
import re
from email.utils import getaddresses

from django.db import transaction
from django.utils import timezone

from ..models import ContactLog, EmailSend, EngagementEvent, ProcessedInboundMessage


def _imap_settings():
    return {
        "host": os.getenv("IMAP_HOST", ""),
        "port": int(os.getenv("IMAP_PORT", "993") or "993"),
        "user": os.getenv("IMAP_USER", ""),
        "password": os.getenv("IMAP_PASSWORD", ""),
        "mailbox": os.getenv("IMAP_MAILBOX", "INBOX"),
    }


def _extract_message_ids(header_value):
    if not header_value:
        return []
    return re.findall(r"<[^<>]+>", header_value)


def match_email_send_for_reply(in_reply_to, references, from_addr, subject):
    """Priorité (section J) : 1) In-Reply-To, 2) References, 3) repli
    adresse expéditeur + sujet, UNIQUEMENT si l'association est non ambiguë
    (exactement un EmailSend correspondant)."""
    for message_id in _extract_message_ids(in_reply_to) + _extract_message_ids(references):
        record = EmailSend.objects.filter(message_id=message_id, is_test=False).first()
        if record:
            return record

    if not from_addr:
        return None
    matches = list(EmailSend.objects.filter(to_email__iexact=from_addr, is_test=False, status="sent"))
    if subject:
        normalized = re.sub(r"^(re|fw|fwd)\s*:\s*", "", subject.strip(), flags=re.IGNORECASE).lower()
        matches = [m for m in matches if normalized and normalized in (m.subject or "").lower()]
    if len(matches) == 1:
        return matches[0]
    return None


def process_inbound_message(msg, message_identity=None):
    """Traite un message déjà parsé (email.message.Message) — aucune
    logique réseau ici, entièrement testable avec des messages construits
    à la main.

    Section E (Round D, verrous production) — transaction atomique réelle :
    la réclamation de l'idempotency key, la mise à jour de Prospect, la
    création du ContactLog(outcome="replied") et de l'EngagementEvent sont
    désormais UNE seule unité `transaction.atomic()`. Un crash pendant cette
    fonction (process tué, connexion DB coupée) fait donc rollback de tout
    le bloc — aucun état partiel neuf ne peut plus apparaître à partir de
    maintenant.

    Ceinture ET bretelles pour un état partiel PRÉEXISTANT (avant ce
    correctif, ou tout incident résiduel) : si l'EngagementEvent existe déjà
    (idempotency_key) mais que SON ContactLog n'a jamais été créé (tracé via
    `metadata["contact_log_id"]`, jamais un simple "un ContactLog replied
    existe quelque part" — un prospect peut légitimement répondre plusieurs
    fois), la reprise RÉPARE l'état (crée le ContactLog manquant, met à jour
    Prospect) au lieu de retourner immédiatement `already_recorded`.

    `message_identity` (section 7, correctif final) : quand fourni (cas
    réel, depuis poll_inbound_replies), la création de l'EngagementEvent est
    le verrou d'idempotence ATOMIQUE — via EngagementEvent.idempotency_key,
    contrainte unique en base — en plus du registre ProcessedInboundMessage.
    Sans `message_identity` (appel direct historique, tests unitaires),
    comportement inchangé : toujours créé, toujours (ré)traité."""
    in_reply_to = msg.get("In-Reply-To", "")
    references = msg.get("References", "")
    from_header = msg.get("From", "")
    subject = msg.get("Subject", "") or ""
    from_addrs = getaddresses([from_header]) if from_header else []
    from_addr = from_addrs[0][1] if from_addrs else ""

    record = match_email_send_for_reply(in_reply_to, references, from_addr, subject)
    if not record or not record.prospect_id:
        return {"action": "unmatched"}

    prospect = record.prospect
    now = timezone.now()

    event_defaults = {
        "campaign_prospect": record.campaign_prospect, "prospect": prospect,
        "campaign": record.campaign_prospect.campaign if record.campaign_prospect else None,
        "event_type": "email_replied", "source": "prospectpilot",
        "metadata": {"email_send_id": record.pk, "contact_log_id": None},
    }

    with transaction.atomic():
        if message_identity:
            event, created = EngagementEvent.objects.select_for_update().get_or_create(
                idempotency_key=f"inbound-reply:{message_identity}", defaults=event_defaults,
            )
        else:
            event, created = EngagementEvent.objects.create(**event_defaults), True

        if created or not event.metadata.get("contact_log_id"):
            prospect.last_replied_at = now
            if prospect.status != "do_not_contact":
                prospect.status = "replied"
            prospect.save(update_fields=["last_replied_at", "status"])

            # Déclenche l'arrêt de séquence existant (stop_on_reply déjà lu
            # depuis ContactLog.outcome="replied" par
            # campaign_sequencing.py::_stop_reason) — aucune deuxième
            # logique d'arrêt créée ici.
            contact_log = ContactLog.objects.create(
                prospect=prospect, channel="email",
                subject=subject or "(sans objet)",
                message="Réponse reçue (détection IMAP automatique).",
                outcome="replied",
                campaign_prospect=record.campaign_prospect,
            )
            event.metadata = {**event.metadata, "contact_log_id": contact_log.pk}
            event.save(update_fields=["metadata"])
            action = "matched" if created else "repaired"
        else:
            action = "already_recorded"

    return {"action": action, "prospect_id": prospect.pk, "email_send_id": record.pk}


def _message_identity(msg):
    """Identifiant stable pour le registre d'idempotence (section 5,
    correctif). Utilise l'en-tête Message-ID (unique par construction pour
    tout email légitime) ; à défaut (message malformé, rare), une empreinte
    synthétique des en-têtes disponibles pour ne jamais planter."""
    message_id = (msg.get("Message-ID") or "").strip()
    if message_id:
        return message_id
    fallback_source = f"{msg.get('From', '')}|{msg.get('Subject', '')}|{msg.get('Date', '')}"
    return "synthetic:" + hashlib.sha256(fallback_source.encode("utf-8")).hexdigest()


def poll_inbound_replies():
    """Seul point de ce module qui ouvre une vraie connexion IMAP.

    Correctif (section 5) : lecture STRICTEMENT sans effet de bord sur
    l'état \\Seen de la boîte —
    - `conn.select(mailbox, readonly=True)` : le serveur lui-même refuse
      toute modification de drapeau pendant la session ;
    - `FETCH BODY.PEEK[]` (jamais `RFC822`, qui marque implicitement \\Seen
      sur de nombreux serveurs) ;
    - aucun `STORE` n'est jamais appelé.
    Ne répond jamais au prospect. Si les identifiants ne sont pas
    configurés, ne tente aucune connexion — comportement dormant, pas une
    erreur.

    Correctif (section 7, audit correctif final) — idempotence résistante
    aux crashes : `ProcessedInboundMessage.status` est un vrai état
    (pending -> processing -> processed/failed), pas une simple ligne dont
    la seule EXISTENCE vaudrait « déjà traité ». Un crash entre la
    « réclamation » (passage à `processing`) et la fin du traitement laisse
    le message dans un état RÉESSAYABLE au prochain poll (jamais
    `processed`), donc jamais silencieusement perdu. La réclamation est
    faite sous verrou (select_for_update, sans effet sur SQLite qui ne le
    supporte pas — dégrade proprement en SELECT simple, comme partout
    ailleurs dans ce projet) pour rester correcte même si deux pollers
    tournaient en parallèle. Une fois RÉELLEMENT `processed`, un double
    polling ne crée toujours jamais de doublon (voir aussi le verrou
    ceinture-et-bretelles dans process_inbound_message via
    EngagementEvent.idempotency_key)."""
    settings_ = _imap_settings()
    if not settings_["host"] or not settings_["user"] or not settings_["password"]:
        return {"action": "imap_not_configured", "results": []}

    results = []
    conn = imaplib.IMAP4_SSL(settings_["host"], settings_["port"])
    try:
        conn.login(settings_["user"], settings_["password"])
        conn.select(settings_["mailbox"], readonly=True)
        status, data = conn.search(None, "ALL")
        if status != "OK":
            return {"action": "search_failed", "results": []}
        for num in data[0].split():
            fetch_status, msg_data = conn.fetch(num, "(BODY.PEEK[])")
            if fetch_status != "OK" or not msg_data or not msg_data[0]:
                continue
            raw_email = msg_data[0][1]
            msg = email.message_from_bytes(raw_email)

            message_identity = _message_identity(msg)

            with transaction.atomic():
                registry_row, created = ProcessedInboundMessage.objects.select_for_update().get_or_create(
                    message_id=message_identity, defaults={"status": "processing"},
                )
                if not created:
                    if registry_row.status == "processed":
                        results.append({"action": "already_processed", "message_id": message_identity})
                        continue
                    # pending/processing/failed laissé par un crash ou un
                    # message non reconnu précédent -> nouvelle tentative.
                    registry_row.status = "processing"
                    registry_row.save(update_fields=["status", "updated_at"])

            try:
                result = process_inbound_message(msg, message_identity=message_identity)
            except Exception as exc:
                registry_row.status = "failed"
                registry_row.result = f"error: {exc}"[:30]
                registry_row.save(update_fields=["status", "result", "updated_at"])
                results.append({"action": "processing_error", "message_id": message_identity})
                continue

            result["message_id"] = message_identity
            results.append(result)
            registry_row.status = "processed"
            registry_row.result = result["action"]
            registry_row.save(update_fields=["status", "result", "updated_at"])
    finally:
        try:
            conn.logout()
        except Exception:
            pass
    return {"action": "polled", "results": results}
