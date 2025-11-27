#!/usr/bin/python

# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import json
import os
import random
import uuid
import logging
from pythonjsonlogger.json import JsonFormatter

from locust import HttpUser, task, between
from locust_plugins.users.playwright import PlaywrightUser, pw, PageWithRetry, event

from openfeature import api
from openfeature.contrib.provider.ofrep import OFREPProvider

from playwright.async_api import Route, Request

logger = logging.getLogger()
logger.setLevel(logging.INFO)

handler = logging.StreamHandler()
formatter = JsonFormatter(
    fmt="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    rename_fields={"asctime": "timestamp"}
)
handler.setFormatter(formatter)

logger.addHandler(handler)

# Initialize Flagd provider
base_url = f"http://{os.environ.get('FLAGD_HOST', 'localhost')}:{os.environ.get('FLAGD_OFREP_PORT', 8016)}"
api.set_provider(OFREPProvider(base_url=base_url))

def get_flagd_value(FlagName):
    # Initialize OpenFeature
    client = api.get_client()
    return client.get_integer_value(FlagName, 0)

categories = [
    "binoculars",
    "telescopes",
    "accessories",
    "assembly",
    "travel",
    "books",
    None,
]

products = [
    "0PUK6V6EV0",
    "1YMWWN1N4O",
    "2ZYFJ3GM2N",
    "66VCHSJNUP",
    "6E92ZMYYFZ",
    "9SIQT8TOJO",
    "L9ECAV7KIM",
    "LS4PSXUNUM",
    "OLJCESPC7Z",
    "HQTGWGPNH4",
]

people_file = open('people.json')
people = json.load(people_file)

class WebsiteUser(HttpUser):
    wait_time = between(1, 10)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    @task(1)
    def index(self):
        logger.info("User accessing index page")
        self.client.get("/")

    @task(10)
    def browse_product(self):
        product = random.choice(products)
        logger.info(f"User browsing product: {product}")
        self.client.get("/api/products/" + product)

    @task(3)
    def get_recommendations(self):
        product = random.choice(products)
        logger.info("User getting recommendations for product", extra={"product": product})
        params = {
            "productIds": [product],
        }
        self.client.get("/api/recommendations", params=params)

    @task(2)
    def get_product_reviews(self):
        product = random.choice(products)
        logger.info("User getting product reviews for product", extra={"product": product})
        self.client.get("/api/product-reviews/" + product)

    @task(1)
    def ask_product_ai_assistant(self):
        product = random.choice(products)
        question = 'Can you summarize the product reviews?'
        logger.info("Asking the AI Assistant a question for product", extra={"product": product, "question": question})
        question = {
            "question": question
        }
        self.client.post("/api/product-ask-ai-assistant/" + product, json=question)

    @task(3)
    def get_ads(self):
        category = random.choice(categories)
        logger.info("User getting ads for category", extra={"category": category})
        params = {
            "contextKeys": [category],
        }
        self.client.get("/api/data/", params=params)

    @task(3)
    def view_cart(self):
        logger.info("User viewing cart")
        self.client.get("/api/cart")

    @task(2)
    def add_to_cart(self, user=""):
        if user == "":
            user = str(uuid.uuid1())
        product = random.choice(products)
        quantity = random.choice([1, 2, 3, 4, 5, 10])
        logger.info("User adding to cart", extra={"user": user, "quantity": quantity, "product": product})
        self.client.get("/api/products/" + product)
        cart_item = {
            "item": {
                "productId": product,
                "quantity": quantity,
            },
            "userId": user,
        }
        self.client.post("/api/cart", json=cart_item)

    @task(1)
    def checkout(self):
        user = str(uuid.uuid1())
        self.add_to_cart(user=user)
        checkout_person = random.choice(people)
        checkout_person["userId"] = user
        self.client.post("/api/checkout", json=checkout_person)
        logger.info("Checkout completed for user", extra={"user": user})

    @task(1)
    def checkout_multi(self):
        user = str(uuid.uuid1())
        item_count = random.choice([2, 3, 4])
        for i in range(item_count):
            self.add_to_cart(user=user)
        checkout_person = random.choice(people)
        checkout_person["userId"] = user
        self.client.post("/api/checkout", json=checkout_person)
        logger.info("Multi-item checkout completed for user", extra={"user": user})

    @task(5)
    def flood_home(self):
        flood_count = get_flagd_value("loadGeneratorFloodHomepage")
        if flood_count > 0:
            logger.info("User flooding homepage", extra={"count": flood_count})
            for _ in range(0, flood_count):
                self.client.get("/")

    def on_start(self):
        session_id = str(uuid.uuid4())
        logger.info("Starting user session", extra={"session_id": session_id})
        self.index()


browser_traffic_enabled = os.environ.get("LOCUST_BROWSER_TRAFFIC_ENABLED", "").lower() in ("true", "yes", "on")

if browser_traffic_enabled:
    class WebsiteBrowserUser(PlaywrightUser):
        headless = True  # to use a headless browser, without a GUI

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)

        @task
        @pw
        async def open_cart_page_and_change_currency(self, page: PageWithRetry):
            try:
                page.on("console", lambda msg: print(msg.text))
                await page.goto("/cart", wait_until="domcontentloaded")
                await page.select_option('[name="currency_code"]', 'CHF')
                await page.wait_for_timeout(2000)  # giving the browser time to export the traces
                logger.info("Currency changed to CHF")
            except Exception as e:
                logger.error("Error in change currency task", extra={"error": str(e)})

        @task
        @pw
        async def add_product_to_cart(self, page: PageWithRetry):
            try:
                page.on("console", lambda msg: print(msg.text))
                await page.goto("/", wait_until="domcontentloaded")
                await page.click('p:has-text("Roof Binoculars")')
                await page.wait_for_load_state("domcontentloaded")
                await page.click('button:has-text("Add To Cart")')
                await page.wait_for_load_state("domcontentloaded")
                await page.wait_for_timeout(2000)  # giving the browser time to export the traces
                logger.info("Product added to cart successfully")
            except Exception as e:
                logger.error("Error in add to cart task", extra={"error": str(e)})
