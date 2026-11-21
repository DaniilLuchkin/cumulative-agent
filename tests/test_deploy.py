import importlib.util
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("render_nginx", ROOT / "deploy/render_nginx.py")
renderer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(renderer)


class DeploymentTests(unittest.TestCase):
    def test_ip_validation_prevents_config_injection_and_private_address_certificates(self):
        for address in ("127.0.0.1", "10.0.0.1", "192.168.1.2", "169.254.169.254", "203.0.113.1", "example.com", "1.1.1.1; include secret;", "::1"):
            with self.subTest(address=address), self.assertRaises(ValueError):
                renderer.render(address)

    def test_challenge_bootstrap_exposes_no_plaintext_application(self):
        config = renderer.render("129.146.1.1")
        self.assertIn("location ^~ /.well-known/acme-challenge/", config)
        self.assertIn("return 503;", config)
        self.assertNotIn("proxy_pass", config)
        self.assertNotIn("ssl_certificate", config)

    def test_tls_preserves_acme_challenge_and_owner_auth_headers(self):
        config = renderer.render("129.146.1.1", tls=True)
        self.assertIn("return 308 https://129.146.1.1$request_uri;", config)
        self.assertIn("listen 443 ssl;", config)
        self.assertIn("proxy_pass http://127.0.0.1:8000;", config)
        self.assertIn("proxy_set_header Authorization $http_authorization;", config)
        self.assertIn("location ^~ /.well-known/acme-challenge/", config)
        self.assertNotIn("X-Frame-Options", config)

    def test_install_scripts_parse_as_bash(self):
        subprocess.run(["bash", "-n", str(ROOT / "deploy/install-ubuntu.sh"), str(ROOT / "deploy/enable-ip-https.sh"), str(ROOT / "deploy/update-branch.sh")], check=True)
