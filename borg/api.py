import json
import base64
import os
import logging
import traceback
import time
from datetime import timedelta
import requests

from restless.dj import DjangoResource
from restless.resources import skip_prepare

from django.conf.urls import  url
from django.template import Context, Template
try:
    from django.utils.encoding import smart_text
except ImportError:
    from django.utils.encoding import smart_unicode as smart_text

from django.contrib import auth
from django.utils import timezone
from django.db.models import Q
from django.views.generic import View
from django.http import Http404,HttpResponse
from django.shortcuts import get_object_or_404

from harvest.models import Job
from tablemanager.models import Publish,Workspace,Input,DataSource
from wmsmanager.models import WmsLayer,WmsServer
from harvest.jobstatemachine import JobStatemachine
from monitor.models import SlaveServer,PublishSyncStatus
from livelayermanager.models import Layer as LiveLayer
from livelayermanager.models import SqlViewLayer as LiveSqlViewLayer

from borg_utils.hg_batch_push import try_set_push_owner, try_clear_push_owner, try_push_to_repository
from borg_utils.jobintervals import JobInterval
from borg_utils.borg_config import BorgConfiguration
from borg_utils.resource_status import ResourceStatus,ResourceAction
from harvest.jobstates import Completed

logger = logging.getLogger(__name__)

class BasicHttpAuthMixin(object):
    """
    :py:class:`restless.views.Endpoint` mixin providing user authentication
    based on HTTP Basic authentication.
    """

    def authenticate(self, request):
        if 'HTTP_AUTHORIZATION' in request.META:
            authdata = request.META['HTTP_AUTHORIZATION'].split()
            if len(authdata) == 2 and authdata[0].lower() == "basic":
                try:
                    raw = authdata[1].encode('ascii')
                    auth_parts = base64.b64decode(raw).split(b':')
                except:
                    return
                try:
                    uname, passwd = (smart_text(auth_parts[0]),
                        smart_text(auth_parts[1]))
                except DjangoUnicodeDecodeError:
                    return

                user = auth.authenticate(username=uname, password=passwd)
                if user is not None and user.is_active:
                    # We don't user auth.login(request, user) because
                    # may be running without session
                    request.user = user
        return request.user.is_authenticated()

class LegendApi(View):
    @staticmethod
    def urls():
        return [
            url(r'^legends/(?P<server>.+)/(?P<layer>.+)/$',LegendApi.as_view(),name='legends'),
        ]
     
    def get(self,request,server,layer):
        wmsServer = get_object_or_404(WmsServer,pk=server)
        wmslayer = get_object_or_404(WmsLayer,server=wmsServer,name=layer)
        if not wmslayer.legend :
            raise Http404("Legend not found for layer({}:{})".format(wmsServer.name,layer))
        res = requests.get(wmslayer.legend,auth=(wmsServer.user,wmsServer.password),stream=True)
        res.raise_for_status()
        return HttpResponse(res.raw,res.headers.get('content-type',None))

class JobApi(DjangoResource,BasicHttpAuthMixin):
    def is_authenticated(self):
        if self.request.user.is_authenticated():
            return True
        else:
            return self.authenticate(self.request)

    @staticmethod
    def urls():
        return [
            url(r'^jobs/$',JobApi.as_list(),name='create_job'),
        ]
     
    @skip_prepare
    def create(self):
        job_batch_id = JobInterval.Triggered.job_batch_id()
        resp = {"status":True}
        result = None
        for name in self.data.get('publishes') or []:
            resp[name] = {}
            try:
                result = JobStatemachine.create_job_by_name(name,JobInterval.Triggered,job_batch_id)
                if result[0]:
                    resp[name]["status"] = True
                    resp[name]["job_id"] = result[1]
                    resp[name]["message"] = "Succeed"
                else:
                    resp["status"] = False
                    resp[name]["status"] = False
                    resp[name]["message"] = result[1]
            except :
                msg = traceback.format_exc()
                logger.error(msg)
                resp["status"] = False
                resp[name]["status"] = False
                resp[name]["message"] = msg

        return resp

