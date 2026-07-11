import requests

API_KEY = "sk-live-abc123def456"


def fetch(url):
    return requests.get(url, headers={"Authorization": API_KEY})
