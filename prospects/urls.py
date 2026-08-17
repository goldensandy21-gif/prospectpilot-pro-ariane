from django.urls import path
from . import views
from . import acquisition_views
from . import api_views

urlpatterns = [
    path("",views.dashboard,name="dashboard"),
    path("prospects/",views.prospect_list,name="prospect_list"),
    path("prospects/import/",views.prospect_import,name="prospect_import"),
    path("prospects/new/",views.prospect_create,name="prospect_create"),
    path("prospects/<int:pk>/",views.prospect_detail,name="prospect_detail"),
    path("prospects/<int:pk>/edit/",views.prospect_edit,name="prospect_edit"),
    path("prospects/<int:pk>/discover-site/",views.discover_site,name="discover_site"),
    path("prospects/<int:pk>/audit/",views.start_audit,name="start_audit"),
    path("prospects/<int:pk>/commoncrawl/",views.start_commoncrawl,name="start_commoncrawl"),
    path("prospects/<int:pk>/enrich/",views.start_enrichment,name="start_enrichment"),
    path("prospects/<int:pk>/report/pdf/",views.prospect_pdf_view,name="prospect_pdf"),
    path("prospects/<int:pk>/optout/",views.mark_optout,name="mark_optout"),
    path("search/",views.company_search,name="company_search"),
    path("search/company/<str:siren>/",views.company_preview,name="company_preview"),
    path("search/import-all/",views.import_all_search,name="import_all_search"),
    path("search/company/<str:siren>/reject/",views.reject_company,name="reject_company"),
    path("prospects/<int:pk>/email/preview/",views.email_preview,name="email_preview"),
    path("prospects/<int:pk>/email/send/",views.email_send,name="email_send"),
    path("api/crm/prospects/<int:pk>/",views.crm_api_prospect,name="api_crm_prospect"),
    path("api/email/prospects/<int:pk>/preview/",views.email_api_preview,name="api_email_preview"),
    path("api/email/prospects/<int:pk>/send/",views.email_api_send,name="api_email_send"),
    path("api/sms/status/",views.sms_api_status,name="api_sms_status"),
    path("unsubscribe/<uuid:token>/",views.unsubscribe,name="unsubscribe"),
    path("exports/csv/",views.export_csv,name="export_csv"),
    path("exports/xlsx/",views.export_xlsx,name="export_xlsx"),
    path("responses/",views.response_board,name="response_board"),
    path("suppressions/",views.suppression_list,name="suppression_list"),
    path("integrations/search-console/",views.search_console_home,name="search_console_home"),
    path("integrations/search-console/connect/",views.search_console_connect,name="search_console_connect"),
    path("integrations/search-console/callback/",views.search_console_callback,name="search_console_callback"),
    path("integrations/search-console/sync/",views.search_console_sync,name="search_console_sync"),

    # ETAPE 22/30 — Acquisition Intelligence & recherche PredictNeed IA
    path("acquisition/",acquisition_views.acquisition_intelligence,name="acquisition_intelligence"),
    path("search/acquisition/",acquisition_views.acquisition_search,name="acquisition_search"),
    path("search/acquisition/<int:pk>/",acquisition_views.acquisition_search_run_detail,name="acquisition_search_run_detail"),

    # ETAPE 15/17 — campagnes
    path("campaigns/",acquisition_views.campaign_list,name="campaign_list"),
    path("campaigns/new/",acquisition_views.campaign_create,name="campaign_create"),
    path("campaigns/<int:pk>/",acquisition_views.campaign_detail,name="campaign_detail"),
    path("campaigns/<int:pk>/preview/",acquisition_views.campaign_preview,name="campaign_preview"),
    path("campaigns/<int:pk>/validate/",acquisition_views.campaign_validate,name="campaign_validate"),
    path("campaigns/<int:pk>/send/",acquisition_views.campaign_send_batch,name="campaign_send_batch"),
    path("campaigns/<int:pk>/send-test/",acquisition_views.campaign_send_test,name="campaign_send_test"),

    # ETAPE 19 — tracking de clic
    path("t/<str:token>/",acquisition_views.campaign_click,name="campaign_click"),

    # ETAPE 16 — transparence individuelle
    path("privacy/prospect/<uuid:token>/",acquisition_views.prospect_privacy,name="prospect_privacy"),

    # ETAPE 34 — réglages e-mail PredictNeed IA
    path("settings/email/",acquisition_views.email_settings,name="email_settings"),

    # ETAPE 20 — API serveur-à-serveur (PredictNeed -> ProspectPilot)
    path("api/predictneed/events/",api_views.predictneed_events_webhook,name="predictneed_events_webhook"),
]
