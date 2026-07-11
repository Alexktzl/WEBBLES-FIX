import os
import requests

API_KEY = os.environ["API_KEY"]


def fetch(url):
    return requests.get(url, headers={"Authorization": API_KEY})