class MetadataApi(DjangoResource,BasicHttpAuthMixin):
    def is_authenticated(self):
        if self.request.user.is_authenticated():
            return True
        else:
            return self.authenticate(self.request)

    @staticmethod
    def urls():
        return[
            url(r'^metajobs/$',MetadataApi.as_list(),name='publish_meta'),
        ]
     
    @skip_prepare
    def create(self):
        resp = {"status":True}
        result = None
        try_set_push_owner("meta_resource")
        try:
            for layer in self.data.get('layers') or []:
                workspace,name = layer.split(":")
                resp[layer] = {}
                #get the workspace object
                try:
                    workspaces = Workspace.objects.filter(name=workspace)
                    if not len(workspaces):
                        #workspace does not exist
                        resp["status"] = False
                        resp[layer]["status"] = False
                        resp[layer]["message"] = "Workspace does not exist.".format(name)
                        continue
                    
                    try:
                        #try to locate it from publishs, and publish the meta data if found
                        pub = Publish.objects.get(workspace__in=workspaces,name=name)
                        try:
                            pub.publish_meta_data()
                            resp[layer]["status"] = True
                            resp[layer]["message"] = "Succeed."
                            continue
                        except:
                            msg = traceback.format_exc()
                            logger.error(msg)
                            resp["status"] = False
                            resp[layer]["status"] = False
                            resp[layer]["message"] = "Publish meta data failed!{}".format(msg)
                            continue
                    except Publish.DoesNotExist:
                        pass
                        
                    #not a publish object, try to locate it from live layers, and publish it if found
                    try:
                        livelayer = LiveLayer.objects.filter(datasource__workspace__in=workspaces).get(Q(name=name) | Q(table=name))
                        try:
                            target_status = livelayer.next_status(ResourceAction.PUBLISH)
                            livelayer.status = target_status
                            livelayer.save(update_fields=["status","last_publish_time"])
                            resp[layer]["status"] = True
                            resp[layer]["message"] = "Succeed."
                            continue
                        except :
                            msg = traceback.format_exc()
                            logger.error(msg)
                            resp["status"] = False
                            resp[layer]["status"] = False
                            resp[layer]["message"] = "Publish live layer failed!{}".format(msg)
                            continue
                    except LiveLayer.DoesNotExist:
                        pass

                    #not a publish object, try to locate it from live sqlview layers, and publish it if found
                    try:
                        livelayer = LiveSqlViewLayer.objects.get(datasource__workspace__in=workspaces,name=name)
                        try:
                            target_status = livelayer.next_status(ResourceAction.PUBLISH)
                            livelayer.status = target_status
                            livelayer.save(update_fields=["status","last_publish_time"])
                            resp[layer]["status"] = True
                            resp[layer]["message"] = "Succeed."
                            continue
                        except :
                            msg = traceback.format_exc()
                            logger.error(msg)
                            resp["status"] = False
                            resp[layer]["status"] = False
                            resp[layer]["message"] = "Publish live sqlview layer failed!{}".format(msg)
                            continue
                    except LiveSqlViewLayer.DoesNotExist:
                        pass

                    #not a publish object, try to locate it from wms layers, and publish it if found
                    try:
                        wmslayer = WmsLayer.objects.get(server__workspace__in=workspaces,kmi_name=name)
                        try:
                            target_status = wmslayer.next_status(ResourceAction.PUBLISH)
                            wmslayer.status = target_status
                            wmslayer.save(update_fields=["status","last_publish_time"])
                            resp[layer]["status"] = True
                            resp[layer]["message"] = "Succeed."
                            continue
                        except:
                            msg = traceback.format_exc()
                            logger.error(msg)
                            resp["status"] = False
                            resp[layer]["status"] = False
                            resp[layer]["message"] = "Publish wms layer failed!{}".format(msg)
                            continue
                    except WmsLayer.DoesNotExist:
                        #layer does not exist,
                        resp["status"] = False
                        resp[layer]["status"] = False
                        resp[layer]["message"] = "Does not exist.".format(name)
                        continue
                except :
                    msg = traceback.format_exc()
                    logger.error(msg)
                    resp["status"] = False
                    resp[layer]["status"] = False
                    resp[layer]["message"] = msg
                    continue

            #push all files into repository at once.
            try:
                try_push_to_repository('meta_resource',enforce=True)
            except:
                #push failed, set status to false, and proper messages for related layers.
                msg = traceback.format_exc()
                logger.error(msg)
                resp["status"] = False
                for layer in self.data.get('layers') or []:
                    if resp[layer]["status"]:
                        #publish succeed but push failed
                        resp[layer]["status"] = False
                        resp[layer]["message"] = "Push to repository failed!{}".format(msg)
        finally:
            try_clear_push_owner("meta_resource")
            
        return resp


