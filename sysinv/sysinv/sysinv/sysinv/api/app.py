# vim: tabstop=4 shiftwidth=4 softtabstop=4
# -*- encoding: utf-8 -*-

# Copyright © 2012 New Dream Network, LLC (DreamHost)
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

from oslo_config import cfg
import os
import pecan

from sysinv.api import acl
from sysinv.api import config
from sysinv.api import hooks
from sysinv.api import middleware
from sysinv.common import constants
from sysinv.common import policy

auth_opts = [
    cfg.StrOpt('auth_strategy',
               default='keystone',
               help='Method to use for auth: noauth or keystone.'),
        ]

CONF = cfg.CONF
CONF.register_opts(auth_opts)


def get_pecan_config():
    # Set up the pecan configuration
    filename = config.__file__.replace('.pyc', '.py')
    return pecan.configuration.conf_from_file(filename)


def make_tempdir():
    # Set up sysinv-api tempdir for large POST request
    tempdir = constants.SYSINV_TMPDIR
    if not os.path.isdir(tempdir):
        os.makedirs(tempdir)
    os.environ['TMPDIR'] = tempdir


def setup_app(pecan_config=None, extra_hooks=None):
    policy.init()

    app_hooks = [hooks.MultiFormDataHook(),
                 hooks.ConfigHook(),
                 hooks.DBHook(),
                 hooks.ContextHook(pecan_config.app.acl_public_routes),
                 hooks.RPCHook(),
                 hooks.AuditLogging()]

    if extra_hooks:
        app_hooks.extend(extra_hooks)

    if not pecan_config:
        pecan_config = get_pecan_config()

    if pecan_config.app.enable_acl:
        app_hooks.append(hooks.AdminAuthHook())

    pecan.configuration.set_config(dict(pecan_config), overwrite=True)

    make_tempdir()

    app = pecan.make_app(
        pecan_config.app.root,
        static_root=pecan_config.app.static_root,
        debug=CONF.debug,
        force_canonical=getattr(pecan_config.app, 'force_canonical', True),
        hooks=app_hooks,
        wrap_app=middleware.ParsableErrorMiddleware,
        guess_content_type_from_ext=False,
    )

    if pecan_config.app.enable_acl:
        return acl.install(app, cfg.CONF, pecan_config.app.acl_public_routes)

    return app


class VersionSelectorApplication(object):
    def __init__(self):
        pc = get_pecan_config()
        pc.app.enable_acl = (CONF.auth_strategy == 'keystone')
        self.v1 = setup_app(pecan_config=pc)

    def __call__(self, environ, start_response):
        if 'HTTP_X_FORWARDED_PROTO' in environ:
            environ['wsgi.url_scheme'] = environ.get('HTTP_X_FORWARDED_PROTO')
        return self.v1(environ, start_response)
