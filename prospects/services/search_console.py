from datetime import timedelta
from django.conf import settings
from django.utils import timezone
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/webmasters.readonly"]

def client_config():
    return {
        "web":{
            "client_id":settings.GOOGLE_OAUTH_CLIENT_ID,
            "client_secret":settings.GOOGLE_OAUTH_CLIENT_SECRET,
            "auth_uri":"https://accounts.google.com/o/oauth2/auth",
            "token_uri":"https://oauth2.googleapis.com/token",
            "redirect_uris":[settings.GOOGLE_OAUTH_REDIRECT_URI],
        }
    }

def create_flow(state=None):
    flow = Flow.from_client_config(client_config(), scopes=SCOPES, state=state)
    flow.redirect_uri = settings.GOOGLE_OAUTH_REDIRECT_URI
    return flow

def credentials_from_connection(connection):
    return Credentials(
        token=connection.token,
        refresh_token=connection.refresh_token or None,
        token_uri=connection.token_uri,
        client_id=settings.GOOGLE_OAUTH_CLIENT_ID,
        client_secret=settings.GOOGLE_OAUTH_CLIENT_SECRET,
        scopes=connection.scopes,
    )

def list_properties(connection):
    service = build("searchconsole","v1",credentials=credentials_from_connection(connection),cache_discovery=False)
    return service.sites().list().execute().get("siteEntry",[])

def fetch_metrics(connection, property_url, days=28):
    service = build("searchconsole","v1",credentials=credentials_from_connection(connection),cache_discovery=False)
    end = timezone.localdate() - timedelta(days=1)
    start = end - timedelta(days=days-1)
    body = {
        "startDate":start.isoformat(),
        "endDate":end.isoformat(),
        "dimensions":["date","query","page"],
        "rowLimit":25000,
    }
    return service.searchanalytics().query(siteUrl=property_url, body=body).execute()
