# vim: tabstop=4 shiftwidth=4 softtabstop=4
#
# Copyright (c) 2019 Wind River Systems, Inc.
#
# SPDX-License-Identifier: Apache-2.0
#
# All Rights Reserved.
#

""" System inventory Armada manifest operator."""

import os
import json
import ruamel.yaml as yaml
import tempfile

from glob import glob
from six import iteritems
from sysinv.common import constants
from sysinv.common import exception
from sysinv.openstack.common import log as logging

LOG = logging.getLogger(__name__)

KEY_SCHEMA = 'schema'
VAL_SCHEMA_MANIFEST = 'armada/Manifest/v1'
VAL_SCHEMA_CHART_GROUP = 'armada/ChartGroup/v1'
VAL_SCHEMA_CHART = 'armada/Chart/v1'

KEY_METADATA = 'metadata'
KEY_METADATA_NAME = 'name'

KEY_DATA = 'data'
KEY_DATA_CHART_GROUPS = 'chart_groups'  # for manifest doc updates
KEY_DATA_CHART_GROUP = 'chart_group'    # for chart group doc updates
KEY_DATA_CHART_NAME = 'chart_name'      # for chart doc updates

# Attempt to keep a compact filename
FILE_PREFIX = {
    KEY_DATA_CHART_GROUPS: 'm-',  # for manifest doc overrides
    KEY_DATA_CHART_GROUP: 'cg-',  # for chart group doc overrides
    KEY_DATA_CHART_NAME: 'c-'     # for chart doc overrides
}
FILE_SUFFIX = '-meta.yaml'
SUMMARY_FILE = 'armada-overrides.yaml'


