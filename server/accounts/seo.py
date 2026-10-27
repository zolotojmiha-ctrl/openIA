from django.http import HttpResponse

def robots(request):
    if request.get_host().split(':')[0]=='app.armada-sibir.ru':text='User-agent: *\nDisallow: /\n'
    else:text='User-agent: *\nAllow: /\nDisallow: /cabinet/\nDisallow: /portal/\nSitemap: https://armada-sibir.ru/sitemap.xml\n'
    return HttpResponse(text,content_type='text/plain; charset=utf-8')

def sitemap(request):
    return HttpResponse('<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://armada-sibir.ru/</loc></url></urlset>',content_type='application/xml')
