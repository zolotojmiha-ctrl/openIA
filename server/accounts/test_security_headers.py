from django.http import HttpResponse
from django.test import SimpleTestCase,RequestFactory
from .middleware import ContentPolicyMiddleware

class ContentPolicyTests(SimpleTestCase):
    def test_default_policy_blocks_objects_base_changes_and_framing(self):
        response=ContentPolicyMiddleware(lambda request:HttpResponse('SYNTHETIC'))(RequestFactory().get('/'))
        self.assertEqual(response['Content-Security-Policy'],"object-src 'none'; base-uri 'self'; frame-ancestors 'none'")
    def test_private_preview_keeps_its_existing_policy(self):
        response=HttpResponse('SYNTHETIC');response['Content-Security-Policy']="sandbox; default-src 'none'"
        result=ContentPolicyMiddleware(lambda request:response)(RequestFactory().get('/'))
        self.assertEqual(result['Content-Security-Policy'],"sandbox; default-src 'none'")
