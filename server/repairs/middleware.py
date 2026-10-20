from django.core.files.uploadhandler import FileUploadHandler,StopUpload
from django.http import JsonResponse,HttpResponse
from django.utils.deprecation import MiddlewareMixin

class FieldUploadLimit(FileUploadHandler):
    """Stop multipart spooling before a phone upload exhausts the NAS temporary disk."""
    def __init__(self,request):super().__init__(request);self.total=0;self.count=0
    def new_file(self,*args,**kwargs):
        self.count+=1
        if self.count>20:self.request.field_upload_rejected=True;raise StopUpload()
    def receive_data_chunk(self,raw_data,start):
        self.total+=len(raw_data)
        if self.total>40*1024*1024:self.request.field_upload_rejected=True;raise StopUpload()
        return raw_data
    def file_complete(self,file_size):return None

class FieldUploads(MiddlewareMixin):
    def process_request(self,request):
        if request.method=='POST' and request.path.startswith('/cabinet/repairs/'):
            request.upload_handlers.insert(0,FieldUploadLimit(request))
    def process_view(self,request,view,args,kwargs):
        if request.method=='POST' and request.path.startswith('/cabinet/repairs/'):
            # Parse before CSRF middleware, but perform no write or authorization here.
            request.POST
            if getattr(request,'field_upload_rejected',False):
                text='До 20 файлов и до 40 МБ за одну отправку. Черновик сохранён: уберите часть файлов и повторите.'
                if request.headers.get('Accept')=='application/json':return JsonResponse({'ok':False,'errors':{'files':[{'message':text}]}},status=413)
                return HttpResponse(text,status=413,content_type='text/plain; charset=utf-8')
