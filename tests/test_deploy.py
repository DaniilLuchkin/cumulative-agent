import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIND = ROOT / "deploy" / "find_miniapp.py"

SITE_9443 = """
# managed by hand
server {
    listen 80;
    server_name 40.233.91.116;
    return 301 https://$host:9443$request_uri;
}
server {
    listen 9443 ssl;
    server_name 40.233.91.116;
    ssl_certificate CERT;
    ssl_certificate_key KEY;
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
    }
}
"""


def run(folder):
    return subprocess.run(["python3", str(FIND), folder], capture_output=True, text=True)


class FindMiniAppTests(unittest.TestCase):
    def site(self, folder, text, name="cumulative"):
        cert = Path(folder, "fullchain.pem")
        cert.write_text("x")
        Path(folder, name).write_text(text.replace("CERT", str(cert)).replace("KEY", str(cert)))
        return cert

    def test_custom_tls_port_is_kept_in_the_address(self):
        with tempfile.TemporaryDirectory() as folder:
            self.site(folder, SITE_9443)
            result = run(folder)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(json.loads(result.stdout), {"url": "https://40.233.91.116:9443/app", "port": 8000})

    def test_standard_port_and_other_upstream(self):
        with tempfile.TemporaryDirectory() as folder:
            self.site(folder, SITE_9443.replace("listen 9443 ssl", "listen 443 ssl http2").replace("127.0.0.1:8000", "localhost:8080"))
            self.assertEqual(json.loads(run(folder).stdout), {"url": "https://40.233.91.116/app", "port": 8080})

    def test_no_certificate_or_no_tls_means_nothing(self):
        with tempfile.TemporaryDirectory() as folder:
            cert = self.site(folder, SITE_9443)
            os.remove(cert)
            self.assertEqual(run(folder).returncode, 1)
        with tempfile.TemporaryDirectory() as folder:
            self.site(folder, "server { listen 80; server_name a.b; location / { proxy_pass http://127.0.0.1:8000; } }")
            self.assertEqual(run(folder).returncode, 1)
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(run(folder).returncode, 1)

    def test_installer_parses(self):
        subprocess.run(["bash", "-n", str(ROOT / "deploy" / "install.sh")], check=True)


if __name__ == "__main__":
    unittest.main()