class MudmapResource(DjangoResource,BasicHttpAuthMixin):
    def is_authenticated(self):
        if self.request.user.is_authenticated():
            return True
        else:
            return self.authenticate(self.request)

    @staticmethod
    def urls():
        return [
            url(r'^mudmap/(?P<application>[a-zA-Z0-9_\-]+)/(?P<name>[a-zA-Z0-9_\-]+)/(?P<user>[a-zA-Z0-9_\-\.]+@[a-zA-Z0-9\-]+(\.[a-zA-Z0-9\-]+)+)/$',MudmapResource.as_list(),name='publish_mudmap'),
        ]
     
    @skip_prepare
    def create(self,application,name,user, *args, **kwargs):
        try:
            json_data = self.data
            application = application.lower()
            name = name.lower()
            user = user.lower()
            #prepare the folder
            folder = os.path.join(BorgConfiguration.MUDMAP_HOME,application,name)
            if os.path.exists(folder):
                if not os.path.isdir(folder):
                    raise "{} is not a folder".format(folder)
            else:
                os.makedirs(folder)
            #write the json file into folder
            file_name = os.path.join(folder,"{}.json".format(user))
            with open(file_name,"wb") as f:
                f.write(json.dumps(self.data))
            #get list of geojson files
            json_files = [os.path.join(folder,f) for f in os.listdir(folder) if f[-5:] == ".json"]
            
            job_id = self._publish(application,name,user)
            if job_id:
                return {"jobid":job_id}
        except:
            logger.error(traceback.format_exc())
            raise
    
    def _publish(self,application,name,user):
        #get list of geojson files
        folder = os.path.join(BorgConfiguration.MUDMAP_HOME,application,name)
        if os.path.exists(folder):
            json_files = [os.path.join(folder,f) for f in os.listdir(folder) if f[-5:] == ".json"]
        else:
            json_files = None
        #generate the source data
        data_source = DataSource.objects.get(name="mudmap")
        input_name = "{}_{}".format(application,name)
        mudmap_input = None
        if json_files:
            #create or update input 
            try:
                mudmap_input = Input.objects.get(name=input_name)
            except Input.DoesNotExist:
                mudmap_input = Input(name=input_name,data_source=data_source,generate_rowid=False)

            source = Template(data_source.vrt).render(Context({"files":json_files,"self":mudmap_input}))
            mudmap_input.source = source
            mudmap_input.full_clean(exclude=["data_source"])
            if mudmap_input.pk:
                mudmap_input.last_modify_time = timezone.now()
                mudmap_input.save(update_fields=["source","last_modify_time","info"])
            else:
                mudmap_input.save()

            #get or create publish
            mudmap_publish = None
            try:
                mudmap_publish = Publish.objects.get(name=input_name)
            except Publish.DoesNotExist:
                #not exist, create it
                workspace = Workspace.objects.get(name="mudmap")
                mudmap_publish = Publish(
                    name=input_name,
                    workspace=workspace,
                    interval=JobInterval.Manually,
                    status=ResourceStatus.Enabled,
                    input_table=mudmap_input,sql="$$".join(Publish.TRANSFORM).strip()
                )
            mudmap_publish.full_clean(exclude=["interval"])   
            mudmap_publish.save()
            #pubish the job
            result = JobStatemachine._create_job(mudmap_publish,JobInterval.Triggered)

            if result[0]:
                return result[1]
            else:
                raise Exception(result[1])
            
        else:
            #no more json files, delete input, and all other dependent objects.
            try:
                mudmap_input = Input.objects.get(name=input_name)
                mudmap_input.delete()
                return None
            except Input.DoesNotExist:
                #already deleted
                pass

    @skip_prepare
    def delete_list(self,application,name,user, *args, **kwargs):
        try:
            application = application.lower()
            name = name.lower()
            user = user.lower()
            #delere the file from the folder
            folder = os.path.join(BorgConfiguration.MUDMAP_HOME,application,name)
            if os.path.exists(folder) and os.path.isdir(folder):
                #delete the json file from the folder
                file_name = os.path.join(folder,"{}.json".format(user))
                if os.path.exists(file_name):
                    os.remove(file_name)
    
                #get list of geojson files
                files = [f for f in os.listdir(folder)]
                if not files:
                    #remove folder
                    try:
                        os.rmdir(folder)
                    except:
                        #remove failed,but ignore.
                        pass
    
            job_id = self._publish(application,name,user)
        except:
            logger.error(traceback.format_exc())
            raise
    
