import httpx
from django.conf import settings

ENDPOINT = "https://pagespeedonline.googleapis.com/pagespeedonline/v5/runPagespeed"

def run_pagespeed(url):
    params = [
        ("url",url),("strategy","mobile"),
        ("category","performance"),("category","seo"),("category","accessibility")
    ]
    if settings.PAGESPEED_API_KEY:
        params.append(("key",settings.PAGESPEED_API_KEY))
    with httpx.Client(timeout=80) as client:
        response = client.get(ENDPOINT, params=params)
        response.raise_for_status()
        data = response.json()
    cats = data.get("lighthouseResult",{}).get("categories",{})
    def score(name):
        v = cats.get(name,{}).get("score")
        return round(v*100) if isinstance(v,(int,float)) else None
    return {
        "performance_score":score("performance"),
        "seo_score":score("seo"),
        "accessibility_score":score("accessibility"),
    }