class ArmadaManifestOperator(object):

    def __init__(self, manifest_fqpn=None):
        self.manifest_path = None   # Location to write overrides

        self.content = []  # original app manifest content

        self.docs = {
            KEY_DATA_CHART_GROUPS: {},  # LUT for all manifest docs
            KEY_DATA_CHART_GROUP: {},   # LUT for all chart group docs
            KEY_DATA_CHART_NAME: {}     # LUT for all chart docs
        }

        self.updated = {
            KEY_DATA_CHART_GROUPS: set(),  # indicate manifest doc change
            KEY_DATA_CHART_GROUP: set(),   # indicate chart group update
            KEY_DATA_CHART_NAME: set()     # indicate chart doc update
        }

        if manifest_fqpn:
            self.load(manifest_fqpn)

    def __str__(self):
        return json.dumps({
            'manifest': self.docs[KEY_DATA_CHART_GROUPS],
            'chart_groups': self.docs[KEY_DATA_CHART_GROUP],
            'charts': self.docs[KEY_DATA_CHART_NAME],
        }, indent=2)

    def load_summary(self, path):
        """ Load the list of generated overrides files

        Generate a list of override files that were written for the manifest.
        This is used to generate Armada --values overrides for the manifest.

        :param path: location of the overrides summary file
        :return: a list of override files written
        """
        files_written = []
        summary_fqpn = os.path.join(path, SUMMARY_FILE)
        if os.path.exists(summary_fqpn):
            self.manifest_path = os.path.dirname(summary_fqpn)
            with open(summary_fqpn, 'r') as f:
                files_written = list(yaml.load_all(
                    f, Loader=yaml.RoundTripLoader))[0]
        return files_written

    def load(self, manifest_fqpn):
        """ Load the application manifest for processing

        :param manifest_fqpn: fully qualified path name of the application manifest
        """
        if os.path.exists(manifest_fqpn):
            self.manifest_path = os.path.dirname(manifest_fqpn)
            with open(manifest_fqpn, 'r') as f:
                self.content = list(yaml.load_all(
                    f, Loader=yaml.RoundTripLoader))

            # Generate the lookup tables
            # For the individual chart docs
            self.docs[KEY_DATA_CHART_NAME] = {
                i[KEY_METADATA][KEY_METADATA_NAME]: i
                for i in self.content
                if i[KEY_SCHEMA] == VAL_SCHEMA_CHART}

            # For the chart group docs
            self.docs[KEY_DATA_CHART_GROUP] = {
                i[KEY_METADATA][KEY_METADATA_NAME]: i
                for i in self.content
                if i[KEY_SCHEMA] == VAL_SCHEMA_CHART_GROUP}

            # For the single manifest doc
            self.docs[KEY_DATA_CHART_GROUPS] = {
                i[KEY_METADATA][KEY_METADATA_NAME]: i
                for i in self.content
                if i[KEY_SCHEMA] == VAL_SCHEMA_MANIFEST}
        else:
            LOG.error("Manifest file %s does not exist" % manifest_fqpn)

    def _cleanup_meta_files(self, path):
        """ Remove any previously written overrides files

        :param path: directory containing manifest overrides files
        """
        for k, v in iteritems(FILE_PREFIX):
            fileregex = "{}*{}".format(v, FILE_SUFFIX)
            filepath = os.path.join(self.manifest_path, fileregex)
            for f in glob(filepath):
                os.remove(f)

    def _write_file(self, path, filename, pathfilename, data):
        """ Write a yaml file

        :param path: path to write the file
        :param filename: name of the file
        :param pathfilename: FQPN of the file
        :param data: file data
        """
        try:
            fd, tmppath = tempfile.mkstemp(dir=path, prefix=filename,
                                           text=True)

            with open(tmppath, 'w') as f:
                yaml.dump(data, f, Dumper=yaml.RoundTripDumper,
                          default_flow_style=False)
                os.close(fd)
                os.rename(tmppath, pathfilename)
                # Change the permission to be readable to non-root
                # users(ie.Armada)
                os.chmod(pathfilename, 0o644)
        except Exception:
            if os.path.exists(tmppath):
                os.remove(tmppath)
            LOG.exception("Failed to write meta overrides %s" % pathfilename)
            raise

    def save_summary(self, path=None):
        """ Write a yaml file containing the list of override files generated

        :param path: optional alternative location to write the file
        """
        files_written = []
        for k, v in iteritems(self.updated):
            for i in v:
                filename = '{}{}{}'.format(FILE_PREFIX[k], i, FILE_SUFFIX)
                filepath = os.path.join(self.manifest_path, filename)
                files_written.append(filepath)

        # Write the list of files generated. This can be read to include with
        # the Armada overrides
        if path and os.path.exists(path):
            # if provided, write to an alternate location
            self._write_file(path, SUMMARY_FILE,
                             os.path.join(path, SUMMARY_FILE),
                             files_written)
        else:
            # if not provided, write to the armada directory
            self._write_file(self.manifest_path, SUMMARY_FILE,
                             os.path.join(self.manifest_path, SUMMARY_FILE),
                             files_written)

    def save(self):
        """ Save the overrides files

        Write the elements of the manifest (manifest, chart_group, chart) that
        was updated into an overrides file. The files are written to the same
        directory as the application manifest.
        """
        if os.path.exists(self.manifest_path):

            # cleanup any existing meta override files
            self._cleanup_meta_files(self.manifest_path)

            # Only write the updated docs as meta overrides
            for k, v in iteritems(self.updated):
                for i in v:
                    filename = '{}{}{}'.format(FILE_PREFIX[k], i, FILE_SUFFIX)
                    filepath = os.path.join(self.manifest_path, filename)
                    self._write_file(self.manifest_path, filename, filepath,
                                self.docs[k][i])
        else:
            LOG.error("Manifest directory %s does not exist" % self.manifest_path)

    def _validate_manifest(self, manifest):
        """ Ensure that the manifest is known

        :param manifest: name of the manifest
        """
        if manifest not in self.docs[KEY_DATA_CHART_GROUPS]:
            LOG.error("%s is not %s" % (manifest, self.docs[KEY_DATA_CHART_GROUPS].keys()))
            return False
        return True

    def _validate_chart_group(self, chart_group):
        """ Ensure that the chart_group is known

        :param chart_group: name of the chart_group
        """
        if chart_group not in self.docs[KEY_DATA_CHART_GROUP]:
            LOG.error("%s is an unknown chart_group" % chart_group)
            return False
        return True

    def _validate_chart_groups_from_list(self, chart_group_list):
        """ Ensure that all the charts groups in chart group list are known

        :param chart_group_list: list of chart groups
        """
        for cg in chart_group_list:
            if not self._validate_chart_group(cg):
                return False
        return True

    def _validate_chart(self, chart):
        """ Ensure that the chart is known

        :param chart: name of the chart
        """
        if chart not in self.docs[KEY_DATA_CHART_NAME]:
            LOG.error("%s is an unknown chart" % chart)
            return False
        return True

    def _validate_chart_from_list(self, chart_list):
        """ Ensure that all the charts in chart list are known

        :param chart_list: list of charts
        """
        for c in chart_list:
            if not self._validate_chart(c):
                return False
        return True

    def manifest_chart_groups_delete(self, manifest, chart_group):
        """ Delete a chart group from a manifest

        This method will delete a chart group from a manifest's list of charts
        groups.

        :param manifest: manifest containing the list of chart groups
        :param chart_group: chart group name to delete
        """
        if (not self._validate_manifest(manifest) or
                not self._validate_chart_group(chart_group)):
            return

        if chart_group not in self.docs[KEY_DATA_CHART_GROUPS][manifest][KEY_DATA][
                KEY_DATA_CHART_GROUPS]:
            LOG.info("%s is not currently enabled. Cannot delete." %
                     chart_group)
            return

        self.docs[KEY_DATA_CHART_GROUPS][manifest][KEY_DATA][
            KEY_DATA_CHART_GROUPS].remove(chart_group)
        self.updated[KEY_DATA_CHART_GROUPS].update([manifest])

    def manifest_chart_groups_insert(self, manifest, chart_group, before_group=None):
        """ Insert a chart group into a manifest

        This method will insert a chart group into a manifest at the end of the
        list of chart groups. If the before_group parameter is used the chart
        group can be placed at a specific point in the chart group list.

        :param manifest: manifest containing the list of chart groups
        :param chart_group: chart group name to insert
        :param before_group: chart group name to be appear after the inserted
            chart group in the list
        """
        if (not self._validate_manifest(manifest) or
                not self._validate_chart_group(chart_group)):
            return

        if chart_group in self.docs[KEY_DATA_CHART_GROUPS][manifest][KEY_DATA][KEY_DATA_CHART_GROUPS]:
            LOG.error("%s is already enabled. Cannot insert." %
                      chart_group)
            return

        if before_group:
            if not self._validate_chart_group(before_group):
                return

            if before_group not in self.docs[KEY_DATA_CHART_GROUPS][manifest][KEY_DATA][
                    KEY_DATA_CHART_GROUPS]:
                LOG.error("%s is not currently enabled. Cannot insert %s" %
                          (before_group, chart_group))
                return

            cgs = self.docs[KEY_DATA_CHART_GROUPS][manifest][KEY_DATA][KEY_DATA_CHART_GROUPS]
            insert_index = cgs.index(before_group)
            cgs.insert(insert_index, chart_group)
            self.docs[KEY_DATA_CHART_GROUPS][manifest][KEY_DATA][KEY_DATA_CHART_GROUPS] = cgs
        else:
            self.docs[KEY_DATA_CHART_GROUPS][manifest][KEY_DATA][
                KEY_DATA_CHART_GROUPS].append(chart_group)

        self.updated[KEY_DATA_CHART_GROUPS].update([manifest])

    def manifest_chart_groups_set(self, manifest, chart_group_list=None):
        """ Set the chart groups for a specific manifest

        This will replace the current set of charts groups in the manifest as
        specified by the armada/Manifest/v1 schema with the provided list of
        chart groups.

        :param manifest: manifest containing the list of chart groups
        :param chart_group_list: list of chart groups to replace the current set
            of chart groups
        """
        if not self._validate_manifest(manifest):
            return

        if chart_group_list:
            if not self._validate_chart_groups_from_list(chart_group_list):
                return

            self.docs[KEY_DATA_CHART_GROUPS][manifest][KEY_DATA][KEY_DATA_CHART_GROUPS] = chart_group_list

        else:
            LOG.error("Cannot set the manifest chart_groups to an empty list")

    def chart_group_chart_delete(self, chart_group, chart):
        """ Delete a chart from a chart group

        This method will delete a chart from a chart group's list of charts.

        :param chart_group: chart group name
        :param chart: chart name to remove from the chart list
        """
        if (not self._validate_chart_group(chart_group) or
                not self._validate_chart(chart)):
            return

        if chart not in self.docs[KEY_DATA_CHART_GROUP][chart_group][KEY_DATA][
                KEY_DATA_CHART_GROUP]:
            LOG.info("%s is not currently enabled. Cannot delete." %
                     chart)
            return

        self.docs[KEY_DATA_CHART_GROUP][chart_group][KEY_DATA][
            KEY_DATA_CHART_GROUP].remove(chart)
        self.updated[KEY_DATA_CHART_GROUP].update([chart_group])

    def chart_group_chart_insert(self, chart_group, chart, before_chart=None):
        """ Insert a chart into a chart group

        This method will insert a chart into a chart group at the end of the
        list of charts. If the before_chart parameter is used the chart can be
        placed at a specific point in the chart list.

        :param chart_group: chart group name
        :param chart: chart name to insert
        :param before_chart: chart name to be appear after the inserted chart in
            the list
        """
        if (not self._validate_chart_group(chart_group) or
                not self._validate_chart(chart)):
            return

        if chart in self.docs[KEY_DATA_CHART_GROUP][chart_group][KEY_DATA][KEY_DATA_CHART_GROUP]:
            LOG.error("%s is already enabled. Cannot insert." %
                      chart)
            return

        if before_chart:
            if not self._validate_chart(before_chart):
                return

            if before_chart not in self.docs[KEY_DATA_CHART_GROUP][chart_group][KEY_DATA][
                    KEY_DATA_CHART_GROUP]:
                LOG.error("%s is not currently enabled. Cannot insert %s" %
                          (before_chart, chart))
                return

            cg = self.docs[KEY_DATA_CHART_GROUP][chart_group][KEY_DATA][KEY_DATA_CHART_GROUP]
            insert_index = cg.index(before_chart)
            cg.insert(insert_index, chart)
            self.docs[KEY_DATA_CHART_GROUP][chart_group][KEY_DATA][KEY_DATA_CHART_GROUP] = cg
        else:
            self.docs[KEY_DATA_CHART_GROUP][chart_group][KEY_DATA][
                KEY_DATA_CHART_GROUP].append(chart)

        self.updated[KEY_DATA_CHART_GROUP].update([chart_group])

    def chart_group_set(self, chart_group, chart_list=None):
        """ Set the charts for a specific chart group

        This will replace the current set of charts specified in the chart group
        with the provided list.

        :param chart_group: chart group name
        :param chart_list: list of charts to replace the current set of charts
        """
        if not self._validate_chart_group(chart_group):
            return

        if chart_list:
            if not self._validate_chart_from_list(chart_list):
                return

            self.docs[KEY_DATA_CHART_GROUP][chart_group][KEY_DATA][KEY_DATA_CHART_GROUP] = chart_list

        else:
            LOG.error("Cannot set the chart_group charts to an empty list")

    def chart_group_add(self, chart_group, data):
        """ Add a new chart group to the manifest.

        To support a self-contained dynamic plugin, this method is called to
        introduced a new chart group based on the armada/ChartGroup/v1 schema.

        :param chart_group: chart group name
        :param data: chart group data
        """
        # Not implemented... yet.
        pass

    def chart_add(self, chart, data):
        """ Add a new chart to the manifest.

        To support a self-contained dynamic plugin, this method is called to
        introduced a new chart based on the armada/Chart/v1 schema.

        :param chart: chart name
        :param data: chart data
        """
        # Not implemented... yet.
        pass