class PublishStatusApi(DjangoResource,BasicHttpAuthMixin):
    def is_authenticated(self):
        if self.request.user.is_authenticated():
            return True
        else:
            return self.authenticate(self.request)

    @staticmethod
    def urls():
        return [
            url(r'^publishs/(?P<name>[a-zA-Z0-9_\-]+)/$',PublishStatusApi.as_detail(),name='publish_status'),
        ]
     

    @classmethod
    def _get_milliseconds(cls,d) :
        if not d: 
            return None
        d = timezone.localtime(d)
        return time.mktime(d.timetuple()) * 1000 + d.microsecond / 1000

    @skip_prepare
    def detail(self,name):
        try:
            try:
                publish = Publish.objects.get(name=name)
            except Publish.DoesNotExist:
                return {
                    "layer" :{
                        "name":name,
                    },
                }
            if publish.status != ResourceStatus.Enabled.name:
                return {
                    "layer" :{
                        "id" : publish.id,
                        "workspace":publish.workspace.name,
                        "name":publish.name,
                        "status":publish.status
                    },
                }
            
            publishing_job = None
            latest_published_job = None
            deploied_job = None
            deploied_jobid = None
            deploytime = None
            deploymessage = None
            if publish.job_id :
                publishing_job = Job.objects.filter(publish = publish,finished__isnull=True).order_by("-id").first()
                latest_published_job = Job.objects.filter(publish = publish,launched__isnull=False,finished__isnull=False).order_by("-id").first()
                    
        
            sync_statuses = PublishSyncStatus.objects.filter(publish=publish).order_by("-deploied_job_id")
            if len(sync_statuses) >= 1:
                deploied_jobid = sync_statuses[0].deploied_job_id
                deploytime = sync_statuses[0].deploy_time
            outofsync_statuses = [status for status in sync_statuses if status.sync_job_id != None or status.deploied_job_id != deploied_jobid]
    
            resp = {
                "layer" :{
                    "id" : publish.id,
                    "workspace":publish.workspace.name,
                    "name":publish.name,
                    "status":publish.status
                },
            }
            if publish.job_id:
                resp["publish"] = {
                    "publishing_jobid" : publishing_job.id if publishing_job else None,
                    "publishing_failed" : publishing_job.jobstate.is_error_state if publishing_job else False,
                    "publishing_message": publisheding_job.message if publishing_job and publishing_job.jobstate.is_error_state else None,
                    "published_jobid" : latest_published_job.id if latest_published_job else None,
                    "publish_time" : self._get_milliseconds(latest_published_job.finished) if latest_published_job else None,
                    "deploied_jobid":deploied_jobid,
                    "deploy_time":self._get_milliseconds(deploytime) if deploytime else None,
                }
                now = timezone.now()
                if outofsync_statuses:
                    resp["publish"]["outofsync_servers"] = []
                    for status in outofsync_statuses:
                        resp["publish"]["outofsync_servers"].append({
                            "server":status.slave_server.name,
                            "deploied_jobid": status.deploied_job_id,
                            "deploy_time":self._get_milliseconds(status.deploy_time),
                            "sync_jobid": status.sync_job_id,
                            "sync_message": status.sync_message,
                            "sync_time":self._get_milliseconds(status.sync_time),
                            "last_poll_time": self._get_milliseconds(status.slave_server.last_poll_time) ,
                            "last_sync_time": self._get_milliseconds(status.slave_server.last_sync_time) ,
                        })
    
            return resp
        except:
            logger.error(traceback.format_exc())
            raise


urlpatterns =  JobApi.urls() + MetadataApi.urls() + LegendApi.urls()

