from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser
import httpx
from django.conf import settings

class RobotsPolicy:
    def __init__(self, start_url: str):
        parsed = urlparse(start_url)
        self.robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
        self.parser = RobotFileParser()
        self.parser.set_url(self.robots_url)
        self.raw = ""
        self.available = False

    def load(self):
        try:
            with httpx.Client(timeout=10, headers={"User-Agent":settings.USER_AGENT}) as client:
                response = client.get(self.robots_url)
            if response.status_code == 200:
                self.raw = response.text
                self.parser.parse(response.text.splitlines())
                self.available = True
        except httpx.HTTPError:
            self.available = False
        return self

    def allowed(self, url: str) -> bool:
        if not self.available:
            return True
        return self.parser.can_fetch(settings.USER_AGENT, url)

    def crawl_delay(self):
        if not self.available:
            return None
        return self.parser.crawl_delay(settings.USER_AGENT) or self.parser.crawl_delay("*")
