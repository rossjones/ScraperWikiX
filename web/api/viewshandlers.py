import urllib
import urllib2

from django.template import RequestContext, loader, Context
from django.http import HttpResponseRedirect, HttpResponse, Http404, HttpResponseNotFound
from django.shortcuts import render_to_response
from django.shortcuts import get_object_or_404
from django.core.urlresolvers import reverse
from django.contrib.auth.models import User
from settings import MAX_API_ITEMS, API_DOMAIN
from django.views.decorators.http import condition

import csv
from django.utils.encoding import smart_str
from django.core.serializers.json import DateTimeAwareJSONEncoder
from django.utils import simplejson


from codewiki.models import Scraper, Code
from codewiki.managers.datastore import DataStore

from django.contrib.auth.decorators import login_required

from models import api_key
from forms import applyForm
from cStringIO import StringIO

import base64

def getscraperorresponse(request):
    try:
        scraper = Code.unfiltered.get(short_name=request.GET.get('name'))
    except Code.DoesNotExist:
        message =  "Sorry, this datastore does not exist"
        return HttpResponse(str({'heading':'Not found', 'body':message}))
    
    if not scraper.actionauthorized(request.user, "apiread"):
        return HttpResponse(str(scraper.authorizationfailedmessage(request.user, "apiread")))
    return scraper


def stringnot(v):
    if v == None:
        return ""
    if type(v) in [unicode, str]:
        return v.encode("utf-8")
    return v


def stream_csv(dataproxy):
    n = 0
    while True:
        line = dataproxy.receiveonelinenj()
        try:
            ret = simplejson.loads(line)
        except ValueError, e:
            yield str(e)
            break
        if "error" in ret:
            yield str(ret)
            break
        fout = StringIO()
        writer = csv.writer(fout, dialect='excel')
        if n == 0:
            writer.writerow([ k.encode('utf-8') for k in ret["keys"] ])
        for row in ret["data"]:
            writer.writerow([ stringnot(v)  for v in row ])
        
        yield fout.getvalue()
        n += 1
        if "moredata" not in ret:
            break  

def data_handler(request):
    scraper = getscraperorresponse(request)
    if isinstance(scraper, HttpResponse):  return scraper
    dataproxy = DataStore(scraper.guid, "")  
    rc, arg = dataproxy.request(('item_count',))
    
        # redirect to the sqlite interface
        # (could pull out the column order and put in place of the *)
    if arg == 0:
        tablename = request.GET.get('tablename', "swdata")
        squery = ["select * from `%s`" % tablename]
        if "limit" in request.GET:
            squery.append('limit %d' % int(request.GET.get('limit')))
        if "offset" in request.GET:
            squery.append('offset %d' % int(request.GET.get('offset')))
        qsdata = { "name":request.GET.get("name"), "query":" ".join(squery) }
        if "format" in request.GET:
            qsdata["format"] = request.GET.get("format")
        if "callback" in request.GET:
            qsdata["callback"] = request.GET.get("callback")
        return HttpResponseRedirect("%s?%s" % (reverse("api:method_sqlite"), urllib.urlencode(qsdata)))

    # do the old data handler case
    limit = int(request.GET.get('limit', 100))
    offset = int(request.GET.get('offset', 0))
    rc, arg = dataproxy.data_dictlist(limit=limit, offset=offset)
    if not rc:
        return HttpResponse("Error: "+arg)
    
    format = request.GET.get("format", "json")
    if format != "jsondict" and len(arg) != 0:
        keys = set()
        for row in arg:   keys.update(row)
        keys = sorted(list(keys))
        rows = [ [row.get(key, "")  for key in keys]  for row in arg ]
        arg = { "keys":keys, "data":rows }
    if format == "json" or format == "jsondict":
        result = simplejson.dumps(arg, cls=DateTimeAwareJSONEncoder, indent=4)
        callback = request.GET.get("callback")
        if callback:
            result = "%s(%s)" % (callback, result)
        response = HttpResponse(result, mimetype='text/csv')
        response['Content-Disposition'] = 'attachment; filename=%s.csv' % (scraper.short_name)
        return response
        
    assert format == "csv"
    fout = StringIO()
    writer = csv.writer(fout, dialect='excel')
    writer.writerow([ k.encode('utf-8') for k in arg["keys"] ])
    for row in arg["data"]:
        writer.writerow([ stringnot(v)  for v in row ])
    response = HttpResponse(fout.getvalue(), mimetype='text/plain')
    response['Content-Disposition'] = 'attachment; filename=%s.json' % (scraper.short_name)
    return response
    

@condition(etag_func=None)
def sqlite_handler(request):
    scraper = getscraperorresponse(request)
    if isinstance(scraper, HttpResponse):  return scraper
    dataproxy = DataStore("sqlviewquery", "")  # zero length short name means it will open up a :memory: database

    attachlist = request.GET.get('attach', '').split(";")
    attachlist.insert(0, request.GET.get('name'))   # just the first entry on the list
        
    for aattach in attachlist:
        if aattach:
            aa = aattach.split(",")
            sqlitedata = dataproxy.request(("sqlitecommand", "attach", aa[0], (len(aa) == 2 and aa[1] or None)))
    
    sqlquery = request.GET.get('query')
    format = request.GET.get("format")
    
    reqt = None
    if format == "csv":
        reqt = ("streamchunking", 10)
    req = ("sqlitecommand", "execute", sqlquery, reqt)
    dataproxy.m_socket.sendall(simplejson.dumps(req) + '\n')
    
    if format == "csv":
        st = stream_csv(dataproxy)
        response = HttpResponse(st, mimetype='text/csv')
        response['Content-Disposition'] = 'attachment; filename=%s.csv' % (scraper.short_name)
            # unless you put in a content length, the middleware will measure the length of your data
            # (unhelpfully consuming everything in your generator) before then returning a zero length result 
        response["Content-Length"] = -1
        return response
    
    result = dataproxy.receiveonelinenj()
    if format == "jsondict":
        try:
            res = simplejson.loads(result)
        except ValueError, e:
            return HttpResponse("Error:%s" % (e.message,))
        if "error" not in res:
            dictlist = [ dict(zip(res["keys"], values))  for values in res["data"] ]
            result = simplejson.dumps(dictlist, cls=DateTimeAwareJSONEncoder, indent=4)
    callback = request.GET.get("callback")
    if callback:
        result = "%s(%s)" % (callback, result)
    response = HttpResponse(result, mimetype='text/plain')
    response['Content-Disposition'] = 'attachment; filename=%s.json' % (scraper.short_name)
    return response