def platform_mode_manifest_updates(dbapi, manifest_op, app_name, mode):
    """ Update the application manifest based on the platform

    This is used for

    :param dbapi: DB api object
    :param manifest_op: ArmadaManifestOperator for updating the application
        manifest
    :param app_name: application name
    :param mode: mode to control how to apply the application manifest
    """

    if not app_name:
        LOG.info("App is None. No platform mode based manifest updates taken.")

    elif app_name not in constants.HELM_APP_APPLY_MODES.keys():
        LOG.info("App %s is not supported. No platform mode based manifest "
                 "updates taken." % app_name)

    elif app_name == constants.HELM_APP_OPENSTACK:

        if mode == constants.OPENSTACK_RESTORE_DB:
            # During application restore, first bring up
            # MariaDB service.
            manifest_op.manifest_chart_groups_set(
                'armada-manifest',
                ['kube-system-ingress',
                 'openstack-ingress',
                 'openstack-mariadb'])

        elif mode == constants.OPENSTACK_RESTORE_STORAGE:
            # After MariaDB data is restored, restore Keystone,
            # Glance and Cinder.
            manifest_op.manifest_chart_groups_set(
                'armada-manifest',
                ['kube-system-ingress',
                 'openstack-ingress',
                 'openstack-mariadb',
                 'openstack-memcached',
                 'openstack-rabbitmq',
                 'openstack-keystone',
                 'openstack-glance',
                 'openstack-cinder'])

        else:
            # When mode is OPENSTACK_RESTORE_NORMAL or None,
            # bring up all the openstack services.
            try:
                system = dbapi.isystem_get_one()
            except exception.NotFound:
                LOG.exception("System %s not found.")
                raise

            if (system.distributed_cloud_role ==
                    constants.DISTRIBUTED_CLOUD_ROLE_SYSTEMCONTROLLER):
                # remove the chart_groups not needed in this configuration
                manifest_op.manifest_chart_groups_delete(
                    'armada-manifest', 'openstack-ceph-rgw')
                manifest_op.manifest_chart_groups_delete(
                    'armada-manifest', 'openstack-compute-kit')
                manifest_op.manifest_chart_groups_delete(
                    'armada-manifest', 'openstack-heat')
                manifest_op.manifest_chart_groups_delete(
                    'armada-manifest', 'openstack-telemetry')