import os
import secrets
import subprocess
from django.conf import settings
from django.test import SimpleTestCase

class LocalModeSettingsTests(SimpleTestCase):
    def run_check(self,host):
        env=os.environ.copy()
        env.pop('ARMADA_DEVELOPMENT',None)
        env.update(ARMADA_LAN_TEST='1',ARMADA_ALLOWED_HOSTS=host,ARMADA_SECRET_KEY=secrets.token_urlsafe(64),DATABASE_URL='postgresql://placeholder:placeholder@127.0.0.1/placeholder')
        return subprocess.run([str(settings.BASE_DIR/'.venv/bin/python'),str(settings.BASE_DIR/'server/manage.py'),'check'],env=env,capture_output=True,text=True)
    def test_explicit_private_address_accepted(self):
        self.assertEqual(self.run_check('192.168.50.248,127.0.0.1').returncode,0)
    def test_public_address_rejected(self):
        self.assertNotEqual(self.run_check('8.8.8.8').returncode,0)
    def test_domain_rejected(self):
        self.assertNotEqual(self.run_check('app.armada-sibir.ru').returncode,0)
    def test_wildcard_rejected(self):
        self.assertNotEqual(self.run_check('*').returncode,0)
