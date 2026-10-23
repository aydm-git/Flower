import os
import sys
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch

from werkzeug.middleware.dispatcher import DispatcherMiddleware
from werkzeug.test import Client
from werkzeug.wrappers import Response


class ImageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.sources = []

    def handle_starttag(self, tag, attrs):
        if tag == "img":
            self.sources.append(dict(attrs).get("src", ""))


class ProductImageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Import the actual application with an isolated database and upload folder.
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
        cls.addClassCleanup(sys.path.pop, 0)
        temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(temporary.cleanup)
        with patch.dict(os.environ, {"SECRET_KEY": "image-tests-only"}):
            from config import Config
            with patch.multiple(
                Config,
                SQLALCHEMY_DATABASE_URI="sqlite://",
                UPLOAD_DIR=temporary.name,
            ):
                from app import app
        cls.app = app
        cls.app.config["TESTING"] = True
        from models import DEFAULT_PRODUCTS, db
        cls.product_count = len(DEFAULT_PRODUCTS)

        def close_database():
            with cls.app.app_context():
                db.session.remove()
                db.engine.dispose()

        cls.addClassCleanup(close_database)

    def check_catalog_images(self, client, catalog_url):
        page = client.get(catalog_url)
        self.assertEqual(page.status_code, 200)
        parser = ImageParser()
        parser.feed(page.get_data(as_text=True))
        self.assertEqual(len(parser.sources), self.product_count)
        for source in parser.sources:
            with self.subTest(source=source):
                self.assertTrue(source)
                with client.get(source) as image:
                    self.assertEqual(image.status_code, 200)
                    self.assertEqual(image.mimetype, "image/jpeg")
                    self.assertTrue(image.data.startswith(b"\xff\xd8\xff"))

    def test_images_at_root(self):
        self.check_catalog_images(self.app.test_client(), "/")

    def test_images_under_application_prefix(self):
        mounted_app = DispatcherMiddleware(
            Response("Not found", status=404), {"/Flower": self.app}
        )
        self.check_catalog_images(Client(mounted_app, Response), "/Flower/")

    def test_product_page_images(self):
        for product_id in range(1, self.product_count + 1):
            with self.subTest(product_id=product_id):
                self.check_catalog_images(
                    self.app.test_client(), f"/product/{product_id}"
                )


if __name__ == "__main__":
    unittest.main()
