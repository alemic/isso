#!/usr/bin/env python
# -*- encoding: utf-8 -*-
#
# The MIT License (MIT)
#
# Copyright (c) 2012-2013 Martin Zimmermann.
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.
#
# Isso – a lightweight Disqus alternative

from __future__ import print_function

import pkg_resources
dist = pkg_resources.get_distribution("isso")

try:
    import uwsgi
except ImportError:
    uwsgi = None
    try:
        import gevent.monkey; gevent.monkey.patch_all()
    except ImportError:
        gevent = None

import sys
import os
import errno
import logging
import tempfile

from os.path import dirname, join
from argparse import ArgumentParser
from functools import partial, reduce

from itsdangerous import URLSafeTimedSerializer

from werkzeug.routing import Map
from werkzeug.exceptions import HTTPException, InternalServerError

from werkzeug.wsgi import SharedDataMiddleware
from werkzeug.local import Local, LocalManager
from werkzeug.serving import run_simple
from werkzeug.contrib.fixers import ProxyFix
from werkzeug.contrib.profiler import ProfilerMiddleware

local = Local()
local_manager = LocalManager([local])

from isso import db, migrate, wsgi, ext, views
from isso.core import ThreadedMixin, ProcessMixin, uWSGIMixin, Config
from isso.utils import parse, http, JSONRequest, origin
from isso.views import comments

from isso.ext.notifications import Stdout, SMTP

logging.getLogger('werkzeug').setLevel(logging.ERROR)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s")

logger = logging.getLogger("isso")


class Isso(object):

    salt = b"Eech7co8Ohloopo9Ol6baimi"

    def __init__(self, conf):

        self.conf = conf
        self.db = db.SQLite3(conf.get('general', 'dbpath'), conf)
        self.signer = URLSafeTimedSerializer(conf.get('general', 'session-key'))

        super(Isso, self).__init__(conf)

        subscribers = []
        subscribers.append(Stdout(None))

        if conf.get("general", "notify") == "smtp":
            subscribers.append(SMTP(self))

        self.signal = ext.Signal(*subscribers)

        self.urls = Map()

        views.Info(self)
        comments.API(self)

    def sign(self, obj):
        return self.signer.dumps(obj)

    def unsign(self, obj, max_age=None):
        return self.signer.loads(obj, max_age=max_age or self.conf.getint('general', 'max-age'))

    def dispatch(self, request):
        local.request = request

        local.host = wsgi.host(request.environ)
        local.origin = origin(self.conf.getiter("general", "host"))(request.environ)

        adapter = self.urls.bind_to_environ(request.environ)

        try:
            handler, values = adapter.match()
        except HTTPException as e:
            return e
        else:
            try:
                response = handler(request.environ, request, **values)
            except HTTPException as e:
                return e
            except Exception:
                logger.exception("%s %s", request.method, request.environ["PATH_INFO"])
                return InternalServerError()
            else:
                return response

    def wsgi_app(self, environ, start_response):
        response = self.dispatch(JSONRequest(environ))
        return response(environ, start_response)

    def __call__(self, environ, start_response):
        return self.wsgi_app(environ, start_response)


def make_app(conf=None):

    if uwsgi:
        class App(Isso, uWSGIMixin):
            pass
    elif gevent or sys.argv[0].endswith("isso"):
        class App(Isso, ThreadedMixin):
            pass
    else:
        class App(Isso, ProcessMixin):
            pass

    isso = App(conf)

    for host in conf.getiter("general", "host"):
        with http.curl('HEAD', host, '/', 5) as resp:
            if resp is not None:
                logger.info("connected to %s", host)
                break
    else:
        logger.warn("unable to connect to %s", ", ".join(conf.getiter("general", "host")))

    wrapper = [local_manager.make_middleware]

    if isso.conf.getboolean("server", "profile"):
        wrapper.append(partial(ProfilerMiddleware,
            sort_by=("cumtime", ), restrictions=("isso/(?!lib)", 10)))

    wrapper.append(partial(SharedDataMiddleware, exports={
        '/js': join(dirname(__file__), 'js/'),
        '/css': join(dirname(__file__), 'css/')}))

    wrapper.append(partial(wsgi.CORSMiddleware,
        origin=origin(isso.conf.getiter("general", "host"))))

    wrapper.extend([wsgi.SubURI, ProxyFix])

    return reduce(lambda x, f: f(x), wrapper, isso)


def main():

    parser = ArgumentParser(description="a blog comment hosting service")
    subparser = parser.add_subparsers(help="commands", dest="command")

    parser.add_argument('--version', action='version', version='%(prog)s ' + dist.version)
    parser.add_argument("-c", dest="conf", default="/etc/isso.conf",
            metavar="/etc/isso.conf", help="set configuration file")

    imprt = subparser.add_parser('import', help="import Disqus XML export")
    imprt.add_argument("dump", metavar="FILE")
    imprt.add_argument("-n", "--dry-run", dest="dryrun", action="store_true",
                       help="perform a trial run with no changes made")

    serve = subparser.add_parser("run", help="run server")

    args = parser.parse_args()
    conf = Config.load(args.conf)

    if args.command == "import":
        xxx = tempfile.NamedTemporaryFile()
        dbpath = conf.get("general", "dbpath") if not args.dryrun else xxx.name

        conf.set("guard", "enabled", "off")
        migrate.disqus(db.SQLite3(dbpath, conf), args.dump)
        sys.exit(0)

    if conf.get("server", "listen").startswith("http://"):
        host, port, _ = parse.host(conf.get("server", "listen"))
        try:
            from gevent.pywsgi import WSGIServer
            WSGIServer((host, port), make_app(conf)).serve_forever()
        except ImportError:
            run_simple(host, port, make_app(conf), threaded=True,
                       use_reloader=conf.getboolean('server', 'reload'))
    else:
        sock = conf.get("server", "listen").partition("unix://")[2]
        try:
            os.unlink(sock)
        except OSError as ex:
            if ex.errno != errno.ENOENT:
                raise
        wsgi.SocketHTTPServer(sock, make_app(conf)).serve_forever()
