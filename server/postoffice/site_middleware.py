from django.core.exceptions import RequestDataTooBig, TooManyFieldsSent, TooManyFilesSent
from django.core.files.uploadhandler import FileUploadHandler, StopUpload
from django.http import JsonResponse
from django.http.multipartparser import MultiPartParserError
from django.utils.deprecation import MiddlewareMixin

LIMIT=5*1024*1024

def rejected():
    return JsonResponse({'ok':False,'errors':{'resume':['Прикрепите одно резюме в PDF или DOCX размером до 5 МБ.']}},status=413)

class PublicResumeLimit(FileUploadHandler):
    def __init__(self,request):super().__init__(request);self.count=0;self.total=0
    def new_file(self,*args,**kwargs):
        self.count+=1
        if self.count>1:self.request.public_upload_rejected=True;raise StopUpload()
    def receive_data_chunk(self,raw_data,start):
        self.total+=len(raw_data)
        if self.total>LIMIT:self.request.public_upload_rejected=True;raise StopUpload()
        return raw_data
    def file_complete(self,file_size):return None

class PublicFormsUploads(MiddlewareMixin):
    def process_request(self,request):
        if request.method=='POST' and request.path.startswith('/forms/'):
            try:size=int(request.META.get('CONTENT_LENGTH') or 0)
            except ValueError:return rejected()
            if size>LIMIT+128*1024:return rejected()
            request.upload_handlers.insert(0,PublicResumeLimit(request))
    def process_view(self,request,view,args,kwargs):
        if request.method=='POST' and request.path.startswith('/forms/'):
            try:request.POST
            except (RequestDataTooBig,TooManyFieldsSent,TooManyFilesSent,MultiPartParserError):return rejected()
            if getattr(request,'public_upload_rejected',False):return rejected()
